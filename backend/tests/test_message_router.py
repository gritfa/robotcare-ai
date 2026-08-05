"""聊天消息路由层：闲聊/产品操作/上下文追问/知识问题四类的判定边界。

这些用例钉的是"哪条消息该不该进 RAG"。误判代价不对称：把闲聊当知识问题
只是浪费一次检索，把真实求助当闲聊打发掉是答非所问——所以兜底必须是 knowledge，
下面 test_substantive_questions_never_routed_away_from_retrieval 专门守这条。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.message_router import classify_message


@pytest.mark.parametrize(
    "content",
    ["你好", "您好！", "hi", "Hello~", "在吗？", "谢谢！", "好的，收到", "拜拜", "哈喽 😊"],
)
def test_greetings_are_smalltalk_and_skip_retrieval(content):
    decision = classify_message(content)
    assert decision.intent == "smalltalk"
    assert decision.needs_retrieval is False
    assert decision.reply and "售后知识助手" in decision.reply


@pytest.mark.parametrize(
    ("content", "action_code"),
    [
        ("开始诊断", "start_diagnostic"),
        ("我要做一次诊断", "start_diagnostic"),
        ("帮我排查一下", "start_diagnostic"),
        ("生成报告", "generate_report"),
        ("帮我导出一份售后报告", "generate_report"),
        ("我想下载报告", "generate_report"),
        ("上传图片", "upload_image"),
        ("我要发张照片给你", "upload_image"),
        ("可以传个截图吗", "upload_image"),
    ],
)
def test_product_actions_route_to_action_not_knowledge(content, action_code):
    decision = classify_message(content)
    assert decision.intent == "action"
    assert decision.action_code == action_code
    assert decision.needs_retrieval is False


def test_greeting_prefix_does_not_swallow_the_real_request():
    # 先剥招呼语再分类：招呼语只是礼貌前缀，不能把后面的正事一起吃掉
    assert classify_message("你好，帮我生成报告").intent == "action"
    assert classify_message("你好，扫地机不吸尘怎么办").intent == "knowledge"
    assert classify_message("谢谢！那第二步呢", has_history=True).intent == "followup"


@pytest.mark.parametrize(
    "content",
    [
        "拖布不转怎么办",
        "E5 错误码是什么意思",
        "报告里的结论是怎么得出来的",
        "诊断流程一共有几步",
        "上传的图片会保存多久",
    ],
)
def test_substantive_questions_never_routed_away_from_retrieval(content):
    # 后三条含"报告""诊断""上传图片"字样但问的是知识，必须进检索——
    # 操作规则只认祈使句，不能见词就抢
    decision = classify_message(content, has_history=True)
    assert decision.intent == "knowledge"
    assert decision.needs_retrieval is True


@pytest.mark.parametrize("content", ["那第二步呢", "为什么", "还有吗", "这个怎么办", "下一步呢"])
def test_followups_need_history_and_still_go_through_retrieval(content):
    # 追问要靠上文才完整，但它问的仍是知识——照常检索，只是打上 followup 标记
    with_history = classify_message(content, has_history=True)
    assert with_history.intent == "followup"
    assert with_history.needs_retrieval is True
    # 首轮没有可指代的上文，同样一句话只能当普通知识问题处理
    assert classify_message(content, has_history=False).intent == "knowledge"


def test_every_decision_carries_an_explainable_rule():
    # matched_rule 会落进消息留痕和日志，事后要能回答"这条为什么没进检索"
    for content, expected in [
        ("你好", "smalltalk.greeting_only"),
        ("开始诊断", "action.start_diagnostic"),
        ("生成报告", "action.generate_report"),
        ("上传图片", "action.upload_image"),
        ("吸力变小了", "knowledge.default"),
    ]:
        assert classify_message(content).matched_rule == expected


@pytest.mark.parametrize(
    "content",
    ["你能做什么", "你是谁", "你有什么功能", "怎么用", "能干嘛？", "help"],
)
def test_capability_questions_get_self_introduction_not_manual_search(content):
    # "你能做什么"问的是助手的能力边界，说明书里本来就没有答案，
    # 此前去检索会返回一大段产品功能列表，读起来像搜索结果不像客服
    decision = classify_message(content)
    assert decision.intent == "capability"
    assert decision.needs_retrieval is False
    assert decision.uses_model_placeholder, "能力介绍要按当前型号填充"
    assert "不提供拆机" in (decision.reply or "")
