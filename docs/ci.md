# 持续集成（CI）

## 状态边界

- `【已验证：静态配置】` 仓库已配置 GitHub Actions 工作流 `.github/workflows/ci.yml`，包含 SQLite 后端回归、PostgreSQL 16 + pgvector 集成、前端测试/构建、Playwright Chromium E2E、部署配置契约和知识数据检查。
- `【待验证：远端运行】` 只有 GitHub Actions 页面出现对应提交的真实成功记录后，才能将该提交的 CI 运行状态改记为 `【已验证】`。
- 本文不声称 GitHub Actions 已经运行，也不把本地命令成功等同于远端 CI 成功。

## CI 检查项

### Backend

`【已验证：工作流配置】` SQLite job 使用 Ubuntu runner 和 Python 3.13，在 `backend` 目录安装当前项目及 `test` 可选依赖，然后运行 pytest：

```powershell
Set-Location backend
python -m pip install -e ".[test]"
python -m pytest
```

本地完整回归的当前证据为 `171 passed, 1 skipped`；包含登录时序防护、三桶认证限流、业务/Embedding 配额、敏感读取审计、数据库状态约束、全状态合成数据及 Alembic `20260722_0007`。唯一跳过项需要 `ROBOTCARE_TEST_POSTGRES_URL` 和显式破坏性测试开关。

### Backend PostgreSQL + pgvector

`【已验证：工作流配置】` `backend-postgres` job 使用 `pgvector/pgvector:pg16` service container，安装 `.[test,postgres]`，先执行 Alembic，再运行真实数据库测试和 smoke 脚本：

```bash
cd backend
python -m pip install -e ".[test,postgres]"
alembic upgrade head
python -m pytest tests/test_postgres_integration.py -v
python ../scripts/postgres_integration_smoke.py
```

该 job 计划验证：`vector` 扩展、`vector(256)` 字段、HNSW 索引、数据库 Top-K、型号隔离、Alembic head、应用 `/ready`、种子型号以及附件/报告目录可写。`【待验证】` 工作流尚未在 GitHub Actions 实际运行，所以不能将这些运行结果记为已通过。

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

`【已验证：本机 Edge】` Playwright 使用系统安装的 Microsoft Edge，在隔离 FastAPI、Vite 和 SQLite 测试库上完成 8 条关键链路：

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

E2E 包装脚本直接管理 Uvicorn/Vite 子进程；正常通过和故意失败路径均已验证释放隔离端口。测试仅用 `/health` 等待进程启动，并由测试配置显式自动建表；这不替代生产 Alembic revision 门禁，也不证明 PostgreSQL、Docker、HTTPS 或多浏览器兼容。`【已验证：工作流配置】` CI 另配置 Chromium E2E 和失败证据上传；`【待验证：远端运行】` 尚无 GitHub Actions 成功记录。

### Knowledge

`【已验证：工作流配置】` 使用 Python 3.13 和标准库解析以下文件：

- `knowledge/sources.json`：`sources` 必须是数组，条数至少为 4。
- `knowledge/diagnostic_flows.json`：`flows` 必须是数组，条数至少为 4。
- `knowledge/eval_cases.jsonl`：每个非空行必须是一个 JSON 对象，条数至少为 100；当前为 113 条。

工作流随后执行 `backend/scripts/audit_eval_dataset.py`。当前本地报告为 `docs/evidence/rag_eval_offline_audit.json/.md`：`safety_block 15/15`、`source_page 14/14`、`step_selection 14/14`；`model_isolation`、`retrieval_recall`、`refusal`、`classification`、`faithfulness` 均为 `not_run`。

结构与离线审计通过不代表完整 RAG 评测通过，也不代表人工审核。当前 113 条全部标记为 `needs_human_review`，CI 不得自动把它们改写成已审核。

## 缓存建议

- Python 使用 `actions/setup-python` 的 pip 缓存，缓存键由 `backend/pyproject.toml` 派生。
- Node.js 使用 `actions/setup-node` 的 npm 缓存，缓存键由 `frontend/package-lock.json` 派生。
- 不缓存 `frontend/node_modules`、构建产物或 Python 虚拟环境；这些目录跨 runner 或依赖版本复用容易产生不可重复结果。
- 如果未来引入独立锁文件，应让缓存键以锁文件为准，而不是只依赖宽范围版本声明。

## Docker 验证边界

- `【已验证：静态契约】` `scripts/verify_deployment_config.py` 检查 pgvector 镜像、`/ready` 健康检查、持久卷、启动 Alembic、外部密钥、认证生产门禁、前后端回环绑定、Noto CJK 字体、Nginx 安全/健康端点和 CI 必需任务；CI 的 deployment-config job 还计划执行 `docker compose config --quiet`。
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

在专用测试数据库中执行以下命令；测试 URL 必须包含 `robotcare_test`，因为集成用例会 downgrade/upgrade 数据库：

```powershell
cd D:\个人项目\robotcare-ai\backend
$env:ROBOTCARE_TEST_POSTGRES_URL='postgresql+psycopg://robotcare:<password>@127.0.0.1:5432/robotcare_test'
$env:ROBOTCARE_DATABASE_URL=$env:ROBOTCARE_TEST_POSTGRES_URL
$env:ROBOTCARE_ALLOW_DESTRUCTIVE_POSTGRES_TESTS='1'
$env:ROBOTCARE_JWT_SECRET='<test-only-secret>'
$env:ROBOTCARE_ATTACHMENT_DIR="$env:TEMP\robotcare-attachments"
$env:ROBOTCARE_REPORT_DIR="$env:TEMP\robotcare-reports"
alembic upgrade head
python -m pytest tests/test_postgres_integration.py -v
python ..\scripts\postgres_integration_smoke.py
```

Docker 验收还应执行 `docker compose config --quiet`、`docker compose build`、`docker compose up -d`、`docker compose ps`，并分别请求回环地址上的后端 `/health`、`/ready` 与前端 `/healthz`；前端容器健康检查应从容器网络直连 `backend:8000/ready`，公共 Nginx 不暴露详细 readiness。随后验证 HTTPS 反代与容器重建后数据仍保留。远端 CI 验收必须记录提交 SHA、workflow URL、各 job 结论和失败日志；没有这些证据时保持【待验证】。
