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


@pytest.mark.parametrize(
    "text",
    [
        "机器没有冒烟，只是主刷卡住",
        "目前并无焦味，想清洁外部传感器",
        "电池未见鼓包，设备只是无法回充",
    ],
)
def test_negated_high_risk_symptoms_are_not_false_positive(text):
    assert detect_safety_block(text) is None


def test_negated_smoke_does_not_hide_another_real_risk():
    result = detect_safety_block("机器没有冒烟，但是电池已经鼓包")
    assert result is not None
    assert result.category == "battery_damage"


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


# ---------------------------------------------------------------------------
# 输出侧检测 detect_unsafe_generated_answer（模型回答专用，与输入侧独立）
# 背景：2026-07-31 SYN-FA-006——输入侧规则套用在模型输出上，把
# "若电源线破损、发热或冒烟，须立即停用并联系售后"的安全警告误判为 unsafe_answer。

from app.safety import detect_unsafe_generated_answer  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "如果设备冒烟，请立即停止使用并联系售后。",
        "若电源线破损、发热或冒烟，须立即停用并联系售后。",
        "请勿拆机。",
        "不要短接充电触点。",
        "禁止绕过安全保护。",
        "电池鼓包时应停止充电并联系售后。",
        "请勿拆机，不要短接触点。",
        "切勿自行更换内部电池。",
        "不得拆卸电机进行维修。",
        "应避免关闭安全保护功能。",
        "出现焦味或异常发热时，请断开电源并联系官方售后。",
        "若边刷不转，请检查边刷是否缠绕毛发。",
        # SYN-FA-008 回归：警示词嵌在危险动作匹配区间内部也要识别
        "驱动轮电机不可自行拆解维修。",
        "主板故障不可自行维修，请联系售后。",
        # SYN-FA-015 回归：上盖/顶盖是用户可开启区域，"打开"兼有开机含义
        "打开基站上盖检查集尘袋。",
        "打开上盖取出尘盒即可。",
        "取下上盖更换集尘袋。",
        "打开机器人电源开关。",
    ],
)
def test_generated_safety_warnings_are_allowed(text):
    assert detect_unsafe_generated_answer(text) is None


@pytest.mark.parametrize(
    "marker", ["请勿", "不要", "禁止", "切勿", "不得", "避免", "严禁"]
)
def test_each_warning_marker_neutralizes_dangerous_action(marker):
    assert detect_unsafe_generated_answer(f"{marker}拆机。") is None
    assert detect_unsafe_generated_answer(f"{marker}短接充电触点。") is None


@pytest.mark.parametrize(
    ("category", "text"),
    [
        ("disassembly", "拆开外壳检查主板。"),
        ("disassembly", "建议拆机检查风机内部。"),
        ("disassembly", "不要犹豫，拆开外壳检查内部。"),  # 警示词不跨分句放行
        ("short_charging_contacts", "可以短接充电触点测试。"),
        ("bypass_protection", "关闭安全保护后继续运行。"),
        ("internal_component_repair", "自行更换内部电池。"),
        ("internal_component_repair", "拆卸电机进行维修。"),
        ("unsafe_reassurance", "冒烟属于正常现象，可以继续使用。"),
        ("unsafe_reassurance", "电池鼓包不影响使用。"),
        ("disassembly", "打开后盖检查内部线路。"),
        ("disassembly", "撬开机身外壳。"),
    ],
)
def test_affirmative_dangerous_instructions_are_blocked(category, text):
    result = detect_unsafe_generated_answer(text)
    assert result is not None
    assert result.category == category
    assert result.risk_level == "critical"


def test_input_side_detection_is_not_weakened():
    """输入侧对危险状态词保持一律拦截（用户描述险情必须走人工售后）。"""
    assert detect_safety_block("如果设备冒烟，请立即停止使用并联系售后。") is not None
    assert detect_safety_block("机器人充电时突然冒烟") is not None
    assert detect_safety_block("我想拆机检查里面的零件") is not None
