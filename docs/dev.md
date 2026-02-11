# Video2Api 开发指南

## 环境要求
- Python 3.11+
- Node.js 20+（CI 使用 Node 20，本地更高版本通常也可）
- npm
- 可选：Playwright 浏览器（仅本地 e2e/真实浏览器自动化需要）

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

## 日志 V2（统一事件模型）
- 新日志统一写入 `event_logs`（`api/audit/task/system`），旧表 `audit_logs`、`sora_job_events` 保留但不再作为日志中心主数据源。
- 日志中心接口：
  - `GET /api/v1/admin/logs`：游标分页查询（`items/has_more/next_cursor`）
  - `GET /api/v1/admin/logs/stats`：统计卡片数据（总量、失败率、P95、Top）
  - `GET /api/v1/admin/logs/stream`：SSE 实时流（`event: log` / `event: ping`）
  - `GET /api/v1/admin/sora-requests/dashboard`：Sora 请求看板聚合数据（趋势/Top/延迟/状态码/热力图/样本）
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

### Sora 请求看板接口说明
- 统计口径：仅统计服务端直连外呼（`httpx/curl-cffi`）到 `*.chatgpt.com` 的请求。
  - 匹配规则：`host == 'chatgpt.com'` 或 `host` 以 `.chatgpt.com` 结尾。
  - 不统计：本地 API 入站日志、浏览器上下文内页面 `fetch/xhr`。
- 落库口径：`event_logs` 中 `source='task'` 且 `resource_type='sora_outbound'`。
- 关键参数：
  - `window`：`1h|6h|24h|7d`（默认 `24h`）
  - `bucket`：`auto|1m|5m|1h`（默认 `auto`）
  - `endpoint_limit`：`5~30`（默认 `10`）
  - `host`：按目标域名精确过滤（可选）
  - `transport`：`all|httpx|curl_cffi`（默认 `all`）
  - `profile_id`：按 profile 过滤（可选）
  - `sample_limit`：样本条数 `10~100`（默认 `30`）

## 常见问题排查
- ixBrowser 连接不上：检查 `.env` 的 `IXBROWSER_API_BASE`，并确认本地 ixBrowser 服务已启动。
- Playwright 报浏览器缺失：执行 `python -m playwright install` 或 `make playwright-install`。
- 端口冲突：修改 `.env` 的 `PORT`，并在启动命令或 `make backend-dev PORT=xxxx` 中使用相同端口。
