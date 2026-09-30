#!/bin/sh
# 容器启动时按环境变量生成运行时配置 /config.js（nginx 镜像执行 /docker-entrypoint.d/ 下的脚本）。
# 写到 /tmp/edp/config.js（nginx.conf 里映射到 /config.js），根文件系统可以只读。
#   EDP_WIDGET_URL：访客 Widget 的地址（控制台生成嵌入代码时使用）。
set -eu
mkdir -p /tmp/edp
target=/tmp/edp/config.js
escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
printf 'window.EDP_CONFIG = { widgetUrl: "%s" }\n' "$(escape "${EDP_WIDGET_URL:-}")" > "$target"
