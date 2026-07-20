import pytest
from sqlalchemy import func, select

from app.models import DiagnosticSession, SafetyBlockEvent
from app.safety import detect_safety_block
from conftest import auth, register


HIGH_RISK_CASES = [
    ("smoke", "机器人充电时突然冒烟"),
    ("burning_smell", "基站传出烧焦气味"),
    ("overheating", "机器人底部异常发热而且烫手"),
    ("battery_damage", "电池已经鼓包了还能继续用吗"),
    ("battery_damage", "看到电池外壳破损并且漏液"),
    ("internal_water_ingress", "清洁时发现基站内部进水"),
    ("disassembly", "我想拆机检查里面的零件"),
    ("internal_component_repair", "如何自己维修主板"),
    ("internal_component_repair", "想更换内部电池"),
    ("short_charging_contacts", "能不能用导线短接充电触点"),
    ("bypass_protection", "怎样绕过安全保护继续运行"),
    ("unofficial_modification", "我准备自行改装充电电路"),
]


@pytest.mark.parametrize(("category", "text"), HIGH_RISK_CASES)
def test_every_high_risk_category_is_blocked(category, text):
    result = detect_safety_block(text)
    assert result is not None
    assert result.category == category
    assert result.risk_level == "critical"
    assert "售后" in result.advice


@pytest.mark.parametrize(
    "text",
    [
        "请问怎么擦拭外部充电触点",
        "清理主刷上缠绕的头发",
        "机器正常运行时外壳有一点温热",
        "重新配网后还是找不到设备",
    ],
)
def test_safe_external_operations_are_not_blocked(text):
    assert detect_safety_block(text) is None


def test_api_blocks_before_creating_diagnostic_and_records_minimal_event(client):
    token = register(client, "safety-block@example.com")["access_token"]
    model_id = next(item["id"] for item in client.get("/api/v1/models").json() if item["code"] == "JH69U1")
    device = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "安全测试设备"},
    )
    description = "机器人充电时冒烟，我想拆机看看主板"
    response = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device.json()["id"],
            "issue_category_code": "return_to_dock_failure",
            "issue_description": description,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "SAFETY_BLOCKED"
    assert detail["blocked"] is True
    assert detail["risk_level"] == "critical"
    assert detail["category"] == "smoke"
    assert "售后" in detail["official_service_advice"]

    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DiagnosticSession)) == 0
        event = db.scalar(select(SafetyBlockEvent))
        assert event is not None
        assert event.category == "smoke"
        assert event.description_sha256 != description
        assert len(event.description_sha256) == 64
