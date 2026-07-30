from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from .config import Settings, get_settings
from .database import build_session_factory
from .migration_guard import ensure_database_at_head
from .models import (
    Attachment,
    AuditLog,
    AuthSession,
    DiagnosticFlow,
    DiagnosticSession,
    RefreshToken,
    RobotModel,
    SafetyBlockEvent,
    ServiceReport,
    StepExecution,
    User,
    UserDevice,
)
from .pdf_report import ensure_report_pdf, report_pdf_path
from .security import hash_password


DEMO_PASSWORD_ENV = "ROBOTCARE_DEMO_PASSWORD"
DEFAULT_DEMO_PASSWORD = "Demo123456"
DEMO_EMAILS = (
    "demo.alice@example.com",
    "demo.bob@example.com",
    "demo.chen@example.com",
    "demo.li@example.com",
    "demo.wang@example.com",
    "demo.disabled@example.com",
    "demo.admin@example.com",
)

PROFILE_SPECS = (
    ("demo.alice@example.com", "user", "active"),
    ("demo.bob@example.com", "user", "active"),
    ("demo.chen@example.com", "user", "active"),
    ("demo.li@example.com", "user", "active"),
    ("demo.wang@example.com", "user", "active"),
    ("demo.disabled@example.com", "user", "disabled"),
    ("demo.admin@example.com", "admin", "active"),
)

ISSUE_DESCRIPTIONS = {
    "jh69u1-return-to-dock": (
        "清扫结束后机器在充电座前反复转向，连续两次没有自动回充。",
        "E12",
    ),
    "jh69u1-mop-washing": (
        "基站能够供电，但拖布清洗时没有正常进水，污水箱也是空的。",
        None,
    ),
    "vc35u1-wifi-setup": (
        "更换路由器后 App 一直提示配网超时，手机连接的是家庭 2.4G Wi-Fi。",
        "NET-01",
    ),
    "vc35u1-cleaning-noise": (
        "清扫地毯边缘时底部出现间歇性异响，移到瓷砖后声音仍然存在。",
        None,
    ),
}


class DemoDataError(RuntimeError):
    pass


def _assert_safe_environment(settings: Settings) -> None:
    if settings.environment.strip().lower() == "production":
        raise DemoDataError("Demo data is forbidden when ROBOTCARE_ENVIRONMENT=production")


def _demo_image_bytes(label: str, accent: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (960, 640), (245, 247, 250))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((120, 100, 840, 540), radius=90, fill=(225, 230, 236), outline=accent, width=12)
    draw.ellipse((270, 145, 690, 565), fill=(247, 248, 250), outline=accent, width=10)
    draw.rectangle((425, 125, 535, 175), fill=accent)
    draw.text((40, 40), f"RobotCare synthetic evidence - {label}", fill=(28, 39, 51))
    draw.text((40, 590), "No real customer or device is shown", fill=(88, 98, 110))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _category_decision(flow: DiagnosticFlow) -> dict[str, object]:
    code = flow.issue_category.code
    return {
        "selected_category_code": code,
        "candidate_category_codes": [code],
        "final_category_code": code,
        "confirmation_method": "deterministic_rule",
        "seed_source": "synthetic_demo_data",
    }


def _report_content(diagnostic: DiagnosticSession) -> str:
    lines = [
        "RobotCare AI 第三方售后诊断报告",
        f"设备型号：{diagnostic.device.robot_model.code}",
        f"用户问题：{diagnostic.issue_description}",
        f"错误码：{diagnostic.error_code or '未提供'}",
        f"最终类别：{diagnostic.flow.issue_category.code}",
        "已执行的安全排查步骤：",
    ]
    for index, execution in enumerate(diagnostic.executions, start=1):
        result = "已解决" if execution.outcome == "resolved" else "未解决"
        lines.append(f"{index}. {execution.step.title} - {result}")
        lines.append(f"   操作：{execution.step.instruction}")
        lines.append(f"   来源：{execution.step.source_label}（第 {execution.step.source_page} 页）")
    lines.append("附件文件：")
    lines.extend(
        (f"- {item.original_filename}" for item in diagnostic.attachments),
    )
    if not diagnostic.attachments:
        lines.append("- 未提供")
    lines.extend(
        [
            "最终结果：自助排查未解决，建议联系海尔官方售后。",
            "免责声明：本报告使用完全合成的演示数据，由独立第三方工具生成，不代表海尔官方诊断结论。",
        ]
    )
    return "\n".join(lines)


def reset_demo_data(
    db: Session,
    *,
    attachment_dir: Path,
    report_dir: Path,
    report_secret: str,
) -> dict[str, int]:
    users = list(db.scalars(select(User).where(User.email.in_(DEMO_EMAILS))))
    user_ids = [user.id for user in users]
    if not user_ids:
        return {"users": 0, "diagnostics": 0, "attachments": 0, "reports": 0}

    device_ids = list(
        db.scalars(select(UserDevice.id).where(UserDevice.user_id.in_(user_ids)))
    )
    diagnostic_ids = list(
        db.scalars(select(DiagnosticSession.id).where(DiagnosticSession.user_id.in_(user_ids)))
    )
    attachments = (
        list(db.scalars(select(Attachment).where(Attachment.session_id.in_(diagnostic_ids))))
        if diagnostic_ids
        else []
    )
    reports = (
        list(db.scalars(select(ServiceReport).where(ServiceReport.session_id.in_(diagnostic_ids))))
        if diagnostic_ids
        else []
    )
    auth_session_ids = list(
        db.scalars(select(AuthSession.id).where(AuthSession.user_id.in_(user_ids)))
    )

    db.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids)))
    db.execute(delete(SafetyBlockEvent).where(SafetyBlockEvent.user_id.in_(user_ids)))
    if diagnostic_ids:
        db.execute(delete(Attachment).where(Attachment.session_id.in_(diagnostic_ids)))
        db.execute(delete(ServiceReport).where(ServiceReport.session_id.in_(diagnostic_ids)))
        db.execute(delete(StepExecution).where(StepExecution.session_id.in_(diagnostic_ids)))
        db.execute(delete(DiagnosticSession).where(DiagnosticSession.id.in_(diagnostic_ids)))
    if device_ids:
        db.execute(delete(UserDevice).where(UserDevice.id.in_(device_ids)))
    if auth_session_ids:
        db.execute(delete(RefreshToken).where(RefreshToken.session_id.in_(auth_session_ids)))
        db.execute(delete(AuthSession).where(AuthSession.id.in_(auth_session_ids)))
    db.execute(delete(User).where(User.id.in_(user_ids)))
    db.commit()

    for attachment in attachments:
        (attachment_dir / attachment.stored_filename).unlink(missing_ok=True)
    for report in reports:
        report_pdf_path(report_dir, report, report_secret).unlink(missing_ok=True)

    return {
        "users": len(users),
        "diagnostics": len(diagnostic_ids),
        "attachments": len(attachments),
        "reports": len(reports),
    }


def load_demo_data(
    db: Session,
    *,
    attachment_dir: Path,
    report_dir: Path,
    report_secret: str,
    password: str = DEFAULT_DEMO_PASSWORD,
    replace: bool = False,
) -> dict[str, object]:
    if not 8 <= len(password) <= 128:
        raise DemoDataError("Demo password must contain 8 to 128 characters")
    existing = db.scalar(select(func.count()).select_from(User).where(User.email.in_(DEMO_EMAILS))) or 0
    if existing and not replace:
        raise DemoDataError("Demo users already exist; use load --replace for a deterministic refresh")
    if existing:
        reset_demo_data(
            db,
            attachment_dir=attachment_dir,
            report_dir=report_dir,
            report_secret=report_secret,
        )

    flows = list(
        db.scalars(
            select(DiagnosticFlow)
            .where(DiagnosticFlow.status == "published", DiagnosticFlow.active.is_(True))
            .options(
                selectinload(DiagnosticFlow.steps),
                selectinload(DiagnosticFlow.issue_category),
            )
            .order_by(DiagnosticFlow.stable_key)
        )
    )
    flow_by_key = {flow.stable_key: flow for flow in flows}
    missing = set(ISSUE_DESCRIPTIONS) - set(flow_by_key)
    if missing:
        raise DemoDataError(f"Published diagnostic flows are missing: {sorted(missing)}")

    models = {item.code: item for item in db.scalars(select(RobotModel))}
    if {"JH69U1", "VC35U1"} - set(models):
        raise DemoDataError("JH69U1 and VC35U1 must be seeded before demo data")

    password_hash = hash_password(password)
    now = datetime.now(timezone.utc)
    users: dict[str, User] = {}
    for index, (email, role, status) in enumerate(PROFILE_SPECS):
        user = User(
            email=email,
            password_hash=password_hash,
            role=role,
            status=status,
            created_at=now - timedelta(days=45 - index * 4),
        )
        db.add(user)
        users[email] = user
    db.flush()

    active_user_emails = [item[0] for item in PROFILE_SPECS if item[1] == "user" and item[2] == "active"]
    devices: dict[tuple[str, str], UserDevice] = {}
    for user_index, email in enumerate(active_user_emails):
        for model_index, model_code in enumerate(("JH69U1", "VC35U1")):
            device = UserDevice(
                user_id=users[email].id,
                robot_model_id=models[model_code].id,
                nickname=f"{model_code}-{['客厅', '卧室'][model_index]}演示机",
                serial_number=f"SYN-{model_code}-{user_index + 1:03d}",
                created_at=now - timedelta(days=35 - user_index * 3 - model_index),
            )
            db.add(device)
            devices[(email, model_code)] = device
    db.flush()

    scenario_statuses = ("in_progress", "resolved", "unresolved")
    reports: list[ServiceReport] = []
    attachment_count = 0
    diagnostics: list[DiagnosticSession] = []
    # 演示场景只覆盖 ISSUE_DESCRIPTIONS 显式声明的流程；目录里新增的其他流程
    # （如 D1 合成型号 rc-*）不自动进入演示数据，保证演示规模与断言稳定。
    flow_items = [flow_by_key[key] for key in sorted(ISSUE_DESCRIPTIONS)]
    for flow_index, flow in enumerate(flow_items):
        model_code = flow.stable_key.split("-", 1)[0].upper()
        for status_index, diagnostic_status in enumerate(scenario_statuses):
            email = active_user_emails[(flow_index + status_index) % len(active_user_emails)]
            description, error_code = ISSUE_DESCRIPTIONS[flow.stable_key]
            created_at = now - timedelta(days=28 - flow_index * 5 - status_index * 2)
            diagnostic = DiagnosticSession(
                user_id=users[email].id,
                device_id=devices[(email, model_code)].id,
                flow_id=flow.id,
                issue_description=description,
                error_code=error_code,
                category_decision=_category_decision(flow),
                status=diagnostic_status,
                current_position=1,
                resolved=None,
                created_at=created_at,
                updated_at=created_at + timedelta(minutes=12),
            )
            db.add(diagnostic)
            db.flush()

            ordered_steps = sorted(flow.steps, key=lambda item: item.position)
            if diagnostic_status == "in_progress":
                completed_steps = ordered_steps[: status_index]
                diagnostic.current_position = len(completed_steps) + 1
            elif diagnostic_status == "resolved":
                completed_steps = ordered_steps[:2]
                diagnostic.current_position = None
                diagnostic.resolved = True
            else:
                completed_steps = ordered_steps
                diagnostic.current_position = None
                diagnostic.resolved = False

            for step_index, step in enumerate(completed_steps):
                outcome = "resolved" if diagnostic_status == "resolved" and step_index == len(completed_steps) - 1 else "not_resolved"
                db.add(
                    StepExecution(
                        session_id=diagnostic.id,
                        step_id=step.id,
                        outcome=outcome,
                        created_at=created_at + timedelta(minutes=4 * (step_index + 1)),
                    )
                )

            if diagnostic_status in {"in_progress", "unresolved"} and (flow_index + status_index) % 2 == 0:
                image_bytes = _demo_image_bytes(
                    f"{model_code}-{diagnostic.id}",
                    (20 + flow_index * 35, 125, 110 + status_index * 30),
                )
                stored_filename = f"demo-diagnostic-{diagnostic.id}.png"
                attachment_dir.mkdir(parents=True, exist_ok=True)
                (attachment_dir / stored_filename).write_bytes(image_bytes)
                db.add(
                    Attachment(
                        session_id=diagnostic.id,
                        original_filename=f"{model_code}_故障现场_合成图片.png",
                        stored_filename=stored_filename,
                        content_type="image/png",
                        size_bytes=len(image_bytes),
                        created_at=created_at + timedelta(minutes=2),
                    )
                )
                attachment_count += 1

            diagnostics.append(diagnostic)
            db.flush()
            if diagnostic_status == "unresolved":
                db.refresh(diagnostic)
                report = ServiceReport(
                    session_id=diagnostic.id,
                    report_number=f"RC-DEMO-{diagnostic.id:06d}",
                    content=_report_content(diagnostic),
                    created_at=created_at + timedelta(minutes=20),
                )
                db.add(report)
                reports.append(report)

    safety_specs = (
        ("smoke_or_burning", "机器运行时出现焦味并伴随少量烟雾", "立即断电、远离可燃物并联系官方售后"),
        ("battery_damage", "电池仓外壳疑似鼓包", "停止充电和使用，不要拆机，联系官方售后"),
        ("liquid_ingress", "清洁液进入机身内部", "立即断电，不要再次开机或充电"),
        ("internal_repair", "用户询问如何短接充电触点", "禁止短接或绕过保护，联系专业售后"),
    )
    first_device = next(iter(devices.values()))
    for index, (category, reason, advice) in enumerate(safety_specs):
        db.add(
            SafetyBlockEvent(
                user_id=users[active_user_emails[index]].id,
                device_id=list(devices.values())[index].id,
                category=category,
                risk_level="critical",
                reason=reason,
                advice=advice,
                description_sha256=hashlib.sha256(reason.encode("utf-8")).hexdigest(),
                created_at=now - timedelta(days=index + 1),
            )
        )

    admin = users["demo.admin@example.com"]
    for index, action in enumerate(
        ("robot_model.active_set", "safety_block.detail_read", "diagnostic.detail_read", "service_report.detail_read")
    ):
        db.add(
            AuditLog(
                actor_user_id=admin.id,
                action=action,
                resource_type=("robot_model", "safety_block", "diagnostic", "service_report")[index],
                resource_id=str(first_device.id if index == 0 else diagnostics[index].id),
                details_json={"source": "synthetic_demo_data", "trace_id": f"demo-trace-{index + 1:02d}"},
                created_at=now - timedelta(hours=index + 1),
            )
        )

    db.commit()
    for report in reports:
        db.refresh(report)
        ensure_report_pdf(report_dir, report, report_secret)

    return {
        "synthetic": True,
        "users": len(users),
        "active_users": 6,
        "disabled_users": 1,
        "devices": len(devices),
        "diagnostics": len(diagnostics),
        "diagnostic_statuses": {status: 4 for status in scenario_statuses},
        "attachments": attachment_count,
        "reports": len(reports),
        "safety_blocks": len(safety_specs),
        "audit_logs": 4,
        "login": {
            "regular_user": active_user_emails[0],
            "admin_user": "demo.admin@example.com",
            "password_source": DEMO_PASSWORD_ENV,
        },
    }


def demo_summary(db: Session) -> dict[str, object]:
    user_ids = list(db.scalars(select(User.id).where(User.email.in_(DEMO_EMAILS))))
    if not user_ids:
        return {"synthetic": True, "users": 0, "diagnostics": 0}
    status_rows = db.execute(
        select(DiagnosticSession.status, func.count())
        .where(DiagnosticSession.user_id.in_(user_ids))
        .group_by(DiagnosticSession.status)
    ).all()
    diagnostic_ids = list(
        db.scalars(select(DiagnosticSession.id).where(DiagnosticSession.user_id.in_(user_ids)))
    )
    return {
        "synthetic": True,
        "users": len(user_ids),
        "devices": db.scalar(select(func.count()).select_from(UserDevice).where(UserDevice.user_id.in_(user_ids))) or 0,
        "diagnostics": sum(count for _, count in status_rows),
        "diagnostic_statuses": dict(status_rows),
        "attachments": (
            db.scalar(select(func.count()).select_from(Attachment).where(Attachment.session_id.in_(diagnostic_ids))) or 0
        ),
        "reports": (
            db.scalar(select(func.count()).select_from(ServiceReport).where(ServiceReport.session_id.in_(diagnostic_ids))) or 0
        ),
        "safety_blocks": db.scalar(
            select(func.count()).select_from(SafetyBlockEvent).where(SafetyBlockEvent.user_id.in_(user_ids))
        ) or 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage privacy-safe synthetic RobotCare demo data")
    subparsers = parser.add_subparsers(dest="command", required=True)
    load_parser = subparsers.add_parser("load", help="Load the deterministic synthetic dataset")
    load_parser.add_argument("--replace", action="store_true", help="Replace only previously managed demo data")
    load_parser.add_argument("--yes", action="store_true", help="Confirm the local demo-data mutation")
    reset_parser = subparsers.add_parser("reset", help="Remove only managed demo data and files")
    reset_parser.add_argument("--yes", action="store_true", help="Confirm the local demo-data deletion")
    subparsers.add_parser("summary", help="Print synthetic dataset counts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    _assert_safe_environment(settings)
    if args.command in {"load", "reset"} and not args.yes:
        raise SystemExit("Refusing mutation without --yes")

    session_factory = build_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    attachment_dir = Path(settings.attachment_dir).resolve()
    report_dir = Path(settings.report_dir).resolve()
    try:
        ensure_database_at_head(engine)
        with session_factory() as db:
            if args.command == "load":
                result = load_demo_data(
                    db,
                    attachment_dir=attachment_dir,
                    report_dir=report_dir,
                    report_secret=settings.jwt_secret,
                    password=os.getenv(DEMO_PASSWORD_ENV, DEFAULT_DEMO_PASSWORD),
                    replace=args.replace,
                )
            elif args.command == "reset":
                result = reset_demo_data(
                    db,
                    attachment_dir=attachment_dir,
                    report_dir=report_dir,
                    report_secret=settings.jwt_secret,
                )
            else:
                result = demo_summary(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
