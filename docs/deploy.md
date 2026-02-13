# Video2Api 部署与迁移（简版）

目标：把当前实例的管理员账号/密码、系统设置、各种 token 等，快速迁移到新服务器，避免每次部署后重复手动配置。

## 推荐方式：一键备份/一键恢复

### 1) 旧服务器：生成备份包

尽量先停止后端（避免并发写入；脚本也会用 SQLite backup API 做一致性拷贝）。

```bash
make state-backup
```

默认输出到：`data/backups/video2api-state-<时间戳>.tgz`

备份包含：
- `state/.env`（如果存在）
- `state/<sqlite>.db`（来自 `SQLITE_DB_PATH` 指向的库，包含 users/system_settings/watermark 等数据）
- `state/metadata.json`

### 2) 新服务器：恢复备份包

把 tgz 拷到新服务器后执行（示例强制覆盖已有文件）：

```bash
make state-restore ARGS="--backup /path/to/video2api-state-xxx.tgz --restore-env --force-env --force-db"
```

说明：
- `--restore-env`：把备份包里的 `state/.env` 写回项目根目录 `.env`
- `--force-env`：覆盖已有 `.env`
- `--force-db`：覆盖已有 SQLite 文件
- 如需指定落库路径：加 `--db-path /your/path/video2api.db`

## 重要提醒（避免误丢数据）

- 迁移/线上建议把 `.env` 的 `SQLITE_RESET_ON_SCHEMA_MISMATCH=False`。
  - 否则当代码与数据库 schema 版本不一致时，可能会自动重建数据库（清空历史数据）。

