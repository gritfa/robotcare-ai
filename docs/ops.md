
## 备份与恢复（阶段2新增）

- 每日备份：`./scripts/backup.sh`（DB 全量 pg_dump custom 格式 + /app/data 附件报告卷 + SHA256 清单，默认保留 14 天），crontab 示例见脚本头注释。
- 恢复演练（新库验证，不碰生产库）：
  1. `docker compose exec -T postgres createdb -U robotcare robotcare_restore`
  2. `docker compose exec -T postgres pg_restore -U robotcare -d robotcare_restore < backups/robotcare_db_<stamp>.dump`
  3. 核对：`psql -tc "SELECT version_num FROM alembic_version"` 应等于当前 head；抽查 `robot_models`/`generation_records` 行数与生产一致。
  4. 附件卷：解包 tar 后抽查任意附件 SHA256 与 `attachments` 表记录一致。
- 告警 webhook：`ROBOTCARE_ALERT_WEBHOOK_URL`（企业微信/钉钉群机器人，`ROBOTCARE_ALERT_WEBHOOK_FORMAT=wecom|dingtalk|generic`），触发事件=高危安全阻断、生成服务不可用；同事件冷却 `ROBOTCARE_ALERT_COOLDOWN_SECONDS`（默认 4 小时）；未配置即整体关闭；发送失败只记日志绝不影响业务。消息只含中文摘要，绝不含用户邮箱/问题原文/密钥。
