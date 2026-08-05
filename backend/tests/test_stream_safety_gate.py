"""流式安全闸门：真流式不得打开"未校验内容已在用户屏幕上"的窗口。

改造前是"整段生成完再按 48 字符切片下发"，首字延迟＝完整生成延迟；
改造后 token 到达即过闸下发。但本模块开头的硬规则（引用校验、REFUSE 标记、
输出侧危险操作检测）都需要成句甚至全文才能判定，所以闸门按句放行、
末尾残句永不放行、任一句可疑就关闸退化成非流式。

这些测试守的就是"提速没有把安全规则悄悄放宽"。
"""

from __future__ import annotations

import pytest

from app.generation_service import (
    REFUSE_TOKEN,
    StreamSafetyGate,
    answer_events,
    supports_streaming,
)


class StreamingProvider:
    """按给定分片顺序吐出增量的假后端。"""

    model_name = "stream-test-model"

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.stream_calls = 0
        self.generate_calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.generate_calls += 1
        return "".join(self.pieces)

    def generate_stream(self, *, system: str, prompt: str):
        self.stream_calls += 1
        yield from self.pieces


class NonStreamingProvider:
    model_name = "plain-test-model"

    def __init__(self, text: str) -> None:
        self.text = text
        self.generate_calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.generate_calls += 1
        return self.text


def test_gate_releases_only_complete_sentences():
    gate = StreamSafetyGate(snippet_count=3)

    assert gate.feed("先清空尘盒") == ""  # 未成句，不放行
    assert gate.feed("并清理滤网 [1]。") == "先清空尘盒并清理滤网 [1]。"
    assert gate.released == "先清空尘盒并清理滤网 [1]。"


def test_gate_never_releases_trailing_fragment():
    """末尾残句永远留在手里——模型把 REFUSE 附在结尾时才不会泄漏出去。"""
    gate = StreamSafetyGate(snippet_count=3)

    gate.feed("第一步这样做 [1]。")
    gate.feed("接下来还没写完的半句")

    assert gate.released == "第一步这样做 [1]。"
    assert "接下来" not in gate.released


def test_gate_closes_when_refuse_token_appears_mid_stream():
    gate = StreamSafetyGate(snippet_count=3)

    assert gate.feed("正常的一句话 [1]。") != ""
    assert gate.feed(f"{REFUSE_TOKEN}（注：资料未提供滤网单价）。") == ""
    assert gate.closed is True
    # 关闸后不再放行任何内容
    assert gate.feed("后面还有一句 [1]。") == ""


def test_gate_closes_on_out_of_range_citation():
    """引用越界是 citation_invalid 的判据，绝不能先给用户看到。"""
    gate = StreamSafetyGate(snippet_count=2)

    assert gate.feed("这句引用了不存在的片段 [7]。") == ""
    assert gate.closed is True


def test_gate_closes_on_unsafe_content():
    gate = StreamSafetyGate(snippet_count=3)

    released = gate.feed("请拆开机器外壳并短接电机触点 [1]。")

    assert released == ""
    assert gate.closed is True


def test_gate_blocks_everything_when_refuse_starts_the_answer():
    gate = StreamSafetyGate(snippet_count=3)

    assert gate.feed(f"{REFUSE_TOKEN}。") == ""
    assert gate.closed is True
    assert gate.released == ""


def _run(events):
    """消费事件生成器，返回 (事件列表, 最终结果)。"""
    collected = []
    while True:
        try:
            collected.append(next(events))
        except StopIteration as stop:
            return collected, stop.value


def _answer(client, provider, *, streaming: bool, query: str = "吸力变小了怎么办"):
    """在同一个 session 内完成入库与作答。

    入库与作答分处两个 session 时检索恒为空（ingest 未提交即被丢弃），
    最终只会得到 knowledge_gap——测的就不是闸门了。
    """
    from sqlalchemy import select

    from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf
    from app.models import RobotModel
    from test_generation_service import SYNTHETIC_DIR

    code = "RC-S200"
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
        ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=SYNTHETIC_DIR / f"{code}_manual.pdf",
            source_url=f"synthetic://robotcare-demo/{code.lower()}/manual",
            provider=HashingNgramEmbeddingProvider(),
        )
        return _run(
            answer_events(
                db,
                user_id=None,
                robot_model_id=model_id,
                query=query,
                embedding_provider=HashingNgramEmbeddingProvider(),
                generation_provider=provider,
                min_score=0.0,
                streaming=streaming,
            )
        )


def test_stage_events_report_real_progress(client):
    """前端此前只能显示一句不变的"正在检索…"——阶段点必须是真实的三段。"""
    provider = NonStreamingProvider("先清空尘盒并清理滤网 [1]。")
    events, _result = _answer(client, provider, streaming=False)

    stages = [value for kind, value in events if kind == "stage"]
    assert stages == ["retrieving", "retrieved", "generating"]


def test_streaming_emits_deltas_before_completion(client):
    provider = StreamingProvider(["先清空尘盒 [1]。", "再检查吸尘口是否堵塞 [1]。", "完成。"])
    events, result = _answer(client, provider, streaming=True)

    deltas = [value for kind, value in events if kind == "delta"]
    assert provider.stream_calls == 1
    assert provider.generate_calls == 0
    assert deltas, "流式模式必须在生成过程中就产出增量"
    assert result.status == "answered"
    # 已下发内容必须是最终答案的前缀，调用方才能安全补发剩余部分
    assert result.answer.startswith(result.streamed_text)


def test_non_streaming_provider_falls_back_to_whole_answer(client):
    """后端不支持流式时必须回退整段生成，不能走没测过的路径。"""
    provider = NonStreamingProvider("先清空尘盒并清理滤网 [1]。")
    assert supports_streaming(provider) is False

    events, result = _answer(client, provider, streaming=True)

    assert provider.generate_calls == 1
    assert [value for kind, value in events if kind == "delta"] == []
    assert result.status == "answered"
    assert result.streamed_text == ""


def test_streaming_does_not_leak_trailing_refuse_marker(client):
    """2026-08-05 生产实测过的真实故障：模型在完整回答末尾附加 REFUSE 说明。"""
    provider = StreamingProvider(
        ["先清空尘盒并清理滤网 [1]。", f"{REFUSE_TOKEN}（注：资料未提供滤网单价）"]
    )
    events, result = _answer(client, provider, streaming=True)

    deltas = "".join(value for kind, value in events if kind == "delta")
    assert REFUSE_TOKEN not in deltas
    assert REFUSE_TOKEN not in (result.answer or "")
    assert REFUSE_TOKEN not in result.streamed_text


def test_streaming_unsafe_answer_is_refused_and_nothing_leaks(client):
    provider = StreamingProvider(["请拆开机器外壳并短接电机触点 [1]。", "然后重新装回。"])
    events, result = _answer(client, provider, streaming=True)

    deltas = "".join(value for kind, value in events if kind == "delta")
    assert result.status == "refused"
    assert result.refusal_reason == "unsafe_answer"
    assert deltas == "", "危险内容一个字都不该到过用户屏幕"
    assert result.streamed_text == ""


def test_streaming_citation_invalid_is_refused(client):
    provider = StreamingProvider(["这句引用了不存在的片段 [9]。"])
    _events, result = _answer(client, provider, streaming=True)

    assert result.status == "refused"
    assert result.refusal_reason == "citation_invalid"
    assert result.streamed_text == ""


@pytest.mark.parametrize("streaming", [True, False])
def test_streaming_and_sync_agree_on_final_answer(client, streaming):
    """同一段内容，走不走流式的最终结果必须一致——提速不改判定。"""
    pieces = ["先清空尘盒 [1]。", "再检查吸尘口是否堵塞 [1]。"]
    provider = StreamingProvider(pieces)
    _events, result = _answer(client, provider, streaming=streaming)

    assert result.status == "answered"
    assert result.answer == "".join(pieces)
