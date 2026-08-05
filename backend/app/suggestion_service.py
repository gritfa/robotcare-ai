"""按型号生成"试试这些问题"的入口建议。

背景（2026-08-05 产品体检）：建议问题此前是 5 条写死在 ChatView 里的文本，
不分型号，而且只在"已建会话且会话为空"时才出现——用户首次进聊天页只看到
一句"选择或新建一个会话"，冷启动毫无抓手。

三级数据源，按可靠性排序：

1. **真实高频且确实答得上来的问题**（GenerationRecord status='answered'）。
   这是唯一能证明"问了会有好结果"的数据——建议一个答不上来的问题
   等于把用户直接推进拒答。
2. **该型号已发布的诊断流程类别**。流程存在意味着该故障有审核过的处置路径，
   同样保证问了有结果。
3. **通用兜底**。新型号刚接入、既无问答记录也无流程时用。

隐私口径：来源 1 只取**出现两次以上**的问题，且长度受限。单次出现的提问可能
带个人信息（住址、手机号、订单号），把它展示给同型号的其他用户是数据泄漏；
出现两次以上意味着这是一句通用问法，不是某个人的具体情况。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import DiagnosticFlow, GenerationRecord, IssueCategory, RobotModel
from .rate_limit_service import utcnow


DEFAULT_LIMIT = 6
# 只回收足够短的提问：长提问往往夹带用户自己的具体情况
MAX_SUGGESTION_CHARS = 30
# 出现次数下限：单次出现的提问可能含个人信息，不外显
MIN_OCCURRENCES = 2
LOOKBACK_DAYS = 90

# 明显含个人信息的提问一律不外显，即便它被问过很多次
_PII_PATTERN = re.compile(
    r"(1[3-9]\d{9}|\d{6,}|@|订单|地址|收货|我叫|手机号|电话|身份证|快递)"
)


@dataclass(frozen=True)
class SuggestedQuestion:
    text: str
    source: str  # "history" | "flow" | "fallback"


FALLBACK_QUESTIONS = (
    "无法启动怎么办？",
    "为什么回不了充电座？",
    "如何重新配网？",
    "主刷卡住怎么清理？",
    "滤网多久清洗一次？",
)


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def _acceptable(text: str) -> bool:
    normalized = _normalize(text)
    if not normalized or len(normalized) > MAX_SUGGESTION_CHARS:
        return False
    return not _PII_PATTERN.search(normalized)


def _from_history(db: Session, robot_model_id: int, limit: int) -> list[SuggestedQuestion]:
    """真实问过、且当时成功作答的问题。"""
    since = utcnow() - timedelta(days=LOOKBACK_DAYS)
    rows = db.execute(
        select(GenerationRecord.query, func.count(GenerationRecord.id).label("hits"))
        .where(
            GenerationRecord.robot_model_id == robot_model_id,
            GenerationRecord.status == "answered",
            GenerationRecord.created_at >= since,
        )
        .group_by(GenerationRecord.query)
        .having(func.count(GenerationRecord.id) >= MIN_OCCURRENCES)
        .order_by(func.count(GenerationRecord.id).desc(), GenerationRecord.query)
        .limit(limit * 3)
    ).all()
    picked: list[SuggestedQuestion] = []
    for query, _hits in rows:
        if _acceptable(query):
            picked.append(SuggestedQuestion(text=_normalize(query), source="history"))
        if len(picked) >= limit:
            break
    return picked


def _from_flows(db: Session, robot_model_id: int, limit: int) -> list[SuggestedQuestion]:
    """已发布诊断流程的故障类别——有流程即有审核过的处置路径。"""
    names = db.scalars(
        select(IssueCategory.name)
        .join(DiagnosticFlow, DiagnosticFlow.issue_category_id == IssueCategory.id)
        .where(
            DiagnosticFlow.robot_model_id == robot_model_id,
            DiagnosticFlow.status == "published",
            DiagnosticFlow.active.is_(True),
        )
        .distinct()
        .order_by(IssueCategory.name)
        .limit(limit)
    ).all()
    return [SuggestedQuestion(text=f"{name}怎么办？", source="flow") for name in names]


def suggested_questions(
    db: Session, robot_model_id: int, limit: int = DEFAULT_LIMIT
) -> list[SuggestedQuestion]:
    """按型号给出建议问题，三级数据源依次补齐到 limit 条。"""
    if db.scalar(select(RobotModel.id).where(RobotModel.id == robot_model_id)) is None:
        return []

    picked = _from_history(db, robot_model_id, limit)
    seen = {item.text for item in picked}

    if len(picked) < limit:
        for item in _from_flows(db, robot_model_id, limit):
            if item.text not in seen:
                picked.append(item)
                seen.add(item.text)
            if len(picked) >= limit:
                break

    for text in FALLBACK_QUESTIONS:
        if len(picked) >= limit:
            break
        if text not in seen:
            picked.append(SuggestedQuestion(text=text, source="fallback"))
            seen.add(text)

    return picked[:limit]
