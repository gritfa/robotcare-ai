"""/knowledge/* routes: search, answer, status, health."""

import io
import logging
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..alerting import send_alert
from ..citation_page_service import CitationPageCache, CitationPageCacheFull
from ..config import get_settings
from ..database import get_db
from ..generation_service import generate_answer
from ..knowledge_admin_service import resolve_stored_file
from ..knowledge_service import (
    get_knowledge_health,
    get_knowledge_status,
    record_knowledge_gap_event,
    search_knowledge,
)
from ..models import KnowledgeDocument, KnowledgeGapEvent, RobotModel, User
from ..observability import emit_json_log, request_trace_id
from ..rate_limit_service import (
    aggregate_knowledge_version,
    enforce_business_rate_limit,
    enforce_embedding_rate_limit,
    knowledge_cache_key,
    normalize_knowledge_query,
)
from ..safety import detect_safety_block
from ..schemas import (
    AnswerCitationRead,
    KnowledgeAnswerRequest,
    KnowledgeAnswerResponse,
    KnowledgeHealthRead,
    KnowledgeModelHealthRead,
    KnowledgeProbeRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeStatusRead,
)
from ._shared import safety_block_http_exception
from ..security import CAPABILITY_MESSAGES, get_current_user, has_capability

router = APIRouter(prefix="/api/v1")


@router.post("/knowledge/search", response_model=list[KnowledgeSearchResult])
def knowledge_search(
    payload: KnowledgeSearchRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[KnowledgeSearchResult]:
    robot_model = db.scalar(select(RobotModel).where(RobotModel.id == payload.robot_model_id))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    safety_block = detect_safety_block(payload.query)
    if safety_block is not None:
        emit_json_log(
            logging.WARNING,
            "knowledge_safety_block",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            category=safety_block.category,
            risk_level=safety_block.risk_level,
        )
        raise safety_block_http_exception(safety_block)
    settings = get_settings()
    enforce_business_rate_limit(
        db,
        request,
        action="knowledge_search",
        user_id=user.id,
        settings=settings,
    )
    threshold_version = db.scalar(
        select(KnowledgeDocument.sha256)
        .where(
            KnowledgeDocument.robot_model_id == payload.robot_model_id,
            KnowledgeDocument.status == "active",
        )
        .order_by(KnowledgeDocument.id.desc())
        .limit(1)
    )
    # 缓存版本键只聚合启用中的文档：停用不改 sha256，若把停用文档也算进来，
    # 键不变 → 缓存命中 → 停用后的 TTL 窗口内仍会把已停用内容发给用户。
    # 修在键上而不是加 invalidate()：多进程部署时各进程各自算键，天然一致。
    document_shas = list(
        db.scalars(
            select(KnowledgeDocument.sha256)
            .where(
                KnowledgeDocument.robot_model_id == payload.robot_model_id,
                KnowledgeDocument.status == "active",
            )
            .order_by(KnowledgeDocument.sha256)
        )
    )
    knowledge_version = aggregate_knowledge_version(document_shas)
    min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)
    normalized_query = normalize_knowledge_query(payload.query)
    cache_key = knowledge_cache_key(
        settings,
        model_code=robot_model.code,
        normalized_query=normalized_query,
        knowledge_version=knowledge_version,
        top_k=payload.top_k,
        min_score=min_score,
    )
    cache = request.app.state.knowledge_search_cache
    started_at = perf_counter()
    try:
        with cache.singleflight(cache_key):
            cached = cache.get(cache_key)
            if cached is not None:
                results = list(cached)
                emit_json_log(
                    logging.INFO,
                    "knowledge_search",
                    trace_id=request_trace_id(request),
                    robot_model_id=payload.robot_model_id,
                    top_k=payload.top_k,
                    min_score=min_score,
                    result_count=len(results),
                    cache_hit=True,
                    duration_ms=round((perf_counter() - started_at) * 1000, 3),
                )
                # 缓存命中的空结果同样是内容缺口：命中与否不影响埋点。
                if not results:
                    record_knowledge_gap_event(
                        db,
                        robot_model_id=payload.robot_model_id,
                        query_normalized=normalized_query,
                        source="search_empty",
                        trace_id=request_trace_id(request),
                    )
                return results
            enforce_embedding_rate_limit(
                db,
                request,
                user_id=user.id,
                settings=settings,
            )
            raw_results = search_knowledge(
                db,
                robot_model_id=payload.robot_model_id,
                query=normalized_query,
                top_k=payload.top_k,
                min_score=min_score,
                provider=request.app.state.embedding_provider,
            )
            results = [
                KnowledgeSearchResult(**result.__dict__)
                for result in raw_results
            ]
            cache.set(cache_key, results)
        emit_json_log(
            logging.INFO,
            "knowledge_search",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            top_k=payload.top_k,
            min_score=min_score,
            result_count=len(results),
            cache_hit=False,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            sources=[
                {
                    "document_title": item.document_title,
                    "document_sha256": item.document_sha256,
                    "page_number": item.page_number,
                    "score": round(item.score, 6),
                }
                for item in raw_results
            ],
        )
        if not results:
            record_knowledge_gap_event(
                db,
                robot_model_id=payload.robot_model_id,
                query_normalized=normalized_query,
                source="search_empty",
                trace_id=request_trace_id(request),
            )
        return results
    except RuntimeError as exc:
        emit_json_log(
            logging.ERROR,
            "knowledge_search_failed",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            duration_ms=round((perf_counter() - started_at) * 1000, 3),
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc


@router.post("/knowledge/answer", response_model=KnowledgeAnswerResponse)
def knowledge_answer(
    payload: KnowledgeAnswerRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KnowledgeAnswerResponse:
    """检索增强回答：安全前置阻断 → 检索 → 生成（强制引用/三类拒答）→ 留痕。"""
    robot_model = db.scalar(select(RobotModel).where(RobotModel.id == payload.robot_model_id))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    safety_block = detect_safety_block(payload.query)
    if safety_block is not None:
        emit_json_log(
            logging.WARNING,
            "knowledge_safety_block",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            category=safety_block.category,
            risk_level=safety_block.risk_level,
        )
        raise safety_block_http_exception(safety_block)
    settings = get_settings()
    enforce_business_rate_limit(
        db,
        request,
        action="knowledge_answer",
        user_id=user.id,
        settings=settings,
    )
    enforce_embedding_rate_limit(db, request, user_id=user.id, settings=settings)
    threshold_version = db.scalar(
        select(KnowledgeDocument.sha256)
        .where(
            KnowledgeDocument.robot_model_id == payload.robot_model_id,
            KnowledgeDocument.status == "active",
        )
        .order_by(KnowledgeDocument.id.desc())
        .limit(1)
    )
    min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)
    normalized_query = normalize_knowledge_query(payload.query)
    try:
        result = generate_answer(
            db,
            user_id=user.id,
            robot_model_id=payload.robot_model_id,
            query=normalized_query,
            embedding_provider=request.app.state.embedding_provider,
            generation_provider=request.app.state.generation_provider,
            top_k=payload.top_k,
            min_score=min_score,
            gap_source="knowledge_answer_refusal",
        )
    except RuntimeError as exc:
        emit_json_log(
            logging.ERROR,
            "knowledge_answer_failed",
            trace_id=request_trace_id(request),
            robot_model_id=payload.robot_model_id,
            error_type=type(exc).__name__,
        )
        send_alert("generation_unavailable", "智能回答服务调用失败（生成模型不可用），请检查 DashScope 配置与额度")
        raise HTTPException(status_code=503, detail="Generation service unavailable") from exc
    # 拒答埋点已下沉到生成层（generation_service.refuse），聊天与本端点共用
    # 同一条路径，这里不再重复记录——否则本端点的缺口会被计两次。
    return KnowledgeAnswerResponse(
        status=result.status,
        answer=result.answer,
        citations=[AnswerCitationRead(**item.__dict__) for item in result.citations],
        refusal_reason=result.refusal_reason,
        record_id=result.record_id,
    )


@router.get("/knowledge/status", response_model=list[KnowledgeStatusRead])
def knowledge_status(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[KnowledgeStatusRead]:
    del user
    return [KnowledgeStatusRead(**item.__dict__) for item in get_knowledge_status(db)]


def _probe_external_models(
    db: Session, request: Request, model_health: list
) -> KnowledgeProbeRead:
    """对 embedding / 检索链路 / 生成模型各发一次真实请求。

    配置存在不等于服务可用（DNS、key 失效、权限、接口格式都可能 503），
    所以每一项都必须由实际调用证明。探测会产生真实模型调用费用，
    仅在显式传 probe=true 时执行，并走 embedding 限流。
    """
    errors: list[str] = []
    embedding_ok = False
    retrieval_ok = False
    generation_ok = False

    try:
        vector = request.app.state.embedding_provider.embed_query("健康探测")
        embedding_ok = bool(vector)
    except Exception as exc:  # noqa: BLE001 — 探测必须报告任何失败而不是 500
        errors.append(f"embedding: {type(exc).__name__}: {exc}"[:200])

    if embedding_ok:
        ready_model = next((item for item in model_health if item.ready), None)
        if ready_model is None:
            errors.append("retrieval: no ready knowledge model to probe")
        else:
            try:
                search_knowledge(
                    db,
                    robot_model_id=ready_model.robot_model_id,
                    query="健康探测",
                    top_k=1,
                    min_score=0.0,
                    provider=request.app.state.embedding_provider,
                )
                retrieval_ok = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"retrieval: {type(exc).__name__}: {exc}"[:200])

    try:
        reply = request.app.state.generation_provider.generate(
            system="你是健康探测程序。", prompt="请只回复 OK。"
        )
        generation_ok = bool(reply and reply.strip())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"generation: {type(exc).__name__}: {exc}"[:200])

    return KnowledgeProbeRead(
        embedding_service=embedding_ok,
        retrieval_end_to_end=retrieval_ok,
        generation_service=generation_ok,
        errors=errors,
    )


@router.get("/knowledge/health", response_model=KnowledgeHealthRead)
def knowledge_health(
    request: Request,
    probe: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KnowledgeHealthRead:
    model_health = get_knowledge_health(db)
    embedding_configured = bool(
        getattr(request.app.state, "embedding_configured", False)
    )
    knowledge_ready = bool(model_health) and all(item.ready for item in model_health)

    probe_result: KnowledgeProbeRead | None = None
    if probe:
        # probe=true 会对 embedding / 检索 / 生成各发一次**真实**外部调用，
        # 是有成本的运维动作，不是普通用户该有的按钮：收权到 viewer 及以上。
        if not has_capability(user.role, "read_operations"):
            raise HTTPException(
                status_code=403,
                detail=CAPABILITY_MESSAGES["read_operations"],
            )
        enforce_embedding_rate_limit(db, request, user_id=user.id)
        # 探测里包含一次真实生成，此前只受 embedding 日配额约束，
        # 等于给生成开了一条不计费的旁路 —— 补上生成侧的分钟级限流。
        enforce_business_rate_limit(
            db,
            request,
            action="knowledge_answer",
            user_id=user.id,
            settings=get_settings(),
        )
        probe_result = _probe_external_models(db, request, model_health)

    if not embedding_configured:
        health_status = "external_model_unavailable"
    elif probe_result is not None and not (
        probe_result.embedding_service
        and probe_result.retrieval_end_to_end
        and probe_result.generation_service
    ):
        # 深度探测发现外部模型实际不可用：覆盖"配置看起来正常"的结论
        health_status = "external_model_unavailable"
    elif not knowledge_ready:
        health_status = "knowledge_degraded"
    else:
        health_status = "normal"
    return KnowledgeHealthRead(
        status=health_status,
        ready=health_status == "normal",
        embedding_configured=embedding_configured,
        models=[KnowledgeModelHealthRead(**item.__dict__) for item in model_health],
        probe=probe_result,
    )


class _PageOutOfRange(RuntimeError):
    """请求页码超出原件总页数（与"文件损坏"区分，前者是 404 后者是 422）。"""


def _extract_pdf_page(path, page_number: int) -> bytes:
    reader = PdfReader(str(path))
    if page_number > len(reader.pages):
        raise _PageOutOfRange(str(page_number))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_number - 1])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@router.get("/knowledge/citations/{document_sha256}/pages/{page_number}")
def read_citation_page(
    document_sha256: str,
    page_number: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """取出引用命中的**那一页**原件。

    问题背景：证据抽屉中的“打开说明书原页”此前使用官方外链，
    而海尔那份是 27MB 的整本 PDF——等于让手机用户下完整本书自己翻到第 15 页。
    引用不可核验，"每条回答都标注资料页码"这个卖点就只剩一个角标。

    用 sha256 而不是 document_id 定位：引用记录里本来就有 sha256（含历史数据），
    不必为了这个接口改动已落库的 citations_json 结构。

    权限：登录用户即可读，但只限 active 型号下的 active 文档——
    这和他们本来就能检索到的内容范围完全一致，不放大可见面。
    """
    settings = get_settings()
    # 抽页要读原件并重新编码，成本与一次检索相当，沿用检索的限流口径
    enforce_business_rate_limit(
        db, request, action="knowledge_search", user_id=user.id, settings=settings
    )
    if page_number < 1:
        raise HTTPException(status_code=422, detail="Page number starts at 1")

    document = db.scalar(
        select(KnowledgeDocument)
        .join(RobotModel, RobotModel.id == KnowledgeDocument.robot_model_id)
        .where(
            KnowledgeDocument.sha256 == document_sha256.lower(),
            KnowledgeDocument.status == "active",
            RobotModel.active.is_(True),
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Cited document not found")

    path = resolve_stored_file(
        getattr(request.app.state, "knowledge_dir", ""), document.stored_filename
    )
    if path is None:
        # 存档功能上线前入库的老文档没有原件，前端据此回退到外链
        raise HTTPException(
            status_code=404, detail="该资料没有留存原件，请改用来源链接查看"
        )

    cache = getattr(request.app.state, "citation_page_cache", None)
    cache_key = (
        CitationPageCache.make_key(document.sha256, page_number)
        if cache is not None
        else ""
    )
    payload = cache.get(cache_key) if cache is not None else None
    cache_hit = payload is not None

    if payload is None:
        try:
            if cache is not None:
                with cache.extraction_slot():
                    payload = _extract_pdf_page(path, page_number)
            else:
                payload = _extract_pdf_page(path, page_number)
        except CitationPageCacheFull:
            # 并发解析已达到上限；要求客户端稍后重试以保护进程内存。
            raise HTTPException(
                status_code=503,
                detail="原件读取繁忙，请稍后重试",
                headers={"Retry-After": "3"},
            ) from None
        except _PageOutOfRange:
            raise HTTPException(status_code=404, detail="Cited page is out of range") from None
        except Exception as exc:  # noqa: BLE001 — 损坏/加密原件不该以 500 收场
            emit_json_log(
                logging.WARNING,
                "citation_page_unreadable",
                trace_id=request_trace_id(request),
                user_id=user.id,
                document_sha256=document.sha256,
                page_number=page_number,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=422, detail="该资料原件无法解析，请改用来源链接查看"
            ) from None
        if cache is not None:
            cache.set(cache_key, payload)

    emit_json_log(
        logging.INFO,
        "citation_page_read",
        trace_id=request_trace_id(request),
        user_id=user.id,
        robot_model_id=document.robot_model_id,
        page_number=page_number,
        cache_hit=cache_hit,
    )
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/pdf",
        headers={
            # inline：用户要的是"看一眼这页"，不是下载一个文件
            "Content-Disposition": f'inline; filename="page-{page_number}.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )
