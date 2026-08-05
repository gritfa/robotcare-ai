"""聊天消息路由层：把一条用户消息分到闲聊/产品操作/上下文追问/知识问题四类。

2026-08-05 立项动机：此前会话端点对**任何**消息都走同一条 RAG 链路
（embedding 限流 → 向量检索 → 生成 → 强制引用校验）。后果是"你好"这种闲聊
也要烧一次 embedding 配额和一次生成调用，最后因为检索不到东西而拒答，
用户看到的是"资料中没有找到能回答这个问题的内容"——把打招呼当成了知识缺口。
"生成报告""开始诊断""上传图片"这类产品操作同理，被当成知识问题去检索说明书。

这一层的定位是**在 RAG 之前分流**，不是给生成提示词继续打补丁：
提示词只能约束"检索到片段之后怎么答"，管不了"这条消息该不该进检索"。

设计约束（改这里前先读）：
1. **安全前置阻断永远在路由之前**（见 routers/conversations.py::_prepare_turn）。
   路由不得成为绕过安全检测的旁路——先判危险，再谈分流。
2. 规则式、确定性、可解释：每个判定都回填 matched_rule，落进消息留痕和日志，
   便于事后复盘"这条为什么没进检索"。不引入 LLM 分类器——分类本身再调一次模型，
   既加延迟又把"闲聊不烧模型"的收益还回去了。
3. 先剥招呼语再判其余类别："你好，帮我生成报告"必须落 action 而不是 smalltalk，
   "你好，扫地机不吸尘怎么办"必须落 knowledge。判 smalltalk 的充要条件是
   **剥掉招呼语后什么实质内容都不剩**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MessageIntent = Literal["smalltalk", "capability", "action", "followup", "knowledge"]
ActionCode = Literal["start_diagnostic", "generate_report", "upload_image"]


@dataclass(frozen=True)
class RoutingDecision:
    """一条消息的路由结论。

    needs_retrieval 是给调用方的唯一开关：为 False 时不得触碰 embedding 配额、
    向量检索和生成模型（这正是本层要省掉的三件事）。
    """

    intent: MessageIntent
    matched_rule: str
    reply: str | None = None
    action_code: ActionCode | None = None

    @property
    def needs_retrieval(self) -> bool:
        return self.intent in ("knowledge", "followup")

    @property
    def uses_model_placeholder(self) -> bool:
        """回复文案里是否含 {model_code} 占位符，需要调用方按当前型号填充。"""

        return bool(self.reply and "{model_code}" in self.reply)

    def metadata(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "matched_rule": self.matched_rule,
            "action_code": self.action_code,
        }


# 招呼/客套/道别：只在"整条消息除此之外没有别的内容"时才算闲聊，
# 所以这里剥的是前后缀，剥完看还剩什么。
_GREETING_PATTERN = re.compile(
    r"(你好呀?|您好|哈喽|哈啰|嗨|hi|hello|hey|早上好|下午好|晚上好|早安|晚安|"
    r"在吗|在么|有人吗|有人在吗|请问一下|打扰了|"
    r"谢谢|多谢|感谢|辛苦了|麻烦你了|好的|收到|明白|知道了|ok|okay|嗯|"
    r"再见|拜拜|bye|回头见|不客气|没事了)",
    re.IGNORECASE,
)
# 剥招呼语之后允许残留的噪声：标点、语气词、空白、表情
_FILLER_PATTERN = re.compile(
    r"[\s，,。．.！!？?~～、；;：:（）()\[\]【】\-—…'\"“”'']|[\U0001F300-\U0001FAFF]|"
    r"^(啊|呀|哦|噢|额|呃|哈+|嘿)+$"
)

_ACTION_RULES: tuple[tuple[str, ActionCode, re.Pattern[str]], ...] = (
    (
        "action.start_diagnostic",
        "start_diagnostic",
        re.compile(
            r"^(我要|我想|帮我|请|麻烦|想)?\s*"
            r"(开始|发起|进行|进入|做|来|走)?\s*(一次|一下|个)?\s*"
            r"(智能)?(诊断|排查)(一下|流程|吧|一次)?\s*$"
        ),
    ),
    (
        "action.generate_report",
        "generate_report",
        re.compile(
            r"^(我要|我想|帮我|请|麻烦|想)?\s*"
            r"(生成|导出|下载|出|开|发)\s*(一份|个|张)?\s*"
            r"(售后|维修|诊断)?\s*报告\s*(吧|给我|下来)?\s*$"
        ),
    ),
    (
        "action.upload_image",
        "upload_image",
        re.compile(
            r"^(我要|我想|帮我|请|麻烦|想|可以)?\s*"
            r"(上传|发|传|加|添加|贴)\s*(一张|几张|一个|张|个|些)?\s*"
            r"(图片|照片|图|截图|视频|附件)\s*(给你|上去|吗|么)?\s*$"
        ),
    ),
)

# 追问：指代词/省略句，本身信息不完整，必须靠上文才有意义。
# 只在已有历史消息时才成立——首轮就说"那第二步呢"没有可指代的上文。
_FOLLOWUP_PATTERN = re.compile(
    r"^(那|这|那么|然后|接下来|后来|再|还)?\s*"
    r"(它|他|这个|那个|这些|那些|上面|刚才|刚刚|前面|上一步|这一步)?\s*"
    r"(呢|吗|么|怎么办|怎么弄|为什么|为啥|还有呢|还有吗|继续|说下去|详细说说|展开讲讲|"
    r"第[一二三四五六七八九十\d]+步(呢|是什么|怎么做)?|下一步(呢|是什么|怎么做)?)\s*[？?。.！!]*$"
)
_FOLLOWUP_MAX_CHARS = 14

# 能力介绍（"你能做什么""你是谁"）：此前会去检索说明书，返回一大段产品功能列表，
# 读起来像搜索结果而不像客服自我介绍。这类问题问的是**助手的能力边界**，
# 说明书里本来就没有答案，检索多少次都答不对。
_CAPABILITY_PATTERN = re.compile(
    r"^(请问)?(你|您|这个?(机器人|助手|客服|ai)|你们)?\s*"
    r"(是谁|叫什么|能做什么|会做什么|能干什么|可以做什么|有什么功能|能帮我?做什么|"
    r"怎么用|如何使用|使用说明|帮助|help|能干嘛|能干啥|可以干嘛|有啥用|干什么用的)"
    r"[\s？?。.！!]*$"
)

CAPABILITY_REPLY = (
    "我可以帮你：\n"
    "1. 查询 {model_code} 的使用方法\n"
    "2. 排查无法启动、回充、配网等常见问题\n"
    "3. 引导你完成安全的分步检查\n"
    "4. 问题未解决时整理售后报告\n\n"
    "我的回答都依据官方说明书并标注页码；说明书里没有的内容我会直说，不会编。\n"
    "我不提供拆机或内部维修指导——涉及这类情况我会建议你联系官方售后。\n\n"
    "你现在遇到了什么问题？"
)

SMALLTALK_REPLY = (
    "你好，我是这台设备的售后知识助手。"
    "你可以直接描述遇到的问题（比如「拖布不转」「充不上电」），"
    "我会依据官方说明书回答并标注来源页码；"
    "也可以让我「开始诊断」走分步排查，或在排查完成后「生成报告」。"
)

_ACTION_REPLIES: dict[ActionCode, str] = {
    "start_diagnostic": "好的，这就为你发起分步诊断。点击下方按钮进入诊断流程，我会一步步引导你排查。",
    "generate_report": "好的，可以生成售后报告。点击下方按钮进入报告页，报告会包含本次会话摘要与诊断结论。",
    "upload_image": "好的，可以上传图片辅助判断。点击下方按钮选择照片（支持故障现象、错误码屏显等）。",
}


def _normalize(content: str) -> str:
    return content.strip().lower()


def _strip_greetings(text: str) -> str:
    """剥掉招呼/客套/道别，返回剩下的实质内容（可能为空串）。"""

    stripped = _GREETING_PATTERN.sub(" ", text)
    return _FILLER_PATTERN.sub("", stripped).strip()


def classify_message(content: str, *, has_history: bool = False) -> RoutingDecision:
    """把一条用户消息路由到四类之一。

    判定顺序固定：剥招呼语 → 空即闲聊 → 产品操作 → 上下文追问 → 兜底知识问题。
    兜底必须是 knowledge：拿不准时宁可多走一次检索，也不能把真实的求助
    误判成闲聊直接打发掉（漏检索只是浪费一次调用，误判闲聊是答非所问）。
    """

    normalized = _normalize(content)
    core = _strip_greetings(normalized)

    if not core:
        return RoutingDecision(
            intent="smalltalk", matched_rule="smalltalk.greeting_only", reply=SMALLTALK_REPLY
        )

    if _CAPABILITY_PATTERN.match(core):
        return RoutingDecision(
            intent="capability", matched_rule="capability.self_introduction", reply=CAPABILITY_REPLY
        )

    for rule_name, action_code, pattern in _ACTION_RULES:
        if pattern.match(core):
            return RoutingDecision(
                intent="action",
                matched_rule=rule_name,
                reply=_ACTION_REPLIES[action_code],
                action_code=action_code,
            )

    if has_history and len(core) <= _FOLLOWUP_MAX_CHARS and _FOLLOWUP_PATTERN.match(core):
        return RoutingDecision(intent="followup", matched_rule="followup.anaphora")

    return RoutingDecision(intent="knowledge", matched_rule="knowledge.default")
