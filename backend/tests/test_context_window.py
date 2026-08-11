"""C3：多轮上下文窗口与界面承诺一致。

问题背景：上下文只带最近 6 条（3 轮问答），界面却写
"追问时无需重复背景"。用户按界面说的做，第 4 轮追问时开头那句
"我的 JH69U1 加水后拖地还是干" 已经滑出窗口，模型只看到 "那第二步呢"。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.context_window import (
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_MESSAGES,
    MAX_MESSAGE_CHARS,
    select_context_messages,
)


def _msg(index: int, role: str = "user", content: str | None = None) -> SimpleNamespace:
    # 用 None 而非 "" 作哨兵：空串是本模块要测的输入之一，不能被默认值吃掉
    return SimpleNamespace(
        id=index, role=role, content=f"第{index}条" if content is None else content
    )


def test_window_covers_eight_rounds_not_three():
    """界面承诺"连续追问"，3 轮就丢背景撑不起这个说法。"""
    assert MAX_CONTEXT_MESSAGES >= 16

    history = [_msg(i, "user" if i % 2 else "assistant") for i in range(1, 41)]
    selected = select_context_messages(history)

    assert len(selected) == MAX_CONTEXT_MESSAGES
    assert selected[-1]["content"] == "第40条"
    # 时间正序，模型才能理解"先问什么后问什么"
    assert [item["content"] for item in selected] == [f"第{i}条" for i in range(25, 41)]


def test_first_question_survives_after_window_slides():
    """首轮问题带着型号/现象/已尝试步骤，是最不该被挤掉的一条。"""
    background = _msg(1, "user", "我的 JH69U1 加水后拖地还是干")
    later = [_msg(i, "user" if i % 2 else "assistant") for i in range(2, 40)]

    selected = select_context_messages(later, background)

    assert selected[0]["content"] == "我的 JH69U1 加水后拖地还是干"
    assert selected[-1]["content"] == "第39条"


def test_first_question_is_not_duplicated_when_still_in_window():
    background = _msg(1, "user", "我的 JH69U1 加水后拖地还是干")
    recent = [background, _msg(2, "assistant", "请检查水箱"), _msg(3, "user", "那第二步呢")]

    selected = select_context_messages(recent, background)

    assert len(selected) == 3
    assert [item["content"] for item in selected].count("我的 JH69U1 加水后拖地还是干") == 1


def test_char_budget_drops_oldest_whole_messages():
    """光限条数挡不住"每条 2000 字"把提示词撑爆，而资料片段才是回答依据。"""
    long_text = "啊" * MAX_MESSAGE_CHARS
    history = [_msg(i, "user", long_text) for i in range(1, 17)]

    selected = select_context_messages(history)

    total = sum(len(item["content"]) for item in selected)
    assert total <= MAX_CONTEXT_CHARS
    assert len(selected) < MAX_CONTEXT_MESSAGES  # 预算先于条数触顶
    # 丢弃以整条为单位：不能把某条切成半句话喂进去
    assert all(len(item["content"]) == MAX_MESSAGE_CHARS for item in selected)
    # 留下的是最近的那些
    assert selected[-1]["content"] == long_text


def test_single_huge_message_cannot_eat_the_whole_budget():
    history = [_msg(1, "user", "背景问题"), _msg(2, "user", "日" * 5000)]

    selected = select_context_messages(history)

    assert all(len(item["content"]) <= MAX_MESSAGE_CHARS + 1 for item in selected)
    assert selected[-1]["content"].endswith("…")


def test_blank_messages_are_dropped():
    history = [_msg(1, "user", "有效问题"), _msg(2, "assistant", "   "), _msg(3, "user", "")]

    selected = select_context_messages(history)

    assert [item["content"] for item in selected] == ["有效问题"]


def test_budget_reserved_for_anchor_even_when_recent_is_long():
    """首轮问题先扣自己的份额，不能被后面的长消息挤没。"""
    background = _msg(1, "user", "我的 JH69U1 加水后拖地还是干")
    recent = [_msg(i, "user", "啊" * MAX_MESSAGE_CHARS) for i in range(2, 30)]

    selected = select_context_messages(recent, background)

    assert selected[0]["content"] == "我的 JH69U1 加水后拖地还是干"
    assert sum(len(item["content"]) for item in selected) <= MAX_CONTEXT_CHARS
