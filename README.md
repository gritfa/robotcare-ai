# RobotCare AI

独立第三方海尔扫地机器人使用指导与安全故障排查平台。

> 状态说明：本仓库正在执行分阶段工程化改造。本文只把经过真实命令验证的能力标记为 `【已验证】`；其余内容均标记为 `【计划】` 或 `【待验证】`。

## 当前状态

| 子系统 | 状态 | 说明 |
| --- | --- | --- |
| FastAPI 后端 | `【已验证】` | 分类冲突、知识安全/健康、并发报告/附件、认证及既有主链路全量回归为 `137 passed, 1 skipped`；唯一跳过项是真实 PostgreSQL 集成测试 |
| Vue 3 前端 | `【已验证】` | 用户主链路、结构化错误、分类确认、安全卡片、知识健康及认证恢复共 `33 passed`；生产构建通过，Vite 转换 1705 个模块 |
| Microsoft Edge E2E | `【已验证：本机关键链路】` | 本机真实 Edge 完成原 3 条闭环，并新增分类冲突改选和持久安全阻断，共 `5 passed (38.4s)`；命令以 0 正常退出，隔离 SQLite 不代表 PostgreSQL/Docker/HTTPS |
| 分类与流程一致性 | `【已验证】` | 型号级确定性关键词/错误码候选；明显冲突拒绝创建，模糊场景要求用户确认；用户选择、系统候选和最终类别写入会话与报告 |
| 知识安全与业务健康 | `【已验证：本地】` | 高风险检索在 Embedding 前阻断；普通用户不能覆盖阈值；`/knowledge/health` 区分正常、知识降级和外部模型不可用；版本化发布清单支持 SHA256 校验、幂等与事务回滚 |
| 认证生命周期 | `【已验证】` | HttpOnly 刷新 Cookie、访问令牌会话绑定、轮换/重放撤销、自然并发宽限、退出失效、用户状态和原子数据库限流均有后端测试；前端覆盖同标签单飞、跨标签 Web Lock 与冲突重试 |
| 可观测性基线 | `【已验证】` | `X-Request-ID`、JSON 请求/领域事件日志、递归脱敏、带 `trace_id` 的安全错误响应以及数据库/Alembic/存储就绪检查均通过测试；尚未接入外部日志、告警或 OpenTelemetry |
| 高风险输入阻断 | `【已验证】` | 诊断和知识检索共享确定性规则；覆盖主要高风险类别和“没有冒烟”等否定语义；前端使用持久安全卡片而非仅 Toast |
| 反馈幂等与并发 | `【已验证】` | 请求携带当前 `step_id`；重复、旧步骤和两请求并发只允许一条执行记录，冲突返回 409 |
| 图片上传安全 | `【已验证】` | Pillow 真实解码及既有限制不变；SQLite 进程锁/PostgreSQL 会话行锁保证并发上传最多 5 张，失败不遗留数据库记录或孤立文件 |
| 官方说明书证据 | `【已验证】` | JH69U1、VC35U1 官方 PDF 已下载、校验 SHA256、按页解析并完成指定页面目视检查；原始 PDF 不提交公开仓库 |
| 诊断流程发布 | `【已验证】` | JSON 是唯一种子来源；4 条逐步骤说明书复核流程已发布，1 条导航流程因因果证据不足保持草稿且普通用户不可调用 |
| 流程版本与迁移 | `【已验证】` | stable_key + version + draft/published/retired；发布版本不可原地修改；Alembic head `20260720_0004` 新增分类决策证据并通过零漂移检查 |
| 型号级向量检索基线 | `【已验证】` | 两份说明书已通过 DashScope `text-embedding-v4` 入库：JH69U1 31 个向量、VC35U1 22 个向量；5 条真实检索冒烟用例通过 |
| RAG/安全评测数据与离线审计 | `【已验证】` | 已形成 113 条分项用例；离线实际执行 `safety_block 15/15`、`source_page 14/14`、`step_selection 14/14`，JSON/Markdown 报告已保存；113 条均为 `needs_human_review` |
| 完整 RAG 在线评测 | `【计划】` | `model_isolation`、`retrieval_recall`、`refusal`、`classification`、`faithfulness` 均为 `not_run`；当前不生成自然语言答案，也不能称评测集已人工审核 |
| PostgreSQL + pgvector 实现 | `【已验证】` | 双方言 256 维字段、无列 CAST 的数据库 Top-K SQL、型号过滤、HNSW 迁移和知识替换失败回滚已通过单元、静态编译与 SQLite 回归 |
| PostgreSQL + pgvector 真实运行 | `【待验证】` | 集成测试与 smoke 脚本已编写，但本机无 Docker/psql，远端 CI 尚未运行，不能声称 PostgreSQL 迁移、索引命中或运行健康已完成 |
| PDF 售后报告 | `【已验证】` | 并发请求返回同一报告；PDF 采用目标锁、临时文件和原子替换，验证 `%PDF-`/`%%EOF`；Edge 下载校验文件存在、非空和文件头 |
| 管理员后端与审计 | `【已验证】` | 后端 RBAC、`AuditLog`、7 个管理员接口、安全管理员 CLI、型号停用语义均通过单元/API/迁移测试 |
| 管理员前端 | `【已验证】` | 真实 API 驱动的运营概览、型号启停、知识健康、安全阻断、未解决报告和审计日志页面已通过单元测试与生产构建；本机 Edge 已验证普通用户访问管理员页面被拒绝，管理员内容运营 E2E 仍为【计划】 |
| 管理员完整内容运营 | `【计划】` | 知识上传/重建/停用、流程审核发布、评测执行与结果持久化尚未实现，不能称管理后台全部完成 |
| Docker 静态部署契约 | `【已验证】` | 部署检查脚本执行与 py_compile 通过；静态检查覆盖启动迁移、外部密钥、认证生产门禁、PostgreSQL/附件/报告持久卷、`/ready` 健康检查和 Noto CJK 字体配置 |
| Docker 实际部署 | `【待验证】` | 本机没有 Docker，尚未实际构建镜像、启动 Compose、执行迁移或验证重建后数据保持 |

## 已验证命令

后端：

```powershell
cd backend
$base = Join-Path $env:TEMP ('robotcare_pytest_' + [guid]::NewGuid().ToString('N'))
python -m pytest -q -p no:cacheprovider --basetemp $base
# 137 passed, 1 skipped
```

前端：

```powershell
cd frontend
npm.cmd ci --cache .npm-cache
npm.cmd run test
npm.cmd run build
# 33 passed；vue-tsc 与 Vite production build 通过，1705 modules transformed
```

本机 Microsoft Edge 关键链路：

```powershell
cd frontend
npm.cmd run test:e2e:edge
# 5 passed (38.4s)，命令正常退出
```

知识数据：

```text
官方来源入口：4
版本化流程：5（4 published，1 draft）
评测用例：113（全部 needs_human_review）
已验证向量：53（JH69U1 31，VC35U1 22）
真实检索冒烟：5/5
离线审计：safety_block 15/15；source_page 14/14；step_selection 14/14
未运行指标：model_isolation / retrieval_recall / refusal / classification / faithfulness
```

真实检索冒烟证据：`docs/evidence/rag_smoke_20260720.json`。

PDF 视觉证据：`docs/evidence/sample_service_report.pdf`。该文件使用脱敏演示数据，已渲染检查中文字体、A4 页面、内容裁切和页脚。

简历可用表述、量化证据和禁止夸大的边界：`docs/resume_evidence.md`。

## 第一阶段目标

- 用户注册和登录。
- 用户保存自己的扫地机器人设备。
- 支持 JH69U1 和 VC35U1 的代表性诊断流程。
- 每次只返回一个管理员审核过的低风险步骤。
- 用户反馈“已解决”后结束诊断；未解决则进入下一步。
- 步骤耗尽后生成可交给官方售后的诊断报告数据。
- 不同用户的设备、诊断和报告严格隔离。
- Vue 页面能够完成登录、设备管理和诊断主流程。

## 数据库迁移

生产式启动默认不再自动创建或修改表。首次创建空数据库：

```powershell
cd backend
.\.venv\Scripts\alembic.exe upgrade head
```

API 启动时会检查数据库 revision；不在 Alembic `head` 时直接拒绝启动。当前 head 为 `20260720_0004`：`0003` 新增认证生命周期表，`0004` 新增诊断分类决策证据；`create_all()` 仅保留给显式开启的隔离测试。

本地开发库已备份为 `robotcare.db.pre-category-0004-20260720.bak` 并升级到 `0004`；4 条已发布流程、1 条草稿流程、2 份知识文档、53 个分片和 53 个向量均已保留。

## 生产知识发布

Web Worker 不自动抓取或重建知识。运维人员将两份官方 PDF 和发布清单放入外部只读挂载目录，确认 SHA256 后执行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.knowledge_cli release --manifest <外部目录>\release.json
```

清单格式参考 `knowledge/release_manifest.example.json`。命令要求数据库已处于 Alembic head，校验型号、来源、SHA256、`text-embedding-v4` 和 256 维；重复发布幂等，整包失败回滚。生产环境缺少 DashScope Key 时配置门禁拒绝启动完整知识能力；这些实现/测试证据不等于真实 PostgreSQL 或 Docker 发布已经运行。

## 认证与可观测性基线

- 【已验证】注册与登录响应体只返回短期访问令牌；opaque 刷新令牌只存在 `robotcare_refresh_token` Cookie 中，属性为 `HttpOnly; SameSite=Lax; Path=/api/v1/auth`，生产环境强制 `Secure`。数据库只保存刷新令牌的 SHA256，不保存明文。
- 【已验证】访问 JWT 含 `sid` 并绑定服务器端 `AuthSession`。刷新采用条件更新轮换；默认 5 秒内的自然并发重复消费返回 `409 + Retry-After` 且不撤销会话，超过宽限的旧令牌重放返回 401 并撤销整个会话。`POST /api/v1/auth/logout` 返回 204、删除 Cookie 并撤销会话。
- 【已验证】用户状态为 `active/disabled`；停用用户不能登录、刷新或继续使用既有访问令牌，前端收到对应 403 会清理认证状态。登录失败计数使用 SQLite/PostgreSQL 方言原子 UPSERT，并在密码校验前用事务锁按账户串行化门禁；8 路登录 API 并发测试仍严格在第 5 次失败时锁定。默认窗口/锁定时间均为 15 分钟，返回 `429` 与 `Retry-After`。键值使用 JWT 密钥派生的 HMAC，不保存原邮箱或 IP。
- 【已验证】所有响应带 `X-Request-ID`；HTTP/校验错误的 JSON 顶层包含 `trace_id`，422 不回显 Pydantic 的 `input/ctx`，未处理异常统一返回通用 500。请求日志只记录方法、路径、状态和耗时，不记录请求头、查询参数或正文；敏感键递归脱敏。
- 【已验证】`GET /health` 只表示存活；`GET /ready` 检查数据库、Alembic、存储和生产 Embedding 配置；`GET /api/v1/knowledge/health` 另检查两个必做型号的文档、分片、向量和 SHA256，不能用进程存活冒充业务知识就绪。
- 【已验证】领域日志覆盖安全阻断、诊断创建/状态变化、知识检索来源/文档 SHA256/页码/分数/耗时，以及 embedding 模型/条数/维度/耗时/结果；不记录用户故障描述、检索文本、分片内容或模型输入。
- 【已验证】注册策略支持 `open/invite/closed`。生产环境禁止 `open`，邀请码模式要求至少 16 字符且拒绝示例占位值；缺失或错误邀请码与关闭注册统一返回通用 403，避免泄露策略细节。Compose 默认使用 `invite`，真实邀请码必须由部署环境注入。
- 【计划】邀请码生命周期管理/轮换、独立注册限流、邮箱验证、垃圾账户清理、修改密码、找回密码、账户删除/个人数据清理，以及外部日志平台、指标告警、分布式 Trace/OTel 仍未实现。

## 目录

```text
robotcare-ai/
├── backend/      # FastAPI、SQLAlchemy、认证、诊断状态机和测试
├── frontend/     # Vue 3 + TypeScript 用户端和真实 API 驱动的管理员控制台
├── knowledge/    # 官方来源清单、诊断流程种子和评测集
├── docs/         # 架构与完成证据
└── .env.example
```

## 产品安全边界

系统只提供清洁刷头、检查传感器、检查充电座、重新配网、重启和复位等低风险操作。涉及拆机、电池、电机、电路或内部零件维修时，必须停止自助排查并建议联系海尔官方售后。

## 官方资料来源

- JH69U1：https://www.haier.com/xjd/sdjqr/20241029_252007.shtml
- VC35U1：https://www.haier.com/xjd/sdjqr/20200902_146632.shtml
- P50U1：https://www.haier.com/xjd/sdjqr/20200831_146586.shtml
- 海尔服务支持：https://www.haier.com/support/

其中 JH69U1、VC35U1 的说明书正文已完成本地抓取、解析和 SQLite 开发环境向量化；P50U1 与支持总入口仍只证明资料入口已登记。PostgreSQL + pgvector 的双方言类型、查询和迁移代码已完成单元/静态验证，但真实 PostgreSQL 运行仍为【待验证】。113 条评测数据和三项离线审计已经存在，但全部用例仍待人工复核，五项在线 RAG/LLM 指标仍为【计划】。

## 管理员账户与当前管理范围

管理员密码只通过进程环境变量传入，不放在命令行或仓库文件中。数据库必须已升级到 Alembic head：

```powershell
cd D:\个人项目\robotcare-ai\backend
$env:ROBOTCARE_DATABASE_URL='sqlite:///./data/robotcare.db'
$env:ROBOTCARE_ADMIN_PASSWORD='<8-128位强密码>'
python -m app.admin_cli create --email admin@example.com
Remove-Item Env:ROBOTCARE_ADMIN_PASSWORD
```

【已验证】新邮箱会创建管理员；已有普通用户会在不修改原密码哈希的情况下提升为管理员；重复执行对已有管理员保持幂等。创建或提升会写入不含密码和令牌的审计日志。

【已验证】当前管理范围包括运营计数、型号列表与启停、知识数量健康、安全阻断列表、未解决报告摘要和审计日志。型号停用后不再出现在公共型号列表中，不能新建设备或新开诊断，但历史诊断仍可读取。

【计划】知识上传/重建/停用、诊断流程审核发布、评测运行与结果持久化不在当前管理员接口范围内。

## PostgreSQL / Docker 下一步验收

【待验证】`docker-compose.yml` 默认使用 `ROBOTCARE_ENVIRONMENT=production` 与安全刷新 Cookie，因此必须部署在 HTTPS 反向代理之后。仅限本机 HTTP 调试时，复制 `.env.compose-local.example` 为本地环境文件，以 `development` 和 `ROBOTCARE_REFRESH_COOKIE_SECURE=false` 覆盖；不得把这套配置用于公网。

以下命令是待执行的运行验收，不是当前已完成证据。必须使用名称包含 `robotcare_test` 的专用测试库；集成测试会执行 Alembic downgrade/upgrade，禁止指向开发库或生产库。

```powershell
cd backend
python -m pip install -e ".[test,postgres]"
$env:ROBOTCARE_TEST_POSTGRES_URL='postgresql+psycopg://robotcare:<password>@127.0.0.1:5432/robotcare_test'
$env:ROBOTCARE_DATABASE_URL=$env:ROBOTCARE_TEST_POSTGRES_URL
$env:ROBOTCARE_ALLOW_DESTRUCTIVE_POSTGRES_TESTS='1'
$env:ROBOTCARE_JWT_SECRET='<test-only-secret>'
$env:ROBOTCARE_ATTACHMENT_DIR="$env:TEMP\robotcare-attachments"
$env:ROBOTCARE_REPORT_DIR="$env:TEMP\robotcare-reports"
alembic upgrade head
python -m pytest tests/test_postgres_integration.py -v
python ../scripts/postgres_integration_smoke.py
```

```powershell
cd D:\个人项目\robotcare-ai
$env:POSTGRES_PASSWORD='<strong-password>'
$env:ROBOTCARE_DATABASE_URL='postgresql+psycopg://robotcare:<strong-password>@postgres:5432/robotcare'
$env:ROBOTCARE_JWT_SECRET='<long-random-secret>'
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
Invoke-WebRequest http://127.0.0.1:5173/healthz
# 容器内部健康检查直接访问 backend:8000/ready；
# 公共 Nginx 不暴露详细 readiness 端点。
```

验收证据至少应保存：PostgreSQL `vector` 扩展版本、`knowledge_chunks.embedding=vector(256)`、HNSW 索引存在、型号隔离与 Top-K 测试输出、Alembic head、后端 `/health` 与 `/ready` 响应、前端健康响应，以及容器重建后数据库/附件/报告仍存在的记录。

## 完成证据规则

任何功能只有同时具备以下证据后，才能从 `【计划】` 或 `【待验证】` 改为 `【已验证】`：

1. 对应代码和配置存在。
2. 自动化测试通过。
3. 真实运行命令及输出已记录。
4. 已知限制写入 `docs/status_evidence.md`。
