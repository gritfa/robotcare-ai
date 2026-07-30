# Docker 实际部署验证（2026-07-30，macOS 本机）

环境：Docker Engine 29.5.2 / macOS（Apple Silicon）。此前 README 标注【待验证：本机没有 Docker】，本轮在真实 Docker 上完成构建、启动、迁移、健康检查与重建持久化验证。

## 执行与结果

1. `docker compose build`：backend + frontend 镜像构建成功。
2. 生产门禁实测：`.env` 设 `ROBOTCARE_ENVIRONMENT=production` 且无 DashScope key 时，
   backend 启动即被 Settings 校验拒绝（`ROBOTCARE_DASHSCOPE_API_KEY is required in production`），
   容器 unhealthy——**门禁按设计生效，不会静默降级启动**。
3. 本机无 DashScope key，改 `development` 完成其余验证（持有 key 的机器可直接 production 启动）：
   `docker compose up -d` 后 postgres/backend/frontend 全部 healthy。
4. 容器内 `GET /ready`（原文摘录）：
   `{"status":"ready", "components":{"database":{"status":"ok"}, "alembic":{"at_head":true,
   "current_revision":"20260730_0008"}, "attachments":{"writable":true}, "reports":{"writable":true}}}`
5. 启动迁移：容器首次启动自动 `alembic upgrade head`，`alembic_version=20260730_0008`；
   种子完成（robot_models=5：2 真实 + 3 合成演示）；`pg_extension` 含 `vector`。
6. 前端 `GET /healthz` 返回 `ok`；前端健康检查同时校验 `backend:8000/ready`。
7. **重建持久化**：插入标记行 → `docker compose down`（容器与网络全部移除）→
   `up -d` 重建 → 标记行仍在（`persist-check-20260730`），三服务重新 healthy。
8. 宿主端口说明：本机 127.0.0.1:8000 被无关进程占用，验证经容器内探测完成；
   部署机需确认 `BACKEND_PORT`/`FRONTEND_PORT` 未被占用或在 `.env` 中改端口。

## 未执行（如实声明）

- production 模式 + 真实 DashScope key 的完整启动（需持 key 机器执行）。
- HTTPS 反向代理、公网暴露、多实例——超出单机 Compose 范围。
