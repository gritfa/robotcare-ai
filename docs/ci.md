# 持续集成（CI）

## 状态边界

- `【已验证：远端运行，历史提交】` 提交 `1bccb82` 的 [GitHub Actions CI](https://github.com/gritfa/robotcare-ai/actions/runs/30235510993) 已完成并全绿；那是双方言时期的工作流（SQLite 回归 + 独立 PG 集成共六个 Job）。
- `【待验证：远端运行】` PostgreSQL 单方言重构后工作流改为五个 Job：PG 回归（全量 pytest + 迁移冒烟）、前端、Chromium E2E（PG service）、部署契约、知识校验（PG service 跑离线审计）。改造后的工作流配置已静态通过部署契约检查，但尚未在远端跑过。
- 远端结果只证明对应提交和 CI service 环境，不自动证明本机 Docker、生产部署、多实例压力、HTTPS 或真实用户负载。

## CI 检查项

### Backend

`【已验证：工作流配置 + 本地运行】` 单方言：`backend-postgres` job 使用 `pgvector/pgvector:pg16` service container（库名 `robotcare_test`），通过 `ROBOTCARE_TEST_DATABASE_URL` 注入连接串，一次 pytest 覆盖全部单元、API、迁移与 pgvector 集成测试（迁移测试自建 `robotcare_migration` 库，离线评测自建 `robotcare_eval` 库），随后在独立 `robotcare_smoke` 库执行 Alembic + 部署冒烟脚本：

```bash
cd backend
python -m pip install -e ".[test]"
python -m pytest                                  # 全量回归（PG 单方言）
# 之后：CREATE DATABASE robotcare_smoke → alembic upgrade head → postgres_integration_smoke.py
```

本地完整回归的当前证据为 `180 passed, 0 skipped (29.7s)`，全部跑在 PostgreSQL 16 + pgvector 测试容器（`127.0.0.1:55433`）上；原 `ROBOTCARE_TEST_POSTGRES_URL` + 破坏性开关的 skip 机制已删除，PG 集成测试成为常规回归的一部分。原 8 条 SQLite 无效状态参数化用例与 PG 集成测试中相同的 8 条无效 UPDATE 断言合并，测试总数由 188 变为 180，无覆盖损失。

### Frontend

`【已验证：工作流配置】` 使用 Ubuntu runner 和 Node.js 24，严格按照 `package-lock.json` 安装、测试并构建：

```powershell
Set-Location frontend
npm ci
npm run test
npm run build
```

`npm ci` 要求 `package.json` 与 `package-lock.json` 保持同步。构建失败时不应改用 `npm install` 掩盖锁文件不一致。

本地当前证据为 Vitest `36 passed`，`vue-tsc` 与 Vite production build 通过，1706 个模块完成转换。新增结构化 429 解析和附件失败队列重试。

### Browser E2E

`【已验证：本机 Edge，SQLite 时期证据】` Playwright 使用系统安装的 Microsoft Edge，在隔离 FastAPI、Vite 和当时的 SQLite 测试库上完成 8 条关键链路。**单方言重构后 E2E 已改为 PostgreSQL**：`run-e2e.mjs` 启动前用 `ROBOTCARE_E2E_DATABASE_URL`（本机默认 `postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_e2e`，CI 用 workflow service）自动创建并清空 E2E 库再拉起后端；后端在该 PG 库上的启动与 `/health` 已本地冒烟通过，但改造后的完整 8 条 Playwright 链路尚未在本机重新执行，远端 Chromium E2E 也尚未在新工作流下跑过。历史 8 条链路为：

- 邀请码注册 → 添加设备 → 单步诊断全部未解决 → 生成并下载 PDF。
- 诊断过程中刷新页面后恢复当前步骤。
- 第二用户不能读取第一用户诊断，普通用户不能进入管理员页面。
- 分类明显冲突时展示候选，用户改选后成功创建正确流程。
- 高风险诊断显示持久安全卡片；PDF 下载验证存在、非空和 `%PDF-`。
- 注册、附件上传以及报告/PDF 超限显示结构化等待时间；附件失败项保留并可重试。

```powershell
Set-Location frontend
npm run test:e2e:edge
# 8 passed (44.9s)，进程正常以 0 退出
```

E2E 包装脚本直接管理 Uvicorn/Vite 子进程；正常通过和故意失败路径均已验证释放隔离端口。测试仅用 `/health` 等待进程启动，并由测试配置显式自动建表（PG 库上 `auto_create_schema=true`，无 Alembic 戳，因此 `/ready` 在 E2E 环境保持 503 属预期）；这不替代生产 Alembic revision 门禁，也不证明 Docker、HTTPS 或多浏览器兼容。Chromium E2E 在旧工作流（SQLite）下远端成功过；新 PG 工作流下为 `【待验证】`。

### Knowledge

`【已验证：工作流配置】` 使用 Python 3.13 和标准库解析以下文件：

- `knowledge/sources.json`：`sources` 必须是数组，条数至少为 4。
- `knowledge/diagnostic_flows.json`：`flows` 必须是数组，条数至少为 4。
- `knowledge/eval_cases.jsonl`：每个非空行必须是一个 JSON 对象，条数至少为 100；当前为 113 条。

工作流随后执行 `backend/scripts/audit_eval_dataset.py`（knowledge job 现挂 `pgvector/pgvector:pg16` service 并安装后端依赖：检索类指标在脚本自建的 `robotcare_eval` PG 库上真实执行）。当前本地报告为 `docs/evidence/rag_eval_offline_audit.json/.md`：`overall_status=passed_with_not_run_metrics`，七个可离线执行指标全部 `passed` 且分数 1.0，仅 `faithfulness` 保持 `not_run`。

结构与离线审计通过不代表完整 RAG 评测通过，也不代表人工审核。当前 113 条全部标记为 `needs_human_review`，CI 不得自动把它们改写成已审核。

## 缓存建议

- Python 使用 `actions/setup-python` 的 pip 缓存，缓存键由 `backend/pyproject.toml` 派生。
- Node.js 使用 `actions/setup-node` 的 npm 缓存，缓存键由 `frontend/package-lock.json` 派生。
- 不缓存 `frontend/node_modules`、构建产物或 Python 虚拟环境；这些目录跨 runner 或依赖版本复用容易产生不可重复结果。
- 如果未来引入独立锁文件，应让缓存键以锁文件为准，而不是只依赖宽范围版本声明。

## Docker 验证边界

- `【已验证：静态契约与远端 CI】` `scripts/verify_deployment_config.py` 检查 pgvector 镜像、`/ready` 健康检查、持久卷、启动 Alembic、外部密钥、认证生产门禁、前后端回环绑定、Noto CJK 字体、Nginx 安全/健康端点和 CI 必需任务；远端 deployment-config Job 已成功执行 `docker compose config --quiet`。
- `【待验证】` 当前 CI 不执行 `docker compose build`、容器启动或容器端到端测试；本机也没有 Docker 可供运行。
- Dockerfile 和 `docker-compose.yml` 的存在不等于镜像能够构建或服务能够正常启动。
- 在 GitHub Actions 增加真实 Docker job，并保存对应提交的构建与健康检查证据之前，不得将 Docker 部署标为 `【已验证】`。
- 后续 Docker 验证至少应覆盖镜像构建、PostgreSQL 健康检查、数据库迁移、后端健康接口和前端静态资源访问。
- `【已验证：配置语义】` Compose 默认 `ROBOTCARE_ENVIRONMENT=production`、`ROBOTCARE_AUTO_CREATE_SCHEMA=false`、`ROBOTCARE_REFRESH_COOKIE_SECURE=true`，只能在 HTTPS 反向代理后用于实际部署。纯 HTTP 本地调试必须使用 `.env.compose-local.example` 覆盖为 development/secure=false，且不得将该文件作为生产环境模板。

本地静态契约验证命令与已记录结果：

```powershell
cd D:\个人项目\robotcare-ai
python scripts\verify_deployment_config.py
# deployment configuration: static contract passed
```

该结果只读取并校验配置文本，不调用 Docker daemon；部署脚本本身的 py_compile 也已通过，但仍不是容器运行证据。

## 本地/远端验收清单

在专用测试数据库容器上执行完整回归（禁止指向开发库或生产库；迁移用例会在同服务器自建 `robotcare_migration` 库执行 downgrade/upgrade）：

```bash
docker run -d --name robotcare-test-pg -p 55433:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=robotcare_test \
  pgvector/pgvector:pg16
cd backend
python -m pip install -e ".[test]"
python -m pytest        # 缺省连 postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_test
```

远端 CI 已记录提交 `1bccb82`、workflow URL 和六个成功 Job。Docker 验收仍应执行 `docker compose build`、`docker compose up -d`、`docker compose ps`，并分别请求回环地址上的后端 `/health`、`/ready` 与前端 `/healthz`；随后验证 HTTPS 反代与容器重建后数据仍保留。没有这些证据时，Docker 实际部署保持【待验证】。
