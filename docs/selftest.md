# Playwright 自测清单（发布/合并前）

本文件把“发布/合并前自测”固化为两层，避免每次靠记忆或临时沟通。

## Level A：离线 UI 自测（推荐每次合并前执行）

特点：
- 不依赖真实 ixBrowser/Sora
- 后端通过 monkeypatch stub 外部依赖
- 使用临时 SQLite DB + 启动临时 FastAPI + Playwright 跑一条 UI 冒烟链路

覆盖范围：
- 登录（Admin/Admin）
- 账号页：点击“扫描账号与次数”并看到表格行
- 任务页：手动指定窗口创建任务并在列表出现
- 养号页：创建 batch 并取消
- 日志页：能加载列表并打开详情抽屉

前置条件：
```bash
make backend-install
make admin-install
make admin-build
make playwright-install   # 首次或本机未安装 Playwright 浏览器时执行
```

运行：
```bash
make selftest-ui
```

可视化调试（非 headless）：
```bash
PW_HEADLESS=0 make selftest-ui
```

## Level B：真实环境 e2e（可选，仅本地）

特点：
- 依赖本地 ixBrowser 服务已启动
- 依赖对应 profile 已登录 Sora
- 不进入 CI（CI 不具备本地环境与登录态）

运行示例：
```bash
SORA_NURTURE_E2E=1 \
SORA_NURTURE_PROFILE_ID=39 \
SORA_NURTURE_GROUP_TITLE=Sora \
make test-e2e -k sora_nurture
```

## 常见问题排查

1) 找不到 `admin/dist/index.html`
- 先执行：`make admin-build`

2) Playwright 报浏览器缺失/无法启动
- 先执行：`make playwright-install`

3) 端口占用/服务未就绪
- 自测会自动选择空闲端口；若仍失败，检查本机安全软件/代理是否拦截本地端口访问

