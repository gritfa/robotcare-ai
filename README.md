# RobotCare AI

[![CI](https://github.com/gritfa/robotcare-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/gritfa/robotcare-ai/actions/workflows/ci.yml)

独立第三方海尔扫地机器人使用指导与安全故障排查平台。

> 状态说明：本仓库正在执行分阶段工程化改造。本文只把经过真实命令验证的能力标记为 `【已验证】`；其余内容均标记为 `【计划】` 或 `【待验证】`。

## 当前状态

| 子系统 | 状态 | 说明 |
| --- | --- | --- |
| FastAPI 后端 | `【已验证】` | 单方言重构后全部测试直接跑在 PostgreSQL 16 + pgvector 测试容器上：`344 passed, 0 skipped (60.42s，2026-08-05 本机)`；原独立 PG 集成测试并入常规回归，不再有 skip |
| Vue 3 前端 | `【已验证】` | 用户主链路、结构化错误、限流重试、附件保留、安全卡片、知识健康、会话流式、知识库后台运维及认证恢复共 `62 passed`；`vue-tsc + vite build` 通过，element-plus 已改为按需引入，入口包 `224.97 kB (gzip 83.78 kB)`，其余按页面/组件懒加载（改造前单主包 1.11 MB） |
| 智能客服会话层 | `【已验证：本地】` | 多轮会话（conversations/conversation_messages，迁移 `20260804_0010`）、SSE 流式回答（先校验引用后分片下发）、会话一键转分步诊断（`20260804_0011` 记录来源会话）、售后报告附带会话摘要；后端 pytest + 前端单测覆盖，真实浏览器长会话压力未测 |
| 聊天消息路由层 | `【已验证：本地】` | `message_router.py` 在 RAG 之前分流四类消息：闲聊（"你好"）与产品操作（"生成报告""开始诊断""上传图片"）直接回复，不消耗 embedding 配额、不检索、不调生成模型；上下文追问与知识问题照常走完整链路。判定规则确定性可解释，`intent`/`routing_rule`/`action_code` 随消息持久化（迁移 `20260805_0012`），会话回放时操作按钮仍在。安全前置阻断永远排在路由之前，路由不是绕过安全检测的旁路。前端 Enter 发送 / Shift+Enter 换行，中文输入法组词期间的回车不发送（`composerKeys.ts`，Safari 个别输入法的 compositionend 顺序问题见文件内说明） |
| 浏览器 E2E | `【已验证：本机关键链路，PostgreSQL 隔离库】` | 2026-08-05 本机 Chromium 在改造后的隔离 PostgreSQL 测试库（`ROBOTCARE_E2E_DATABASE_URL`）上重跑闭环、恢复、越权、分类、安全阻断以及注册/附件/报告/PDF 限流、管理员知识库后台，共 `10 passed (29.0s)`，远端 CI 的 Chromium E2E job 同版本 success；早前 Edge `8 passed (44.9s)` 的证据基于当时的隔离 SQLite 库，**Microsoft Edge 在 PG 库上尚未重跑**；两者都不代表 Docker/HTTPS |
| 分类与流程一致性 | `【已验证】` | 型号级确定性关键词/错误码候选；明显冲突拒绝创建，模糊场景要求用户确认；用户选择、系统候选和最终类别写入会话与报告 |
| 知识安全与业务健康 | `【已验证：本地】` | 高风险检索在 Embedding 前阻断；普通用户不能覆盖阈值；`/knowledge/health` 区分正常、知识降级和外部模型不可用；`?probe=true` 深度探测对 embedding、检索链路、生成模型各发一次真实请求（走 embedding 限流），任一失败即把状态降级为 `external_model_unavailable`，避免"配置存在"冒充"服务可用"；版本化发布清单支持 SHA256 校验、幂等与事务回滚 |
| 认证生命周期 | `【已验证】` | 登录不存在用户时使用固定 Argon2 dummy hash；email、email/IP、独立 IP 三桶原子限流、可信代理 CIDR、清理 CLI，以及刷新轮换/重放撤销、退出失效和用户状态均有测试 |
| 业务接口限流 | `【已验证：单实例 Beta】` | 注册、知识检索、诊断、附件、报告和 PDF 使用数据库原子用户/IP 或邮箱/IP 配额；Embedding 同时有分钟/日配额；429 返回结构化 `RATE_LIMITED` 与 `Retry-After`；缓存只在单进程共享 |
| 可观测性基线 | `【已验证】` | `X-Request-ID`、JSON 请求/领域事件日志、递归脱敏、带 `trace_id` 的安全错误响应以及数据库/Alembic/存储就绪检查均通过测试；尚未接入外部日志、告警或 OpenTelemetry |
| 高风险输入阻断 | `【已验证】` | 诊断和知识检索共享确定性规则；覆盖主要高风险类别和“没有冒烟”等否定语义；前端使用持久安全卡片而非仅 Toast |
| 反馈幂等与并发 | `【已验证】` | 请求携带当前 `step_id`；重复、旧步骤和两请求并发只允许一条执行记录，冲突返回 409 |
| 图片上传安全 | `【已验证】` | Pillow 真实解码及既有限制不变；PostgreSQL 会话行锁（`SELECT ... FOR UPDATE`）保证并发上传最多 5 张，失败不遗留数据库记录或孤立文件 |
| 官方说明书证据 | `【已验证】` | JH69U1、VC35U1 官方 PDF 已下载、校验 SHA256、按页解析并完成指定页面目视检查；仓库所有者确认具备公开再分发授权后，两份原始 PDF 已纳入 `knowledge/raw/` |
| 诊断流程发布 | `【已验证】` | JSON 是唯一种子来源；14 条真实型号逐步骤说明书复核流程已发布（JH69U1 7 条 / VC35U1 7 条，2026-07-31 新增 10 条全部逐字对照官方说明书页），1 条导航流程因因果证据不足保持草稿且普通用户不可调用 |
| 流程版本与迁移 | `【已验证】` | stable_key + version + draft/published/retired；迁移链单方言化后在空 PostgreSQL 库完成 `upgrade head → downgrade base → upgrade head` 往返，head `20260804_0011`，metadata 零漂移 |
| 型号级向量检索基线 | `【已验证】` | 两份说明书已通过 DashScope `text-embedding-v4` 入库：JH69U1 31 个向量、VC35U1 22 个向量；5 条真实检索冒烟用例通过 |
| RAG/安全评测数据与离线审计 | `【已验证】` | 评测集 411 条（113 条原有 + 298 条 D1 合成同源生成，全部 `needs_human_review` 不冒充人工已审）；离线真实执行七指标全 1.0：`safety_block 47/47`、`source_page 45/45`、`step_selection 45/45`、`model_isolation 31/31`、`retrieval_recall 61/61`、`classification 62/62`、`refusal(门控层) 30/30`（合成型号内存库 + 确定性 hashing 词面向量执行，结论不外推到语义向量） |
| LLM 生成层（强制引用/拒答/留痕） | `【已验证：本地 mock】` | `POST /knowledge/answer`：安全前置阻断→检索→生成；检索空/低于阈值不调模型直接拒答，引用缺失/越界拒答，回答命中安全规则拦截留痕；generation_records 全量留痕（prompt 版本/模型/片段 SHA/引用/耗时）；6 项 pytest 用 mock Provider 验证，真实 DashScope 生成调用未在本机执行 |
| faithfulness 在线评测 | `【已验证：全量 34/34，已达发布阈值】` | 2026-08-05 prompt `answer-v5` 全量在线执行（`--scope all`）：主评测 34/34 通过、score `1.0`（结构化引用+输出安全+违禁论断防线，`docs/evidence/generation_online_eval.json`）；独立第三方裁判（deepseek-v4-flash）逐论断核对的语义忠实度 `0.9118`（judged 34/34、31 条 faithful、6 条 unsupported 论断、`run_errors` 为空），达到脚本阈值 0.9，`overall_status=passed_full`（`docs/evidence/llm_judge_faithfulness.json`）。迭代轨迹 v1 `0.6765` → v2 `0.7941` → v3 `0.8824` → v4 `0.8529` → v5 `0.9118`，每轮报告均留档（`*_v4.json`、`*_v5_run2_full.json`）。v5 首轮因 2 条网络故障只判到 32/34（`completed_with_errors`，score `0.9062`），按"分母不全不算达标"只补跑裁判段，首轮报告原样保留在 `docs/evidence/llm_judge_faithfulness_v5_run1_network_errors.json`。剩余 3 条不忠实（FF-002/003/004）均为提问自带说明书没有的预设概念，非模型编造知识 |
| PostgreSQL + pgvector 实现 | `【已验证】` | 单方言：`vector(256)` 字段、无列 CAST 的数据库 Top-K SQL、型号过滤、HNSW 迁移和知识替换失败回滚全部直接在真实 PostgreSQL 上回归（SQLite 双方言分叉已删除） |
| PostgreSQL + pgvector 真实运行 | `【已验证：GitHub Actions】` | PostgreSQL 16 + pgvector Job 已验证扩展、`vector(256)`、HNSW、迁移、Top-K、型号隔离和 `/ready`；本机 Docker、多实例压力与生产数据仍未验证 |
| 远端 CI | `【已验证】` | 单方言五 Job 工作流（PG16+pgvector 回归、前端、Chromium E2E、部署契约、知识校验）已在远端跑通：提交 `bd77ad5` run 30600605830 五个 Job 全部 success（2026-07-31，https://github.com/gritfa/robotcare-ai/actions/runs/30600605830） |
| PDF 售后报告 | `【已验证】` | 并发请求返回同一报告；PDF 采用目标锁、临时文件和原子替换，验证 `%PDF-`/`%%EOF`；Edge 下载校验文件存在、非空和文件头 |
| 管理员后端与审计 | `【已验证】` | 普通列表不返回故障正文、错误码、阻断原因或关联用户/设备 ID；报告、诊断和安全阻断详情按需读取并在返回前写入不含正文的 fail-closed 审计 |
| 管理员前端 | `【已验证】` | 真实 API 驱动的运营概览、型号启停、知识健康、安全阻断、未解决报告和审计日志页面已通过单元测试与生产构建；本机 Edge 已验证普通用户访问管理员页面被拒绝，管理员内容运营 E2E 仍为【计划】 |
| 运营闭环（缺口榜/看板/知识上传） | `【已验证：本地】` | 检索空结果与资料缺口拒答双埋点写 `knowledge_gap_events`（埋点失败不影响主流程）；`GET /admin/content-gaps` 按 (型号, 归一化查询) 聚合并带回处理状态（不含用户信息）；overview 增加近 30 天回答/拒答/拒答原因分布与缺口数；`POST /admin/knowledge/upload` 管理员上传 PDF（魔数/30MB/型号校验，ingest 与审计同事务）；前端缺口榜/生成统计/上传入口已通过单元测试与生产构建；真实语义向量下的缺口数据尚未积累 |
| 知识库后台（文档生命周期/版本/缺口闭环） | `【已验证：本地】` | 文档列表（型号/版本/发布时间/来源/SHA256/分片与向量数/状态）、分页查看分片与页码、停用（`status=disabled` 直接从检索与缓存版本键中剔除，内容与向量保留可恢复）、删除（级联分片+版本+按引用计数清理存档）、原件下载、重新向量化、版本历史与回滚（回滚用旧存档重新入库并前进版本号，不倒退审计链）、上传前差异预览（只解析不入库，逐页给出修改/新增/删除页码）；内容缺口闭环：关联文档 → 一键复测（真实检索+生成重放原问题）→ 能引用作答才自动标记已解决，复测失败会把"已解决"打回处理中，外部模型故障记为"未测成"不改状态。所有写操作进 `audit_logs`。**上传原件从本版本起才留存**，此前入库的文档没有存档，重新向量化/回滚/下载原件会明确报"无存档"而不是静默失败 |
| 管理员完整内容运营 | `【部分实现】` | 知识 PDF 上传已进后台（见上行）；流程审核发布仍走 catalog JSON 文件 + 代码评审，知识重建/停用、评测执行与结果持久化仍未实现，不能称管理后台全部完成 |
| Docker 静态部署契约 | `【已验证】` | 部署检查脚本执行与 py_compile 通过；静态检查覆盖启动迁移、外部密钥、认证生产门禁、PostgreSQL/附件/报告持久卷、`/ready` 健康检查和 Noto CJK 字体配置 |
| Docker 构建缓存 | `【已验证：本机】` | `backend/Dockerfile` 拆成依赖层（只 COPY `pyproject.toml`，空 `app/` 占位装依赖）与代码层（COPY 真实代码后 `pip install --no-deps`）。2026-08-05 本机实测：改依赖后完整重建 `118.7s`；只改 `backend/app/main.py` 再构建 `7.6s`（6 层命中缓存），此前每次改代码都要重装全部依赖 |
| Docker 实际部署 | `【已验证：本机 development】` | Docker 29.5.2 实测：镜像构建、Compose 三服务 healthy、启动自动迁移至 head、pgvector 就绪、down/up 重建数据保持；production 无 key 启动被门禁拒绝（按设计）；带 key 的 production 启动仍待持 key 机器执行（docs/evidence/docker_deploy_20260730.md） |

## 已验证命令

后端（2026-08-05 本机 macOS + PostgreSQL 16/pgvector 测试容器）：

```bash
cd backend
.venv/bin/pytest -p no:cacheprovider
# 344 passed, 2 warnings in 60.42s（退出码 0）
```

前端（2026-08-05 本机）：

```bash
cd frontend
npm ci
npm test          # 62 passed (11 test files)
npm run build     # vue-tsc + vite build 通过
# dist/assets/index-*.js  224.97 kB (gzip 83.78 kB)，element-plus 按需引入后
# 按组件/页面拆成 el-table-column 91.6 kB、el-popper 48.3 kB 等独立块
```

Docker 依赖层缓存（2026-08-05 本机 Docker 29.5.2）：

```bash
docker compose build backend        # 依赖变更：118.7s
# 仅改 backend/app/main.py 后再次执行
docker compose build backend        # 7.6s，依赖层命中缓存
```

本机浏览器关键链路（2026-08-05，隔离 PostgreSQL 测试库）：

```bash
cd frontend
# 本机跑时要避开生产 Compose 栈占用的端口
ROBOTCARE_E2E_BACKEND_PORT=8021 ROBOTCARE_E2E_FRONTEND_PORT=5199 npm run test:e2e
# 10 passed (29.0s)，命令正常退出并释放测试端口
# Microsoft Edge 版本（npm run test:e2e:edge）最近一次证据仍是 SQLite 时期的 8 passed (44.9s)，PG 库上未重跑
```

知识数据：

```text
官方来源入口：4（另有 3 份合成型号说明书 PDF，明确标注 synthetic）
版本化流程：46（45 published，1 draft；14 条真实型号 + 31 条合成型号流程）
评测用例：411（全部 needs_human_review，待人工复核）
真实检索冒烟：5/5（`docs/evidence/rag_smoke_20260720.json`）
离线审计（七指标真实执行，全 1.0）：safety_block 47/47；source_page 45/45；step_selection 45/45；model_isolation 31/31；retrieval_recall 61/61；classification 62/62；refusal(门控层) 30/30
faithfulness：全量 34/34（prompt answer-v5，主评 score 1.0 passed_full；独立裁判语义忠实度 0.9118，judged 34/34、达到 0.9 阈值 passed_full，口径见 `docs/online_eval_scope.md`）
```

真实检索冒烟证据：`docs/evidence/rag_smoke_20260720.json`。

PDF 视觉证据：`docs/evidence/sample_service_report.pdf`。该文件使用脱敏演示数据，已渲染检查中文字体、A4 页面、内容裁切和页脚。

简历可用表述、量化证据和禁止夸大的边界：`docs/resume_evidence.md`。

## 隐私安全的全状态演示数据

本地开发环境可加载一套确定性的合成消费者数据。它不包含真实姓名、手机号、地址、设备照片或真实序列号，所有账号均使用 `example.com` 保留域名，图片明确标记为 synthetic。工具在 `production` 环境会拒绝运行。

```powershell
cd backend
$env:ROBOTCARE_DEMO_PASSWORD='Demo123456'  # 可选；仅当前进程
.\.venv\Scripts\python.exe -m app.demo_data_cli load --replace --yes
.\.venv\Scripts\python.exe -m app.demo_data_cli summary
Remove-Item Env:ROBOTCARE_DEMO_PASSWORD -ErrorAction SilentlyContinue
```

已生成的数据覆盖：7 个合成账号（含管理员和停用账号）、10 台设备、4 条发布流程、12 个诊断（进行中/已解决/未解决各 4 个）、4 张真实 PNG 文件、4 份文本/PDF 报告、4 个高风险阻断和 4 条管理员审计记录。

默认检查账号仅用于本地开发：

- 普通用户：`demo.alice@example.com` / `Demo123456`
- 管理员：`demo.admin@example.com` / `Demo123456`

只删除该工具管理的合成数据及其文件：

```powershell
.\.venv\Scripts\python.exe -m app.demo_data_cli reset --yes
```

详细覆盖矩阵和检查顺序见 `docs/demo_data.md`。这套数据用于页面、权限和业务生命周期演示，不等同于真实用户 Beta、生产负载、PostgreSQL 多实例或线上隐私合规验证。

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

API 启动时会检查数据库 revision；不在 Alembic `head` 时直接拒绝启动。当前 head 为 `20260804_0011`：`0005` 增加数据库状态约束，`0006` 增加独立 IP 登录桶与可信代理配套，`0007` 增加 API/Embedding 配额表，`0008` 增加生成层留痕表 generation_records，`0009` 增加内容缺口事件表 knowledge_gap_events，`0010` 增加会话表 conversations/conversation_messages，`0011` 给诊断增加来源会话字段；`create_all()` 仅保留给显式开启的隔离测试。

（历史记录，SQLite 时期，现已 PG 单方言）本地开发库曾备份为 `robotcare.db.pre-0007-20260722.bak` 并升级到 `0007`；当时的 4 条已发布流程、1 条草稿流程、2 份知识文档、53 个分片和 53 个向量均已保留，`PRAGMA integrity_check=ok` 且无外键异常。

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
- 【已验证】用户不存在和密码错误两个登录分支都恰好执行一次 Argon2 校验。登录限制使用 email（默认 20）、email/IP（默认 5）和独立 IP（默认 30）三桶原子 UPSERT；PostgreSQL 按稳定顺序获取账户/IP advisory locks。只有直连 peer 位于 `ROBOTCARE_TRUSTED_PROXY_CIDRS` 时才从右向左解析转发链，成功登录不清除独立 IP 历史。键值均为 HMAC，不保存原邮箱或 IP。
- 【已验证】所有响应带 `X-Request-ID`；HTTP/校验错误的 JSON 顶层包含 `trace_id`，422 不回显 Pydantic 的 `input/ctx`，未处理异常统一返回通用 500。请求日志只记录方法、路径、状态和耗时，不记录请求头、查询参数或正文；敏感键递归脱敏。
- 【已验证】`GET /health` 只表示存活；`GET /ready` 检查数据库、Alembic、存储和生产 Embedding 配置；`GET /api/v1/knowledge/health` 另检查两个必做型号的文档、分片、向量和 SHA256，不能用进程存活冒充业务知识就绪。
- 【已验证】领域日志覆盖安全阻断、诊断创建/状态变化、知识检索来源/文档 SHA256/页码/分数/耗时，以及 embedding 模型/条数/维度/耗时/结果；不记录用户故障描述、检索文本、分片内容或模型输入。
- 【已验证】注册策略支持 `open/invite/closed`。生产环境禁止 `open`，邀请码模式要求至少 16 字符且拒绝示例占位值；缺失或错误邀请码与关闭注册统一返回通用 403，避免泄露策略细节。Compose 默认使用 `invite`，真实邀请码必须由部署环境注入。
- 【已验证】注册已有邮箱/IP分钟配额；【计划】邀请码生命周期管理/轮换、邮箱验证、垃圾账户清理、修改密码、找回密码、账户删除/个人数据清理，以及外部日志平台、指标告警、分布式 Trace/OTel 仍未实现。

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

其中 JH69U1、VC35U1 的说明书正文已完成本地抓取、解析和开发环境向量化（历史向量最初落在 SQLite 开发库，可用 `backend/scripts/migrate_legacy_sqlite.py` 一次性迁入 PG）；P50U1 与支持总入口仍只证明资料入口已登记。PostgreSQL + pgvector 的单方言类型、查询、迁移、HNSW 与型号级 Top-K 已在本地 PostgreSQL 16 + pgvector 测试容器全量回归通过。评测集现为 411 条（全部待人工复核），七项指标已离线真实执行全 1.0（`docs/evidence/rag_eval_offline_audit.md`）；faithfulness 已用真实 DashScope key 在线跑完全量 34 条（prompt `answer-v5`，主评 1.0），独立裁判的语义忠实度为 0.9118（judged 34/34），**达到 0.9 发布阈值**，详见上方状态表与 `docs/evidence/llm_judge_faithfulness.json`。

## 管理员账户与当前管理范围

管理员密码只通过进程环境变量传入，不放在命令行或仓库文件中。数据库必须已升级到 Alembic head：

```powershell
cd D:\个人项目\robotcare-ai\backend
$env:ROBOTCARE_DATABASE_URL='postgresql+psycopg://robotcare:<password>@127.0.0.1:5432/robotcare'
$env:ROBOTCARE_ADMIN_PASSWORD='<8-128位强密码>'
python -m app.admin_cli create --email admin@example.com
Remove-Item Env:ROBOTCARE_ADMIN_PASSWORD
```

【已验证】新邮箱会创建管理员；已有普通用户会在不修改原密码哈希的情况下提升为管理员；重复执行对已有管理员保持幂等。创建或提升会写入不含密码和令牌的审计日志。

【已验证】当前管理范围包括运营计数、型号列表与启停、知识数量健康、安全阻断列表、未解决报告摘要和审计日志。列表只返回最小摘要；进入报告、诊断或安全阻断详情时才返回敏感正文，并在返回前写入管理员、资源、动作、时间和 trace ID，审计失败返回 503。型号停用后不再出现在公共型号列表中，不能新建设备或新开诊断，但历史诊断仍可读取。

【计划】知识上传/重建/停用、诊断流程审核发布、评测运行与结果持久化不在当前管理员接口范围内。

## PostgreSQL / Docker 下一步验收

【待验证】`docker-compose.yml` 默认使用 `ROBOTCARE_ENVIRONMENT=production` 与安全刷新 Cookie，因此必须部署在 HTTPS 反向代理之后。仅限本机 HTTP 调试时，复制 `.env.compose-local.example` 为本地环境文件，以 `development` 和 `ROBOTCARE_REFRESH_COOKIE_SECURE=false` 覆盖；不得把这套配置用于公网。

单方言测试基线：全部后端测试直接跑在专用 PostgreSQL 测试容器上。先起测试库容器（含 pgvector），再运行完整回归；测试会在该服务器上按需自建 `robotcare_migration` / `robotcare_eval` 库并执行 Alembic downgrade/upgrade，禁止指向开发库或生产库。

```bash
# 一次性：启动专用测试库容器（默认连接串即下面这个）
docker run -d --name robotcare-test-pg -p 55433:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=robotcare_test \
  pgvector/pgvector:pg16

cd backend
python -m pip install -e ".[test]"
# 缺省即为 postgresql+psycopg://postgres:test@127.0.0.1:55433/robotcare_test，
# 需要覆盖时设置 ROBOTCARE_TEST_DATABASE_URL
python -m pytest
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
