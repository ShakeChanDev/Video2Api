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

## 常见问题排查
- ixBrowser 连接不上：检查 `.env` 的 `IXBROWSER_API_BASE`，并确认本地 ixBrowser 服务已启动。
- Playwright 报浏览器缺失：执行 `python -m playwright install` 或 `make playwright-install`。
- 端口冲突：修改 `.env` 的 `PORT`，并在启动命令或 `make backend-dev PORT=xxxx` 中使用相同端口。

