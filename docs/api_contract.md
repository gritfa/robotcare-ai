# RobotCare AI API 契约

> 契约版本：`0.7.0`；统一前缀：`/api/v1`。本文按 2026-07-20 的后端源码和完整回归 `68 passed, 1 skipped` 整理；跳过项为真实 PostgreSQL 集成测试，未被运行验证的边界明确标为【待验证】或【计划】。

## 1. 通用约定

- 【已验证】除注册、登录和型号列表外，业务接口使用 `Authorization: Bearer <access_token>`；注册、登录、当前用户和跨用户隔离测试已通过。
- 【已验证】普通请求和响应使用 `application/json`；附件上传使用 `multipart/form-data`。
- 【计划】时间字段使用 FastAPI/Pydantic 的 ISO 8601 日期时间字符串。
- 【已验证】用户资源在服务端校验所有权；设备和诊断跨用户访问测试返回 `403`。
- 【已验证】常见错误包括 `401` 未认证或令牌无效、`403` 资源越权、`404` 资源/流程不存在、`409` 状态或并发冲突、`413` 文件过大/像素过多、`415` 图片格式无效、`422` 请求校验或安全阻断。
- 【已验证】测试覆盖 `in_progress → resolved` 与 `in_progress → unresolved`；步骤反馈使用 `resolved`、`not_resolved`。
- 【已验证】当前 OpenAPI 已提供图片附件、型号级知识检索/状态、文本/PDF 报告和 7 个管理员接口；没有刷新令牌、登出黑名单、知识上传/重建/停用、流程审核发布、评测结果持久化或生成式 RAG 回答接口。

## 2. 公共接口

### `POST /api/v1/auth/register`

【已验证】注册用户并直接返回 Bearer 访问令牌。

请求：

```json
{
  "email": "user@example.com",
  "password": "at-least-8-characters"
}
```

成功：`201 TokenResponse`。邮箱重复：`409`。

### `POST /api/v1/auth/login`

【已验证】邮箱密码登录。请求字段与注册相同，成功返回 `200 TokenResponse`，凭据错误返回 `401`。

### `GET /api/v1/auth/me`

【已验证】携带 Bearer JWT 返回当前 `UserRead`；API 测试覆盖成功响应。

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
| `GET /api/v1/admin/safety-blocks?limit=50` | `200 AdminSafetyBlockRead[]` | 最近安全阻断，`limit` 范围 1～100；不返回原始故障描述或描述哈希 |
| `GET /api/v1/admin/unresolved-reports?limit=50` | `200 AdminUnresolvedReportRead[]` | 最近未解决诊断的报告摘要；用户邮箱脱敏，不返回报告正文 |
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

【计划】当前没有知识上传/重建/停用、流程审核发布、评测执行或评测结果持久化管理员 API，不能把上述 7 个接口描述为完整管理后台。

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

创建诊断会话。后端用用户设备的型号和 `issue_category_code` 查找启用流程；找不到流程返回 `404`。

```json
{
  "device_id": 1,
  "issue_category_code": "return_to_dock_failure",
  "issue_description": "机器人多次尝试后仍找不到基站",
  "error_code": null
}
```

成功返回 `201 DiagnosticRead`。当前已发布流程：

- `JH69U1` + `return_to_dock_failure`
- `JH69U1` + `base_station_water_tank_issue`
- `VC35U1` + `wifi_setup_failure`
- `VC35U1` + `cleaning_noise`

`VC35U1 + navigation_abnormal` 当前为草稿，普通用户创建时返回 `404`。

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
  "top_k": 5,
  "min_score": 0.25
}
```

成功返回 `200 KnowledgeSearchResult[]`，每项包含 `score`、`content`、`document_title`、`source_url` 和 `page_number`。检索严格按 `robot_model_id` 过滤；低于阈值的结果不会返回。SQLite 分支读取 JSON Text 并在 Python 中计算余弦相似度；PostgreSQL 分支使用 `vector(256)` 和数据库 `<=>` 查询完成型号过滤、阈值、排序与 Top-K，SQL 形态已通过方言编译/单元断言。真实 PostgreSQL 执行仍为【待验证】，默认阈值 `0.25` 也尚未完成完整评测校准。

### `GET /api/v1/knowledge/status`

成功返回各型号的 `document_count`、`chunk_count` 和 `vector_count`。2026-07-20 本地入库证据为 JH69U1 `1/31/31`、VC35U1 `1/22/22`。

## 7. 附件接口

以下接口均为【已验证】并需要 Bearer JWT：

| 方法与路径 | 请求/响应 | 约束 |
| --- | --- | --- |
| `POST /api/v1/diagnostics/{diagnostic_id}/attachments` | multipart 单文件字段 `file`；返回 `201 AttachmentRead` | 只允许真实可解码 JPG/JPEG、PNG、WebP；最大 5MB、2500 万像素、每会话 5 张，仅进行中可上传 |
| `GET /api/v1/diagnostics/{diagnostic_id}/attachments` | `200 AttachmentRead[]` | 只列出当前用户该会话附件 |
| `DELETE /api/v1/diagnostics/{diagnostic_id}/attachments/{attachment_id}` | `204` | 同时删除数据库记录和本地文件 |

附件必须满足扩展名、客户端 MIME 与解码格式一致。附件使用随机存储名，API 只返回原始文件名、内容类型、大小和时间；当前不提供公开图片下载 URL，也不进行视觉诊断。删除时先隔离文件，清理失败会留下可追踪记录。

## 8. 报告接口

以下接口均为【已验证】并需要 Bearer JWT。

| 方法与路径 | 成功响应 | 约束 |
| --- | --- | --- |
| `POST /api/v1/diagnostics/{diagnostic_id}/report` | `201 ReportRead` | 仅 `unresolved` 会话可生成，否则 `409`；已存在时返回同一报告对象 |
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
    "created_at": "2026-07-20T12:00:00Z"
  }
}
```

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

- 【计划】前端不得通过隐藏按钮替代权限校验；以 API 的 `401/403/404/409/422` 为准。
- 【计划】`current_step=null` 可能表示已解决或未解决，必须同时检查 `diagnostic.status`。
- 【计划】只有 `unresolved` 状态展示“生成售后报告”；报告内容当前按纯文本显示。
- 【计划】客户端不得提交或持久化 Wi-Fi 明文密码、访问令牌到诊断描述或报告。

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

- 【已验证】`GET /health` 返回 `200 {"status":"ok"}`；迁移后的数据库启动测试已覆盖。它不属于版本化业务 API。
