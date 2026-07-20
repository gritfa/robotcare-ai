# RobotCare AI

独立第三方海尔扫地机器人使用指导与安全故障排查平台。

> 状态说明：本仓库正在执行分阶段工程化改造。本文只把经过真实命令验证的能力标记为 `【已验证】`；其余内容均标记为 `【计划】` 或 `【待验证】`。

## 当前状态

| 子系统 | 状态 | 说明 |
| --- | --- | --- |
| FastAPI 后端 | `【已验证】` | 认证、设备、诊断、安全、版本化流程、Alembic、附件、报告、检索和管理员接口全量回归为 `68 passed, 1 skipped`；跳过项是真实 PostgreSQL 集成测试 |
| Vue 3 前端 | `【已验证】` | 用户主链路与管理员控制台共 `14 passed`；`vue-tsc` 和生产构建通过，Vite 转换 1701 个模块 |
| 高风险输入阻断 | `【已验证】` | 后端确定性规则覆盖冒烟、焦味、异常发热、电池损坏、内部进水、拆机、内部维修、短接、绕过保护和非官方改装；17 个安全测试通过 |
| 反馈幂等与并发 | `【已验证】` | 请求携带当前 `step_id`；重复、旧步骤和两请求并发只允许一条执行记录，冲突返回 409 |
| 图片上传安全 | `【已验证】` | Pillow 真实解码，校验格式/MIME/扩展名、5MB、2500 万像素、每会话 5 张和会话状态；删除失败进入可追踪清理记录 |
| 官方说明书证据 | `【已验证】` | JH69U1、VC35U1 官方 PDF 已下载、校验 SHA256、按页解析并完成指定页面目视检查；原始 PDF 不提交公开仓库 |
| 诊断流程发布 | `【已验证】` | JSON 是唯一种子来源；4 条逐步骤说明书复核流程已发布，1 条导航流程因因果证据不足保持草稿且普通用户不可调用 |
| 流程版本与迁移 | `【已验证】` | stable_key + version + draft/published/retired；发布版本不可原地修改；Alembic head `20260720_0002` 和生产启动 revision 门禁通过测试 |
| 型号级向量检索基线 | `【已验证】` | 两份说明书已通过 DashScope `text-embedding-v4` 入库：JH69U1 31 个向量、VC35U1 22 个向量；5 条真实检索冒烟用例通过 |
| RAG/安全评测数据与离线审计 | `【已验证】` | 已形成 113 条分项用例；离线实际执行 `safety_block 15/15`、`source_page 14/14`、`step_selection 14/14`，JSON/Markdown 报告已保存；113 条均为 `needs_human_review` |
| 完整 RAG 在线评测 | `【计划】` | `model_isolation`、`retrieval_recall`、`refusal`、`classification`、`faithfulness` 均为 `not_run`；当前不生成自然语言答案，也不能称评测集已人工审核 |
| PostgreSQL + pgvector 实现 | `【已验证】` | 双方言 256 维字段、无列 CAST 的数据库 Top-K SQL、型号过滤、HNSW 迁移和知识替换失败回滚已通过单元、静态编译与 SQLite 回归 |
| PostgreSQL + pgvector 真实运行 | `【待验证】` | 集成测试与 smoke 脚本已编写，但本机无 Docker/psql，远端 CI 尚未运行，不能声称 PostgreSQL 迁移、索引命中或运行健康已完成 |
| PDF 售后报告 | `【已验证】` | 未解决会话可幂等生成和受权下载中文 PDF；跨用户访问、无效状态、文件头与随机存储名测试通过，示例已渲染目视检查 |
| 管理员后端与审计 | `【已验证】` | 后端 RBAC、`AuditLog`、7 个管理员接口、安全管理员 CLI、型号停用语义均通过单元/API/迁移测试 |
| 管理员前端 | `【已验证】` | 真实 API 驱动的运营概览、型号启停、知识健康、安全阻断、未解决报告和审计日志页面已通过单元测试与生产构建；浏览器 E2E 仍为【计划】 |
| 管理员完整内容运营 | `【计划】` | 知识上传/重建/停用、流程审核发布、评测执行与结果持久化尚未实现，不能称管理后台全部完成 |
| Docker 静态部署契约 | `【已验证】` | 静态检查已覆盖启动迁移、外部密钥、PostgreSQL/附件/报告持久卷、健康检查和 Noto CJK 字体配置 |
| Docker 实际部署 | `【待验证】` | 本机没有 Docker，尚未实际构建镜像、启动 Compose、执行迁移或验证重建后数据保持 |

## 已验证命令

后端：

```powershell
cd backend
$base = Join-Path $env:TEMP ('robotcare_pytest_' + [guid]::NewGuid().ToString('N'))
python -m pytest -q -p no:cacheprovider --basetemp $base
# 68 passed, 1 skipped
```

前端：

```powershell
cd frontend
npm.cmd ci --cache .npm-cache
npm.cmd run test
npm.cmd run build
# 14 passed；vue-tsc 与 Vite production build 通过，1701 modules transformed
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

API 启动时会检查数据库 revision；不在 Alembic `head` 时直接拒绝启动。当前 head 为 `20260720_0002`，新增管理员审计日志表；`create_all()` 仅保留给显式开启的隔离测试。

本地旧开发库已通过迁移升级，并保留 `robotcare.db.pre-alembic-20260720.bak` 与 `robotcare.db.pre-admin-0002-20260720.bak` 备份；2 份知识文档、53 个分片和 53 个向量均已保留。

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
Invoke-WebRequest http://127.0.0.1:5173/healthz
Invoke-WebRequest http://127.0.0.1:5173/backend-healthz
```

验收证据至少应保存：PostgreSQL `vector` 扩展版本、`knowledge_chunks.embedding=vector(256)`、HNSW 索引存在、型号隔离与 Top-K 测试输出、Alembic head、后端健康响应、前端健康响应，以及容器重建后数据库/附件/报告仍存在的记录。

## 完成证据规则

任何功能只有同时具备以下证据后，才能从 `【计划】` 或 `【待验证】` 改为 `【已验证】`：

1. 对应代码和配置存在。
2. 自动化测试通过。
3. 真实运行命令及输出已记录。
4. 已知限制写入 `docs/status_evidence.md`。
