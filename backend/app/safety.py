from __future__ import annotations

import re
from dataclasses import dataclass


OFFICIAL_SERVICE_ADVICE = (
    "请立即停止自助排查；如设备正在运行或充电，请在确保人身安全的前提下断开电源，"
    "远离可燃物，不要拆机、短接或继续充电，并联系海尔官方售后。"
)


@dataclass(frozen=True)
class SafetyBlock:
    category: str
    risk_level: str
    reason: str
    advice: str = OFFICIAL_SERVICE_ADVICE


@dataclass(frozen=True)
class SafetyRule:
    category: str
    reason: str
    pattern: re.Pattern[str]


RULES = (
    SafetyRule("smoke", "设备出现冒烟或烟雾，存在火灾和电气风险。", re.compile(r"冒烟|起烟|出现烟雾|往外冒烟")),
    SafetyRule("burning_smell", "设备出现焦味或烧焦气味，可能存在电气过热。", re.compile(r"焦味|烧焦气味|烧焦味|糊味")),
    SafetyRule("overheating", "设备出现异常发热，继续运行或充电可能扩大风险。", re.compile(r"异常发热|严重发热|过热|烫手")),
    SafetyRule(
        "battery_damage",
        "电池出现鼓包、破损或漏液，存在起火、腐蚀和人身伤害风险。",
        re.compile(r"电池.{0,8}(鼓包|膨胀|破损|破裂|漏液)|(?:鼓包|膨胀|破损|破裂|漏液).{0,8}电池"),
    ),
    SafetyRule(
        "internal_water_ingress",
        "机器人或基站内部进水，继续通电可能导致短路。",
        re.compile(r"(?:机器人|机器|主机|基站|充电座).{0,8}(?:内部|里面).{0,5}进水|进水.{0,8}(?:机器人|主机|基站|充电座).{0,5}(?:内部|里面)"),
    ),
    SafetyRule("disassembly", "请求涉及拆机或打开设备内部结构，超出安全自助范围。", re.compile(r"拆机|拆开机身|拆开主机|打开机身外壳|拆卸外壳")),
    SafetyRule(
        "internal_component_repair",
        "请求涉及主板、电路、电机或内部电池维修，必须由专业售后处理。",
        re.compile(r"(?:主板|电路板|内部电路|电机|内部电池).{0,12}(?:维修|修理|更换|焊接|拆卸|怎么修)|(?:维修|修理|更换|焊接|拆卸).{0,12}(?:主板|电路板|内部电路|电机|内部电池)"),
    ),
    SafetyRule(
        "short_charging_contacts",
        "请求涉及短接充电触点，可能导致短路、起火或设备损坏。",
        re.compile(r"(?:短接|短路连接|用线连接).{0,10}(?:充电触点|充电片|充电极片)|(?:充电触点|充电片|充电极片).{0,10}(?:短接|短路连接)"),
    ),
    SafetyRule(
        "bypass_protection",
        "请求涉及绕过安全保护，可能使设备在不安全状态下运行。",
        re.compile(r"(?:绕过|屏蔽|破解|取消).{0,10}(?:安全保护|保护机制|安全限制|保护功能)|强制绕过"),
    ),
    SafetyRule(
        "unofficial_modification",
        "请求涉及非官方改装，可能改变原有电气和安全设计。",
        re.compile(r"非官方改装|自行改装|私自改装|改装(?:电池|电路|主板|充电系统)"),
    ),
)

NEGATION_PREFIXES = ("没有", "并无", "未见", "不存在", "不是", "没", "无", "未")


def _match_is_negated(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 6) : match.start()]
    matched_text = match.group(0)
    return any(
        prefix.endswith(marker) or marker in matched_text
        for marker in NEGATION_PREFIXES
    )


def detect_safety_block(text: str) -> SafetyBlock | None:
    normalized = re.sub(r"\s+", "", text).lower()
    for rule in RULES:
        if any(
            not _match_is_negated(normalized, match)
            for match in rule.pattern.finditer(normalized)
        ):
            return SafetyBlock(
                category=rule.category,
                risk_level="critical",
                reason=rule.reason,
            )
    return None
