"""Static deployment contract checks that do not require a Docker daemon."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


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
        backend_env["ROBOTCARE_REFRESH_COOKIE_SECURE"]
        == "${ROBOTCARE_REFRESH_COOKIE_SECURE:-true}",
        "refresh cookies must default to Secure",
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
    for token in (
        "location = /healthz",
        "client_max_body_size 6m",
    ):
        require(token in nginx, f"Nginx config missing: {token}")
    require(
        "location = /backend-healthz" not in nginx,
        "detailed backend readiness must not be exposed through public Nginx",
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
        "test_postgres_integration.py",
        "postgres_integration_smoke.py",
        "audit_eval_dataset.py",
        "npm run test",
        "npm run build",
        "docker compose config --quiet",
    ):
        require(token in ci, f"CI deployment contract missing: {token}")

    print("deployment configuration: static contract passed")


if __name__ == "__main__":
    main()
