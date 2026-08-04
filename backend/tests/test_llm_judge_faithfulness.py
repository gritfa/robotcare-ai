"""③ LLM 裁判语义忠实度：解析/汇总/判分/重试 的离线单测（不调真实 API）。"""

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_llm_judge_faithfulness import (  # noqa: E402
    SEMANTIC_FAITHFULNESS_THRESHOLD,
    build_judge_prompt,
    compute_outcome,
    judge_case,
    parse_judge_output,
    summarize_claims,
)


class _Snippet:
    def __init__(self, page_number: int, content: str) -> None:
        self.page_number = page_number
        self.content = content


class _FakeJudge:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return self.outputs.pop(0)


VALID = json.dumps(
    {
        "claims": [
            {"claim": "重启基站", "verdict": "supported", "evidence_index": 1, "reason": "片段1有"},
            {"claim": "请联系售后", "verdict": "uncheckable", "evidence_index": None, "reason": "兜底话术"},
        ]
    },
    ensure_ascii=False,
)


def test_parse_valid_and_fenced():
    assert len(parse_judge_output(VALID)) == 2
    assert len(parse_judge_output(f"```json\n{VALID}\n```")) == 2


def test_parse_rejects_bad_structure():
    with pytest.raises(ValueError):
        parse_judge_output("好的，我来分析一下。")
    with pytest.raises(ValueError):
        parse_judge_output('{"claims": []}')
    with pytest.raises(ValueError):
        parse_judge_output('{"claims": [{"claim": "x", "verdict": "maybe"}]}')


def test_judge_case_retries_once_then_succeeds():
    judge = _FakeJudge(["这不是JSON", VALID])
    claims, raw = judge_case(judge, "回答", [_Snippet(3, "片段")])
    assert judge.calls == 2
    assert len(claims) == 2
    assert raw == VALID


def test_judge_case_raises_after_two_failures():
    judge = _FakeJudge(["坏1", "坏2"])
    with pytest.raises(ValueError):
        judge_case(judge, "回答", [_Snippet(3, "片段")])


def test_summarize_unsupported_breaks_faithful():
    faithful = summarize_claims([{"verdict": "supported"}, {"verdict": "uncheckable"}])
    assert faithful["faithful"] is True
    broken = summarize_claims([{"verdict": "supported"}, {"verdict": "unsupported"}])
    assert broken["faithful"] is False and broken["checkable"] == 2
    # 全部 uncheckable：无可核对内容，不得算 faithful
    empty = summarize_claims([{"verdict": "uncheckable"}])
    assert empty["faithful"] is False


def test_compute_outcome_paths():
    assert compute_outcome(judged=0, faithful=0, run_errors=0, limit_applied=False) == ("not_run", 2, 0.0)
    assert compute_outcome(judged=30, faithful=29, run_errors=0, limit_applied=False)[0] == "passed_full"
    assert compute_outcome(judged=30, faithful=20, run_errors=0, limit_applied=False) == (
        "below_threshold", 1, round(20 / 30, 4),
    )
    assert compute_outcome(judged=30, faithful=30, run_errors=1, limit_applied=False)[0] == "completed_with_errors"
    assert compute_outcome(judged=2, faithful=2, run_errors=0, limit_applied=True) == ("partial_limit", 0, 1.0)
    assert SEMANTIC_FAITHFULNESS_THRESHOLD == 0.90


def test_prompt_contains_pages_and_answer():
    prompt = build_judge_prompt("先清洁传感器", [_Snippet(15, "定期清洁跌落传感器")])
    assert "第 15 页" in prompt and "先清洁传感器" in prompt and "unsupported" in prompt
