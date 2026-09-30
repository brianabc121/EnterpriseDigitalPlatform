# syntax=docker/dockerfile:1
# 平台后端镜像：API、实时消费进程、调度进程和数据库迁移共用（设计文档 §19.2）。
#   API：默认命令；实时消费：python -m app.worker；调度：python -m app.scheduler；
#   迁移：alembic upgrade head（deploy/k8s 的 edp-migrate Job）。
# 构建：docker build -t edp-backend backend
# syntax 需要 BuildKit（Docker 23 起默认启用）。
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS build
ARG UV_VERSION=0.8.17
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
# 网络经过重新签发证书的代理时，用 --secret id=ca,src=<根证书> 提供证书（只在构建时使用，不进入镜像）。
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi \
    && pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

FROM ${PYTHON_IMAGE}
# ffmpeg：微信客服语音（AMR）转 MP3。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin edp
WORKDIR /app
COPY --from=build --chown=10001:10001 /app /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EDP_ENV=prod \
    EDP_LOG_FORMAT=json
USER 10001
# 8000：API；9464：Prometheus 指标（EDP_METRICS_PORT）。
EXPOSE 8000 9464
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
