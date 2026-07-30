"""运营告警 webhook（企业微信/钉钉/generic），事件驱动 + 冷却防刷屏。

原则：
- 未配置 URL 时整体静默关闭；发送失败只记日志，绝不影响业务流程；
- 消息只含中文摘要与数量，绝不携带用户邮箱、问题原文、密钥；
- 冷却期为进程内存实现——单实例部署有效（与限流缓存同一约束，README 已声明）。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from time import monotonic

from .config import Settings, get_settings
from .observability import current_trace_id, emit_json_log

_cooldown_lock = threading.Lock()
_last_sent_at: dict[str, float] = {}


def _payload_for(format_name: str, text: str) -> dict:
    if format_name == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if format_name == "generic":
        return {"text": text}
    # 默认企业微信群机器人
    return {"msgtype": "text", "text": {"content": text}}


def send_alert(event_key: str, text: str, *, settings: Settings | None = None) -> bool:
    """发送一条告警。返回是否真正发出（未配置/冷却期/失败都返回 False）。"""
    resolved = settings or get_settings()
    url = (resolved.alert_webhook_url or "").strip()
    if not url:
        return False
    cooldown = max(60, resolved.alert_cooldown_seconds)
    now = monotonic()
    with _cooldown_lock:
        last = _last_sent_at.get(event_key)
        if last is not None and now - last < cooldown:
            return False
        _last_sent_at[event_key] = now
    body = json.dumps(
        _payload_for(resolved.alert_webhook_format, f"【RobotCare】{text}"),
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            ok = 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 —— 告警失败绝不影响业务
        emit_json_log(
            logging.WARNING,
            "alert_webhook_failed",
            trace_id=current_trace_id(),
            event_key=event_key,
            error_type=type(exc).__name__,
        )
        return False
    emit_json_log(
        logging.INFO,
        "alert_webhook_sent",
        trace_id=current_trace_id(),
        event_key=event_key,
        ok=ok,
    )
    return ok


def reset_cooldowns_for_tests() -> None:
    with _cooldown_lock:
        _last_sent_at.clear()
