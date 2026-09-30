COMPOSE := docker compose -f deploy/compose/docker-compose.yml
IM_COMPOSE := docker compose -f deploy/compose/openim/docker-compose.yml
OBS_COMPOSE := docker compose -f deploy/compose/observability/docker-compose.yml

.PHONY: dev-up dev-down dev-reset im-up im-down im-reset obs-up obs-down alerts-check \
	backend-install migrate backend-dev worker-dev scheduler-dev backend-test backend-lint \
	frontend-install console-dev platform-dev widget-dev frontend-lint frontend-test \
	frontend-build e2e-stack e2e openapi test

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

# 可观测性（Prometheus :9090、Grafana :3000、Jaeger :16686），见 README「可观测性」
obs-up:
	$(OBS_COMPOSE) up -d

obs-down:
	$(OBS_COMPOSE) down

# 校验告警规则并运行规则单测（promtool）
alerts-check:
	docker run --rm -v "$(CURDIR)/deploy/observability/prometheus:/rules" \
		--entrypoint promtool prom/prometheus:v3.5.0 check rules /rules/alerts.yml
	docker run --rm -v "$(CURDIR)/deploy/observability/prometheus:/rules" \
		--entrypoint promtool prom/prometheus:v3.5.0 test rules /rules/alerts.test.yml

# ---- 后端 ----
backend-install:
	cd backend && uv sync

migrate:
	cd backend && uv run alembic upgrade head

# 监听 0.0.0.0：OpenIM 容器通过 host.docker.internal 回调后端
backend-dev:
	cd backend && uv run uvicorn app.main:create_app --factory --reload --host 0.0.0.0 --port 8000

# 调度进程：每分钟按 seq 对账（需要 make im-up）
worker-dev:
	cd backend && uv run python -m app.worker

scheduler-dev:
	cd backend && uv run python -m app.scheduler

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

# 访客 Widget：http://localhost:5175/?key=<渠道 key>（控制台"设置"页可直接打开）
widget-dev:
	cd frontend && pnpm --filter @edp/widget dev

# ESLint（Vue、TypeScript）和类型检查
frontend-lint:
	cd frontend && pnpm lint && pnpm -r typecheck

frontend-test:
	cd frontend && pnpm -r test

frontend-build:
	cd frontend && pnpm -r build

# ---- 浏览器验收 ----
# 在后台启动验收需要的全部进程（模拟大模型、企业微信和 clamd；日志在 e2e-logs/），前置 make dev-up、make migrate
e2e-stack:
	bash scripts/ci/start-stack.sh

# 运行验收脚本：make e2e E2E="p0-acceptance g6-ops-observability"（需要 PLATFORM_PASSWORD）
E2E ?= p0-acceptance
e2e:
	bash scripts/ci/run-e2e.sh $(E2E)

# 后端接口变更后执行：导出 OpenAPI 并重新生成前端类型
openapi:
	cd backend && uv run python -m app.cli export-openapi ../frontend/packages/api-client/openapi.json
	cd frontend && pnpm --filter @edp/api-client generate

test: backend-test frontend-test
