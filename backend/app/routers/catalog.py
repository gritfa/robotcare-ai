"""Catalog routes: /models and /models/{model_id}/diagnostic-options."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..models import DiagnosticFlow, RobotModel, User
from ..schemas import DiagnosticOptionRead, ModelRead, SuggestedQuestionRead
from ..security import get_current_user
from ..suggestion_service import suggested_questions

router = APIRouter(prefix="/api/v1")


@router.get("/models", response_model=list[ModelRead])
def list_models(db: Session = Depends(get_db)) -> list[RobotModel]:
    return list(db.scalars(select(RobotModel).where(RobotModel.active.is_(True)).order_by(RobotModel.code)))


@router.get("/models/{model_id}/suggested-questions", response_model=list[SuggestedQuestionRead])
def list_suggested_questions(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SuggestedQuestionRead]:
    """冷启动抓手：给出该型号"问了会有结果"的问题（见 suggestion_service 口径）。"""
    del user
    model_exists = db.scalar(
        select(RobotModel.id).where(RobotModel.id == model_id, RobotModel.active.is_(True))
    )
    if model_exists is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    return [
        SuggestedQuestionRead(text=item.text, source=item.source)
        for item in suggested_questions(db, model_id)
    ]


@router.get("/models/{model_id}/diagnostic-options", response_model=list[DiagnosticOptionRead])
def list_diagnostic_options(
    model_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DiagnosticOptionRead]:
    del user
    model_exists = db.scalar(
        select(RobotModel.id).where(RobotModel.id == model_id, RobotModel.active.is_(True))
    )
    if model_exists is None:
        raise HTTPException(status_code=404, detail="Robot model not found")
    flows = list(
        db.scalars(
            select(DiagnosticFlow)
            .options(selectinload(DiagnosticFlow.issue_category))
            .where(
                DiagnosticFlow.robot_model_id == model_id,
                DiagnosticFlow.status == "published",
                DiagnosticFlow.active.is_(True),
            )
            .order_by(DiagnosticFlow.title)
        )
    )
    return [
        DiagnosticOptionRead(
            stable_key=flow.stable_key,
            version=flow.version,
            issue_category_code=flow.issue_category.code,
            issue_category_name=flow.issue_category.name,
            title=flow.title,
        )
        for flow in flows
    ]
