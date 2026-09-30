# syntax=docker/dockerfile:1
# 前端镜像：APP 选择要打包的应用（console、platform-admin、widget），nginx 在 8080 端口提供静态文件。
# 构建：docker build --build-arg APP=console -t edp-console frontend
ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.29-alpine

FROM ${NODE_IMAGE} AS build
ARG APP=console
ARG PNPM_VERSION=10.33.0
WORKDIR /src
COPY . .
# 网络经过重新签发证书的代理时，用 --secret id=ca,src=<根证书> 提供证书（只在构建时使用）。
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca; fi \
    && npm install -g "pnpm@${PNPM_VERSION}" \
    && pnpm install --frozen-lockfile \
    && pnpm --filter "@edp/${APP}" build

FROM ${NGINX_IMAGE}
ARG APP=console
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/40-edp-config.sh /docker-entrypoint.d/40-edp-config.sh
COPY --from=build /src/apps/${APP}/dist /usr/share/nginx/html
# 以非 root 用户运行：日志写到标准输出，pid、缓存和运行时配置都在 /tmp 或 /var/cache/nginx，
# 根文件系统可以只读（K8s 里给这两个目录挂 emptyDir）。
RUN sed -i 's#^pid .*#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf \
    && sed -i 's#^user .*##' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /var/cache/nginx
USER nginx
EXPOSE 8080
