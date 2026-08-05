"""操作指令的**状态化**应答：同一句"生成报告"，在不同诊断状态下必须给不同答复。

2026-08-05 老板指出的产品缺口：普通用户不知道"聊天里不能直接生成报告"。
报告是分步诊断的产物——没做过诊断就没有可写进报告的排查记录。直接回一句
"做不了"是把系统的内部约束甩给用户；正确做法是按当前状态告诉他**下一步点哪里**。

四种状态各自的答复与按钮：
- 没有诊断     → 说明报告依赖排查记录 + [开始诊断]
- 诊断进行中   → 报出还差几步 + [继续诊断]
- 诊断已解决   → 问题已解决，不生成未解决报告（避免用户误以为要交材料）
- 诊断未解决   → 报告已就绪 + [查看报告] + [下载 PDF]

这些判断放在服务端而不是前端：状态源在数据库，前端算等于把一致性责任推给客户端，
两处实现早晚会漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Conversation, DiagnosticSession, User


@dataclass(frozen=True)
class ActionOutcome:
    reply: str
    quick_actions: list[dict[str, object]]


def _action(code: str, label: str, **params: object) -> dict[str, object]:
    return {"code": code, "label": label, **params}


def latest_diagnostic(
    db: Session, *, user: User, conversation: Conversation
) -> DiagnosticSession | None:
    """取本会话升级出的最新诊断；没有则回落到该用户同型号的最新诊断。

    回落是有意的：用户可能先在别处开了诊断，再回聊天问"报告好了吗"，
    此时按会话严格过滤会误报"你还没做过诊断"。
    """

    from_conversation = db.scalar(
        select(DiagnosticSession)
        .where(DiagnosticSession.source_conversation_id == conversation.id)
        .order_by(DiagnosticSession.id.desc())
        .limit(1)
    )
    if from_conversation is not None:
        return from_conversation
    return db.scalar(
        select(DiagnosticSession)
        .where(
            DiagnosticSession.user_id == user.id,
            DiagnosticSession.flow.has(robot_model_id=conversation.robot_model_id),
        )
        .order_by(DiagnosticSession.id.desc())
        .limit(1)
    )


def _remaining_steps(session: DiagnosticSession) -> int:
    total = len(session.flow.steps)
    done = session.current_position - 1 if session.current_position else 0
    return max(total - done, 0)


def resolve_report_request(
    db: Session, *, user: User, conversation: Conversation
) -> ActionOutcome:
    session = latest_diagnostic(db, user=user, conversation=conversation)

    if session is None:
        return ActionOutcome(
            reply=(
                "售后报告是分步排查的结果整理，需要先完成一次安全分步检查才能生成——"
                "报告里要写清楚你做过哪些排查、结果如何，直接开一份空报告对售后没有帮助。"
                "点下面的按钮开始，整个过程我会一步步引导你。"
            ),
            quick_actions=[_action("start_diagnostic", "开始诊断")],
        )

    if session.status == "in_progress":
        remaining = _remaining_steps(session)
        return ActionOutcome(
            reply=(
                f"你有一次排查还没做完，还剩 {remaining} 个步骤。"
                "做完之后如果问题仍未解决，我会自动把排查记录整理成售后报告。"
            ),
            quick_actions=[
                _action("resume_diagnostic", "继续诊断", diagnostic_id=session.id)
            ],
        )

    if session.status == "resolved":
        return ActionOutcome(
            reply=(
                "上一次排查的结论是问题已解决，所以没有生成未解决报告——"
                "售后报告是给尚未解决的问题用的。如果又出现了新情况，可以直接描述，"
                "或重新开始一次排查。"
            ),
            quick_actions=[_action("start_diagnostic", "重新开始诊断")],
        )

    # unresolved
    if session.report is not None:
        return ActionOutcome(
            reply=(
                f"报告已经准备好了（编号 {session.report.report_number}），"
                "里面包含这次排查的每一步结果和会话摘要，可以直接发给官方售后。"
            ),
            quick_actions=[
                _action("view_report", "查看报告", diagnostic_id=session.id),
                _action("download_report_pdf", "下载 PDF", diagnostic_id=session.id),
            ],
        )
    return ActionOutcome(
        reply=(
            "这次排查的结论是问题未解决，可以生成售后报告。点下面的按钮打开诊断页生成报告。"
        ),
        quick_actions=[_action("view_diagnostic", "打开诊断", diagnostic_id=session.id)],
    )


def resolve_diagnostic_request(
    db: Session, *, user: User, conversation: Conversation
) -> ActionOutcome:
    """"开始诊断"：已有进行中的排查就引导继续，而不是另起一个把上一次晾在那。"""

    session = latest_diagnostic(db, user=user, conversation=conversation)
    if session is not None and session.status == "in_progress":
        remaining = _remaining_steps(session)
        return ActionOutcome(
            reply=(
                f"你有一次排查正在进行中，还剩 {remaining} 个步骤，建议先做完这一次；"
                "如果想换个问题排查，也可以重新开始。"
            ),
            quick_actions=[
                _action("resume_diagnostic", "继续诊断", diagnostic_id=session.id),
                _action("start_diagnostic", "重新开始"),
            ],
        )
    return ActionOutcome(
        reply=(
            "好的，这就为你发起分步诊断。我会根据你的问题选择对应的排查流程，"
            "一步步引导你检查，全程不涉及拆机。"
        ),
        quick_actions=[_action("start_diagnostic", "开始诊断")],
    )


def answer_quick_actions(*, refused: bool) -> list[dict[str, object]]:
    """每条知识回答下方的下一步建议。

    拒答时把"联系官方售后"提到第一位——这时候用户最需要的是别的出口，
    而不是再问一遍同样答不上来的问题。
    """

    if refused:
        return [
            _action("contact_support", "联系官方售后"),
            _action("start_diagnostic", "开始分步诊断"),
        ]
    return [
        _action("start_diagnostic", "开始分步诊断"),
        _action("mark_resolved", "问题已解决"),
        _action("contact_support", "联系官方售后"),
    ]
