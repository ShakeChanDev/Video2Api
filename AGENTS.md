# Video2Api 开发协作指南（给 AI / 新同事）

## 项目一句话
本项目是一个本地运行的 `ixBrowser + Sora` 管理后台：后端 `FastAPI`，前端 `Vue3 + Element Plus`，并使用 `SQLite` 作为本地持久化。

## 语言与提交约定
- 中文沟通。
- git 操作不需要和你确认（按需创建分支、提交、推送、开 PR）。
- commit message 用中文。

## 目录地图（从入口到业务）
- `app/main.py`：FastAPI 入口，路由挂载、静态资源托管（`admin/dist`）、请求日志与审计日志中间件。
- `app/api/`：HTTP API（`auth/ixbrowser/sora/nurture/admin`）。
- `app/services/`：核心业务编排（ixBrowser/Sora 自动化、账号调度、养号等）。
- `app/db/sqlite.py`：SQLite schema 初始化与轻量迁移（`CREATE TABLE` + 条件 `ALTER TABLE`）。
- `app/models/`：Pydantic 数据模型。
- `admin/`：管理台前端（Vite + Vue3）。
- `tests/`：pytest 测试；`unit` 默认离线可跑，`e2e` 依赖本地环境。
- `_refs/`：参考项目代码，只读，不作为改动目标（除非明确要求）。

## 常用命令（优先使用 Makefile）
后端：
- 安装依赖：`make backend-install`
- 初始化默认管理员：`make init-admin`（默认 `Admin / Admin`）
- 启动后端（开发模式）：`make backend-dev`

前端：
- 安装依赖：`make admin-install`
- 本地开发：`make admin-dev`
- 构建静态资源：`make admin-build`

测试：
- 单元测试（默认）：`make test-unit`
- e2e（本地才跑）：`make test-e2e`

等价原生命令（当 Makefile 不可用时）：
- `python -m pip install -r requirements.txt`
- `python scripts/init_admin.py`
- `python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload`
- `python -m pytest -m unit`
- `cd admin && npm ci && npm run build`

## 改动必须自证（Definition of Done）
- 修改后端：至少跑 `make test-unit`，并确保与改动相关的用例覆盖到关键路径。
- 修改前端：至少跑 `make admin-build`。
- 新增/修改环境变量：必须同步更新 `app/core/config.py` 与 `.env.example`，并在 `docs/dev.md` 补充说明（如需要）。
- 不把构建产物或本地数据提交到 git（见下方“数据边界”）。

## 安全与数据边界
- 不在 PR/日志/截图/对话里泄露：`.env` 真值、Sora/ixBrowser token、cookies、账号信息、任何可复用的登录态。
- 不要把以下内容加入版本库：`data/`、`logs/`、`admin/dist/`、`admin/node_modules/`（仓库已 `gitignore`，也不要手动 `git add -f`）。

## 任务描述模板（给 AI）
```text
目标：
范围（要改/不改）：
验收标准（可测的）：
相关文件（路径）：
验证命令（本地 / CI）：
风险点/兼容要求：
```

