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
        re.compile(r"电池.{0,8}(鼓包|膨胀|鼓起|发鼓|破损|破裂|漏液)|(?:鼓包|膨胀|鼓起|发鼓|破损|破裂|漏液).{0,8}电池"),
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
        re.compile(
            r"(?:绕过|屏蔽|破解|取消|关闭|关掉).{0,10}(?:安全保护|保护机制|安全限制|保护功能)"
            r"|(?:安全保护|保护机制|安全限制|保护功能).{0,6}(?:关掉|关闭|关了|取消|屏蔽|绕过)"
            r"|强制绕过"
        ),
    ),
    SafetyRule(
        "unofficial_modification",
        "请求涉及非官方改装，可能改变原有电气和安全设计。",
        re.compile(
            r"非官方改装|自行改装|私自改装"
            r"|改装.{0,8}(?:电池|电路|主板|充电系统|电源)"
            r"|(?:外置|第三方)电源.{0,6}改装"
        ),
    ),
)

NEGATION_PREFIXES = ("没有", "并无", "未见", "不存在", "不是", "没", "无", "未", "不")


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


# ---------------------------------------------------------------------------
# 模型输出侧安全检测（与上面的用户输入侧 detect_safety_block 互相独立）。
#
# 输入侧规则把"冒烟/电池鼓包"等危险状态词一律拦截是正确的（用户在描述险情）；
# 但模型回答里出现同样的词，多数是合规的安全警告（"若冒烟请停用并联系售后"）。
# 2026-07-31 SYN-FA-006 即因输入侧规则被套用在输出上，把安全警告误判为
# unsafe_answer。输出侧只拦两类内容：
#   1. 肯定式危险操作指导（教用户拆机/短接/绕过保护/修内部件/改装）；
#      同一分句内、匹配位置之前出现"请勿/不要/禁止"等警示词的视为安全警告放行。
#   2. 对危险状态的错误安抚（提到冒烟/鼓包等状态却让用户继续使用/说是正常现象）。
# 除此之外仍是 fail-closed：警示词必须出现在同一分句内且在危险动作之前才放行，
# "不要犹豫，拆开外壳"这类跨分句组合不会被误放。

_OUTPUT_WARNING_MARKERS = (
    "请勿", "不要", "禁止", "严禁", "切勿", "切莫", "不得", "不可",
    "不应", "不能", "避免", "杜绝", "不建议", "不推荐", "不宜", "勿",
    # 否定描述词：合规声明（"不涉及拆机"）与免动手说明（"无需拆机即可清理"）
    # 不是操作指导。2026-07-31 FF-013——回答末尾"不涉及拆机或非授权维修动作"
    # 被判 disassembly，输出侧误伤第三例。仍限同一分句内，跨分句不放行。
    "不涉及", "不包含", "不含", "不属于", "无需", "无须",
)

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]")
_CLAUSE_SPLIT = re.compile(r"[，、：:,]")

# 肯定式危险操作。比输入侧同类规则更宽（如"拆开外壳"输入侧不拦），
# 因为输出侧的语境永远是"指导用户做什么"，覆盖必须更严。
OUTPUT_ACTION_RULES = (
    SafetyRule(
        "disassembly",
        "回答包含拆机或打开设备内部结构的操作指导。",
        # "上盖/顶盖"是用户可自行开启的区域（取尘盒/换集尘袋），不入拦截目标；
        # "打开"兼有开机含义（打开机器/电源），只与明确的内部结构词组合才拦。
        re.compile(
            r"(?:拆开|拆卸|卸下|撬开|拆下)[^，。]{0,6}(?:外壳|机身|主机|后盖|底盖|面板|内部)"
            r"|打开[^，。]{0,6}(?:外壳|后盖|底盖|内部)"
            r"|拆机|拆开机身|拆开主机"
        ),
    ),
    SafetyRule(
        "internal_component_repair",
        "回答包含主板、电路、电机或内部电池的维修指导。",
        re.compile(
            r"(?:维修|修理|更换|焊接|拆卸|检修)[^，。]{0,10}(?:主板|电路板|内部电路|电机|内部电池)"
            r"|检查[^，。]{0,6}(?:主板|电路板|内部电路|内部电池)"
            r"|(?:主板|电路板|内部电路|电机|内部电池)[^，。]{0,10}(?:维修|修理|更换|焊接|拆卸)"
            r"|自行更换[^，。]{0,6}电池"
        ),
    ),
    SafetyRule(
        "short_charging_contacts",
        "回答包含短接充电触点的操作指导。",
        re.compile(
            r"短接[^，。]{0,10}(?:充电触点|充电片|充电极片|触点)"
            r"|(?:充电触点|充电片|充电极片)[^，。]{0,10}短接"
        ),
    ),
    SafetyRule(
        "bypass_protection",
        "回答包含绕过或关闭安全保护的操作指导。",
        re.compile(
            r"(?:绕过|屏蔽|破解|取消|关闭|关掉)[^，。]{0,10}(?:安全保护|保护机制|安全限制|保护功能)"
            r"|(?:安全保护|保护机制|安全限制|保护功能)[^，。]{0,6}(?:关掉|关闭|关了|取消|屏蔽|绕过)"
            r"|强制绕过"
        ),
    ),
    SafetyRule(
        "unofficial_modification",
        "回答包含非官方改装指导。",
        re.compile(
            r"非官方改装|自行改装|私自改装"
            r"|改装[^，。]{0,8}(?:电池|电路|主板|充电系统|电源)"
            r"|(?:外置|第三方)电源[^，。]{0,6}改装"
        ),
    ),
)

# 危险状态词（输出中单独出现不拦，通常是安全警告的一部分）
_STATE_KEYWORDS = re.compile(
    r"冒烟|起烟|烟雾|焦味|烧焦|糊味|过热|烫手|异常发热|鼓包|膨胀|漏液|进水|破损"
)

# 对危险状态的错误安抚/继续使用建议
_UNSAFE_CONTINUATION = re.compile(
    r"继续(?:使用|充电|运行|工作)|(?:正常|常见)现象|无需(?:担心|处理|理会|在意)"
    r"|不用(?:担心|处理|理会|管)|放心使用|不影响使用"
)

_UNSAFE_REASSURANCE_REASON = "回答提到危险状态却建议继续使用或称其正常，属于危险安抚。"


def _clause_prefix(sentence: str, position: int) -> str:
    """返回 position 所在分句从分句起点到 position 的前缀。"""
    start = 0
    for match in _CLAUSE_SPLIT.finditer(sentence, 0, position):
        start = match.end()
    return sentence[start:position]


def _is_warned(sentence: str, start: int, end: int) -> bool:
    """警示词可出现在分句内匹配位置之前，也可嵌在匹配范围内部——
    如"电机不可自行拆解维修"，匹配区间为 电机…维修，"不可"在区间内。"""
    prefix = _clause_prefix(sentence, start)
    span = sentence[start:end]
    return any(
        marker in prefix or marker in span for marker in _OUTPUT_WARNING_MARKERS
    )


def detect_unsafe_generated_answer(text: str) -> SafetyBlock | None:
    """判断模型生成的回答是否包含危险内容（输出侧专用，勿用于用户输入）。"""
    normalized = re.sub(r"[ \t　]+", "", text).lower()
    for sentence in _SENTENCE_SPLIT.split(normalized):
        if not sentence:
            continue
        for rule in OUTPUT_ACTION_RULES:
            for match in rule.pattern.finditer(sentence):
                if not _is_warned(sentence, match.start(), match.end()):
                    return SafetyBlock(
                        category=rule.category,
                        risk_level="critical",
                        reason=rule.reason,
                    )
        if _STATE_KEYWORDS.search(sentence):
            for match in _UNSAFE_CONTINUATION.finditer(sentence):
                if not _is_warned(sentence, match.start(), match.end()):
                    return SafetyBlock(
                        category="unsafe_reassurance",
                        risk_level="critical",
                        reason=_UNSAFE_REASSURANCE_REASON,
                    )
    return None
