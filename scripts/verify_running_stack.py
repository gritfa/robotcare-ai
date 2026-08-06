"""Runtime contract checks against the *running* containers.

存在的理由（2026-08-06 体检实锤）：批 2/3/4 的前端改动全部提交、全部本地验证通过，
但生产前端容器停在更早的镜像上，用户一个都看不到。`verify_deployment_config.py`
做的是静态文件检查——它读仓库里的源码，因此对「镜像没重建」这类故障完全免疫。

本脚本只问一个问题：**此刻正在跑的容器里，是不是本轮的代码**。
所有断言都从运行中的容器/接口取证，不读仓库源码。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from ipaddress import ip_address, ip_network
import urllib.error
import urllib.request


FRONTEND_SERVICE = "frontend"
ASSET_ROOT = "/usr/share/nginx/html/assets"


class CheckFailed(AssertionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def container_sh(service: str, script: str) -> str:
    """在运行中的容器里执行一段 sh，返回 stdout。"""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, "sh", "-c", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CheckFailed(
            f"docker compose exec {service} failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:400]}"
        )
    return result.stdout


def http_json(url: str) -> object:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.load(response)
    except urllib.error.URLError as exc:  # pragma: no cover - 环境问题直接报错
        raise CheckFailed(f"cannot reach {url}: {exc}") from exc


def check_frontend_assets() -> list[str]:
    """前端构建产物必须是本轮的。"""
    checks: list[str] = []

    css = container_sh(FRONTEND_SERVICE, f"cat {ASSET_ROOT}/index-*.css")
    require(
        "min-width:1100px" not in css,
        "前端镜像是旧的：全局样式里仍有 min-width:1100px（移动端适配未上线）",
    )
    checks.append("global css has no desktop-only min-width")

    admin_js = container_sh(FRONTEND_SERVICE, f"cat {ASSET_ROOT}/AdminView-*.js")
    for marker in ("反馈", "型号"):
        require(
            marker in admin_js,
            f"前端镜像是旧的：AdminView 里找不到「{marker}」（运营可见性功能未上线）",
        )
    checks.append("AdminView bundle carries this round's operations features")

    chat_js = container_sh(FRONTEND_SERVICE, f"cat {ASSET_ROOT}/ChatView-*.js")
    require(
        "discard" in chat_js,
        "前端镜像是旧的：ChatView 未处理 discard 事件（真流式接线未上线）",
    )
    checks.append("ChatView bundle handles streaming discard")

    return checks


def check_forwarded_chain() -> list[str]:
    """限流按真实客户端 IP 分桶——这条链必须在**运行中的**容器里成立。

    静态校验读的是仓库里的 nginx.conf 与 compose 默认值；只要镜像没重建、
    或者线上用外部环境变量覆盖了可信网段，静态检查照样全绿而线上是坏的。
    坏掉的后果不是「限流不准」，是任意一人失败登录 30 次锁全站（体检 D1）。
    """
    checks: list[str] = []

    nginx_conf = container_sh(FRONTEND_SERVICE, "cat /etc/nginx/conf.d/default.conf")
    require(
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in nginx_conf,
        "运行中的 Nginx 仍在覆盖 X-Forwarded-For：上游反代的真实客户端 IP 会被丢掉，"
        "全站共用一个限流桶",
    )
    checks.append("running Nginx appends to the forwarded chain")

    trusted = container_sh(
        "backend", "printenv ROBOTCARE_TRUSTED_PROXY_CIDRS || true"
    ).strip()
    require(
        bool(trusted),
        "运行中的后端没有 ROBOTCARE_TRUSTED_PROXY_CIDRS：Nginx 这一跳不被信任，"
        "client_ip() 会把所有人都算成 Nginx 容器地址",
    )
    networks = [ip_network(item.strip()) for item in trusted.split(",") if item.strip()]
    require(
        all(network.is_private and network.prefixlen >= 24 for network in networks),
        f"运行中的可信代理网段过宽或非私有（{trusted}）：调用方可以自选限流桶",
    )

    # 真正的端到端证据：Nginx 容器解析出的地址必须落在后端信任的网段里
    nginx_address = ip_address(
        container_sh(
            FRONTEND_SERVICE,
            "getent hosts $(hostname) | awk '{print $1}' | head -1",
        ).strip()
    )
    require(
        any(nginx_address in network for network in networks),
        f"运行中的 Nginx 地址 {nginx_address} 不在后端信任的网段 {trusted} 内："
        "转发来的地址会被整条忽略",
    )
    checks.append(f"backend trusts the running Nginx address ({nginx_address})")

    return checks


def check_backend_contract(base_url: str) -> list[str]:
    """后端跑的必须是本轮的代码——以 openapi 暴露的路径为证。"""
    checks: list[str] = []

    spec = http_json(f"{base_url}/openapi.json")
    assert isinstance(spec, dict)
    paths = spec.get("paths", {})
    assert isinstance(paths, dict)

    required_paths = {
        "/api/v1/admin/feedback": "运营反馈聚合",
        "/api/v1/admin/content-gaps": "内容缺口榜",
        "/api/v1/admin/models": "型号后台 CRUD",
        "/api/v1/knowledge/citations/{document_sha256}/pages/{page_number}": "按页取原件",
    }
    for path, label in required_paths.items():
        require(path in paths, f"后端镜像是旧的：openapi 缺少 {path}（{label} 未上线）")
    require(
        "post" in paths.get("/api/v1/admin/models", {}),
        "后端镜像是旧的：/admin/models 没有 POST（型号创建未上线）",
    )
    checks.append(f"backend openapi exposes all {len(required_paths)} endpoints of this round")

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:8010/api/v1",
        help="运行中后端的 API 根地址",
    )
    args = parser.parse_args()

    passed: list[str] = []
    try:
        passed += check_frontend_assets()
        passed += check_forwarded_chain()
        passed += check_backend_contract(args.backend_url.rstrip("/").removesuffix("/api/v1"))
    except CheckFailed as exc:
        for line in passed:
            print(f"  ok  {line}")
        print(f"FAIL  {exc}", file=sys.stderr)
        print(
            "\n修复：docker compose build frontend backend && docker compose up -d",
            file=sys.stderr,
        )
        return 1

    for line in passed:
        print(f"  ok  {line}")
    print(f"running stack verified: {len(passed)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
