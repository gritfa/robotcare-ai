#!/usr/bin/env bash
# RobotCare AI 每日备份（Compose 部署）：PostgreSQL 全量 dump + 附件/报告数据卷。
# 用法：./scripts/backup.sh [备份目录]，默认 ./backups；建议 crontab 每日执行：
#   0 3 * * * cd /path/to/robotcare-ai && ./scripts/backup.sh >> backups/backup.log 2>&1
# 恢复演练步骤见 docs/ops.md「备份与恢复」。
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "${BACKUP_DIR}"

# 1. 数据库全量 dump（custom 格式，可用 pg_restore 恢复单表）
docker compose exec -T postgres pg_dump -U robotcare -d robotcare -Fc \
  > "${BACKUP_DIR}/robotcare_db_${STAMP}.dump"

# 2. 附件与报告数据卷（backend 容器内 /app/data）
docker compose exec -T backend tar -C /app -czf - data \
  > "${BACKUP_DIR}/robotcare_data_${STAMP}.tar.gz"

# 3. 记录 SHA256 便于恢复时校验
shasum -a 256 "${BACKUP_DIR}/robotcare_db_${STAMP}.dump" \
  "${BACKUP_DIR}/robotcare_data_${STAMP}.tar.gz" \
  >> "${BACKUP_DIR}/sha256sums.txt"

# 4. 清理过期备份（默认保留 14 天）
find "${BACKUP_DIR}" -name 'robotcare_*' -mtime "+${KEEP_DAYS}" -delete

echo "[OK] ${STAMP} 备份完成：$(ls -lh "${BACKUP_DIR}" | grep "${STAMP}" | wc -l | tr -d ' ') 个文件 → ${BACKUP_DIR}"
