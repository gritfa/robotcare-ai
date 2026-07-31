
## 备份与恢复（阶段2新增）

- 每日备份：`./scripts/backup.sh`（DB 全量 pg_dump custom 格式 + /app/data 附件报告卷 + SHA256 清单，默认保留 14 天），crontab 示例见脚本头注释。
- 恢复演练（新库验证，不碰生产库）：
  1. `docker compose exec -T postgres createdb -U robotcare robotcare_restore`
  2. `docker compose exec -T postgres pg_restore -U robotcare -d robotcare_restore < backups/robotcare_db_<stamp>.dump`
  3. 核对：`psql -tc "SELECT version_num FROM alembic_version"` 应等于当前 head；抽查 `users`/`user_devices`/`diagnostic_sessions`/`attachments`/`robot_models`/`generation_records` 等表行数与生产一致。
  4. 附件卷：解包 tar 后核对文件名与字节数和恢复库 `attachments` 表（`stored_filename`/`size_bytes`，表内无 sha256 列）一致，再抽查 tar 内文件与 backend 容器内 `/app/data/attachments/` 在线文件 SHA256 逐一相同。
- 已执行的真实演练记录：`docs/evidence/pg_restore_drill_20260731.md`（迁移版本 + 8 表行数 + 附件 SHA 全部一致）。
- 告警 webhook：`ROBOTCARE_ALERT_WEBHOOK_URL`（企业微信/钉钉群机器人，`ROBOTCARE_ALERT_WEBHOOK_FORMAT=wecom|dingtalk|generic`），触发事件=高危安全阻断、生成服务不可用；同事件冷却 `ROBOTCARE_ALERT_COOLDOWN_SECONDS`（默认 4 小时）；未配置即整体关闭；发送失败只记日志绝不影响业务。消息只含中文摘要，绝不含用户邮箱/问题原文/密钥。

## 内容缺口榜运营用法（阶段3新增）

- 数据来源：用户检索无结果、以及智能回答因资料缺口拒答时，系统各写一条 `knowledge_gap_events`（只含归一化查询与型号，不含用户身份；埋点失败不影响主流程）。
- 每周例行：管理员打开控制台「内容缺口榜」区块（或 `GET /api/v1/admin/content-gaps?days=7&limit=20`），按次数倒序看高频缺口查询、涉及型号与最近发生时间。
- 决策动作：
  1. 缺口对应说明书里已有内容 → 检查该型号文档是否入库/分片质量，必要时通过控制台「上传知识 PDF」重新上传（同一来源 URL 会整体替换旧内容）。
  2. 缺口对应说明书没有的内容 → 列入下一批内容采编计划（补充官方 FAQ/维护手册），入库后观察该查询是否从榜上消失。
  3. 明显无效查询（闲聊、其他品牌）→ 不处理，作为噪声记录。
- 配套看板：概览页「智能回答运营（近 30 天）」显示回答数/拒答数与拒答原因分布；「资料缺口」占比下降即代表这批内容补充有效。
