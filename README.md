# Video2Api

一个本地运行的 `ixBrowser + Sora` 管理后台，支持：

- 登录鉴权（默认管理员 `Admin / Admin`）
- 读取 ixBrowser 分组与窗口
- 扫描 `Sora` 分组窗口账号与可用次数（结果入库，保留最近 10 次）
- 历史回填（本次失败时回填最近一次成功结果）
- 单窗口文生视频任务创建与状态监听（成功后返回任务链接）

## 目录结构

- `app/` 后端 FastAPI
- `admin/` 前端 Vue3 + Element Plus
- `data/video2api.db` SQLite 数据库

## 本地启动

1. 安装后端依赖

```bash
pip install -r requirements.txt
```

2. 初始化管理员

```bash
python scripts/init_admin.py
```

3. 安装前端依赖并构建

```bash
cd admin
npm install
npm run build
cd ..
```

4. 启动后端（自动托管前端静态资源）

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

5. 打开页面

- `http://127.0.0.1:8001/login`

## 开发文档与一键命令

- 开发手册：`docs/dev.md`
- 部署/迁移（备份管理员账号/系统设置/token）：`docs/deploy.md`
- ixBrowser Local API 速查：`docs/ixbrowser-local-api.md`
- AdsPower Local API 速查（参考/对照）：`docs/adspower-local-api.md`
- Sora 非付费额度说明：`docs/sora-quota.md`
- 自测清单（Playwright）：`docs/selftest.md`
- NopeCHA 反检测策略分析：`docs/nopecha-extension-anti-detection-analysis.md`
- 一键启动（Makefile）：`make backend-install init-admin admin-install admin-build backend-dev`
- 单元测试：`make test-unit`
- 离线 UI 自测（Playwright）：`make selftest-ui`

## 环境变量

参考 `.env.example`。

关键项：

- `IXBROWSER_API_BASE`（默认 `http://127.0.0.1:53200`）
- `PORT`（默认 `8001`）
