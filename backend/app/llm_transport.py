"""外部大模型调用的传输层：超时预算 + 有限重试。

背景（2026-08-05 上线前体检）：生成与 embedding 调用此前完全没有超时约束——
OpenAI-compat 侧写死 180s、DashScope SDK 默认 300s，而 Nginx 的
proxy_read_timeout 只有 60s。后果是用户 60s 就收到 504，后端却还在跑并照常
计费；叠加单进程 uvicorn，几个 hang 住的请求就能把整个服务拖死。

本模块的三条口径：

1. **总预算优先于单次超时。** 重试不得把总耗时推过预算，否则"加了重试"
   等于把可用性做得更差——用户早已收到 504，重试只是在给账单添砖加瓦。
   每次尝试的 read 超时都会被压到剩余预算之内。
2. **只重试传输层可恢复错误**（连接失败、超时、429、5xx）。4xx 一律不重试：
   鉴权错、参数错重试多少次结果都一样，只是多烧一次钱。
3. **预算不足以完成一次有意义的重试时直接放弃**，不发注定超时的请求。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Callable, TypeVar

from .observability import current_trace_id, emit_json_log


T = TypeVar("T")

# 低于这个剩余预算就不再重试：连接加首字节都未必够，发出去多半是白烧一次钱
DEFAULT_MIN_RETRY_SECONDS = 8.0
# 重试前的退避，也计入总预算
RETRY_BACKOFF_SECONDS = 0.5

# 传输层可恢复错误的类型名标记。用类型名而不是 import 具体库，
# 是为了让本模块同时覆盖 httpx（生成）与 requests（DashScope SDK 内部）
# 两套异常体系，且不为了分类去硬依赖 SDK 的内部实现。
_RETRYABLE_EXCEPTION_MARKERS = (
    "timeout",
    "connect",
    "connection",
    "remoteprotocol",
    "remotedisconnected",
    "chunkedencoding",
    "readerror",
    "writeerror",
    "networkerror",
    "protocolerror",
)


@dataclass(frozen=True)
class TimeoutPolicy:
    """一次外部模型调用的时间预算。

    connect_seconds/read_seconds 是单次尝试的超时；budget_seconds 是含重试与
    退避在内的总上限，必须小于反向代理的 proxy_read_timeout，否则用户侧早已
    504、后端还在空转。
    """

    connect_seconds: float
    read_seconds: float
    budget_seconds: float
    max_attempts: int = 2
    min_retry_seconds: float = DEFAULT_MIN_RETRY_SECONDS

    def __post_init__(self) -> None:
        if self.connect_seconds <= 0 or self.read_seconds <= 0:
            raise ValueError("timeout values must be positive")
        if self.budget_seconds < self.connect_seconds:
            raise ValueError("budget must cover at least one connect attempt")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


class LLMTransportError(RuntimeError):
    """外部模型调用失败。retryable 表示"再试一次有可能成功"。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class LLMBudgetExceededError(LLMTransportError):
    """总预算耗尽。单独成类，便于 API 层给出"稍后再试"而不是"服务故障"。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


def is_retryable_status(status_code: int) -> bool:
    """429 与 5xx 可重试；其余 4xx 是调用方自己的问题，重试无意义。"""
    return status_code == 429 or status_code >= 500


def is_retryable_exception(exc: BaseException) -> bool:
    """按异常类型名判断是否为传输层可恢复错误。

    显式声明的 LLMTransportError 以自身标记为准；其余按类型名匹配，
    覆盖 httpx 与 requests 两套体系。未知异常一律视为不可重试——
    宁可少试一次，也不要把逻辑错误反复放大成双倍账单。
    """
    if isinstance(exc, LLMTransportError):
        return exc.retryable
    name = type(exc).__name__.lower()
    return any(marker in name for marker in _RETRYABLE_EXCEPTION_MARKERS)


def _as_transport_failure(
    exc: BaseException, *, op_name: str, retryable: bool
) -> BaseException:
    """把底层异常统一成 LLMTransportError（RuntimeError 子类）再抛。

    为什么必须统一（2026-08-06 体检 #10）：调用方全都按 `except RuntimeError`
    接生成失败——同步端点靠它回滚并返回 503，流式端点靠它下发 error 事件。
    而这里此前是 `raise` 原样重抛，httpx.ConnectTimeout / ReadTimeout /
    RemoteProtocolError 都只是 Exception，不是 RuntimeError：
    - 同步端点接不住 → 没有 rollback、没有结构化日志、返回 500 而不是 503；
    - 流式端点接不住 → 异常从 StreamingResponse 的生成器里抛出，SSE 连接
      被直接掐断，**连 error 事件都发不出去**，前端只能显示"回答流意外中断"。
    应用没有全局 exception handler，所以没有第二道网。

    KeyboardInterrupt / SystemExit 这类非 Exception 原样放行——它们不是
    "模型调用失败"，不该被伪装成传输错误。
    """
    if isinstance(exc, LLMTransportError) or not isinstance(exc, Exception):
        return exc
    failure = LLMTransportError(
        f"{op_name} failed: {type(exc).__name__}: {exc}",
        retryable=retryable,
        status_code=getattr(exc, "status_code", None),
    )
    # 保留原始异常，排查时不丢现场（日志里的 error_type 也仍记原始类型）
    failure.__cause__ = exc
    return failure


def call_with_budget(
    operation: Callable[[float], T],
    policy: TimeoutPolicy,
    *,
    op_name: str,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """在总预算内执行 operation，必要时重试。

    operation 接受本次尝试允许的 read 超时秒数（已按剩余预算压缩），
    返回结果或抛异常。
    """
    deadline = monotonic() + policy.budget_seconds
    last_error: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        remaining = deadline - monotonic()
        if remaining <= policy.connect_seconds:
            break
        read_timeout = min(policy.read_seconds, remaining - policy.connect_seconds)
        started_at = monotonic()
        try:
            return operation(read_timeout)
        except BaseException as exc:  # noqa: BLE001 - 分类后按需重抛
            last_error = exc
            retryable = is_retryable_exception(exc)
            elapsed_ms = round((monotonic() - started_at) * 1000, 3)
            remaining_after = deadline - monotonic()
            # 门槛要扣掉 connect：真正给模型读的时间是「剩余预算 − connect」
            # （见上面 read_timeout 的算法）。只按剩余预算判断的话，出厂默认值
            # （connect 5 / read 40 / budget 50）下第一次超时后还剩约 10s，
            # 10 − 0.5 ≥ 8 成立于是重试，可实际 read_timeout = 10 − 5 = 5s，
            # 一次 5 秒的大模型生成必然失败——正好是本模块第 3 条声明要避免的
            # "发一个注定超时的请求"，只把账单翻倍（2026-08-06 体检 #11）。
            will_retry = (
                retryable
                and attempt < policy.max_attempts
                and remaining_after - RETRY_BACKOFF_SECONDS - policy.connect_seconds
                >= policy.min_retry_seconds
            )
            emit_json_log(
                logging.WARNING if will_retry else logging.ERROR,
                "llm_transport_attempt",
                trace_id=current_trace_id(),
                op_name=op_name,
                attempt=attempt,
                max_attempts=policy.max_attempts,
                outcome="retrying" if will_retry else "failed",
                retryable=retryable,
                error_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
                duration_ms=elapsed_ms,
                remaining_budget_ms=round(max(remaining_after, 0) * 1000, 3),
            )
            if not will_retry:
                raise _as_transport_failure(exc, op_name=op_name, retryable=retryable)
            sleep(RETRY_BACKOFF_SECONDS)

    # 预算耗尽（或首轮就没有可用预算）而没有成功结果
    message = f"{op_name} exhausted its {policy.budget_seconds:g}s budget"
    emit_json_log(
        logging.ERROR,
        "llm_transport_attempt",
        trace_id=current_trace_id(),
        op_name=op_name,
        attempt=policy.max_attempts,
        max_attempts=policy.max_attempts,
        outcome="budget_exhausted",
        error_type=type(last_error).__name__ if last_error else None,
    )
    raise LLMBudgetExceededError(message) from last_error


def stream_with_budget(
    chunks: "Iterable[T]",
    policy: TimeoutPolicy,
    *,
    op_name: str,
    monotonic: Callable[[], float] = time.monotonic,
) -> "Iterator[T]":
    """给流式响应套上总墙钟预算。

    为什么单靠 read_seconds 不够（2026-08-06 体检 #4）：流式下的 read 超时只
    约束"两个数据块之间"的间隔。上游每 39 秒吐一个 token，40s 的 read 超时
    一次都不会触发，而总耗时可以无限延长——nginx 60s 早已把用户断开，后端
    仍在读、仍在计费。这正是本模块开头第 1 条要消灭的失效模式，非流式路径走
    call_with_budget 已经覆盖，流式路径此前是从它下面绕过去的。

    两道闸各管一段，缺一不可：单块间隔由 read_seconds 管，总时长由这里管。
    流式不重试——已经有内容下发给用户了，重来一遍只会让答案自相矛盾。

    **已知上界**：越界只能在"拿到一块"之后发现，阻塞在 next() 里是打断不了的
    （能打断它的是底层 read 超时）。所以最坏耗时是 budget + read，而不是 budget。
    部署校验因此断言 budget + read < 反代 proxy_read_timeout——否则用户会先
    收到反代 504，我们自己的预算闸就永远轮不到生效，等于白装。
    """
    deadline = monotonic() + policy.budget_seconds

    def over_budget() -> bool:
        if monotonic() <= deadline:
            return False
        emit_json_log(
            logging.ERROR,
            "llm_stream_budget_exceeded",
            trace_id=current_trace_id(),
            op_name=op_name,
            budget_seconds=policy.budget_seconds,
        )
        return True

    iterator = iter(chunks)
    while True:
        if over_budget():
            raise LLMBudgetExceededError(
                f"{op_name} exceeded its {policy.budget_seconds:g}s streaming budget"
            )
        try:
            chunk = next(iterator)
        except StopIteration:
            return
        except BaseException as exc:  # noqa: BLE001 - 统一成调用方接得住的契约异常
            # 与 call_with_budget 同一口径：流式端点靠 except RuntimeError 才能
            # 下发 error 事件收束 SSE，底层 httpx 异常逃逸会让连接被直接掐断。
            raise _as_transport_failure(
                exc, op_name=op_name, retryable=is_retryable_exception(exc)
            ) from exc
        yield chunk
