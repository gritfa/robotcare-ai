"""智能客服多轮会话：先选型号建会话 → 连续追问 → 每轮带引用页码/安全拦截/如实拒答。

复用 knowledge_answer 的完整链路（安全前置阻断、业务/embedding 限流、
generate_answer 的强制引用与四类拒答），在此之上叠加会话持久化与多轮上下文。
拒答也落 assistant 消息（用户可见的解释文案），保证会话历史完整可回放。
"""

import json
import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..config import get_settings
from ..generation_service import answer_events, generate_answer
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
    utcnow,
)
from ..observability import emit_json_log, request_trace_id
from ..rate_limit_service import enforce_business_rate_limit, enforce_embedding_rate_limit
from ._shared import LIKE_ESCAPE, escape_like, safety_block_http_exception
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
from ..context_window import MAX_CONTEXT_MESSAGES, select_context_messages
from ..security import get_current_user

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

STREAM_CHUNK_CHARS = 48
# 会话详情默认/最大返回条数。默认值远大于 MAX_CONTEXT_MESSAGES，
# 保证「界面看得到的历史」比「模型实际用到的上下文」宽裕得多。
MESSAGE_PAGE_SIZE = 200
MAX_MESSAGE_PAGE_SIZE = 500
# 会话列表单页上限（原本硬编码 50，提出来供测试与前端共用）
CONVERSATION_PAGE_SIZE = 50
MAX_CONVERSATION_PAGE_SIZE = 200
REFUSAL_TEXTS = {
    "knowledge_gap": "抱歉，当前资料中没有找到能回答这个问题的内容，建议联系官方售后获取帮助。",
    "model_refused": "抱歉，现有资料不足以回答这个问题，建议联系官方售后获取帮助。",
    "citation_invalid": "抱歉，本次回答未能通过引用校验，为避免误导已拦截，请换个问法再试。",
    "unsafe_answer": "抱歉，本次回答未通过安全检查已被拦截。涉及危险情况请直接联系官方售后。",
}


def _owned_conversation(db: Session, conversation_id: int, user: User) -> Conversation:
    """按 id 取会话并校验归属。

    **不要**在这里 selectinload(messages)。原来这么写，等于每个用到会话的接口
    发消息、流式、反馈和改标题等操作不应加载完整历史；需要消息的入口按需分页。
    """

    conversation = db.scalar(
        select(Conversation).where(Conversation.id == conversation_id)
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
    # 限制单个用户的会话数量，避免空会话持续增长影响存储和列表性能。
    # 用 409 而不是 429——这不是"太快了等一会儿"，是"你的会话太多了，删掉一些"。
    settings = get_settings()
    existing = db.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.user_id == user.id)
    ) or 0
    if existing >= settings.max_conversations_per_user:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONVERSATION_LIMIT_REACHED",
                "limit": settings.max_conversations_per_user,
                "message": (
                    f"会话数量已达上限 {settings.max_conversations_per_user} 个，"
                    "请先删除一些不再需要的会话。"
                ),
            },
        )
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
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(
        default=CONVERSATION_PAGE_SIZE, ge=1, le=MAX_CONVERSATION_PAGE_SIZE
    ),
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
        keyword = f"%{escape_like(search.strip())}%"
        statement = statement.where(
            or_(
                Conversation.title.ilike(keyword, escape=LIKE_ESCAPE),
                Conversation.messages.any(
                    ConversationMessage.content.ilike(keyword, escape=LIKE_ESCAPE)
                ),
            )
        )
    rows = db.execute(statement.order_by(Conversation.updated_at.desc()).limit(limit)).all()
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
    limit: int = Query(default=MESSAGE_PAGE_SIZE, ge=1, le=MAX_MESSAGE_PAGE_SIZE),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ConversationDetailRead:
    """只回最近 `limit` 条消息。

    此前是 `conversation.messages` 全量加载：一条会话问上几百轮之后，每次打开
    历史消息按需分页，避免加载和序列化不需要的完整会话内容。
    """

    conversation = _owned_conversation(db, conversation_id, user)
    robot_model = db.get(RobotModel, conversation.robot_model_id)
    total = db.scalar(
        select(func.count())
        .select_from(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
    ) or 0
    # 取最近 limit 条（按 id 倒序），再翻回时间正序交给前端渲染
    recent = list(
        db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.id.desc())
            .limit(limit)
        )
    )
    recent.reverse()
    return ConversationDetailRead(
        id=conversation.id,
        robot_model_id=conversation.robot_model_id,
        robot_model_code=robot_model.code,
        title=conversation.title,
        messages=[_message_read(m) for m in recent],
        total_messages=total,
        truncated=total > len(recent),
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
        raise safety_block_http_exception(safety_block)

    settings = get_settings()
    enforce_business_rate_limit(
        db, request, action="knowledge_answer", user_id=user.id, settings=settings
    )

    # 只查最近 MAX_CONTEXT_MESSAGES 条，而不是全量加载后切片
    recent_messages = list(
        db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.id.desc())
            .limit(MAX_CONTEXT_MESSAGES)
        )
    )
    recent_messages.reverse()
    # 首轮问题带着型号、现象、已尝试步骤这些背景，是最不该被时间窗口挤掉的一条。
    # 单独取一次（conversation_id + id 有索引，代价可忽略），滑出窗口就置顶补回。
    first_user_message = db.scalar(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation.id,
            ConversationMessage.role == "user",
        )
        .order_by(ConversationMessage.id.asc())
        .limit(1)
    )
    history = select_context_messages(recent_messages, first_user_message)
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
            .where(
                KnowledgeDocument.robot_model_id == conversation.robot_model_id,
                KnowledgeDocument.status == "active",
            )
            .order_by(KnowledgeDocument.id.desc())
            .limit(1)
        )
        min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)

    # 用户消息在这里只构造、不落库。避免 add+flush 后持有未提交事务，
    # 若事务覆盖完整生成过程，SSE 会持续占用连接和行锁，影响其他请求。
    # created_at 显式取"提问时刻"，否则延迟到生成之后落库会让它比实际晚几秒。
    user_message = ConversationMessage(
        conversation_id=conversation.id,
        role="user",
        content=content,
        created_at=utcnow(),
    )
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


def _generate_turn_events(
    db: Session,
    request: Request,
    conversation: Conversation,
    content: str,
    history: list[dict],
    min_score: float,
    user: User,
):
    """流式变体：产出阶段/增量事件，返回值为最终 AnswerResult。

    失败处理与 _generate_turn_answer 完全一致（回滚 + 结构化日志 + 重抛），
    两条路径的事务语义不能有差异。

    捕获范围是 Exception 而不是 RuntimeError：非 RuntimeError（上游返回体变形导致的
    ValueError、KeyError 等）此前直接穿透，而**流式响应的头已经发出去了**，
    FastAPI 的全局异常处理再也插不进来——客户端收到的是一个没有 error 事件、
    也没有 done 的截断流，前端只能一直转圈。同步端点有 500 兜底，流式没有。
    GeneratorExit（客户端断连）是 BaseException，不在捕获范围内，正是想要的：
    断连不该被记成生成失败，也不该往一个已经关掉的连接里写 error 事件。
    """
    try:
        return (
            yield from answer_events(
                db,
                user_id=user.id,
                robot_model_id=conversation.robot_model_id,
                query=content,
                embedding_provider=request.app.state.embedding_provider,
                generation_provider=request.app.state.generation_provider,
                history=history,
                min_score=min_score,
                commit=False,
                streaming=True,
            )
        )
    except Exception as exc:
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
    # 用户消息与助手消息在这里一起落库。延迟到此刻的直接好处：生成失败或客户端
    # 中断时这一轮天然不留痕，不再依赖"回滚一个开着整场生成的长事务"。
    db.add(user_message)
    if not conversation.title:
        conversation.title = user_message.content[:60]
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
        # user_message 事件此前在流开头下发，靠的是"进流之前已 flush 用户消息"——
        # 改为延迟落库后，生成期间不再持续占用数据库连接，
        # 用户消息在这里还没有 id，事件顺延到落库之后下发。
        # 前端本就先渲染一条乐观消息（ChatView「乐观展示用户消息」），
        # 收到本事件时把它替换成真实记录，所以时机后移对用户无感。
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
            yield _sse("user_message", _message_read(user_message).model_dump(mode="json"))
            for start in range(0, len(routed.content), STREAM_CHUNK_CHARS):
                yield _sse("delta", {"text": routed.content[start : start + STREAM_CHUNK_CHARS]})
            yield _sse("assistant_message", _message_read(routed).model_dump(mode="json"))
            yield _sse("done", {})
            return
        # 真流式：把 answer_events 的过程事件直接转成 SSE 当场下发。
        # 首字延迟不再等于完整生成延迟；阶段事件也从"只有 generating 一个点"
        # 变成 retrieving → retrieved → generating 的真实进度。
        events = _generate_turn_events(
            db, request, conversation, payload.content, history, min_score, user
        )
        result = None
        try:
            while True:
                try:
                    kind, value = next(events)
                except StopIteration as stop:
                    result = stop.value
                    break
                if kind == "stage":
                    yield _sse("stage", {"stage": value})
                elif kind == "delta":
                    yield _sse("delta", {"text": value})
        except RuntimeError:
            # 用户消息随事务一并回滚，与同步端点 503 的语义保持一致
            yield _sse(
                "error",
                {"code": "GENERATION_UNAVAILABLE", "message": "生成服务暂不可用，请稍后重试"},
            )
            return
        except Exception:
            # 流的头已经发出去了，全局异常处理插不进来。不在这里兜住，
            # 客户端拿到的就是一个既没有 error 也没有 done 的截断流。
            # 对用户的说法与上面一致（都是"这轮没成"），但 code 分开，
            # 便于运维区分"外部模型挂了"和"我们自己的代码炸了"（日志已记 error_type）。
            yield _sse(
                "error",
                {"code": "INTERNAL_ERROR", "message": "服务暂时异常，请稍后重试"},
            )
            return
        assistant_message = _commit_turn(
            db, conversation, user_message, _assistant_message_for(conversation, result, decision)
        )
        streamed = result.streamed_text
        if result.status == "answered":
            final_text = assistant_message.content
            if streamed and final_text.startswith(streamed):
                # 常规情况：补发闸门留在手里的尾段（含末尾残句）
                remainder = final_text[len(streamed) :]
                if remainder:
                    yield _sse("delta", {"text": remainder})
            elif streamed:
                # 已下发内容不是最终答案的前缀（闸门关过、或末尾标记被剥离后
                # 文本发生偏移）——让前端丢弃重来，绝不把两段拼出一个假答案
                yield _sse("discard", {"reason": "answer_revised"})
                for start in range(0, len(final_text), STREAM_CHUNK_CHARS):
                    yield _sse("delta", {"text": final_text[start : start + STREAM_CHUNK_CHARS]})
            else:
                for start in range(0, len(final_text), STREAM_CHUNK_CHARS):
                    yield _sse("delta", {"text": final_text[start : start + STREAM_CHUNK_CHARS]})
        elif streamed:
            # 最终判定为拒答，但已经有内容到过用户屏幕：必须明确撤回。
            # 留着不管，用户会把半截未通过校验的文本当成答案。
            yield _sse("discard", {"reason": "refused_after_stream"})
        # 正文与撤回都处理完，再把落库后的用户消息与助手消息交给前端收尾
        yield _sse("user_message", _message_read(user_message).model_dump(mode="json"))
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
