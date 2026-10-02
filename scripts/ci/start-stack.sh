#!/usr/bin/env bash
# 启动浏览器验收需要的平台进程（CI 与本地共用），就绪后返回：
#   模拟大模型 :8900、模拟企业微信 :8901、模拟 clamd :3310、
#   模拟邮箱 IMAP :1143、SMTP :1025（控制接口 :8903），模拟云打印机厂商 :8904，
#   API :8000、实时消费进程、调度进程（Prometheus 指标分别在 :9464、:9465、:9466），
#   控制台 :5173、运营后台 :5174、访客 Widget :5175。
# 前置：deploy/compose 的开发环境已启动并完成迁移（make dev-up、make migrate）；需要 OpenIM 的
# 验收另外 make im-up。日志写到 $LOG_DIR（默认 e2e-logs）。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LOG_DIR=$(mkdir -p "${LOG_DIR:-$ROOT/e2e-logs}" && cd "${LOG_DIR:-$ROOT/e2e-logs}" && pwd)
set -a
# shellcheck source=/dev/null
source "$ROOT/scripts/ci/e2e.env"
set +a

start() { # 名称 目录 命令...
  local name=$1 dir=$2
  shift 2
  (cd "$dir" && nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &)
}

wait_for() { # 地址 [秒]
  local url=$1 deadline=$((SECONDS + ${2:-120}))
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    if ((SECONDS > deadline)); then
      echo "timed out waiting for $url" >&2
      return 1
    fi
    sleep 1
  done
}

wait_port() { # 端口 [秒]
  local port=$1 deadline=$((SECONDS + ${2:-60}))
  until (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
    if ((SECONDS > deadline)); then
      echo "timed out waiting for port $port" >&2
      return 1
    fi
    sleep 1
  done
}

# 后端进程直接用虚拟环境里的 python 启动，不用 `uv run`：`uv run` 在进程运行期间一直占着 uv 的
# 缓存锁，CI 里 setup-uv 收尾时的 `uv cache prune` 会等锁超时（依赖变化、缓存没有命中时）。
cd "$ROOT/backend"
uv sync --quiet
PY="$ROOT/backend/.venv/bin/python"
# 对象存储桶（已存在时跳过）。
"$PY" -m app.cli storage-init >/dev/null
start fake-llm "$ROOT/backend" "$PY" -m tests.fake_llm --port 8900
start fake-clamd "$ROOT/backend" "$PY" -m tests.fake_clamd --port 3310
start fake-wecom "$ROOT/backend" "$PY" -m tests.fake_wecom --port 8901 --platform http://127.0.0.1:8000
start fake-mail "$ROOT/backend" "$PY" -m tests.fake_mail --imap-port 1143 --smtp-port 1025 --http-port 8903
start fake-printer "$ROOT/backend" "$PY" -m tests.fake_printer --port 8904
start api "$ROOT/backend" env EDP_METRICS_PORT=9464 \
  "$PY" -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
start worker "$ROOT/backend" env EDP_METRICS_PORT=9465 "$PY" -m app.worker
start scheduler "$ROOT/backend" env EDP_METRICS_PORT=9466 "$PY" -m app.scheduler

start console "$ROOT/frontend" env VITE_WECOM_JSSDK_URLS=http://127.0.0.1:8901/jssdk/jwxwork.js \
  pnpm --filter @edp/console dev --host 127.0.0.1 --strictPort
start platform-admin "$ROOT/frontend" pnpm --filter @edp/platform-admin dev --host 127.0.0.1 --strictPort
start widget "$ROOT/frontend" pnpm --filter @edp/widget dev --host 127.0.0.1 --strictPort

wait_for http://127.0.0.1:8000/readyz
wait_for http://127.0.0.1:9464/metrics
wait_for http://127.0.0.1:9465/metrics
wait_for http://127.0.0.1:9466/metrics
wait_port 8900
wait_port 8901
wait_port 3310
wait_port 1143
wait_port 1025
wait_port 8903
wait_port 8904
wait_for http://127.0.0.1:5173/
wait_for http://127.0.0.1:5174/
wait_for http://127.0.0.1:5175/
echo "stack is up (logs in $LOG_DIR)"
