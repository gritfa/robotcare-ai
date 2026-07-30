"""告警 webhook：未配置静默、冷却防刷屏、失败不抛出、载荷不含敏感信息。"""

from __future__ import annotations

import json

from app import alerting
from app.config import Settings


class _Sent:
    def __init__(self):
        self.requests = []


def _patch_urlopen(monkeypatch, sink: _Sent, *, fail: bool = False):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        if fail:
            raise OSError("network down")
        sink.requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(alerting.urllib.request, "urlopen", fake_urlopen)


def test_disabled_without_url(monkeypatch):
    alerting.reset_cooldowns_for_tests()
    sink = _Sent()
    _patch_urlopen(monkeypatch, sink)
    settings = Settings(_env_file=None)
    assert alerting.send_alert("k1", "文本", settings=settings) is False
    assert sink.requests == []


def test_sends_wecom_payload_and_cooldown(monkeypatch):
    alerting.reset_cooldowns_for_tests()
    sink = _Sent()
    _patch_urlopen(monkeypatch, sink)
    settings = Settings(alert_webhook_url="https://example.com/hook", _env_file=None)
    assert alerting.send_alert("k1", "作业失败 3 个", settings=settings) is True
    # 冷却期内同事件键不重发
    assert alerting.send_alert("k1", "作业失败 4 个", settings=settings) is False
    # 不同事件键不受影响
    assert alerting.send_alert("k2", "终端离线", settings=settings) is True
    assert len(sink.requests) == 2
    body = sink.requests[0]
    assert body["msgtype"] == "text"
    assert "RobotCare" in body["text"]["content"]
    assert "@" not in body["text"]["content"]  # 不含邮箱等敏感信息


def test_failure_never_raises(monkeypatch):
    alerting.reset_cooldowns_for_tests()
    sink = _Sent()
    _patch_urlopen(monkeypatch, sink, fail=True)
    settings = Settings(alert_webhook_url="https://example.com/hook", _env_file=None)
    assert alerting.send_alert("k1", "任意", settings=settings) is False
