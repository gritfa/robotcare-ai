# RobotCare AI 合成演示数据说明

## 数据边界

本数据集用于本地功能检查和求职演示。邮箱使用 `example.com` 保留域名，序列号、故障描述、图片和操作记录均为程序生成，不对应任何真实消费者或真实设备。`app.demo_data_cli` 在 `production` 环境拒绝运行。

## 覆盖矩阵

| 检查面 | 合成数据 | 可验证页面或接口 |
| --- | --- | --- |
| 普通用户隔离 | 5 个启用用户、每人 2 台设备 | 设备、诊断历史；不同账号数据互不可见 |
| 账号状态 | 1 个停用用户 | 登录应返回停用错误 |
| 管理员 | 1 个管理员 | `/admin` 运营概览和 RBAC |
| 型号 | JH69U1、VC35U1 各 5 台设备 | 设备列表、诊断选项 |
| 发布流程 | 4 条发布流程各 3 个状态 | 当前步骤、已解决、未解决历史 |
| 诊断状态 | 进行中/已解决/未解决各 4 个 | 诊断历史与恢复 |
| 图片 | 4 张真实 PNG 合成图片 | 附件列表与报告附件名称 |
| 售后报告 | 4 份文本报告、4 份 PDF | 用户报告、管理员未解决报告 |
| 安全阻断 | 冒烟、电池、进水、短接共 4 条 | 管理员安全事件列表与详情审计 |
| 审计 | 4 种管理员动作 | 管理员审计日志 |

限流、并发、越权、迁移和 PostgreSQL 方言仍由自动化测试验证；不向演示账号预置锁定或超限状态，以免导致页面无法正常操作。

## 本地账号

统一默认密码：`Demo123456`。可在加载前通过 `ROBOTCARE_DEMO_PASSWORD` 临时覆盖。

- 普通用户：`demo.alice@example.com`
- 管理员：`demo.admin@example.com`
- 停用账号：`demo.disabled@example.com`

其余普通账号为 `demo.bob@example.com`、`demo.chen@example.com`、`demo.li@example.com` 和 `demo.wang@example.com`。

## 命令

```powershell
cd backend
.\.venv\Scripts\python.exe -m app.demo_data_cli load --replace --yes
.\.venv\Scripts\python.exe -m app.demo_data_cli summary
.\.venv\Scripts\python.exe -m app.demo_data_cli reset --yes
```

`--replace` 和 `reset` 只处理上述受管理邮箱及其设备、诊断、会话、附件、报告、安全事件和审计记录；自动化测试同时证明非演示账号会被保留。

## 推荐检查顺序

1. 用普通账号登录，检查两台设备和诊断历史的三种状态。
2. 打开进行中诊断，确认一次只展示一个步骤。
3. 打开未解决诊断，查看附件名称、报告正文和 PDF。
4. 换另一个普通账号，确认看不到第一位用户的数据。
5. 使用管理员账号检查运营概览、安全阻断、未解决报告和审计日志。
6. 使用停用账号登录，确认服务器拒绝访问。

完成以上检查只能表述为“本地合成数据环境通过”。真实 PostgreSQL、Docker/HTTPS、远端 CI、生产负载和真实用户 Beta 仍需独立证据。
