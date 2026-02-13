.DEFAULT_GOAL := help

.PHONY: help \
	backend-install playwright-install init-admin backend-dev \
	reset-user-password \
	state-backup state-restore \
	test-unit test-e2e \
	selftest-ui selftest-heavy-load selftest-nurture \
	admin-install admin-dev admin-build

PY ?= python3
NPM ?= npm

HOST ?= 0.0.0.0
PORT ?= 8001

help:
	@echo "用法: make <目标>"
	@echo ""
	@echo "后端:"
	@echo "  backend-install     安装 Python 依赖 (requirements.txt)"
	@echo "  playwright-install  安装 Playwright 浏览器 (仅本地/e2e)"
	@echo "  init-admin          初始化默认管理员 (Admin/Admin)"
	@echo "  reset-user-password 重置后台用户密码（适用于忘记密码）"
	@echo "  state-backup        备份部署状态（.env + SQLite -> data/backups/*.tgz）"
	@echo "  state-restore       恢复部署状态（从 tgz 写回 .env + SQLite）"
	@echo "  backend-dev         启动后端 (uvicorn, dev 模式)"
	@echo ""
	@echo "前端 (admin/):"
	@echo "  admin-install       安装前端依赖 (npm ci)"
	@echo "  admin-dev           启动 Vite 开发服务器"
	@echo "  admin-build         构建前端静态资源"
	@echo ""
	@echo "测试:"
	@echo "  test-unit           运行单元测试 (默认离线)"
	@echo "  test-e2e            运行 e2e (仅本地)"
	@echo ""
	@echo "自测 (Playwright):"
	@echo "  selftest-ui         Level A：离线 UI 自测（需 admin/dist）"
	@echo "  selftest-heavy-load heavy load 自动换号续跑（需 admin/dist）"
	@echo "  selftest-nurture    Level B：真实环境养号 e2e（需 ixBrowser/Sora）"

backend-install:
	$(PY) -m pip install -r requirements.txt

playwright-install:
	$(PY) -m playwright install

init-admin:
	$(PY) scripts/init_admin.py

reset-user-password:
	$(PY) scripts/reset_user_password.py

state-backup:
	$(PY) scripts/state_backup.py

# 用法示例：
#   make state-restore ARGS="--backup /path/to/video2api-state-xxx.tgz --restore-env --force-env --force-db"
state-restore:
	$(PY) scripts/state_restore.py $(ARGS)

backend-dev:
	$(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT) --reload

test-unit:
	$(PY) -m pytest -m unit

test-e2e:
	$(PY) -m pytest -m e2e

selftest-ui:
	SELFTEST_E2E=1 $(PY) -m pytest -m e2e -k admin_selftest

selftest-heavy-load:
	HEAVY_LOAD_E2E=1 $(PY) -m pytest -m e2e -k heavy_load

selftest-nurture:
	@echo "需要真实 ixBrowser/Sora 环境。示例：SORA_NURTURE_E2E=1 SORA_NURTURE_PROFILE_ID=39 SORA_NURTURE_GROUP_TITLE=Sora make selftest-nurture"
	$(PY) -m pytest -m e2e -k sora_nurture

admin-install:
	cd admin && $(NPM) ci

admin-dev:
	cd admin && $(NPM) run dev

admin-build:
	cd admin && $(NPM) run build
