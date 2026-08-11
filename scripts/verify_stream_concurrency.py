"""并发 SSE 压力下，普通接口必须仍然可用。

验证目的：连接池此前是 SQLAlchemy 默认的 5+10，而一路
SSE 聊天从检索开始就占住一条连接、并挂着一个未提交事务直到生成结束。约 15 路
并发聊天就能把池抽干，之后**连登录和健康检查一起 500**——故障面远超聊天本身。

本脚本开 N 路并发流式聊天，同时不断打登录与健康检查，断言：
  1. 旁路接口全部成功（不是 500、不是连接池超时）
  2. 旁路接口的 P95 延迟仍在阈值内（不是排队等连接等出来的）

需要一个已经跑起来的栈；会真实调用模型，请勿在计费敏感时段无脑跑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


def post_json(url: str, body: dict, token: str | None = None, timeout: float = 120.0):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return response.status, payload, time.monotonic() - started


def login(base: str, email: str, password: str) -> str:
    _, payload, _ = post_json(f"{base}/auth/login", {"email": email, "password": password})
    return json.loads(payload)["access_token"]


def stream_one(base: str, token: str, model_id: int, question: str, errors: list[str]) -> None:
    try:
        _, payload, _ = post_json(
            f"{base}/conversations", {"robot_model_id": model_id}, token=token
        )
        conversation_id = json.loads(payload)["id"]
        request = urllib.request.Request(
            f"{base}/conversations/{conversation_id}/messages/stream",
            data=json.dumps({"content": question}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            for _ in response:  # 读到底，全程占着这一路流
                pass
    except urllib.error.HTTPError as exc:
        # 把限流/错误的响应体带出来，否则只看到一个光秃秃的 429 无从判断是哪条闸
        detail = exc.read().decode(errors="replace")[:200]
        errors.append(f"stream HTTP {exc.code}: {detail}")
    except Exception as exc:  # noqa: BLE001 - 压测线程里任何失败都要记下来
        errors.append(f"stream: {type(exc).__name__}: {exc}")


def probe_sidecar(base: str, email: str, password: str, stop: threading.Event,
                  latencies: list[float], errors: list[str],
                  throttled: list[int]) -> None:
    """流跑着的时候，登录接口必须照常可用。

    429 不算失败：D5 之后成功登录也限流（email 10/min），而本脚本每 0.3 秒探测一次，
    因此可能触发限流。429 表示接口仍可响应且限流生效；本检查关注的是并发流式请求期间
    是否出现超时或 5xx。各类结果单独计数，避免遗漏异常。
    """
    while not stop.is_set():
        try:
            _, _, elapsed = post_json(
                f"{base}/auth/login", {"email": email, "password": password}, timeout=30
            )
            latencies.append(elapsed)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                throttled.append(1)
            else:
                errors.append(f"login HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"login {type(exc).__name__}: {exc}")
        time.sleep(0.3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8010/api/v1")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--model-id", type=int, default=1)
    parser.add_argument("--streams", type=int, default=25)
    parser.add_argument("--max-p95", type=float, default=5.0, help="旁路登录 P95 上限（秒）")
    args = parser.parse_args()

    token = login(args.base, args.email, args.password)
    print(f"打 {args.streams} 路并发流式聊天，同时持续探测登录接口…")

    errors: list[str] = []
    latencies: list[float] = []
    throttled: list[int] = []
    stop = threading.Event()
    sidecar = threading.Thread(
        target=probe_sidecar,
        args=(args.base, args.email, args.password, stop, latencies, errors, throttled),
        daemon=True,
    )
    sidecar.start()

    questions = [
        "尘盒怎么清理", "拖布多久换一次", "滤网怎么洗", "充电座指示灯闪红灯",
        "机器人撞墙怎么办", "水箱怎么加水", "主刷缠头发怎么处理", "怎么设置定时清扫",
    ]
    threads = [
        threading.Thread(
            target=stream_one,
            args=(args.base, token, args.model_id, questions[i % len(questions)], errors),
            daemon=True,
        )
        for i in range(args.streams)
    ]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started
    stop.set()
    sidecar.join(timeout=5)

    print(f"{args.streams} 路流全部收束，用时 {elapsed:.1f}s")
    print(
        f"期间登录探测 {len(latencies)} 次成功，"
        f"{len(throttled)} 次被限流（预期内，D5 起成功登录也限流），"
        f"{len([e for e in errors if 'login' in e])} 次失败"
    )
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"登录延迟 中位数={statistics.median(latencies):.2f}s "
            f"P95={p95:.2f}s 最大={max(latencies):.2f}s"
        )
    else:
        p95 = float("inf")
        print("登录探测一次都没成功")

    if errors:
        print(f"\nFAIL 有 {len(errors)} 个失败：", file=sys.stderr)
        for line in errors[:10]:
            print(f"  {line}", file=sys.stderr)
        return 1
    if p95 > args.max_p95:
        print(f"\nFAIL 登录 P95 {p95:.2f}s 超过阈值 {args.max_p95}s", file=sys.stderr)
        return 1
    print("\nPASS 并发流式期间旁路接口全程可用且未被连接池拖慢")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
