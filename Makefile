COMPOSE := docker compose -f deploy/compose/docker-compose.yml
IM_COMPOSE := docker compose -f deploy/compose/openim/docker-compose.yml

.PHONY: dev-up dev-down dev-reset im-up im-down im-reset \
	backend-install migrate backend-dev backend-test backend-lint \
	frontend-install console-dev platform-dev frontend-test frontend-build \
	openapi test

# ---- 开发环境 ----
dev-up:
	$(COMPOSE) up -d --wait

dev-down:
	$(COMPOSE) down

dev-reset:
	$(COMPOSE) down -v

# OpenIM（P1 起需要）：REST localhost:10002，WebSocket localhost:10001
im-up:
	$(IM_COMPOSE) up -d --wait

im-down:
	$(IM_COMPOSE) down

im-reset:
	$(IM_COMPOSE) down -v

# ---- 后端 ----
backend-install:
	cd backend && uv sync

migrate:
	cd backend && uv run alembic upgrade head

# 监听 0.0.0.0：OpenIM 容器通过 host.docker.internal 回调后端
backend-dev:
	cd backend && uv run uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000

backend-test:
	cd backend && uv run pytest

backend-lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app

# ---- 前端 ----
frontend-install:
	cd frontend && pnpm install

console-dev:
	cd frontend && pnpm --filter @edp/console dev

platform-dev:
	cd frontend && pnpm --filter @edp/platform-admin dev

frontend-test:
	cd frontend && pnpm -r test

frontend-build:
	cd frontend && pnpm -r build

# 后端接口变更后执行：导出 OpenAPI 并重新生成前端类型
openapi:
	cd backend && uv run python -m app.cli export-openapi ../frontend/packages/api-client/openapi.json
	cd frontend && pnpm --filter @edp/api-client generate

test: backend-test frontend-test
