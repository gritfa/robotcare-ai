"""/knowledge/* routes: search, answer, status, health."""

import logging
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..alerting import send_alert
from ..config import get_settings
from ..database import get_db
from ..generation_service import generate_answer
from ..knowledge_service import get_knowledge_health, get_knowledge_status, search_knowledge
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
from ..security import get_current_user

router = APIRouter(prefix="/api/v1")


def record_knowledge_gap_event(
    db: Session,
    *,
    robot_model_id: int,
    query_normalized: str,
    source: str,
    trace_id: str | None = None,
) -> None:
    """内容缺口埋点：写入失败只记日志，绝不影响检索/回答主流程。"""

    try:
        db.add(
            KnowledgeGapEvent(
                robot_model_id=robot_model_id,
                query_normalized=query_normalized,
                source=source,
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 埋点失败不允许打断主流程
        db.rollback()
        emit_json_log(
            logging.ERROR,
            "knowledge_gap_event_write_failed",
            trace_id=trace_id,
            robot_model_id=robot_model_id,
            source=source,
            error_type=type(exc).__name__,
        )


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
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SAFETY_BLOCKED",
                "blocked": True,
                "category": safety_block.category,
                "risk_level": safety_block.risk_level,
                "reason": safety_block.reason,
                "official_service_advice": safety_block.advice,
            },
        )
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
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SAFETY_BLOCKED",
                "blocked": True,
                "category": safety_block.category,
                "risk_level": safety_block.risk_level,
                "reason": safety_block.reason,
                "official_service_advice": safety_block.advice,
            },
        )
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
    if result.status == "refused" and result.refusal_reason == "knowledge_gap":
        record_knowledge_gap_event(
            db,
            robot_model_id=payload.robot_model_id,
            query_normalized=normalized_query,
            source="answer_knowledge_gap",
            trace_id=request_trace_id(request),
        )
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
        enforce_embedding_rate_limit(db, request, user_id=user.id)
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
