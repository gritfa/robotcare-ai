# RobotCare AI API 契约

> 契约版本：`0.11.0`；统一前缀：`/api/v1`。本文按 2026-07-22 的后端源码和完整回归 `168 passed, 1 skipped` 整理；唯一跳过项为真实 PostgreSQL 集成测试。

## 1. 通用约定

- 【已验证】除注册、登录、刷新、退出和型号列表外，业务接口使用 `Authorization: Bearer <access_token>`；访问 JWT 含 `sub`、`sid`、`type=access`、`iat`、`exp`，每次受保护请求还会校验服务器端认证会话和用户状态。
- 【已验证】注册与登录设置 opaque 刷新 Cookie `robotcare_refresh_token`，属性为 `HttpOnly; SameSite=Lax; Path=/api/v1/auth`；生产环境强制 `Secure`。刷新令牌不出现在 JSON 响应，也不应进入 JavaScript 存储。
- 【已验证】普通请求和响应使用 `application/json`；附件上传使用 `multipart/form-data`。
- 【已验证】时间字段使用 FastAPI/Pydantic 的 ISO 8601 日期时间字符串。
- 【已验证】用户资源在服务端校验所有权；设备和诊断跨用户访问测试返回 `403`。
- 【已验证】常见错误包括 `401` 未认证、访问/刷新令牌无效或会话撤销，`403` 资源越权或账号停用，`404` 资源/流程不存在，`409` 状态/并发/注册冲突，`413` 文件过大/像素过多，`415` 图片格式无效，`422` 请求校验或安全阻断，`429` 登录或业务配额限流。
- 【已验证】每个响应带 `X-Request-ID`。客户端提供合法值时原样贯穿；非法或超过 128 字符时服务器生成 UUID。HTTP 错误响应统一在 JSON 顶层增加 `trace_id`；422 不回显 Pydantic `input` 或 `ctx`，未处理异常只返回通用 `500 Internal server error`。
- 【已验证】测试覆盖 `in_progress → resolved` 与 `in_progress → unresolved`；步骤反馈使用 `resolved`、`not_resolved`。
- 【已验证】当前 OpenAPI 已提供刷新/退出、图片附件、型号级知识检索/状态、文本/PDF 报告、管理员最小化列表和被审计敏感详情；没有修改密码、找回密码、账户删除、知识上传/重建/停用、流程审核发布、评测结果持久化或生成式 RAG 回答接口。

## 2. 公共接口

### `POST /api/v1/auth/register`

【已验证】注册用户，创建服务器端认证会话，返回 Bearer 访问令牌并设置刷新 Cookie。

请求：

```json
{
  "email": "user@example.com",
  "password": "at-least-8-characters",
  "invite_code": "deployment-provided-invite"
}
```

成功：`201 TokenResponse`。邮箱重复或并发唯一键竞争：`409`，不会返回数据库 500。新用户状态为 `active`。注册前使用 email/IP 分钟桶，默认分别为 3 和 10；超限遵循下述结构化 429 契约。

注册由 `ROBOTCARE_REGISTRATION_MODE` 控制：

- `open`：开发或受控环境可不传 `invite_code`；生产环境启动时拒绝该模式。
- `invite`：必须提供与服务端配置一致的邀请码；缺失或错误邀请码统一返回 `403 Registration is not available`。
- `closed`：所有注册请求统一返回相同 403。

生产邀请码至少 16 字符，且不能包含示例占位标记；比较使用恒定时间摘要比较。Compose 默认 `invite`，真实密钥只从部署环境注入。邀请码的发放、轮换、撤销和使用次数管理仍为【计划】。

### `POST /api/v1/auth/login`

【已验证】邮箱密码登录。请求字段与注册相同，成功返回 `200 TokenResponse` 并设置刷新 Cookie；凭据错误返回 `401`，停用账号返回 `403`。

登录限流由数据库持久化，默认参数为：

- `ROBOTCARE_LOGIN_EMAIL_MAX_FAILURES=20`
- `ROBOTCARE_LOGIN_MAX_FAILURES=5`（email/IP）
- `ROBOTCARE_LOGIN_IP_MAX_FAILURES=30`
- `ROBOTCARE_REFRESH_REUSE_GRACE_SECONDS=5`
- `ROBOTCARE_LOGIN_WINDOW_MINUTES=15`
- `ROBOTCARE_LOGIN_LOCK_MINUTES=15`

达到任一阈值后当前请求及锁定期内后续请求返回 `429`，响应头包含正整数秒数 `Retry-After`。三个桶均使用 HMAC 键，不保存原邮箱或 IP；成功登录清除 email 与 email/IP 桶，但不清除独立 IP 桶。不存在账号与已有账号分支各执行一次 Argon2 验证并返回相同 401 JSON。仅当直连地址属于 `ROBOTCARE_TRUSTED_PROXY_CIDRS` 时解析转发链；畸形或伪造值回退直连地址。

### `POST /api/v1/auth/refresh`

【已验证】请求体为空，浏览器自动携带刷新 Cookie；成功返回 `200 TokenResponse` 并轮换 Cookie。

- 刷新令牌为 opaque 随机值，数据库只保存 SHA256。
- 条件更新保证一个令牌只能消费一次。
- 默认 5 秒宽限内的自然并发重复消费返回 `409` 与 `Retry-After`，不会撤销会话；宽限外的旧令牌重放返回 `401` 并撤销整个 `AuthSession`，该会话已签发的访问令牌都会失效。
- 缺少、无效、过期或已撤销刷新令牌返回 `401`；停用账号返回 `403`。

### `POST /api/v1/auth/logout`

【已验证】返回 `204` 且无响应体，删除刷新 Cookie并撤销认证会话。随后使用退出前的访问令牌调用受保护接口返回 `401`。没有 Cookie 时，接口会尝试从 Bearer 访问令牌取得 `sid` 并撤销对应会话；无有效凭据仍保持幂等 204。

### `GET /api/v1/auth/me`

【已验证】携带 Bearer JWT 返回当前 `UserRead`；`UserRead` 包含 `status`。用户停用、会话撤销或会话过期时不能继续使用既有访问令牌。

### 结构化业务配额

以下写入或高成本动作具有数据库 user/IP 分钟桶：注册、`knowledge/search`、创建诊断、上传附件、生成文本报告和生成 PDF。Embedding 另有 user/IP 分钟与日桶。默认值以 `.env.example` 为准，服务端使用条件 UPSERT 保证一次请求涉及的桶全通过或全不消费。

```json
{
  "detail": {
    "code": "RATE_LIMITED",
    "action": "knowledge_search",
    "scope": "user",
    "window_kind": "minute",
    "retry_after_seconds": 42
  }
}
```

响应状态为 `429`，并包含与 JSON 一致的 `Retry-After`。高风险文本先执行安全阻断，不消耗配额、缓存或外部 Embedding 调用。知识查询的短 TTL HMAC 缓存和相同请求 singleflight 只在单进程内共享；数据库配额才是跨实例持久化边界。

### `GET /api/v1/models`

【已验证】返回启用的型号数组 `ModelRead[]`；测试通过该接口取得 JH69U1 与 VC35U1。按型号代码排序仍为【计划】单独断言项。

### `GET /api/v1/models/{model_id}/diagnostic-options`

【已验证】需要 Bearer JWT，只返回指定型号当前 `published` 且启用的诊断流程：

```json
[
  {
    "stable_key": "vc35u1-wifi-setup",
    "version": 1,
    "issue_category_code": "wifi_setup_failure",
    "issue_category_name": "配网失败",
    "title": "VC35U1 配网失败安全排查"
  }
]
```

`draft` 和 `retired` 流程不会返回；前端根据用户所选设备动态加载该接口。

## 3. 管理员接口

全部管理员接口均需要 Bearer JWT，并在后端通过 `require_admin` 校验 `user.role == "admin"`。前端 `/admin` 路由守卫只改善体验，不替代服务器授权。API 测试证明普通用户调用以下接口均返回 `403`。

| 方法与路径 | 成功响应 | 当前语义 |
| --- | --- | --- |
| `GET /api/v1/admin/overview` | `200 AdminOverviewRead` | 返回用户、启用型号、已发布流程、知识文档/分片、安全阻断、未解决诊断和售后报告数量 |
| `GET /api/v1/admin/models` | `200 AdminModelRead[]` | 返回启用和停用的全部型号，按型号代码排序 |
| `PATCH /api/v1/admin/models/{model_id}` | `200 AdminModelRead` | 请求 `{"active": true/false}`；设置型号启用状态并写审计日志 |
| `GET /api/v1/admin/knowledge/status` | `200 KnowledgeStatusRead[]` | 返回每个型号的文档、分片和向量数量，只读 |
| `GET /api/v1/admin/safety-blocks?limit=50` | `200 AdminSafetyBlockRead[]` | 最近安全阻断最小化列表；不返回原因、建议、用户或设备内部 ID |
| `GET /api/v1/admin/safety-blocks/{event_id}` | `200 AdminSafetyBlockDetailRead` | 返回敏感详情前先提交 `safety_block.detail_read` 审计 |
| `GET /api/v1/admin/diagnostics/{diagnostic_id}` | `200 AdminDiagnosticDetailRead` | 返回故障描述、错误码和步骤前先提交 `diagnostic.detail_read` 审计 |
| `GET /api/v1/admin/unresolved-reports?limit=50` | `200 AdminUnresolvedReportRead[]` | 最近未解决报告最小化列表；不返回故障描述、错误码或用户内部 ID |
| `GET /api/v1/admin/reports/{report_id}` | `200 AdminServiceReportDetailRead` | 返回报告正文前先提交 `service_report.detail_read` 审计 |
| `GET /api/v1/admin/audit-logs?limit=50` | `200 AdminAuditLogRead[]` | 最近审计事件，`limit` 范围 1～100 |

型号停用语义已通过 API 测试：

- 公共 `GET /models` 不再返回该型号。
- `GET /models/{id}/diagnostic-options` 返回 `404`。
- 使用该型号新建设备返回 `404`。
- 已有该型号设备新建诊断返回 `409`。
- 停用前创建的历史诊断仍可读取，不删除设备、会话或报告。
- 写入 `robot_model.active_set` 审计事件，包含型号代码、变更前状态和变更后状态，不包含密码或令牌。

管理员运营概览响应：

```json
{
  "user_count": 10,
  "active_model_count": 2,
  "published_flow_count": 4,
  "knowledge_document_count": 2,
  "knowledge_chunk_count": 53,
  "safety_block_count": 3,
  "unresolved_diagnostic_count": 1,
  "service_report_count": 1
}
```

三类敏感详情审计仅保存操作人、动作、资源类型/ID、时间、trace 和关联 ID，不复制故障描述、报告正文或图片内容；审计提交失败时返回 503，敏感正文不会发送。

【计划】当前没有知识上传/重建/停用、流程审核发布、评测执行或评测结果持久化管理员 API，不能把上述接口描述为完整管理后台。

## 4. 设备接口

以下接口均已实现并需要 Bearer JWT；所有权隔离已有自动测试，未逐项覆盖的更新/删除分支仍需补充测试。

| 方法与路径 | 请求 | 成功响应 | 关键错误 |
| --- | --- | --- | --- |
| `POST /api/v1/devices` | `DeviceCreate` | `201 DeviceRead` | 型号不存在 `404` |
| `GET /api/v1/devices` | 无 | `200 DeviceRead[]` | `401` |
| `GET /api/v1/devices/{device_id}` | 无 | `200 DeviceRead` | `403/404` |
| `PATCH /api/v1/devices/{device_id}` | `DeviceUpdate` | `200 DeviceRead` | `403/404/422` |
| `DELETE /api/v1/devices/{device_id}` | 无 | `204` | 非本人 `403`；有诊断历史 `409` |

`DeviceCreate`：

```json
{
  "robot_model_id": 1,
  "nickname": "客厅机器人",
  "serial_number": "optional"
}
```

`DeviceUpdate` 只允许更新 `nickname` 和 `serial_number`，两者均可省略。

## 5. 诊断接口

以下接口均已实现并需要 Bearer JWT。

### `POST /api/v1/diagnostics`

创建诊断会话。后端只在用户设备型号的已发布流程中做确定性候选判断，错误码优先于关键词；LLM 不决定最终流程。

```json
{
  "device_id": 1,
  "issue_category_code": "return_to_dock_failure",
  "issue_description": "机器人多次尝试后仍找不到基站",
  "error_code": null,
  "confirm_category_mismatch": false
}
```

成功返回 `201 DiagnosticRead`。当前已发布流程：

- `JH69U1` + `return_to_dock_failure`
- `JH69U1` + `base_station_water_tank_issue`
- `VC35U1` + `wifi_setup_failure`
- `VC35U1` + `cleaning_noise`

`VC35U1 + navigation_abnormal` 当前为草稿，普通用户创建时返回 `404`。

描述与选择明显冲突时不创建会话，返回 `409`：

```json
{
  "detail": {
    "code": "ISSUE_CATEGORY_MISMATCH",
    "selected": "cleaning_noise",
    "suggested": "wifi_setup_failure",
    "suggested_categories": ["wifi_setup_failure"],
    "requires_confirmation": false
  }
}
```

只有多个候选并列的模糊场景会返回 `requires_confirmation=true`，用户明确确认后可用 `confirm_category_mismatch=true` 重试；明显冲突不能靠该字段绕过。成功响应的 `category_decision` 记录用户选择、系统候选、最终类别和确认方式，报告也保存这三类信息。

创建前会执行【已验证】确定性安全规则。命中高风险时不创建诊断会话，返回 `422`：

```json
{
  "detail": {
    "code": "SAFETY_BLOCKED",
    "blocked": true,
    "category": "smoke",
    "risk_level": "critical",
    "reason": "设备出现冒烟或烟雾，存在火灾和电气风险。",
    "official_service_advice": "请立即停止自助排查……并联系海尔官方售后。"
  }
}
```

### 查询会话

| 方法与路径 | 成功响应 | 说明 |
| --- | --- | --- |
| `GET /api/v1/diagnostics` | `200 DiagnosticRead[]` | 只返回当前用户的诊断，按创建时间倒序 |
| `GET /api/v1/diagnostics/{diagnostic_id}` | `200 DiagnosticRead` | 包含已执行步骤 |
| `GET /api/v1/diagnostics/{diagnostic_id}/steps/current` | `200 StepRead` | 已结束时返回 `409`；一次只返回一个步骤 |

### `POST /api/v1/diagnostics/{diagnostic_id}/feedback`

请求：

```json
{
  "step_id": 1,
  "outcome": "not_resolved"
}
```

成功返回 `200 FeedbackResponse`：

- `resolved`：诊断变为 `resolved`，`resolved=true`，`current_step=null`。
- `not_resolved` 且存在下一步：保持 `in_progress`，返回下一步。
- `not_resolved` 且当前为最后一步：诊断变为 `unresolved`，`resolved=false`，`current_step=null`。
- 已结束会话再次反馈：`409`。
- `step_id` 不是当前步骤、同一步骤重复提交或并发请求落后：`409`，不会生成重复执行记录。

## 6. 知识检索接口

以下接口均为【已验证】并需要 Bearer JWT。当前接口只返回真实检索结果，不生成自然语言答案。

### `POST /api/v1/knowledge/search`

请求：

```json
{
  "robot_model_id": 1,
  "query": "机器人无法自动回充怎么办",
  "top_k": 5
}
```

普通用户请求禁止 `min_score`，传入该字段返回 422。阈值由服务端 `ROBOTCARE_KNOWLEDGE_MIN_SCORE` 控制，并可按型号或 `型号:知识SHA256` 覆盖。成功返回 `200 KnowledgeSearchResult[]`；低于阈值返回空数组，前端明确显示无匹配资料。

检索前执行与诊断相同的安全规则。高风险查询返回结构化 `422 SAFETY_BLOCKED`，不会调用 Embedding 或返回说明书片段。“没有冒烟”等否定表达有单独策略和测试。

### `GET /api/v1/knowledge/status`

成功返回各型号的 `document_count`、`chunk_count` 和 `vector_count`。2026-07-20 本地入库证据为 JH69U1 `1/31/31`、VC35U1 `1/22/22`。

### `GET /api/v1/knowledge/health`

返回两个必做型号的文档/分片/向量数量、文档 SHA256 和 `ready`，并给出：

- `normal`：两型号知识与 Embedding 配置均就绪。
- `knowledge_degraded`：缺文档、缺向量或数量不一致。
- `external_model_unavailable`：Embedding 配置不可用。

该接口表示业务知识状态；`/health` 只表示进程存活。

## 7. 附件接口

以下接口均为【已验证】并需要 Bearer JWT：

| 方法与路径 | 请求/响应 | 约束 |
| --- | --- | --- |
| `POST /api/v1/diagnostics/{diagnostic_id}/attachments` | multipart 单文件字段 `file`；返回 `201 AttachmentRead` | 既有限制不变；并发超出 5 张返回 409，不遗留 DB 记录或孤儿文件 |
| `GET /api/v1/diagnostics/{diagnostic_id}/attachments` | `200 AttachmentRead[]` | 只列出当前用户该会话附件 |
| `DELETE /api/v1/diagnostics/{diagnostic_id}/attachments/{attachment_id}` | `204` | 同时删除数据库记录和本地文件 |

附件必须满足扩展名、客户端 MIME 与解码格式一致。附件使用随机存储名，API 只返回原始文件名、内容类型、大小和时间；当前不提供公开图片下载 URL，也不进行视觉诊断。删除时先隔离文件，清理失败会留下可追踪记录。

## 8. 报告接口

以下接口均为【已验证】并需要 Bearer JWT。

| 方法与路径 | 成功响应 | 约束 |
| --- | --- | --- |
| `POST /api/v1/diagnostics/{diagnostic_id}/report` | `201 ReportRead` | 仅 `unresolved` 会话可生成；并发请求返回同一报告，不产生 500 |
| `GET /api/v1/diagnostics/{diagnostic_id}/report` | `200 ReportRead` | 报告不存在返回 `404` |
| `POST /api/v1/diagnostics/{diagnostic_id}/report/pdf` | `201 ReportPdfRead` | 仅 `unresolved` 会话可生成；重复调用返回同一文件元数据 |
| `GET /api/v1/diagnostics/{diagnostic_id}/report/pdf` | PDF 文件流 | 需要 Bearer JWT 和诊断所有权；PDF 尚未生成时返回 `404` |

`ReportRead.content` 保留文本报告以兼容页面展示；PDF 使用独立受权接口，内部随机存储名不会暴露给客户端。

## 9. 响应类型

### `TokenResponse`

```json
{
  "access_token": "jwt",
  "token_type": "bearer",
  "user": {
    "id": 1,
    "email": "user@example.com",
    "role": "user",
    "status": "active",
    "created_at": "2026-07-20T12:00:00Z"
  }
}
```

响应体没有 `refresh_token` 字段。前端只持久化访问令牌和用户摘要；刷新 Cookie 由浏览器管理。

### `ModelRead` 与 `DeviceRead`

```json
{
  "id": 1,
  "robot_model_id": 1,
  "nickname": "客厅机器人",
  "serial_number": null,
  "robot_model": {
    "id": 1,
    "code": "JH69U1",
    "name": "JH69U1 全能扫拖机器人",
    "brand": "海尔"
  },
  "created_at": "2026-07-20T12:00:00Z"
}
```

### `StepRead`

```json
{
  "id": 1,
  "stable_key": "check-dock-placement",
  "position": 1,
  "title": "检查充电座供电",
  "instruction": "确认充电座电源线插紧且指示灯正常，不要拆卸充电座。",
  "source_label": "海尔 JH69U1 官方说明书，第 7 页",
  "source_url": "https://download.haier.com/...pdf",
  "source_page": 7,
  "evidence_level": "direct",
  "evidence_basis": "说明书要求基站靠墙并保留指定空间。",
  "policy_note": "高风险情况由平台规则停止。"
}
```

### `DiagnosticRead`

```json
{
  "id": 1,
  "device_id": 1,
  "issue_description": "机器人找不到基站",
  "error_code": null,
  "status": "in_progress",
  "current_position": 1,
  "resolved": null,
  "report_available": false,
  "flow_stable_key": "jh69u1-return-to-dock",
  "flow_version": 1,
  "created_at": "2026-07-20T12:00:00Z",
  "updated_at": "2026-07-20T12:00:00Z",
  "executions": []
}
```

每个 `executions[]` 元素包含 `id`、`outcome`、完整 `step` 和 `created_at`。

### `FeedbackResponse`

```json
{
  "diagnostic": { "...": "DiagnosticRead" },
  "current_step": { "...": "StepRead 或 null" }
}
```

### `ReportRead`

```json
{
  "id": 1,
  "session_id": 1,
  "report_number": "RC-000001-XXXXXXXX",
  "content": "RobotCare AI 第三方售后诊断报告...",
  "created_at": "2026-07-20T12:00:00Z"
}
```

### `ReportPdfRead`

```json
{
  "report_id": 1,
  "report_number": "RC-000001-XXXXXXXX",
  "filename": "RobotCare-RC-000001-XXXXXXXX.pdf",
  "size_bytes": 4096,
  "download_url": "/api/v1/diagnostics/1/report/pdf"
}
```

### `AttachmentRead`

```json
{
  "id": 1,
  "session_id": 1,
  "original_filename": "charging-contact.jpg",
  "content_type": "image/jpeg",
  "size_bytes": 128000,
  "created_at": "2026-07-20T12:00:00Z"
}
```

### `KnowledgeSearchResult`

```json
{
  "score": 0.82,
  "content": "说明书中的相关片段",
  "document_title": "海尔 JH69U1 使用说明书",
  "source_url": "https://download.haier.com/...pdf",
  "page_number": 15
}
```

## 10. 前端调用约束

- 【已验证】Axios 客户端设置 `withCredentials=true`，使限定路径的 HttpOnly 刷新 Cookie随认证请求发送；刷新令牌不能被 JavaScript 读取或写入本地存储。
- 【已验证】多个业务请求同时收到 401 时共享一个 refresh Promise，只发送一次 `/auth/refresh`；每个原请求最多重试一次，认证端点自身不会触发自动刷新，避免循环。
- 【已验证】刷新成功更新访问令牌后重试原请求；刷新失败清理访问令牌和用户状态并触发认证丢失处理。退出调用在 `finally` 路径清理本地状态，即使网络请求失败也不会保留过期登录界面。
- 【已验证】前端不得通过隐藏按钮替代权限校验；以 API 的 `401/403/404/409/422/429` 为准。
- 【已验证】`current_step=null` 可能表示已解决或未解决，必须同时检查 `diagnostic.status`；只有 `unresolved` 状态展示“生成售后报告”。
- 【计划】客户端输入策略进一步阻止用户把 Wi-Fi 明文密码或联系方式写入自由文本；当前服务器日志已脱敏，但诊断业务字段仍由用户主动输入。

## 11. 管理员 CLI

管理员创建或提升不通过公共 HTTP API 暴露。CLI 启动前检查数据库位于 Alembic head，新用户密码只从 `ROBOTCARE_ADMIN_PASSWORD` 环境变量读取：

```powershell
cd D:\个人项目\robotcare-ai\backend
$env:ROBOTCARE_DATABASE_URL='sqlite:///./data/robotcare.db'
$env:ROBOTCARE_ADMIN_PASSWORD='<8-128位强密码>'
python -m app.admin_cli create --email admin@example.com
Remove-Item Env:ROBOTCARE_ADMIN_PASSWORD
```

- 新邮箱：创建 `role=admin` 用户，输出 `created`，写入 `admin_user.created` 审计事件。
- 已有普通用户：提升为管理员，保留原密码哈希，输出 `promoted`，写入 `admin_user.promoted` 审计事件；不要求设置新密码。
- 已有管理员：不重复变更，输出 `unchanged`。
- 新用户缺少环境变量密码、密码长度不在 8～128 或邮箱无效时失败，不创建用户或审计记录。
- CLI 参数中没有密码选项；审计详情不保存密码。

## 12. 非 `/api/v1` 运行接口

- 【已验证】`GET /health` 返回 `200 {"status":"ok"}`，只表示进程存活；迁移后的本地数据库实际启动得到 200。它不属于版本化业务 API。
- 【已验证】`GET /ready` 返回就绪状态，并检查数据库连通、当前 Alembic revision 是否为 head、附件目录和报告目录是否存在且可写。全部正常返回 `200`：

```json
{
  "status": "ready",
  "trace_id": "request-id",
  "components": {
    "database": {"status": "ok"},
    "alembic": {
      "status": "ok",
      "at_head": true,
      "current_revision": "20260722_0007",
      "expected_revision": "20260722_0007"
    },
    "attachments": {"status": "ok", "exists": true, "is_directory": true, "writable": true},
    "reports": {"status": "ok", "exists": true, "is_directory": true, "writable": true},
    "embedding": {"status": "ok", "configured": true, "required": true}
  }
}
```

任一组件失败返回 `503` 与 `status=not_ready`；`/health` 仍可为 200。Compose 后端健康检查使用 `/ready`，因此数据库未迁移到 head 或持久目录不可写时应保持不健康。
