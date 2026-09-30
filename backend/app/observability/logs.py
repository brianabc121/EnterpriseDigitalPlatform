"""日志格式：text（开发）或 json（EDP_LOG_FORMAT=json）。

json 格式每行一个 JSON，带租户与链路 ID，供 Loki 检索。
"""

import json
import logging
from datetime import UTC, datetime

from opentelemetry import trace

from app.core.config import Settings
from app.observability.context import current_tenant

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, str] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        tenant = current_tenant()
        if tenant is not None:
            entry["tenant_id"] = str(tenant)
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure(settings: Settings, *, force: bool = True) -> None:
    """配置根日志。force 为 False 时只在 json 格式下接管（API 进程保留 uvicorn 自己的文本日志）。"""
    if settings.log_format != "json" and not force:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else logging.Formatter(_TEXT_FORMAT)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # 每个 HTTP 请求一行的日志太多，只保留警告。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if settings.log_format == "json":
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers[:] = []
            logger.propagate = True
