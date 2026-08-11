# RobotCare AI 运维指南

## 备份与恢复

- 每日备份：`./scripts/backup.sh`（DB 全量 pg_dump custom 格式 + /app/data 附件报告卷 + SHA256 清单，默认保留 14 天），crontab 示例见脚本头注释。
- 恢复演练（使用独立数据库，不操作生产库）：
  1. `docker compose exec -T postgres createdb -U robotcare robotcare_restore`
  2. `docker compose exec -T postgres pg_restore -U robotcare -d robotcare_restore < backups/robotcare_db_<stamp>.dump`
  3. 核对：`psql -tc "SELECT version_num FROM alembic_version"` 应等于当前 head；抽查 `users`/`user_devices`/`diagnostic_sessions`/`attachments`/`robot_models`/`generation_records` 等表行数与生产一致。
  4. 附件卷：解包 tar 后核对文件名与字节数和恢复库 `attachments` 表（`stored_filename`/`size_bytes`，表内无 sha256 列）一致，再抽查 tar 内文件与 backend 容器内 `/app/data/attachments/` 在线文件 SHA256 逐一相同。
- 已完成的恢复演练记录：`docs/evidence/pg_restore_drill_20260731.md`（迁移版本、8 张表的行数与附件 SHA 均已核对）。
- 告警 webhook：`ROBOTCARE_ALERT_WEBHOOK_URL`（企业微信/钉钉群机器人，`ROBOTCARE_ALERT_WEBHOOK_FORMAT=wecom|dingtalk|generic`）。高危安全阻断或生成服务不可用时触发告警；相同事件的冷却时间由 `ROBOTCARE_ALERT_COOLDOWN_SECONDS` 控制（默认 4 小时）。未配置时告警关闭，发送失败只记录日志且不影响业务。消息仅包含中文摘要，不得包含用户邮箱、问题原文或密钥。

## 内容缺口运营

- 数据来源：用户检索无结果或智能回答因资料缺口拒答时，系统写入 `knowledge_gap_events`（仅包含归一化查询与型号，不含用户身份；事件记录失败不影响主流程）。
- 每周例行：管理员打开控制台「内容缺口榜」区块（或 `GET /api/v1/admin/content-gaps?days=7&limit=20`），按次数倒序看高频缺口查询、涉及型号与最近发生时间。
- 决策动作：
  1. 缺口对应说明书里已有内容 → 检查该型号文档是否入库/分片质量，必要时通过控制台「上传知识 PDF」重新上传（同一来源 URL 会整体替换旧内容）。
  2. 缺口对应说明书没有的内容 → 列入内容采编计划（补充官方 FAQ/维护手册），入库后观察该查询是否不再出现在缺口榜中。
  3. 明显无效查询（闲聊、其他品牌）→ 不处理，作为噪声记录。
- 配套看板：概览页「智能回答运营（近 30 天）」显示回答数/拒答数与拒答原因分布；「资料缺口」占比下降即代表这批内容补充有效。
