#!/usr/bin/env bash
# 依次运行浏览器验收脚本（scripts/e2e/<名称>.cjs），全部跑完后有失败时以非 0 退出。
#   bash scripts/ci/run-e2e.sh p0-acceptance m4-workbench-acceptance
# 需要全局安装的 Playwright（NODE_PATH 指向全局 node_modules）和 PLATFORM_PASSWORD；
# 截图与 summary.json 写到 $SHOTS_DIR/<名称>（默认 e2e-shots）。
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SHOTS_DIR=${SHOTS_DIR:-$ROOT/e2e-shots}
# 脚本里调用的命令行（如 kb-jobs、kb-extract）与后端进程用同样的配置。
set -a
# shellcheck source=/dev/null
source "$ROOT/scripts/ci/e2e.env"
set +a
export NODE_PATH=${NODE_PATH:-$(npm root -g)}
# start-stack.sh 启动的 API、实时消费、调度进程的指标（g6-ops-observability 检查）。
export METRICS_URLS=${METRICS_URLS-http://127.0.0.1:9464/metrics,http://127.0.0.1:9465/metrics,http://127.0.0.1:9466/metrics}
: "${PLATFORM_PASSWORD:?PLATFORM_PASSWORD is required}"

failed=()
for name in "$@"; do
  echo "::group::$name"
  mkdir -p "$SHOTS_DIR/$name"
  if SHOTS="$SHOTS_DIR/$name" timeout 900 node "$ROOT/scripts/e2e/$name.cjs" >"$SHOTS_DIR/$name/output.log" 2>&1; then
    result=pass
  else
    result=fail
    failed+=("$name")
  fi
  grep -E '"(PASS|FAIL)' "$SHOTS_DIR/$name/output.log" || tail -30 "$SHOTS_DIR/$name/output.log"
  echo "::endgroup::"
  echo "$name: $result"
done

if ((${#failed[@]})); then
  echo "failed: ${failed[*]}"
  exit 1
fi
