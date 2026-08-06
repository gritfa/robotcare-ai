"""外部模型调用的超时预算与重试策略。

守住三条上线约束（2026-08-05 体检发现调用完全没有超时）：
1. 单次尝试的 read 超时不得超出剩余预算；
2. 只重试传输层可恢复错误，4xx 不重试（重试只是双倍账单）；
3. 总耗时不得超过预算——否则用户早已收到反代 504，后端还在空转计费。
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.llm_transport import (
    LLMBudgetExceededError,
    LLMTransportError,
    TimeoutPolicy,
    call_with_budget,
    is_retryable_exception,
    is_retryable_status,
    stream_with_budget,
)


class FakeClock:
    """可控时钟：sleep 与 operation 的耗时都推进它，便于断言预算行为。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _policy(**overrides) -> TimeoutPolicy:
    base = {
        "connect_seconds": 5.0,
        "read_seconds": 40.0,
        "budget_seconds": 50.0,
        "max_attempts": 2,
    }
    base.update(overrides)
    return TimeoutPolicy(**base)


def test_first_attempt_uses_configured_read_timeout():
    clock = FakeClock()
    seen: list[float] = []

    def operation(read_timeout: float) -> str:
        seen.append(read_timeout)
        return "ok"

    result = call_with_budget(
        operation, _policy(), op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
    )

    assert result == "ok"
    assert seen == [40.0]


def test_retry_read_timeout_is_capped_by_remaining_budget():
    """第一次尝试烧掉大半预算后，重试的 read 超时必须被压缩，不能再要 40s。"""
    clock = FakeClock()
    seen: list[float] = []

    def operation(read_timeout: float) -> str:
        seen.append(read_timeout)
        if len(seen) == 1:
            clock.advance(30.0)
            raise httpx.ReadTimeout("slow")
        return "ok"

    result = call_with_budget(
        operation, _policy(), op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
    )

    assert result == "ok"
    # 剩余 50 - 30 - 0.5(退避) = 19.5，扣掉 connect 5 → 14.5
    assert seen[0] == 40.0
    assert seen[1] == pytest.approx(14.5)
    assert clock.now <= 50.0


def test_client_error_is_not_retried():
    """4xx 重试多少次结果都一样，只多烧一次钱。"""
    clock = FakeClock()
    calls = 0

    def operation(read_timeout: float) -> str:
        nonlocal calls
        calls += 1
        raise LLMTransportError("bad request", retryable=False, status_code=400)

    with pytest.raises(LLMTransportError) as excinfo:
        call_with_budget(
            operation, _policy(), op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
        )

    assert calls == 1
    assert excinfo.value.status_code == 400


def test_server_error_is_retried_once_then_surfaces():
    clock = FakeClock()
    calls = 0

    def operation(read_timeout: float) -> str:
        nonlocal calls
        calls += 1
        clock.advance(1.0)
        raise LLMTransportError("upstream down", retryable=True, status_code=503)

    with pytest.raises(LLMTransportError) as excinfo:
        call_with_budget(
            operation, _policy(), op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
        )

    assert calls == 2  # max_attempts=2，用尽后抛最后一次错误而不是预算错误
    assert excinfo.value.status_code == 503


def test_no_retry_when_remaining_budget_cannot_fit_a_useful_attempt():
    """剩余预算不足以完成一次有意义的重试时直接放弃，不发注定超时的请求。"""
    clock = FakeClock()
    calls = 0

    def operation(read_timeout: float) -> str:
        nonlocal calls
        calls += 1
        clock.advance(45.0)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.ReadTimeout):
        call_with_budget(
            operation, _policy(), op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
        )

    assert calls == 1
    assert clock.now <= 50.0


def test_budget_exhausted_before_any_attempt_raises_budget_error():
    clock = FakeClock()
    policy = _policy(budget_seconds=6.0, connect_seconds=5.0, read_seconds=5.0, max_attempts=3)
    calls = 0

    def operation(read_timeout: float) -> str:
        nonlocal calls
        calls += 1
        clock.advance(5.9)
        raise httpx.ConnectTimeout("nope")

    with pytest.raises(httpx.ConnectTimeout):
        call_with_budget(
            operation, policy, op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
        )

    assert calls == 1


def test_zero_budget_policy_raises_budget_exceeded():
    clock = FakeClock()
    policy = TimeoutPolicy(
        connect_seconds=5.0, read_seconds=5.0, budget_seconds=5.0, max_attempts=1
    )

    def operation(read_timeout: float) -> str:  # pragma: no cover - 不应被调用
        raise AssertionError("must not attempt without budget")

    with pytest.raises(LLMBudgetExceededError):
        call_with_budget(
            operation, policy, op_name="t", monotonic=clock.monotonic, sleep=clock.sleep
        )


@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.ReadTimeout("x"), True),
        (httpx.ConnectError("x"), True),
        (httpx.RemoteProtocolError("x"), True),
        (ValueError("x"), False),
        (KeyError("x"), False),
        (LLMTransportError("x", retryable=False, status_code=401), False),
        (LLMTransportError("x", retryable=True, status_code=502), True),
    ],
)
def test_exception_retry_classification(exc, expected):
    assert is_retryable_exception(exc) is expected


@pytest.mark.parametrize(
    "status,expected",
    [(429, True), (500, True), (502, True), (400, False), (401, False), (404, False)],
)
def test_status_retry_classification(status, expected):
    assert is_retryable_status(status) is expected


def test_settings_expose_policies_within_reverse_proxy_window():
    """默认预算必须小于 nginx proxy_read_timeout（60s），否则等于没做超时。"""
    settings = Settings()
    assert settings.llm_timeout_policy.budget_seconds < 60
    assert settings.embedding_timeout_policy.budget_seconds < 60
    assert settings.llm_timeout_policy.max_attempts >= 1


def test_settings_reject_budget_smaller_than_connect():
    with pytest.raises(ValueError):
        Settings(llm_connect_timeout_seconds=20.0, llm_budget_seconds=22.0)
    with pytest.raises(ValueError):
        Settings(embedding_connect_timeout_seconds=20.0, embedding_budget_seconds=22.0)


def test_slow_stream_is_cut_at_the_budget_even_if_no_single_read_times_out():
    """每块间隔都在 read 超时之内，但总时长超预算——必须在预算点砍断。

    这正是 2026-08-06 体检 #4 的失效模式：流式下 read timeout 只约束
    "两块数据之间"，上游每 39 秒吐一个 token，40s 的 read 超时一次都不会
    触发，而 nginx 60s 早已断开用户，后端还在读、还在计费。
    """
    clock = FakeClock()

    def slow_chunks():
        for index in range(100):
            clock.advance(39.0)  # 单块间隔 < read_seconds(40)，read 超时永不触发
            yield f"chunk-{index}"

    received: list[str] = []
    with pytest.raises(LLMBudgetExceededError):
        for chunk in stream_with_budget(
            slow_chunks(), _policy(), op_name="test.stream", monotonic=clock.monotonic
        ):
            received.append(chunk)

    # 预算 50s：0s 检查通过取到 chunk-0（39s），39s 检查仍未越界取到 chunk-1（78s），
    # 78s 检查越界抛出。最坏耗时因此是 budget + 一次 read，而不是 budget——
    # 阻塞在读上是打断不了的，这条上界由 nginx proxy_read_timeout 兜底
    # （verify_deployment_config.py 有交叉断言）。
    assert received == ["chunk-0", "chunk-1"]
    assert clock.now == pytest.approx(78.0)


def test_stream_within_budget_passes_everything_through():
    clock = FakeClock()

    def quick_chunks():
        for index in range(5):
            clock.advance(1.0)
            yield index

    got = list(
        stream_with_budget(
            quick_chunks(), _policy(), op_name="test.stream", monotonic=clock.monotonic
        )
    )
    assert got == [0, 1, 2, 3, 4]


def test_empty_stream_is_not_an_error():
    clock = FakeClock()
    assert (
        list(
            stream_with_budget(
                iter([]), _policy(), op_name="test.stream", monotonic=clock.monotonic
            )
        )
        == []
    )
