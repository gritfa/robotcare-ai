"""知识库后台运维：原件存档、差异预览、重建向量、版本回滚、缺口闭环复测。

与 knowledge_service 的分工：那边负责"入库与检索"这条主链路，这边负责
管理员对既有文档的运维动作。两者共用 ingest_pdf，避免出现第二套入库实现——
知识入库只能有一个真相来源，否则运维路径迟早和主路径产生行为差异。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .generation_service import GenerationProvider, generate_answer
from .knowledge_service import (
    EMBEDDING_MODEL,
    EmbeddingProvider,
    IngestResult,
    extract_pdf_pages,
    ingest_pdf,
    split_pages,
)
from .models import (
    ContentGapResolution,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    utcnow,
)

REPLAY_EXCERPT_LIMIT = 500


# --------------------------------------------------------------------------
# 原件存档
# --------------------------------------------------------------------------


def knowledge_storage_dir(raw_dir: str | Path) -> Path:
    directory = Path(raw_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def store_knowledge_file(storage_dir: Path, content: bytes) -> tuple[str, int, bool]:
    """按内容哈希落盘并返回 (文件名, 字节数, 本次是否新建)。

    用 sha256 命名而不是 uuid：同一份 PDF 无论被哪个型号引用都只存一份，
    且覆盖上传时旧版本文件天然保留下来——这正是版本回滚要依赖的东西。
    返回"是否新建"是为了让入库失败时只清理本次产生的文件，
    绝不能顺手删掉别的文档正在用的同名存档。
    """

    digest = hashlib.sha256(content).hexdigest()
    stored_filename = f"{digest}.pdf"
    target = knowledge_storage_dir(storage_dir) / stored_filename
    created = not target.exists()
    if created:
        target.write_bytes(content)
    return stored_filename, len(content), created


def resolve_stored_file(storage_dir: str | Path, stored_filename: str | None) -> Path | None:
    """把存档文件名解析成真实路径；缺文件或越权路径一律返回 None。"""

    if not stored_filename:
        return None
    # 文件名来自数据库，但仍然按不可信输入处理：拒绝任何带路径分隔符的值，
    # 避免一行被污染的记录变成任意文件读取。
    if "/" in stored_filename or "\\" in stored_filename or stored_filename.startswith("."):
        return None
    path = Path(storage_dir) / stored_filename
    return path if path.is_file() else None


def file_reference_count(db: Session, stored_filename: str) -> int:
    """统计还有多少文档/版本引用这个存档文件——共享存档不能被单个删除动作带走。"""

    documents = db.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(KnowledgeDocument.stored_filename == stored_filename)
    )
    versions = db.scalar(
        select(func.count())
        .select_from(KnowledgeDocumentVersion)
        .where(KnowledgeDocumentVersion.stored_filename == stored_filename)
    )
    return int(documents or 0) + int(versions or 0)


# --------------------------------------------------------------------------
# 上传前差异预览
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentDiffPreview:
    status: str  # "new" | "identical" | "changed"
    model_code: str
    source_url: str
    incoming_title: str
    incoming_sha256: str
    incoming_page_count: int
    incoming_chunk_count: int
    document_id: int | None = None
    current_version: int | None = None
    current_title: str | None = None
    current_sha256: str | None = None
    current_page_count: int | None = None
    current_chunk_count: int | None = None
    current_updated_at: datetime | None = None
    page_delta: int | None = None
    chunk_delta: int | None = None
    pages_comparable: bool = False
    pages_incomparable_reason: str | None = None
    changed_pages: list[int] = field(default_factory=list)
    added_pages: list[int] = field(default_factory=list)
    removed_pages: list[int] = field(default_factory=list)


def _page_digests(pages) -> dict[int, str]:
    return {
        page.page_number: hashlib.sha256(page.text.strip().encode("utf-8")).hexdigest()
        for page in pages
    }


def preview_document_diff(
    db: Session,
    *,
    robot_model_id: int,
    model_code: str,
    source_url: str,
    pdf_path: str | Path,
    storage_dir: str | Path,
    title: str | None = None,
) -> DocumentDiffPreview:
    """解析待上传 PDF 并与线上版本比对，全程不写库、不调用 embedding。

    预览的价值在于"上传前"，所以它必须比真正入库便宜得多——这里刻意不做
    向量化，只做文本层比对。
    """

    path = Path(pdf_path)
    content = path.read_bytes()
    incoming_sha = hashlib.sha256(content).hexdigest()
    incoming_pages, metadata_title = extract_pdf_pages(path)
    incoming_chunks = split_pages(incoming_pages)
    incoming_title = title or metadata_title or path.stem

    existing = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.robot_model_id == robot_model_id,
            KnowledgeDocument.source_url == source_url,
        )
    )
    if existing is None:
        return DocumentDiffPreview(
            status="new",
            model_code=model_code,
            source_url=source_url,
            incoming_title=incoming_title,
            incoming_sha256=incoming_sha,
            incoming_page_count=len(incoming_pages),
            incoming_chunk_count=len(incoming_chunks),
        )

    current_chunk_count = (
        db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == existing.id)
        )
        or 0
    )
    base = {
        "model_code": model_code,
        "source_url": source_url,
        "incoming_title": incoming_title,
        "incoming_sha256": incoming_sha,
        "incoming_page_count": len(incoming_pages),
        "incoming_chunk_count": len(incoming_chunks),
        "document_id": existing.id,
        "current_version": existing.version,
        "current_title": existing.title,
        "current_sha256": existing.sha256,
        "current_page_count": existing.page_count,
        "current_chunk_count": current_chunk_count,
        "current_updated_at": existing.updated_at,
        "page_delta": len(incoming_pages) - existing.page_count,
        "chunk_delta": len(incoming_chunks) - current_chunk_count,
    }
    if existing.sha256 == incoming_sha:
        return DocumentDiffPreview(status="identical", **base)

    current_file = resolve_stored_file(storage_dir, existing.stored_filename)
    if current_file is None:
        # 老文档没有存档原件，逐页比对无从谈起。这里如实说明而不是拿
        # 分片文本硬凑一个看似精确、实则不可信的页级差异。
        return DocumentDiffPreview(
            status="changed",
            pages_comparable=False,
            pages_incomparable_reason="线上版本没有留存原始文件，无法逐页比对（仅该文档下次上传后可用）",
            **base,
        )

    current_pages, _ = extract_pdf_pages(current_file)
    current_digests = _page_digests(current_pages)
    incoming_digests = _page_digests(incoming_pages)
    changed = sorted(
        page
        for page, digest in incoming_digests.items()
        if page in current_digests and current_digests[page] != digest
    )
    added = sorted(set(incoming_digests) - set(current_digests))
    removed = sorted(set(current_digests) - set(incoming_digests))
    return DocumentDiffPreview(
        status="changed",
        pages_comparable=True,
        changed_pages=changed,
        added_pages=added,
        removed_pages=removed,
        **base,
    )


# --------------------------------------------------------------------------
# 重建向量 / 版本回滚
# --------------------------------------------------------------------------


class ArchivedFileMissing(RuntimeError):
    """没有可用的原件存档——重建和回滚都必须失败得明确，不能静默跳过。"""


def reindex_document(
    db: Session,
    *,
    document: KnowledgeDocument,
    provider: EmbeddingProvider,
    storage_dir: str | Path,
    actor_user_id: int | None,
    commit: bool = False,
) -> IngestResult:
    path = resolve_stored_file(storage_dir, document.stored_filename)
    if path is None:
        raise ArchivedFileMissing(
            "该文档没有可用的原始文件存档，无法重新向量化；请重新上传同一份 PDF"
        )
    return ingest_pdf(
        db,
        robot_model_id=document.robot_model_id,
        pdf_path=path,
        source_url=document.source_url,
        provider=provider,
        title=document.title,
        commit=commit,
        stored_filename=document.stored_filename,
        file_size=document.file_size,
        actor_user_id=actor_user_id,
        change_kind="reindex",
        note=f"重新向量化（{EMBEDDING_MODEL}）",
        force=True,
    )


def rollback_document(
    db: Session,
    *,
    document: KnowledgeDocument,
    target: KnowledgeDocumentVersion,
    provider: EmbeddingProvider,
    storage_dir: str | Path,
    actor_user_id: int | None,
    commit: bool = False,
) -> IngestResult:
    """用历史版本的存档原件重新入库；版本号继续向前，不回退。"""

    path = resolve_stored_file(storage_dir, target.stored_filename)
    if path is None:
        raise ArchivedFileMissing(
            f"版本 v{target.version} 没有留存原始文件，无法回滚"
        )
    return ingest_pdf(
        db,
        robot_model_id=document.robot_model_id,
        pdf_path=path,
        source_url=document.source_url,
        provider=provider,
        title=target.title,
        commit=commit,
        stored_filename=target.stored_filename,
        file_size=target.file_size,
        actor_user_id=actor_user_id,
        change_kind="rollback",
        note=f"回滚至 v{target.version}（sha {target.sha256[:12]}）",
        force=True,
    )


# --------------------------------------------------------------------------
# 内容缺口闭环
# --------------------------------------------------------------------------


def gap_query_hash(query_normalized: str) -> str:
    return hashlib.sha256(query_normalized.encode("utf-8")).hexdigest()


def get_or_create_resolution(
    db: Session, *, robot_model_id: int, query_normalized: str
) -> ContentGapResolution:
    query_hash = gap_query_hash(query_normalized)
    existing = db.scalar(
        select(ContentGapResolution).where(
            ContentGapResolution.robot_model_id == robot_model_id,
            ContentGapResolution.query_hash == query_hash,
        )
    )
    if existing is not None:
        return existing
    resolution = ContentGapResolution(
        robot_model_id=robot_model_id,
        query_normalized=query_normalized,
        query_hash=query_hash,
        status="open",
    )
    db.add(resolution)
    db.flush()
    return resolution


@dataclass(frozen=True)
class ReplayOutcome:
    status: str  # "passed" | "failed" | "error"
    answer_excerpt: str | None
    citation_count: int
    detail: str


def replay_gap_query(
    db: Session,
    *,
    resolution: ContentGapResolution,
    embedding_provider: EmbeddingProvider,
    generation_provider: GenerationProvider,
    actor_user_id: int | None,
    auto_resolve: bool = True,
) -> ReplayOutcome:
    """把当初答不上来的原问题原样再问一遍，用结果决定缺口能否标记为已解决。

    "已解决"必须由一次真实重放来背书：上传了资料不等于问得出来，
    切分位置不对、型号挂错、相似度不过阈值都会让缺口原地不动。
    """

    try:
        result = generate_answer(
            db,
            user_id=actor_user_id,
            robot_model_id=resolution.robot_model_id,
            query=resolution.query_normalized,
            embedding_provider=embedding_provider,
            generation_provider=generation_provider,
            commit=False,
        )
    except Exception as exc:  # 外部模型不可用属于"没测成",不是"没解决"
        outcome = ReplayOutcome(
            status="error",
            answer_excerpt=None,
            citation_count=0,
            detail=f"复测未能完成：{type(exc).__name__}",
        )
        resolution.replay_status = "error"
        resolution.replay_answer_excerpt = outcome.detail
        resolution.replay_citation_count = 0
        resolution.replay_checked_at = utcnow()
        return outcome

    citation_count = len(result.citations)
    if result.status == "answered" and citation_count > 0:
        outcome = ReplayOutcome(
            status="passed",
            answer_excerpt=(result.answer or "")[:REPLAY_EXCERPT_LIMIT],
            citation_count=citation_count,
            detail="已能基于知识库引用作答",
        )
    else:
        outcome = ReplayOutcome(
            status="failed",
            answer_excerpt=(result.answer or "")[:REPLAY_EXCERPT_LIMIT] or None,
            citation_count=citation_count,
            detail=f"仍未能作答（{result.refusal_reason or result.status}）",
        )

    resolution.replay_status = outcome.status
    resolution.replay_answer_excerpt = outcome.answer_excerpt or outcome.detail
    resolution.replay_citation_count = citation_count
    resolution.replay_checked_at = utcnow()
    resolution.updated_by = actor_user_id
    if outcome.status == "passed" and auto_resolve:
        resolution.status = "resolved"
        resolution.resolved_at = utcnow()
    elif outcome.status == "failed" and resolution.status == "resolved":
        # 复测失败必须把状态打回去：留着一个通不过复测的"已解决"，
        # 缺口榜就开始骗人了。
        resolution.status = "investigating"
        resolution.resolved_at = None
    return outcome
