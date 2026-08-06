"""Static deployment contract checks that do not require a Docker daemon."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def settings_field_names() -> list[str]:
    """静态解析 Settings 的字段名（用 ast 而非 import，避免依赖后端运行时环境）。"""
    tree = ast.parse((ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8"))
    settings_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    return [
        node.target.id
        for node in settings_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]


def _int_expression(node: ast.AST) -> int:
    """求值形如 30 * 1024 * 1024 的常量算式。

    literal_eval 不接受 BinOp，而这类上限常量在仓库里就是写成乘法的；
    只支持乘法与整数字面量，不引入 eval。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _int_expression(node.left) * _int_expression(node.right)
    raise AssertionError(f"unsupported constant expression: {ast.dump(node)}")


def module_constant(path: Path, name: str) -> int:
    """静态解析某个模块里的整数常量（含 30 * 1024 * 1024 这类算式）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return _int_expression(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


def config_default(field: str) -> float:
    """取 Settings 中某个数值字段的默认值（支持 Field(default=...) 与裸字面量）。"""
    tree = ast.parse((ROOT / "backend" / "app" / "config.py").read_text(encoding="utf-8"))
    settings_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    for node in settings_class.body:
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue
        if node.target.id != field or node.value is None:
            continue
        if isinstance(node.value, ast.Constant):
            return float(node.value.value)
        if isinstance(node.value, ast.Call):
            for keyword in node.value.keywords:
                if keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                    return float(keyword.value.value)
    raise AssertionError(f"Settings.{field} has no readable numeric default")


def main() -> None:
    compose_path = ROOT / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    postgres = services["postgres"]
    require(postgres["image"].startswith("pgvector/pgvector:pg"), "pgvector image missing")
    require("healthcheck" in postgres, "PostgreSQL healthcheck missing")
    require(
        "postgres_data:/var/lib/postgresql/data" in postgres["volumes"],
        "PostgreSQL persistent volume missing",
    )
    require(
        str(postgres["environment"]["POSTGRES_PASSWORD"]).startswith("${POSTGRES_PASSWORD:?"),
        "PostgreSQL password must be injected externally",
    )

    backend = services["backend"]
    backend_env = backend["environment"]
    require(backend["build"]["context"] == ".", "backend build must include knowledge data")
    require(
        str(backend_env["ROBOTCARE_JWT_SECRET"]).startswith("${ROBOTCARE_JWT_SECRET:?"),
        "production JWT secret must not be fixed in Compose",
    )
    require(
        str(backend_env["ROBOTCARE_DATABASE_URL"]).startswith("${ROBOTCARE_DATABASE_URL:?"),
        "database URL must be injected externally",
    )
    require(
        backend_env["ROBOTCARE_DASHSCOPE_API_KEY"] == "${ROBOTCARE_DASHSCOPE_API_KEY:-}",
        "DashScope key must be injected externally",
    )
    require(backend_env["ROBOTCARE_AUTO_CREATE_SCHEMA"] == "false", "Alembic must own schema")
    require(
        backend_env["ROBOTCARE_ENVIRONMENT"] == "${ROBOTCARE_ENVIRONMENT:-production}",
        "Compose must default to the production settings gate",
    )
    require(
        backend_env["ROBOTCARE_REGISTRATION_MODE"]
        == "${ROBOTCARE_REGISTRATION_MODE:-invite}",
        "Compose must default to invite-only registration",
    )
    require(
        backend_env["ROBOTCARE_REGISTRATION_INVITE_SECRET"]
        == "${ROBOTCARE_REGISTRATION_INVITE_SECRET:-}",
        "registration invite secret must come from the external environment",
    )
    require(
        backend_env["ROBOTCARE_DASHSCOPE_API_KEY"]
        == "${ROBOTCARE_DASHSCOPE_API_KEY:-}",
        "embedding key must come from the external environment",
    )
    require(
        backend_env["ROBOTCARE_KNOWLEDGE_MIN_SCORE"]
        == "${ROBOTCARE_KNOWLEDGE_MIN_SCORE:-0.25}",
        "knowledge threshold must be controlled by the server environment",
    )
    require(
        any(
            isinstance(item, str) and ":/knowledge-release:ro" in item
            for item in backend["volumes"]
        ),
        "knowledge release source must be mounted read-only",
    )
    require(
        backend_env["ROBOTCARE_REFRESH_COOKIE_SECURE"]
        == "${ROBOTCARE_REFRESH_COOKIE_SECURE:-true}",
        "refresh cookies must default to Secure",
    )
    require(
        backend_env["ROBOTCARE_TRUSTED_PROXY_CIDRS"]
        == "${ROBOTCARE_TRUSTED_PROXY_CIDRS:-172.30.0.10/32}",
        "backend must trust only the pinned Nginx proxy address by default",
    )
    require(
        backend_env["ROBOTCARE_LOGIN_EMAIL_MAX_FAILURES"]
        == "${ROBOTCARE_LOGIN_EMAIL_MAX_FAILURES:-20}",
        "account-wide login threshold missing",
    )
    require(
        backend_env["ROBOTCARE_LOGIN_IP_MAX_FAILURES"]
        == "${ROBOTCARE_LOGIN_IP_MAX_FAILURES:-30}",
        "independent IP login threshold missing",
    )
    rate_limit_defaults = {
        "ROBOTCARE_REGISTRATION_EMAIL_PER_MINUTE": "3",
        "ROBOTCARE_REGISTRATION_IP_PER_MINUTE": "10",
        "ROBOTCARE_KNOWLEDGE_SEARCH_USER_PER_MINUTE": "20",
        "ROBOTCARE_KNOWLEDGE_SEARCH_IP_PER_MINUTE": "60",
        "ROBOTCARE_DIAGNOSTIC_CREATE_USER_PER_MINUTE": "10",
        "ROBOTCARE_DIAGNOSTIC_CREATE_IP_PER_MINUTE": "30",
        "ROBOTCARE_ATTACHMENT_UPLOAD_USER_PER_MINUTE": "20",
        "ROBOTCARE_ATTACHMENT_UPLOAD_IP_PER_MINUTE": "60",
        "ROBOTCARE_REPORT_CREATE_USER_PER_MINUTE": "5",
        "ROBOTCARE_REPORT_CREATE_IP_PER_MINUTE": "15",
        "ROBOTCARE_PDF_CREATE_USER_PER_MINUTE": "5",
        "ROBOTCARE_PDF_CREATE_IP_PER_MINUTE": "15",
        "ROBOTCARE_EMBEDDING_USER_PER_MINUTE": "10",
        "ROBOTCARE_EMBEDDING_IP_PER_MINUTE": "30",
        "ROBOTCARE_EMBEDDING_USER_PER_DAY": "100",
        "ROBOTCARE_EMBEDDING_IP_PER_DAY": "300",
        "ROBOTCARE_KNOWLEDGE_SEARCH_CACHE_TTL_SECONDS": "30",
    }
    for name, default in rate_limit_defaults.items():
        require(
            backend_env[name] == f"${{{name}:-{default}}}",
            f"business rate-limit setting missing: {name}",
        )
    # 全量透传契约：Settings 里的每个配置项都必须在 Compose 中出现。
    # 容器内没有 .env（根 .dockerignore 排掉了），漏一个就是"该配置在 Docker
    # 部署下永久锁死为默认值"，且不会报任何错。2026-08-05 上线体检发现
    # LLM_BACKEND / ALERT_WEBHOOK_URL 等 18 项处于这种静默失效状态，
    # 而同类问题在 generation_service 注释里刚修过一次——所以改成全量断言，
    # 让"新增配置项忘记透传"在 CI 阶段就失败。
    composed_env_names = set(backend_env)
    untransmitted = [
        f"ROBOTCARE_{name.upper()}"
        for name in settings_field_names()
        if f"ROBOTCARE_{name.upper()}" not in composed_env_names
    ]
    require(
        not untransmitted,
        "Compose must pass through every Settings field (container has no .env); "
        f"missing: {', '.join(untransmitted)}",
    )

    # 模板必须覆盖全部配置项，否则运维照着 .env.example 部署就会漏配
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    undocumented = [
        f"ROBOTCARE_{name.upper()}"
        for name in settings_field_names()
        if f"ROBOTCARE_{name.upper()}" not in env_example
    ]
    require(
        not undocumented,
        f".env.example must document every Settings field; missing: {', '.join(undocumented)}",
    )

    # 真密钥文件不得对同机其他用户可读（2026-08-05 体检：0644 明文含真 key）
    env_file = ROOT / ".env"
    if env_file.exists():
        mode = env_file.stat().st_mode & 0o077
        require(
            mode == 0,
            f".env holds live secrets and must not be group/world readable "
            f"(found mode {oct(env_file.stat().st_mode & 0o777)}, expected 0600)",
        )

    # 外部模型总预算必须小于反向代理的读超时，否则用户已收到 504、
    # 后端仍在跑并照常计费（2026-08-05 体检：旧值 180s vs 反代 60s）
    nginx_conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    proxy_read_timeout = re.search(r"proxy_read_timeout\s+(\d+)s", nginx_conf)
    require(proxy_read_timeout is not None, "Nginx proxy_read_timeout must be pinned")
    proxy_seconds = float(proxy_read_timeout.group(1))
    for field in ("llm_budget_seconds", "embedding_budget_seconds"):
        require(
            config_default(field) < proxy_seconds,
            f"Settings.{field} default must stay below Nginx proxy_read_timeout "
            f"({proxy_seconds:g}s), otherwise the client gets a 504 while the "
            "backend keeps burning tokens",
        )

    # 流式的最坏耗时是 budget + read，不是 budget：越界只能在拿到一块之后
    # 发现，阻塞在读上是打断不了的（见 llm_transport.stream_with_budget）。
    # 反代必须给到这个上界，否则预算闸永远轮不到生效（2026-08-06 体检 #4）。
    worst_case_stream = config_default("llm_budget_seconds") + config_default(
        "llm_read_timeout_seconds"
    )
    require(
        worst_case_stream < proxy_seconds,
        f"streaming worst case (llm_budget + llm_read = {worst_case_stream:g}s) must stay "
        f"below Nginx proxy_read_timeout ({proxy_seconds:g}s), otherwise the reverse proxy "
        "cuts the user off before the backend's own budget gate can fire",
    )

    require(
        "127.0.0.1:${BACKEND_PORT:-8000}:8000" in backend["ports"],
        "backend must bind to host loopback instead of a public interface",
    )
    require("backend_data:/app/data" in backend["volumes"], "backend files are not persistent")
    require("healthcheck" in backend, "backend healthcheck missing")
    require(
        backend["depends_on"]["postgres"]["condition"] == "service_healthy",
        "backend must wait for healthy PostgreSQL",
    )

    frontend = services["frontend"]
    require("healthcheck" in frontend, "frontend proxy healthcheck missing")
    require(
        "127.0.0.1:${FRONTEND_PORT:-5173}:80" in frontend["ports"],
        "frontend must bind to host loopback instead of bypassing HTTPS",
    )
    require(
        frontend["depends_on"]["backend"]["condition"] == "service_healthy",
        "frontend must wait for a healthy backend",
    )

    backend_dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    for token in (
        "fonts-noto-cjk",
        "backend/alembic.ini",
        "COPY backend/migrations",
        "COPY knowledge /app/knowledge",
        "ENTRYPOINT",
        "HEALTHCHECK",
        "http://127.0.0.1:8000/ready",
    ):
        require(token in backend_dockerfile, f"backend Dockerfile missing: {token}")

    entrypoint = (ROOT / "backend" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    require("alembic upgrade head" in entrypoint, "container startup migration missing")
    require('exec "$@"' in entrypoint, "entrypoint must exec the server process")

    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    for token in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "frame-ancestors 'none'",
    ):
        require(token in nginx, f"Nginx security header missing: {token}")
    require("location = /healthz" in nginx, "Nginx config missing: location = /healthz")

    # 上传上限必须与后端交叉校验，不能各写各的（2026-08-06 体检 #5：
    # nginx 6m vs 后端 30MB，27MB 说明书永远传不进去，而当时的断言只检查
    # "存在 client_max_body_size 6m 这一行"，对不一致完全免疫）。
    body_size = re.search(r"client_max_body_size\s+(\d+)m", nginx)
    require(body_size is not None, "Nginx client_max_body_size must be pinned in megabytes")
    nginx_upload_bytes = int(body_size.group(1)) * 1024 * 1024
    backend_upload_bytes = module_constant(
        ROOT / "backend" / "app" / "routers" / "admin.py", "MAX_KNOWLEDGE_PDF_BYTES"
    )
    require(
        backend_upload_bytes <= nginx_upload_bytes,
        f"backend accepts uploads up to {backend_upload_bytes / 1048576:g}MB but Nginx caps the "
        f"body at {nginx_upload_bytes / 1048576:g}MB — the reverse proxy would reject them with "
        "an HTML 413 the frontend cannot parse",
    )
    require(
        "location = /backend-healthz" not in nginx,
        "detailed backend readiness must not be exposed through public Nginx",
    )
    require(
        "proxy_set_header X-Forwarded-For $remote_addr;" in nginx,
        "Nginx must overwrite caller-controlled forwarded addresses",
    )
    require(
        "$proxy_add_x_forwarded_for" not in nginx,
        "single-hop Nginx must not append an untrusted forwarded chain",
    )
    internal_network = compose["networks"]["robotcare_internal"]
    require(
        internal_network["ipam"]["config"][0]["subnet"] == "172.30.0.0/24",
        "Compose trusted-proxy network must use its reviewed subnet",
    )
    require(
        frontend["networks"]["robotcare_internal"]["ipv4_address"]
        == "172.30.0.10",
        "Nginx must keep the address trusted by the backend",
    )
    frontend_dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    require(
        "http://backend:8000/ready" in frontend_dockerfile,
        "frontend container healthcheck must verify backend readiness internally",
    )

    root_dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    frontend_dockerignore = (ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8")
    for token in ("backend/.venv", "backend/data", "frontend", "knowledge/raw"):
        require(token in root_dockerignore, f"root .dockerignore missing: {token}")
    for token in ("node_modules", "dist", ".npm-cache"):
        require(token in frontend_dockerignore, f"frontend .dockerignore missing: {token}")

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for token in (
        "pgvector/pgvector:pg16",
        # 单方言：PG 集成测试并入完整 pytest；契约改为检查测试库注入与冒烟脚本。
        "ROBOTCARE_TEST_DATABASE_URL",
        "postgres_integration_smoke.py",
        "audit_eval_dataset.py",
        "npm run test",
        "npm run build",
        "npm run test:e2e",
        "playwright install --with-deps chromium",
        "docker compose config --quiet",
    ):
        require(token in ci, f"CI deployment contract missing: {token}")

    print("deployment configuration: static contract passed")


if __name__ == "__main__":
    main()
