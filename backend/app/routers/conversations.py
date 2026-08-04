"""智能客服多轮会话：先选型号建会话 → 连续追问 → 每轮带引用页码/安全拦截/如实拒答。

复用 knowledge_answer 的完整链路（安全前置阻断、业务/embedding 限流、
generate_answer 的强制引用与四类拒答），在此之上叠加会话持久化与多轮上下文。
拒答也落 assistant 消息（用户可见的解释文案），保证会话历史完整可回放。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..config import get_settings
from ..generation_service import generate_answer
from ..models import (
    Conversation,
    ConversationMessage,
    KnowledgeDocument,
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
)
from ..security import get_current_user

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

MAX_CONTEXT_MESSAGES = 6
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
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ConversationRead]:
    rows = db.execute(
        select(Conversation, RobotModel.code)
        .join(RobotModel, RobotModel.id == Conversation.robot_model_id)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(50)
    ).all()
    return [
        ConversationRead(
            id=c.id,
            robot_model_id=c.robot_model_id,
            robot_model_code=code,
            title=c.title,
            updated_at=c.updated_at,
        )
        for c, code in rows
    ]


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


@router.post("/{conversation_id}/messages", response_model=ChatMessageResponse)
def post_message(
    conversation_id: int,
    payload: ChatMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatMessageResponse:
    conversation = _owned_conversation(db, conversation_id, user)

    safety_block = detect_safety_block(payload.content)
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
    enforce_embedding_rate_limit(db, request, user_id=user.id, settings=settings)

    robot_model = db.get(RobotModel, conversation.robot_model_id)
    threshold_version = db.scalar(
        select(KnowledgeDocument.sha256)
        .where(KnowledgeDocument.robot_model_id == conversation.robot_model_id)
        .order_by(KnowledgeDocument.id.desc())
        .limit(1)
    )
    min_score = settings.knowledge_score_threshold(robot_model.code, threshold_version)

    history = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages[-MAX_CONTEXT_MESSAGES:]
    ]
    user_message = ConversationMessage(
        conversation_id=conversation.id, role="user", content=payload.content
    )
    db.add(user_message)
    if not conversation.title:
        conversation.title = payload.content[:60]

    try:
        result = generate_answer(
            db,
            user_id=user.id,
            robot_model_id=conversation.robot_model_id,
            query=payload.content,
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
        raise HTTPException(status_code=503, detail="Generation service unavailable") from exc

    if result.status == "answered":
        assistant_message = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=result.answer,
            citations_json=[
                {
                    "index": c.index,
                    "source_url": c.source_url,
                    "page_number": c.page_number,
                    "score": c.score,
                    "document_sha256": c.document_sha256,
                }
                for c in result.citations
            ],
            generation_record_id=result.record_id,
        )
    else:
        assistant_message = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=REFUSAL_TEXTS.get(result.refusal_reason, REFUSAL_TEXTS["model_refused"]),
            refusal_reason=result.refusal_reason,
            generation_record_id=result.record_id,
        )
    db.add(assistant_message)
    conversation.updated_at = user_message.created_at
    db.commit()
    db.refresh(user_message)
    db.refresh(assistant_message)
    return ChatMessageResponse(
        user_message=_message_read(user_message),
        assistant_message=_message_read(assistant_message),
    )
