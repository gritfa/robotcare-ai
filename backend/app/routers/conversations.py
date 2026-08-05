"""智能客服多轮会话：先选型号建会话 → 连续追问 → 每轮带引用页码/安全拦截/如实拒答。

复用 knowledge_answer 的完整链路（安全前置阻断、业务/embedding 限流、
generate_answer 的强制引用与四类拒答），在此之上叠加会话持久化与多轮上下文。
拒答也落 assistant 消息（用户可见的解释文案），保证会话历史完整可回放。
"""

import json
import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..config import get_settings
from ..generation_service import generate_answer
from ..chat_actions import (
    answer_quick_actions,
    resolve_diagnostic_request,
    resolve_report_request,
)
from ..message_router import RoutingDecision, classify_message
from ..models import (
    Conversation,
    ConversationMessage,
    KnowledgeDocument,
    MessageFeedback,
    RobotModel,
    User,
)
from ..observability import emit_json_log, request_trace_id
from ..rate_limit_service import enforce_business_rate_limit, enforce_embedding_rate_limit
from ..safety import detect_safety_block
from ..schemas import (
    AnswerCitationRead,
    ChatMessageRequest,
    ChatMessageResponse,
    ConversationCreateRequest,
    ConversationDetailRead,
    ConversationMessageRead,
    ConversationRead,
    ConversationUpdateRequest,
    MessageFeedbackRead,
    MessageFeedbackRequest,
)
from ..security import get_current_user

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

MAX_CONTEXT_MESSAGES = 6
STREAM_CHUNK_CHARS = 48
REFUSAL_TEXTS = {
    "knowledge_gap": "抱歉，当前资料中没有找到能回答这个问题的内容，建议联系官方售后获取帮助。",
    "model_refused": "抱歉，现有资料不足以回答这个问题，建议联系官方售后获取帮助。",
    "citation_invalid": "抱歉，本次回答未能通过引用校验，为避免误导已拦截，请换个问法再试。",
    "unsafe_answer": "抱歉，本次回答未通过安全检查已被拦截。涉及危险情况请直接联系官方售后。",
}


def _owned_conversation(db: Session, conversation_id: int, user: User) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == conversation_id)
    )
    if conversation is None or conversation.user_id != user.id:
        # 404 而非 403：不向他人泄露会话是否存在
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _message_read(message: ConversationMessage) -> ConversationMessageRead:
    return ConversationMessageRead(
        id=message.id,
        role=message.role,
        content=message.content,
        citations=[AnswerCitationRead(**c) for c in (message.citations_json or [])],
        refusal_reason=message.refusal_reason,
        intent=message.intent,
        action_code=message.action_code,
        quick_actions=message.quick_actions_json or [],
        created_at=message.created_at,
    )


@router.post("", response_model=ConversationRead, status_code=201)
def create_conversation(
    payload: ConversationCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConversationRead:
    robot_model = db.scalar(select(RobotModel).where(RobotModel.id == payload.robot_model_id))
    if robot_model is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    conversation = Conversation(user_id=user.id, robot_model_id=robot_model.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return ConversationRead(
        id=conversation.id,
        robot_model_id=robot_model.id,
        robot_model_code=robot_model.code,
        title=conversation.title,
        updated_at=conversation.updated_at,
    )


@router.get("", response_model=list[ConversationRead])
def list_conversations(
    search: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ConversationRead]:
    statement = (
        select(Conversation, RobotModel.code)
        .join(RobotModel, RobotModel.id == Conversation.robot_model_id)
        .where(Conversation.user_id == user.id)
    )
    if search:
        # 标题 + 消息正文一起搜：用户记得的往往是"我问过拖布"，而不是会话标题
        keyword = f"%{search.strip()}%"
        statement = statement.where(
            or_(
                Conversation.title.ilike(keyword),
                Conversation.messages.any(ConversationMessage.content.ilike(keyword)),
            )
        )
    rows = db.execute(statement.order_by(Conversation.updated_at.desc()).limit(50)).all()
    return [
        ConversationRead(
            id=c.id,
            robot_model_id=c.robot_model_id,
            robot_model_code=code,
            title=c.title,
            resolved=c.resolved,
            updated_at=c.updated_at,
        )
        for c, code in rows
    ]


@router.patch("/{conversation_id}", response_model=ConversationRead)
def update_conversation(
    conversation_id: int,
    payload: ConversationUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConversationRead:
    """改标题 / 标记问题是否已解决。"""

    conversation = _owned_conversation(db, conversation_id, user)
    if payload.title is not None:
        conversation.title = payload.title.strip()[:120]
    if payload.resolved is not None:
        conversation.resolved = payload.resolved
    db.commit()
    db.refresh(conversation)
    robot_model = db.get(RobotModel, conversation.robot_model_id)
    return ConversationRead(
        id=conversation.id,
        robot_model_id=conversation.robot_model_id,
        robot_model_code=robot_model.code,
        title=conversation.title,
        resolved=conversation.resolved,
        updated_at=conversation.updated_at,
    )


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """删除会话及其消息。

    GenerationRecord 不随之删除：那是生成层的审计留痕（prompt 版本、片段 SHA、
    拒答原因），用户删自己的聊天记录不该抹掉系统的可追溯性。消息表的
    generation_record_id 是单向引用，删消息不影响留痕本身。
    """

    conversation = _owned_conversation(db, conversation_id, user)
    db.delete(conversation)
    db.commit()


@router.get("/{conversation_id}", response_model=ConversationDetailRead)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConversationDetailRead:
    conversation = _owned_conversation(db, conversation_id, user)
    robot_model = db.get(RobotModel, conversation.robot_model_id)
    return ConversationDetailRead(
        id=conversation.id,
        robot_model_id=conversation.robot_model_id,
        robot_model_code=robot_model.code,
        title=conversation.title,
        messages=[_message_read(m) for m in conversation.messages],
    )


def _prepare_turn(
    db: Session,
    request: Request,
    conversation: Conversation,
    content: str,
    user: User,
) -> tuple[ConversationMessage, list[dict], float | None, RoutingDecision]:
    """安全前置阻断 + 限流 + 路由分流 + 阈值 + 多轮上下文 + 落用户消息（flush 未 commit）。

    顺序不可调整：安全检测永远排在路由之前，闲聊/操作分支绝不能成为绕过
    安全阻断的旁路。业务限流对四类消息一视同仁（防刷），但 embedding 限流和
    阈值计算只在真要走检索时才执行——这正是路由层要省掉的开销。
    """
    safety_block = detect_safety_block(content)
    if safety_block is not None:
        emit_json_log(
            logging.WARNING,
            "chat_safety_block",
            trace_id=request_trace_id(request),
            conversation_id=conversation.id,
            category=safety_block.category,
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
        db, request, action="knowledge_answer", user_id=user.id, settings=settings
    )

    history = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages[-MAX_CONTEXT_MESSAGES:]
    ]
    decision = classify_message(content, has_history=bool(history))
    emit_json_log(
        logging.INFO,
        "chat_message_routed",
        trace_id=request_trace_id(request),
        conversation_id=conversation.id,
        **decision.metadata(),
    )

    min_score: float | None = None
    if decision.needs_retrieval:
        enforce_embedding_rate_limit(db, request, user_id=user.id, settings=settings)
        robot_model = db.get(RobotModel, conversation.robot_model_id)
        threshold_version = db.scalar(
            select(KnowledgeDocument.sha256)
            .where(KnowledgeDocument.robot_model_id == conversation.robot_model_id)
            .order_by(KnowledgeDocument.id.desc())
            .limit(1)
        )
        min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)

    user_message = ConversationMessage(
        conversation_id=conversation.id, role="user", content=content
    )
    db.add(user_message)
    if not conversation.title:
        conversation.title = content[:60]
    db.flush()
    return user_message, history, min_score, decision


def _generate_turn_answer(
    db: Session,
    request: Request,
    conversation: Conversation,
    content: str,
    history: list[dict],
    min_score: float,
    user: User,
):
    try:
        return generate_answer(
            db,
            user_id=user.id,
            robot_model_id=conversation.robot_model_id,
            query=content,
            embedding_provider=request.app.state.embedding_provider,
            generation_provider=request.app.state.generation_provider,
            history=history,
            min_score=min_score,
            commit=False,
        )
    except RuntimeError as exc:
        db.rollback()
        emit_json_log(
            logging.ERROR,
            "chat_answer_failed",
            trace_id=request_trace_id(request),
            conversation_id=conversation.id,
            error_type=type(exc).__name__,
        )
        raise


def _routed_reply(
    db: Session, conversation: Conversation, decision: RoutingDecision, user: User
) -> tuple[str, list[dict[str, object]]]:
    """把路由结论落成用户可见的文案与按钮。

    产品操作要看当前诊断状态才知道该说什么（"生成报告"在没做过诊断和报告已就绪时
    是两种完全不同的回答，见 chat_actions.py）；能力介绍要填当前型号。
    """

    if decision.action_code == "generate_report":
        outcome = resolve_report_request(db, user=user, conversation=conversation)
        return outcome.reply, outcome.quick_actions
    if decision.action_code == "start_diagnostic":
        outcome = resolve_diagnostic_request(db, user=user, conversation=conversation)
        return outcome.reply, outcome.quick_actions

    reply = decision.reply or ""
    if decision.uses_model_placeholder:
        robot_model = db.get(RobotModel, conversation.robot_model_id)
        reply = reply.format(model_code=robot_model.code)
    if decision.intent == "capability":
        return reply, [
            {"code": "start_diagnostic", "label": "开始分步诊断"},
            {"code": "contact_support", "label": "联系官方售后"},
        ]
    if decision.action_code == "upload_image":
        return reply, [{"code": "upload_image", "label": "去上传图片"}]
    return reply, []


def _routed_assistant_message(
    conversation: Conversation,
    decision: RoutingDecision,
    reply: str,
    quick_actions: list[dict[str, object]],
) -> ConversationMessage:
    """闲聊/能力介绍/产品操作的直接回复：不检索、不调生成模型、不产生 GenerationRecord。

    这类消息没有引用，是因为它本来就不该有——引用是"依据说明书作答"的凭证，
    打招呼和点按钮都不是在作答，硬造引用才是失真。
    """

    return ConversationMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=reply,
        intent=decision.intent,
        routing_rule=decision.matched_rule,
        action_code=decision.action_code,
        quick_actions_json=quick_actions,
    )


def _assistant_message_for(
    conversation: Conversation, result, decision: RoutingDecision
) -> ConversationMessage:
    if result.status == "answered":
        return ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=result.answer,
            intent=decision.intent,
            routing_rule=decision.matched_rule,
            quick_actions_json=answer_quick_actions(refused=False),
            citations_json=[
                {
                    "index": c.index,
                    "source_url": c.source_url,
                    "page_number": c.page_number,
                    "score": c.score,
                    "document_sha256": c.document_sha256,
                    "snippet": c.snippet,
                    "document_title": c.document_title,
                }
                for c in result.citations
            ],
            generation_record_id=result.record_id,
        )
    return ConversationMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=REFUSAL_TEXTS.get(result.refusal_reason, REFUSAL_TEXTS["model_refused"]),
        refusal_reason=result.refusal_reason,
        intent=decision.intent,
        routing_rule=decision.matched_rule,
        quick_actions_json=answer_quick_actions(refused=True),
        generation_record_id=result.record_id,
    )


def _commit_turn(
    db: Session,
    conversation: Conversation,
    user_message: ConversationMessage,
    assistant_message: ConversationMessage,
) -> ConversationMessage:
    db.add(assistant_message)
    conversation.updated_at = user_message.created_at
    db.commit()
    db.refresh(user_message)
    db.refresh(assistant_message)
    return assistant_message


@router.post("/{conversation_id}/messages", response_model=ChatMessageResponse)
def post_message(
    conversation_id: int,
    payload: ChatMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    conversation = _owned_conversation(db, conversation_id, user)
    user_message, history, min_score, decision = _prepare_turn(
        db, request, conversation, payload.content, user
    )
    if not decision.needs_retrieval:
        reply, quick_actions = _routed_reply(db, conversation, decision, user)
        assistant_message = _commit_turn(
            db,
            conversation,
            user_message,
            _routed_assistant_message(conversation, decision, reply, quick_actions),
        )
        return ChatMessageResponse(
            user_message=_message_read(user_message),
            assistant_message=_message_read(assistant_message),
        )
    try:
        result = _generate_turn_answer(
            db, request, conversation, payload.content, history, min_score, user
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Generation service unavailable") from exc
    assistant_message = _commit_turn(
        db, conversation, user_message, _assistant_message_for(conversation, result, decision)
    )
    return ChatMessageResponse(
        user_message=_message_read(user_message),
        assistant_message=_message_read(assistant_message),
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/{conversation_id}/messages/stream")
def post_message_stream(
    conversation_id: int,
    payload: ChatMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE 流式变体：先走完整安全/引用/输出侧校验，再把已验证的回答分片下发。

    进入流之前的错误（越权 404、安全拦截 422、限流 429）仍走普通 HTTP 状态码；
    进入流之后的生成失败以 error 事件下发。答案只有在通过全部校验后才开始
    分片，不存在"未校验内容先出现在用户屏幕"的窗口。
    """
    conversation = _owned_conversation(db, conversation_id, user)
    user_message, history, min_score, decision = _prepare_turn(
        db, request, conversation, payload.content, user
    )

    def event_stream() -> Iterator[str]:
        yield _sse("user_message", _message_read(user_message).model_dump(mode="json"))
        if not decision.needs_retrieval:
            # 闲聊/操作照样走 delta→assistant_message→done，前端只有一条渲染路径；
            # 区别只是没有 generating 阶段（本来就不调模型，不该显示"正在生成"）。
            reply, quick_actions = _routed_reply(db, conversation, decision, user)
            routed = _commit_turn(
                db,
                conversation,
                user_message,
                _routed_assistant_message(conversation, decision, reply, quick_actions),
            )
            for start in range(0, len(routed.content), STREAM_CHUNK_CHARS):
                yield _sse("delta", {"text": routed.content[start : start + STREAM_CHUNK_CHARS]})
            yield _sse("assistant_message", _message_read(routed).model_dump(mode="json"))
            yield _sse("done", {})
            return
        yield _sse("stage", {"stage": "generating"})
        try:
            result = _generate_turn_answer(
                db, request, conversation, payload.content, history, min_score, user
            )
        except RuntimeError:
            # 用户消息随事务一并回滚，与同步端点 503 的语义保持一致
            yield _sse(
                "error",
                {"code": "GENERATION_UNAVAILABLE", "message": "生成服务暂不可用，请稍后重试"},
            )
            return
        assistant_message = _commit_turn(
            db, conversation, user_message, _assistant_message_for(conversation, result, decision)
        )
        if result.status == "answered":
            for start in range(0, len(assistant_message.content), STREAM_CHUNK_CHARS):
                yield _sse(
                    "delta",
                    {"text": assistant_message.content[start : start + STREAM_CHUNK_CHARS]},
                )
        yield _sse(
            "assistant_message", _message_read(assistant_message).model_dump(mode="json")
        )
        yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{conversation_id}/messages/{message_id}/feedback", response_model=MessageFeedbackRead
)
def submit_message_feedback(
    conversation_id: int,
    message_id: int,
    payload: MessageFeedbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageFeedbackRead:
    """给单条回答打"有帮助/没帮助"，没帮助时带原因码。

    只允许对 assistant 消息反馈——给自己的提问打分没有意义，放开只会污染统计。
    同一条消息重复提交按覆盖处理（用户改主意是正常行为，不该报错）。
    """

    conversation = _owned_conversation(db, conversation_id, user)
    message = db.get(ConversationMessage, message_id)
    if message is None or message.conversation_id != conversation.id:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.role != "assistant":
        raise HTTPException(status_code=422, detail="Only assistant messages accept feedback")

    feedback = db.scalar(
        select(MessageFeedback).where(MessageFeedback.message_id == message.id)
    )
    if feedback is None:
        feedback = MessageFeedback(message_id=message.id, user_id=user.id, helpful=payload.helpful)
        db.add(feedback)
    feedback.helpful = payload.helpful
    # "有帮助"不该挂着上一次的差评原因
    feedback.reason = payload.reason if not payload.helpful else None
    db.commit()
    db.refresh(feedback)
    return MessageFeedbackRead(
        message_id=message.id, helpful=feedback.helpful, reason=feedback.reason
    )
