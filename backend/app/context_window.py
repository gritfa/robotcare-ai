"""决定"哪些历史消息喂给模型"。

体检发现（2026-08-06）：上下文只带最近 6 条（3 轮问答），界面却承诺
"追问时无需重复背景"。用户按界面说的做，第 4 轮追问时开头那句
"我的 JH69U1 加水后拖地还是干" 已经滑出窗口，模型只看到 "那第二步呢"。

**两处 6 是分开的**：路由层 `MAX_CONTEXT_MESSAGES` 决定查几条，
生成层 `_history_block` 又按 `MAX_HISTORY_MESSAGES` 截一次。
只改前者等于白改，所以两处统一走这里的常量。

窗口策略不是简单的"取最近 N 条"：
- **首轮用户问题单独保留**。它承载了型号、现象、已尝试步骤这些背景，
  是最不该被挤掉的一条 —— 恰恰又是按时间截断时最先掉的那条。
- **总量按字符预算兜底**。光限条数挡不住"6 条各 2000 字"把提示词撑爆，
  而提示词里还要放资料片段，那才是回答的依据。
"""

from __future__ import annotations

from typing import Any, Iterable

# 16 条 ≈ 8 轮问答。再多的收益递减，而每一条都要占走资料片段的位置。
MAX_CONTEXT_MESSAGES = 16
# 历史部分的总字符预算。按中文 1 token ≈ 1~1.5 字符保守折算，约 2~3k token，
# 给资料片段和回答留足空间。字符是估算口径：不同模型 tokenizer 不同，
# 与其引入一个和线上模型未必一致的分词器，不如用字符做保守上界。
MAX_CONTEXT_CHARS = 3000
# 单条上限：一条超长粘贴（日志、报错全文）不该独吞整个预算
MAX_MESSAGE_CHARS = 500


def _clip(content: str) -> str:
    text = (content or "").strip()
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[:MAX_MESSAGE_CHARS] + "…"


def select_context_messages(
    recent: Iterable[Any],
    first_user_message: Any | None = None,
    *,
    max_messages: int = MAX_CONTEXT_MESSAGES,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[dict]:
    """从时间正序的历史里选出要喂给模型的上下文。

    `recent` 是最近若干条（时间正序），`first_user_message` 是本会话的第一条
    用户消息 —— 若它已滑出 `recent`，会被置顶补回来，保证背景不丢。
    返回 `[{"role": ..., "content": ...}, ...]`，时间正序。
    """
    items = [m for m in recent if getattr(m, "content", "").strip()]
    items = items[-max_messages:]

    anchor = None
    if first_user_message is not None and getattr(first_user_message, "content", "").strip():
        already_present = any(
            getattr(m, "id", None) == getattr(first_user_message, "id", None) for m in items
        )
        if not already_present:
            anchor = first_user_message

    # 预算从新往旧分配：越近的轮次对当前追问越关键。
    # 首轮问题先扣掉自己的份额，确保它一定放得下。
    budget = max_chars
    anchor_entry = None
    if anchor is not None:
        anchor_text = _clip(anchor.content)
        anchor_entry = {"role": anchor.role, "content": anchor_text}
        budget -= len(anchor_text)

    selected: list[dict] = []
    for message in reversed(items):
        text = _clip(message.content)
        if len(text) > budget:
            # 预算用尽：更早的轮次整条丢弃，而不是把某条切成半句话
            break
        budget -= len(text)
        selected.append({"role": message.role, "content": text})
    selected.reverse()

    if anchor_entry is not None:
        return [anchor_entry, *selected]
    return selected
