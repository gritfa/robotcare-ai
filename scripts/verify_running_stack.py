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
