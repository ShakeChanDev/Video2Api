.DEFAULT_GOAL := help

.PHONY: help \
	backend-install playwright-install init-admin backend-dev \
	test-unit test-e2e \
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

backend-install:
	$(PY) -m pip install -r requirements.txt

playwright-install:
	$(PY) -m playwright install

init-admin:
	$(PY) scripts/init_admin.py

backend-dev:
	$(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT) --reload

test-unit:
	$(PY) -m pytest -m unit

test-e2e:
	$(PY) -m pytest -m e2e

admin-install:
	cd admin && $(NPM) ci

admin-dev:
	cd admin && $(NPM) run dev

admin-build:
	cd admin && $(NPM) run build
