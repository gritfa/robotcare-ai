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
        .where(KnowledgeDocument.robot_model_id == payload.robot_model_id)
        .order_by(KnowledgeDocument.id.desc())
        .limit(1)
    )
    document_shas = list(
        db.scalars(
            select(KnowledgeDocument.sha256)
            .where(KnowledgeDocument.robot_model_id == payload.robot_model_id)
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
        .where(KnowledgeDocument.robot_model_id == payload.robot_model_id)
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


@router.get("/knowledge/health", response_model=KnowledgeHealthRead)
def knowledge_health(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> KnowledgeHealthRead:
    del user
    model_health = get_knowledge_health(db)
    embedding_configured = bool(
        getattr(request.app.state, "embedding_configured", False)
    )
    knowledge_ready = bool(model_health) and all(item.ready for item in model_health)
    if not embedding_configured:
        health_status = "external_model_unavailable"
    elif not knowledge_ready:
        health_status = "knowledge_degraded"
    else:
        health_status = "normal"
    return KnowledgeHealthRead(
        status=health_status,
        ready=knowledge_ready and embedding_configured,
        embedding_configured=embedding_configured,
        models=[KnowledgeModelHealthRead(**item.__dict__) for item in model_health],
    )
