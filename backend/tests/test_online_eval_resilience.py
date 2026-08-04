"""在线评测故障注入测试：外部服务异常时必须稳定留痕（不连网、不调真实模型）。

锁定口径：
- 生成模型中途挂（超时/限流/断网）：已评用例保留、异常逐条进 run_errors、
  报告照常写盘、overall_status=run_error、退出码非 0；
- 检索 embedding 挂：同上，绝不因异常在 try 块外而裸崩丢报告；
- ingest 阶段挂：留痕 __ingest__:<型号> 并放弃评测，报告仍写盘、非 0 退出；
- 异常消息里混入密钥：拒绝写盘（防泄漏优先于留痕）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import scripts.run_online_generation_eval as eval_mod  # noqa: E402
from app.config import get_settings  # noqa: E402

FAKE_KEY = "sk-test-resilience-fake-key-000000"


@pytest.fixture()
def eval_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ROBOTCARE_DASHSCOPE_API_KEY", FAKE_KEY)
    monkeypatch.delenv("ROBOTCARE_LLM_BACKEND", raising=False)
    get_settings.cache_clear()
    yield tmp_path / "report.json"
    get_settings.cache_clear()


def _run_main(monkeypatch, output: Path, extra_args: list[str] | None = None) -> int:
    argv = [
        "run_online_generation_eval.py",
        "--scope", "synthetic",
        "--limit", "3",
        "--output", str(output),
    ] + (extra_args or [])
    monkeypatch.setattr(sys, "argv", argv)
    return eval_mod.main()


class _FlakyGenerationProvider:
    """第 1 条正常拒答（合规输出），之后模拟外部服务超时。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model_name = "fake-flaky-model"
        self.calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        if self.calls > 1:
            raise TimeoutError("Read timed out (simulated outage)")
        return "REFUSE"


def test_generation_outage_keeps_partial_results_and_report(monkeypatch, eval_env):
    monkeypatch.setattr(eval_mod, "DashScopeGenerationProvider", _FlakyGenerationProvider)
    exit_code = _run_main(monkeypatch, eval_env)
    assert exit_code == 1
    report = json.loads(eval_env.read_text(encoding="utf-8"))
    assert report["overall_status"] == "run_error"
    assert report["evaluated"] == 1  # 挂掉之前的用例保留
    assert len(report["run_errors"]) == 2
    assert all("timed out" in e["error"] for e in report["run_errors"])
    assert all(e["case_id"].startswith("SYN-FA-") for e in report["run_errors"])


def test_retrieval_outage_is_recorded_not_crashed(monkeypatch, eval_env):
    def _broken_search(*args, **kwargs):
        raise ConnectionError("embedding endpoint unreachable (simulated)")

    monkeypatch.setattr(eval_mod, "search_knowledge", _broken_search)
    exit_code = _run_main(monkeypatch, eval_env)
    assert exit_code == 1
    report = json.loads(eval_env.read_text(encoding="utf-8"))
    assert report["overall_status"] == "run_error"
    assert report["evaluated"] == 0
    assert len(report["run_errors"]) == 3
    assert all("unreachable" in e["error"] for e in report["run_errors"])


def test_ingest_outage_leaves_structured_trace(monkeypatch, eval_env):
    def _broken_ingest(*args, **kwargs):
        raise RuntimeError("Requests rate limit exceeded (simulated)")

    monkeypatch.setattr(eval_mod, "ingest_pdf", _broken_ingest)
    exit_code = _run_main(monkeypatch, eval_env)
    assert exit_code == 1
    report = json.loads(eval_env.read_text(encoding="utf-8"))
    assert report["overall_status"] == "run_error"
    assert report["evaluated"] == 0
    assert len(report["run_errors"]) == 1
    assert report["run_errors"][0]["case_id"].startswith("__ingest__:")
    assert "rate limit" in report["run_errors"][0]["error"].lower()


def test_error_message_containing_secret_refuses_to_write_report(monkeypatch, eval_env):
    class _LeakyProvider:
        def __init__(self, *args, **kwargs) -> None:
            self.model_name = "fake-leaky-model"

        def generate(self, *, system: str, prompt: str) -> str:
            raise RuntimeError(f"auth failed for key {FAKE_KEY}")

    monkeypatch.setattr(eval_mod, "DashScopeGenerationProvider", _LeakyProvider)
    with pytest.raises(RuntimeError, match="密钥"):
        _run_main(monkeypatch, eval_env)
    assert not eval_env.exists()  # 防泄漏优先：拒绝写盘
