# 管理员 AI 服务配置上线验收（2026-08-24）

## 结论

管理员 AI 服务配置已实现并部署到阿里云当前 RobotCare 实例。代码、隔离测试、生产数据库迁移、线上 RBAC、即时启停和 Edge 页面均有本轮实测证据；当前公网入口仍是 HTTP，因而 API Key 录入被前后端主动禁止，不能据此声称 HTTPS 生产闭环完成。

## 已证明做成

- GitHub `main` 与服务器代码：`1924519d7e9030517708519b5a88ff2741502459`。
- 数据库迁移：生产库从 `20260806_0017` 升级到 `20260824_0018`，新增单例表 `ai_provider_configs`。
- 后端回归：阿里云 PostgreSQL 16 + pgvector 独立测试库运行全部 `516` 条测试，退出码 `0`；测试创建的数据库和临时超级用户在退出时删除。两条输出警告来自 Starlette/httpx 与 LangGraph 的上游弃用提示。
- 前端回归：Vitest `14` 个文件、`94` 条测试通过；`vue-tsc -b && vite build` 退出码 `0`。
- 线上就绪：服务重启后 `/ready` 返回 `ready`，Alembic 当前/期望版本均为 `20260824_0018`，数据库、附件、报告、知识目录与 Embedding 探测均为 `ok`。
- RBAC：管理员读取 `/api/v1/admin/ai-config` 返回 `200`；`viewer` 测试账户返回 `403`。
- 密钥回显：读取接口不含 `generation_api_key` 或 `embedding_api_key`，只返回是否已配置。
- 一键启停：线上执行停用后 `enabled=false`、`runtime_ready=false`；随后无需重启重新启用，最终 `enabled=true`、`runtime_ready=true`。
- HTTP 密钥门禁：经 Nginx 转发的公网 HTTP 请求提交固定无效探针 Key 返回 `403`；Edge 页面显示 HTTPS 警告，且「加密保存配置」按钮不可用。门禁不依赖服务器误设的 `development` 环境值。
- Edge 页面：真实登录 `/admin` 后确认 AI 配置面板、`qwen3.7-max`、`text-embedding-v4`、连接测试入口和一键停用按钮均可见。
- 部署前备份：`/var/backups/personal-apps/20260824T091455Z`。

## 异常与恢复记录

第一版 HTTP 门禁只在 `production` 环境生效，但线上 `.env` 实际仍为 `development`。验收探针因此写入了两个固定无效 Key。发现后立即将该行的两个加密字段置空、重启服务，使其回退到服务器原有环境变量；随后 `/ready` 的真实 Embedding 探测恢复为 `reachable=true`。最终版本改为识别反向代理转发的公网 HTTP，不再依赖环境模式，并再次完成 516 条全量回归及线上 403 验证。

## 尚未证明或刻意未做

- 未执行后台「连接测试」，因为它会真实调用一次生成模型和一次向量模型并产生费用。
- 未在生产库通过页面保存真实 API Key；当前 HTTP 门禁按设计阻止该操作。密钥加密入库、不回显和热加载由隔离 PostgreSQL 测试覆盖。
- 未完成域名、TLS 证书、HTTPS 跳转和 `ROBOTCARE_ENVIRONMENT=production` 切换，因此不能称公开生产环境安全闭环。
- 本轮服务重启后的 `/ready` 执行了真实 Embedding 可达性探测，会产生少量向量 API 调用；没有执行真实生成模型测试。
