#!/bin/sh
# 容器启动时按环境变量生成运行时配置 /config.js（nginx 镜像执行 /docker-entrypoint.d/ 下的脚本）。
# 写到 /tmp/edp/config.js（nginx.conf 里映射到 /config.js），根文件系统可以只读。
#   EDP_WIDGET_URL：访客 Widget 的地址（控制台生成嵌入代码时使用）。
#   EDP_TRANSPORT_PUBLIC_KEY：接口传输加密的服务器公钥（后端 python -m app.cli transport-public-key
#     的输出，设计文档 §25.15）；为空时前端从后端获取。
set -eu
mkdir -p /tmp/edp
target=/tmp/edp/config.js
escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
printf 'window.EDP_CONFIG = { widgetUrl: "%s", transportKey: "%s" }\n' \
  "$(escape "${EDP_WIDGET_URL:-}")" "$(escape "${EDP_TRANSPORT_PUBLIC_KEY:-}")" > "$target"
