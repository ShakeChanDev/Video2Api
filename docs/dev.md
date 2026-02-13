# Video2Api 开发指南

## 环境要求
- Python 3.11+
- Node.js 20+（CI 使用 Node 20，本地更高版本通常也可）
- npm
- 可选：Playwright 浏览器（仅本地 e2e/真实浏览器自动化需要）
- Sora 额度说明（非付费）：见 `docs/sora-quota.md`

## 快速开始（推荐）
一键命令在仓库根目录执行：
```bash
make backend-install
make init-admin
make admin-install
make admin-build
make backend-dev
```

启动后访问：
- `http://127.0.0.1:8001/login`

## 后端开发（FastAPI）
1. 安装依赖
```bash
python -m pip install -r requirements.txt
```

2. 配置环境变量
- 复制 `.env.example` 为 `.env` 并按需修改

### 部署免手填（bootstrap，可选）
为了让新服务器部署后不再手动录入“管理员账号/密码、去水印解析配置”，可以在 `.env` 中配置以下变量：

- 管理员自举：
  - `BOOTSTRAP_ADMIN_USERNAME`：可选，默认 `Admin`
  - `BOOTSTRAP_ADMIN_PASSWORD`：必填；为空则不启用自举
  - 行为：当 `BOOTSTRAP_ADMIN_PASSWORD` 非空时，服务每次启动都会确保该用户存在；若已存在则会把密码重置为 `.env` 指定值（便于部署一致性）。
- 去水印自举（custom 解析服务）：
  - `BOOTSTRAP_WATERMARK_CUSTOM_PARSE_URL/TOKEN/PATH`：任一非空则写入数据库 `watermark_free_config(id=1)`；仅覆盖你提供的非空字段。
  - `BOOTSTRAP_WATERMARK_CUSTOM_PARSE_PATH` 会自动补全前导 `/`。
- 对外视频接口 Token：
  - `VIDEO_API_BEARER_TOKEN`：本来就支持从 `.env` 读取，无需写入数据库。

示例（不要提交真实 token/密码）：
```bash
BOOTSTRAP_ADMIN_USERNAME=Admin
BOOTSTRAP_ADMIN_PASSWORD=ChangeMe123!
BOOTSTRAP_WATERMARK_CUSTOM_PARSE_URL=http://127.0.0.1:18080
BOOTSTRAP_WATERMARK_CUSTOM_PARSE_TOKEN=your-token-here
BOOTSTRAP_WATERMARK_CUSTOM_PARSE_PATH=/get-sora-link
VIDEO_API_BEARER_TOKEN=your-video-api-token
```

3. 初始化默认管理员
```bash
python scripts/init_admin.py
```

4. 启动后端（开发模式）
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

说明：
- `.env` 中的 `PORT` 需要与你的启动参数保持一致（或直接用 `make backend-dev PORT=xxxx` 统一）。
- SQLite 默认使用 `data/video2api.db`，可通过 `SQLITE_DB_PATH` 指定路径。
- 当 SQLite schema 版本与代码不一致时，默认会自动重建数据库（不保留历史数据）；如需禁用自动重建，设置 `SQLITE_RESET_ON_SCHEMA_MISMATCH=False` 并手动处理数据库文件。

### 对外视频接口（`/v1/videos`）
- 该接口用于第三方系统直接创建/查询视频任务，底层复用现有 Sora 任务内核。
- 鉴权采用独立 Bearer Token：`Authorization: Bearer <TOKEN>`。
- 默认可在 `.env` 中配置 `VIDEO_API_BEARER_TOKEN`，也可在管理后台系统设置里更新 Token（保存后立即生效）。
- 未配置时接口返回 `503`（关闭状态）。
- 已提供接口：
  - `POST /v1/videos`：创建任务
  - `GET /v1/videos/{video_id}`：查询任务（支持 `107` 或 `video_107`）

### ixBrowser 服务结构（重构后）
- `app/services/ixbrowser_service.py`：主协调层（对外服务入口、扫描/调度编排、模型构建）。
- `app/services/ixbrowser/realtime_quota_service.py`：实时配额监听、入库与 SSE 推送。
- `app/services/ixbrowser/sora_job_runner.py`：Sora 任务阶段状态机与去水印收尾。
- `app/services/ixbrowser/sora_publish_workflow.py`：Sora 发布链路（发布、草稿检索、页面请求/轮询、发布链接捕获）。
- `app/services/ixbrowser/sora_generation_workflow.py`：Sora 生成链路（提交、进度轮询、genid 获取、兼容生成任务发布）。
- ixBrowser Local API 速查：`docs/ixbrowser-local-api.md`
- AdsPower Local API 速查（参考/对照）：`docs/adspower-local-api.md`

## 前端开发（admin/）
1. 安装依赖
```bash
cd admin
npm ci
```

2. 启动开发服务器
```bash
npm run dev
```

3. 构建静态资源（后端会托管 `admin/dist`）
```bash
npm run build
```

## 测试
单元测试（默认离线可跑）：
```bash
pytest -m unit
```

e2e（本地才跑）：
```bash
pytest -m e2e
```

## Playwright（可选）
如果需要本地真实浏览器自动化（例如 e2e 或调试），先安装浏览器：
```bash
python -m playwright install
```

### Playwright 反检测配置
- `PLAYWRIGHT_STEALTH_ENABLED`：是否启用反检测增强（默认 `True`）。
- `PLAYWRIGHT_STEALTH_PLUGIN_ENABLED`：是否启用 `playwright-stealth` 插件（默认 `True`）。插件不可用时会自动降级到内置脚本。
- UA/指纹：Playwright 侧不再强制覆盖 UA（避免与指纹浏览器 Profile 配置不一致）。如需调整 UA，请在 ixBrowser Profile 中修改。
- `PLAYWRIGHT_RESOURCE_BLOCKING_MODE`：资源拦截策略。
  - `light`（默认）：仅拦截 `media`。
  - `legacy`：拦截 `image/media/font`（历史兼容）。
  - `off`：关闭资源拦截。

建议：
- 若线上稳定性下降，优先回退 `PLAYWRIGHT_RESOURCE_BLOCKING_MODE=legacy/off`。
- 若第三方依赖异常，可设置 `PLAYWRIGHT_STEALTH_PLUGIN_ENABLED=False`，保留内置反检测脚本。

## 日志 V2（统一事件模型）
- 新日志统一写入 `event_logs`（`api/audit/task/system`），旧表 `audit_logs`、`sora_job_events` 保留但不再作为日志中心主数据源。
- 日志中心接口：
  - `GET /api/v1/admin/logs`：游标分页查询（`items/has_more/next_cursor`）
  - `GET /api/v1/admin/logs/stats`：统计卡片数据（总量、失败率、P95、Top）
  - `GET /api/v1/admin/logs/stream`：SSE 实时流（`event: log` / `event: ping`）
- 默认策略：
  - API 日志全量采集（可通过配置改为 `failed_slow` 或 `failed_only`）
  - 仅记录 `path + query`，不记录请求体
  - 慢请求阈值默认 `2000ms`
  - 脱敏模式默认 `basic`
  - 事件日志保留默认 `30` 天
  - 事件日志大小上限默认 `100MB`（超限后按时间从旧到新自动裁剪）
- 相关环境变量（见 `.env.example`）：
  - `EVENT_LOG_RETENTION_DAYS`
  - `EVENT_LOG_CLEANUP_INTERVAL_SEC`
  - `EVENT_LOG_MAX_MB`
  - `API_LOG_CAPTURE_MODE`
  - `API_SLOW_THRESHOLD_MS`
  - `LOG_MASK_MODE`
  - `SYSTEM_LOGGER_INGEST_LEVEL`

## 常见问题排查
- ixBrowser 连接不上：检查 `.env` 的 `IXBROWSER_API_BASE`，并确认本地 ixBrowser 服务已启动。
- Playwright 报浏览器缺失：执行 `python -m playwright install` 或 `make playwright-install`。
- 端口冲突：修改 `.env` 的 `PORT`，并在启动命令或 `make backend-dev PORT=xxxx` 中使用相同端口。
