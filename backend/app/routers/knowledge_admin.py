"""/admin/knowledge/documents/* 与 /admin/content-gaps/* 写操作：知识库后台运维。

与 admin.py 的分工：那边是只读运营看板 + 上传入口，这边是文档生命周期
（列表/分片/停用/删除/重建/回滚/预览）和内容缺口闭环。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..knowledge_admin_service import (
    ArchivedFileMissing,
    file_reference_count,
    gap_query_hash,
    get_or_create_resolution,
    preview_document_diff,
    reindex_document,
    replay_gap_query,
    resolve_stored_file,
    rollback_document,
)
from ..models import (
    AuditLog,
    ContentGapResolution,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeGapEvent,
    RobotModel,
    User,
    utcnow,
)
from ..observability import emit_json_log, request_trace_id
from ..schemas import (
    AdminContentGapReplayRead,
    AdminContentGapReplayRequest,
    AdminContentGapResolutionRead,
    AdminContentGapUpdate,
    AdminKnowledgeChunkPage,
    AdminKnowledgeChunkRead,
    AdminKnowledgeDiffPreviewRead,
    AdminKnowledgeDocumentDetailRead,
    AdminKnowledgeDocumentRead,
    AdminKnowledgeDocumentUpdate,
    AdminKnowledgeDocumentVersionRead,
    AdminKnowledgeRollbackRequest,
)
from ..security import require_admin, require_knowledge_manage, require_operations_read

router = APIRouter(prefix="/api/v1")

MAX_KNOWLEDGE_PDF_BYTES = 30 * 1024 * 1024


# --------------------------------------------------------------------------
# 公共装配
# --------------------------------------------------------------------------


def _chunk_counts(db: Session, document_ids: list[int]) -> dict[int, tuple[int, int]]:
    if not document_ids:
        return {}
    rows = db.execute(
        select(
            KnowledgeChunk.document_id,
            func.count().label("chunk_count"),
            func.count(KnowledgeChunk.embedding).label("vector_count"),
        )
        .where(KnowledgeChunk.document_id.in_(document_ids))
        .group_by(KnowledgeChunk.document_id)
    ).all()
    return {row.document_id: (int(row.chunk_count), int(row.vector_count)) for row in rows}


def _document_read(
    document: KnowledgeDocument,
    *,
    model_code: str,
    counts: tuple[int, int],
    storage_dir: Path,
) -> dict:
    chunk_count, vector_count = counts
    return {
        "id": document.id,
        "robot_model_id": document.robot_model_id,
        "model_code": model_code,
        "title": document.title,
        "source_url": document.source_url,
        "sha256": document.sha256,
        "page_count": document.page_count,
        "chunk_count": chunk_count,
        "vector_count": vector_count,
        "status": document.status,
        "version": document.version,
        "embedding_model": document.embedding_model,
        "file_size": document.file_size,
        # 报"有没有原件"而不是报路径：前端据此决定重建/回滚/下载按钮能不能点，
        # 存档路径属于服务端内部信息，没有理由发给浏览器。
        "has_archived_file": resolve_stored_file(storage_dir, document.stored_filename) is not None,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def _load_document(db: Session, document_id: int) -> KnowledgeDocument:
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return document


def _knowledge_dir(request: Request) -> Path:
    return Path(request.app.state.knowledge_dir)


# --------------------------------------------------------------------------
# 文档列表 / 详情 / 分片 / 原件
# --------------------------------------------------------------------------


@router.get("/admin/knowledge/documents", response_model=list[AdminKnowledgeDocumentRead])
def list_knowledge_documents(
    request: Request,
    model_code: str | None = Query(default=None, max_length=50),
    status: str | None = Query(default=None, pattern="^(active|disabled)$"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_operations_read),
) -> list[AdminKnowledgeDocumentRead]:
    del admin
    statement = (
        select(KnowledgeDocument, RobotModel.code)
        .join(RobotModel, RobotModel.id == KnowledgeDocument.robot_model_id)
        .order_by(RobotModel.code, KnowledgeDocument.title, KnowledgeDocument.id)
    )
    if model_code:
        statement = statement.where(RobotModel.code == model_code)
    if status:
        statement = statement.where(KnowledgeDocument.status == status)
    rows = db.execute(statement).all()
    counts = _chunk_counts(db, [row[0].id for row in rows])
    storage_dir = _knowledge_dir(request)
    return [
        AdminKnowledgeDocumentRead(
            **_document_read(
                document,
                model_code=code,
                counts=counts.get(document.id, (0, 0)),
                storage_dir=storage_dir,
            )
        )
        for document, code in rows
    ]


@router.get(
    "/admin/knowledge/documents/{document_id}",
    response_model=AdminKnowledgeDocumentDetailRead,
)
def get_knowledge_document(
    request: Request,
    document_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_operations_read),
) -> AdminKnowledgeDocumentDetailRead:
    del admin
    document = db.scalar(
        select(KnowledgeDocument)
        .options(selectinload(KnowledgeDocument.versions))
        .where(KnowledgeDocument.id == document_id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    model_code = db.scalar(select(RobotModel.code).where(RobotModel.id == document.robot_model_id))
    counts = _chunk_counts(db, [document.id]).get(document.id, (0, 0))
    storage_dir = _knowledge_dir(request)
    return AdminKnowledgeDocumentDetailRead(
        **_document_read(
            document, model_code=model_code or "", counts=counts, storage_dir=storage_dir
        ),
        versions=[
            AdminKnowledgeDocumentVersionRead(
                version=entry.version,
                sha256=entry.sha256,
                title=entry.title,
                page_count=entry.page_count,
                chunk_count=entry.chunk_count,
                change_kind=entry.change_kind,
                note=entry.note,
                has_archived_file=resolve_stored_file(storage_dir, entry.stored_filename)
                is not None,
                embedding_model=entry.embedding_model,
                created_at=entry.created_at,
            )
            for entry in document.versions
        ],
    )


@router.get(
    "/admin/knowledge/documents/{document_id}/chunks",
    response_model=AdminKnowledgeChunkPage,
)
def list_knowledge_chunks(
    document_id: int,
    page_number: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    admin: User = Depends(require_operations_read),
) -> AdminKnowledgeChunkPage:
    """分页看分片：切分对不对、页码有没有错位，只能靠看真实分片来判断。"""

    del admin
    _load_document(db, document_id)
    conditions = [KnowledgeChunk.document_id == document_id]
    if page_number is not None:
        conditions.append(KnowledgeChunk.page_number == page_number)
    total = (
        db.scalar(select(func.count()).select_from(KnowledgeChunk).where(*conditions)) or 0
    )
    rows = db.scalars(
        select(KnowledgeChunk)
        .where(*conditions)
        .order_by(KnowledgeChunk.chunk_index)
        .offset(offset)
        .limit(limit)
    ).all()
    return AdminKnowledgeChunkPage(
        document_id=document_id,
        total=int(total),
        offset=offset,
        limit=limit,
        items=[
            AdminKnowledgeChunkRead(
                chunk_index=chunk.chunk_index,
                page_number=chunk.page_number,
                content=chunk.content,
                has_embedding=chunk.embedding is not None,
            )
            for chunk in rows
        ],
    )


@router.get("/admin/knowledge/documents/{document_id}/file")
def download_knowledge_file(
    request: Request,
    document_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_operations_read),
) -> FileResponse:
    document = _load_document(db, document_id)
    path = resolve_stored_file(_knowledge_dir(request), document.stored_filename)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="该文档没有留存原始文件（存档功能上线前入库），请重新上传后再下载",
        )
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="admin.knowledge.download",
            resource_type="knowledge_document",
            resource_id=str(document.id),
            details_json={"sha256": document.sha256, "trace_id": request_trace_id(request)},
        )
    )
    db.commit()
    filename = f"{document.title or 'document'}.pdf".replace("/", "_")
    return FileResponse(path, media_type="application/pdf", filename=filename)


# --------------------------------------------------------------------------
# 停用 / 改名 / 删除
# --------------------------------------------------------------------------


@router.patch(
    "/admin/knowledge/documents/{document_id}",
    response_model=AdminKnowledgeDocumentRead,
)
def update_knowledge_document(
    request: Request,
    document_id: int,
    payload: AdminKnowledgeDocumentUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_knowledge_manage),
) -> AdminKnowledgeDocumentRead:
    document = _load_document(db, document_id)
    changes: dict[str, object] = {}
    if payload.status is not None and payload.status != document.status:
        document.status = payload.status
        changes["status"] = payload.status
    if payload.title is not None and payload.title != document.title:
        document.title = payload.title
        changes["title"] = payload.title
    if changes:
        db.add(
            AuditLog(
                actor_user_id=admin.id,
                action="admin.knowledge.update",
                resource_type="knowledge_document",
                resource_id=str(document.id),
                details_json={**changes, "trace_id": request_trace_id(request)},
            )
        )
    db.commit()
    db.refresh(document)
    model_code = db.scalar(select(RobotModel.code).where(RobotModel.id == document.robot_model_id))
    counts = _chunk_counts(db, [document.id]).get(document.id, (0, 0))
    return AdminKnowledgeDocumentRead(
        **_document_read(
            document,
            model_code=model_code or "",
            counts=counts,
            storage_dir=_knowledge_dir(request),
        )
    )


@router.delete("/admin/knowledge/documents/{document_id}", status_code=204)
def delete_knowledge_document(
    request: Request,
    document_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    document = _load_document(db, document_id)
    storage_dir = _knowledge_dir(request)
    stored_filenames = {document.stored_filename} | {
        entry.stored_filename for entry in document.versions
    }
    audit = AuditLog(
        actor_user_id=admin.id,
        action="admin.knowledge.delete",
        resource_type="knowledge_document",
        resource_id=str(document.id),
        details_json={
            "title": document.title,
            "sha256": document.sha256,
            "source_url": document.source_url,
            "trace_id": request_trace_id(request),
        },
    )
    db.delete(document)
    db.add(audit)
    db.commit()
    # 文件在事务提交之后才删：先删文件再提交，一旦回滚就是数据还在、原件没了。
    for stored_filename in stored_filenames:
        if not stored_filename:
            continue
        if file_reference_count(db, stored_filename) > 0:
            continue  # 同一份 PDF 可能被别的型号/版本共用
        path = resolve_stored_file(storage_dir, stored_filename)
        if path is not None:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 重新向量化 / 版本回滚 / 上传前预览
# --------------------------------------------------------------------------


@router.post(
    "/admin/knowledge/documents/{document_id}/reindex",
    response_model=AdminKnowledgeDocumentDetailRead,
)
def reindex_knowledge_document(
    request: Request,
    document_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_knowledge_manage),
) -> AdminKnowledgeDocumentDetailRead:
    document = _load_document(db, document_id)
    try:
        reindex_document(
            db,
            document=document,
            provider=request.app.state.embedding_provider,
            storage_dir=_knowledge_dir(request),
            actor_user_id=admin.id,
            commit=False,
        )
        db.add(
            AuditLog(
                actor_user_id=admin.id,
                action="admin.knowledge.reindex",
                resource_type="knowledge_document",
                resource_id=str(document.id),
                details_json={"trace_id": request_trace_id(request)},
            )
        )
        db.commit()
    except ArchivedFileMissing as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        db.rollback()
        emit_json_log(
            logging.ERROR,
            "admin_knowledge_reindex_failed",
            trace_id=request_trace_id(request),
            document_id=document_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc
    return get_knowledge_document(request, document_id, db=db, admin=admin)


@router.post(
    "/admin/knowledge/documents/{document_id}/rollback",
    response_model=AdminKnowledgeDocumentDetailRead,
)
def rollback_knowledge_document(
    request: Request,
    document_id: int,
    payload: AdminKnowledgeRollbackRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminKnowledgeDocumentDetailRead:
    document = _load_document(db, document_id)
    target = db.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_id == document_id,
            KnowledgeDocumentVersion.version == payload.version,
        )
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if target.sha256 == document.sha256:
        raise HTTPException(status_code=409, detail="该版本与当前线上内容相同，无需回滚")
    try:
        rollback_document(
            db,
            document=document,
            target=target,
            provider=request.app.state.embedding_provider,
            storage_dir=_knowledge_dir(request),
            actor_user_id=admin.id,
            commit=False,
        )
        db.add(
            AuditLog(
                actor_user_id=admin.id,
                action="admin.knowledge.rollback",
                resource_type="knowledge_document",
                resource_id=str(document.id),
                details_json={
                    "target_version": payload.version,
                    "target_sha256": target.sha256,
                    "trace_id": request_trace_id(request),
                },
            )
        )
        db.commit()
    except ArchivedFileMissing as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc
    return get_knowledge_document(request, document_id, db=db, admin=admin)


@router.post("/admin/knowledge/preview", response_model=AdminKnowledgeDiffPreviewRead)
def preview_knowledge_upload(
    request: Request,
    model_code: str = Form(min_length=1, max_length=50),
    source_url: str = Form(min_length=1, max_length=2000),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_knowledge_manage),
) -> AdminKnowledgeDiffPreviewRead:
    """上传前看差异：只解析、不入库、不调用 embedding，也不落存档。"""

    del admin
    robot_model = db.scalar(select(RobotModel).where(RobotModel.code == model_code))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    content = file.file.read(MAX_KNOWLEDGE_PDF_BYTES + 1)
    if len(content) > MAX_KNOWLEDGE_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 30MB upload limit")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="Uploaded file is not a PDF")
    title = Path(file.filename).stem if file.filename else None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        handle.write(content)
        pdf_path = Path(handle.name)
    try:
        preview = preview_document_diff(
            db,
            robot_model_id=robot_model.id,
            model_code=robot_model.code,
            source_url=source_url,
            pdf_path=pdf_path,
            storage_dir=_knowledge_dir(request),
            title=title or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        pdf_path.unlink(missing_ok=True)
    return AdminKnowledgeDiffPreviewRead(**preview.__dict__)


# --------------------------------------------------------------------------
# 内容缺口闭环
# --------------------------------------------------------------------------


def _gap_exists(db: Session, *, robot_model_id: int, query_normalized: str) -> bool:
    return (
        db.scalar(
            select(func.count())
            .select_from(KnowledgeGapEvent)
            .where(
                KnowledgeGapEvent.robot_model_id == robot_model_id,
                KnowledgeGapEvent.query_normalized == query_normalized,
            )
        )
        or 0
    ) > 0


def _resolution_payload(db: Session, resolution: ContentGapResolution) -> dict:
    linked_title = None
    if resolution.linked_document_id is not None:
        linked_title = db.scalar(
            select(KnowledgeDocument.title).where(
                KnowledgeDocument.id == resolution.linked_document_id
            )
        )
    return {
        "status": resolution.status,
        "linked_document_id": resolution.linked_document_id,
        "linked_document_title": linked_title,
        "replay_status": resolution.replay_status,
        "replay_citation_count": resolution.replay_citation_count,
        "replay_answer_excerpt": resolution.replay_answer_excerpt,
        "replay_checked_at": resolution.replay_checked_at,
        "resolved_at": resolution.resolved_at,
        "note": resolution.note,
    }


@router.patch("/admin/content-gaps", response_model=AdminContentGapResolutionRead)
def update_content_gap(
    request: Request,
    payload: AdminContentGapUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_knowledge_manage),
) -> AdminContentGapResolutionRead:
    """关联文档 / 改状态 / 记备注。缺口本身没有主键，用 (型号, 归一化问题) 定位。"""

    robot_model_id = payload.robot_model_id
    query_normalized = payload.query_normalized
    if not _gap_exists(db, robot_model_id=robot_model_id, query_normalized=query_normalized):
        raise HTTPException(status_code=404, detail="Content gap not found")
    resolution = get_or_create_resolution(
        db, robot_model_id=robot_model_id, query_normalized=query_normalized
    )
    if payload.linked_document_id is not None:
        document = db.get(KnowledgeDocument, payload.linked_document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Knowledge document not found")
        if document.robot_model_id != robot_model_id:
            # 把缺口挂到别的型号的文档上，闭环就废了：复测查的是本型号的库
            raise HTTPException(
                status_code=422, detail="关联文档必须属于同一型号"
            )
        resolution.linked_document_id = document.id
    if payload.note is not None:
        resolution.note = payload.note
    if payload.status is not None:
        resolution.status = payload.status
        resolution.resolved_at = utcnow() if payload.status == "resolved" else None
    resolution.updated_by = admin.id
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="admin.content_gap.update",
            resource_type="content_gap",
            resource_id=gap_query_hash(query_normalized)[:32],
            details_json={
                "robot_model_id": robot_model_id,
                "status": resolution.status,
                "linked_document_id": resolution.linked_document_id,
                "trace_id": request_trace_id(request),
            },
        )
    )
    db.commit()
    db.refresh(resolution)
    return AdminContentGapResolutionRead(**_resolution_payload(db, resolution))


@router.post("/admin/content-gaps/replay", response_model=AdminContentGapReplayRead)
def replay_content_gap(
    request: Request,
    payload: AdminContentGapReplayRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_knowledge_manage),
) -> AdminContentGapReplayRead:
    """把原问题重新问一遍，用真实检索+生成的结果决定缺口是否真的补上了。"""

    if not _gap_exists(
        db, robot_model_id=payload.robot_model_id, query_normalized=payload.query_normalized
    ):
        raise HTTPException(status_code=404, detail="Content gap not found")
    resolution = get_or_create_resolution(
        db,
        robot_model_id=payload.robot_model_id,
        query_normalized=payload.query_normalized,
    )
    outcome = replay_gap_query(
        db,
        resolution=resolution,
        embedding_provider=request.app.state.embedding_provider,
        generation_provider=request.app.state.generation_provider,
        actor_user_id=admin.id,
        auto_resolve=payload.auto_resolve,
    )
    db.add(
        AuditLog(
            actor_user_id=admin.id,
            action="admin.content_gap.replay",
            resource_type="content_gap",
            resource_id=gap_query_hash(payload.query_normalized)[:32],
            details_json={
                "robot_model_id": payload.robot_model_id,
                "replay_status": outcome.status,
                "citation_count": outcome.citation_count,
                "trace_id": request_trace_id(request),
            },
        )
    )
    db.commit()
    db.refresh(resolution)
    emit_json_log(
        logging.INFO,
        "admin_content_gap_replayed",
        trace_id=request_trace_id(request),
        actor_user_id=admin.id,
        robot_model_id=payload.robot_model_id,
        replay_status=outcome.status,
        citation_count=outcome.citation_count,
    )
    return AdminContentGapReplayRead(
        replay_status=outcome.status,
        citation_count=outcome.citation_count,
        answer_excerpt=outcome.answer_excerpt,
        detail=outcome.detail,
        gap_status=resolution.status,
        checked_at=resolution.replay_checked_at,
    )
