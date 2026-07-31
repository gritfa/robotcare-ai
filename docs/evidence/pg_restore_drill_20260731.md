# PostgreSQL 备份真实恢复演练（2026-07-31，macOS 本机 Compose 栈）

环境：Docker Compose 三服务栈（postgres/backend/frontend 全部 healthy），backend 镜像为当前 main 代码重新构建，启动自动迁移至 `20260730_0009`。演练按 `docs/ops.md`「备份与恢复」流程执行，全程真实命令，不碰生产库。

## 数据准备

`app.demo_data_cli load --replace --yes` 加载确定性合成演示数据：7 用户 / 10 设备 / 12 诊断（4 in_progress + 4 resolved + 4 unresolved）/ 4 附件 / 4 报告 / 4 安全阻断。

## 执行与结果

1. **备份**：`./scripts/backup.sh` → `backups/robotcare_db_20260731_092919.dump`（pg_dump custom）+ `robotcare_data_20260731_092919.tar.gz`（/app/data 卷），SHA256 已记入 `backups/sha256sums.txt`：
   - dump: `9cdc507b2ab0…8043c323b5`；data tar: `994921cb3c94…24be9cbff0`
2. **恢复到新库**：`createdb robotcare_restore` → `pg_restore -d robotcare_restore < dump` 无错误退出。
3. **迁移版本核对**：恢复库 `alembic_version = 20260730_0009`，与当前代码 head 一致。
4. **行数核对**（生产库 vs 恢复库，逐表完全一致）：

   | 表 | robotcare | robotcare_restore |
   |---|---|---|
   | users | 7 | 7 |
   | user_devices | 10 | 10 |
   | diagnostic_sessions | 12 | 12 |
   | attachments | 4 | 4 |
   | robot_models | 5 | 5 |
   | generation_records | 0 | 0 |
   | knowledge_chunks | 0 | 0 |
   | service_reports | 4 | 4 |

5. **附件卷完整性**：解包 tar 后 4 个附件文件齐全；抽查 2 个，`size_bytes` 与恢复库 `attachments` 表记录一致，且 tar 内文件 SHA256 与 backend 容器内在线文件逐字节一致：
   - `demo-diagnostic-1.png` 9333B `61d83088acb1…577caf10`（tar == 容器内）
   - `demo-diagnostic-3.png` 9498B `6f1c8811cbd7…d82ce14cb`（tar == 容器内）

## 附带修正

原 ops.md 演练第 4 步写"附件 SHA256 与 `attachments` 表记录一致"，但该表并无 sha256 列（只有 `stored_filename`/`size_bytes`）——本次已把手册改为可执行的核对方式（文件名与字节数对表 + tar 内文件与容器内在线文件比 SHA256）。

## 边界声明

- 本演练在本机 Compose 单实例上完成，数据为合成演示数据；生产机首次部署后应用真实数据重跑一遍同流程。
- generation_records/knowledge_chunks 为 0 行（本机未配 DashScope key，无生成与向量数据），行数核对逻辑不受影响。
