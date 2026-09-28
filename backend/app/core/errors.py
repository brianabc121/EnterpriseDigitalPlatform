from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """业务错误。统一序列化为 {"error": {"code": ..., "message": ...}}。"""

    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def headers(self) -> dict[str, str] | None:
        return None


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"

    def headers(self) -> dict[str, str] | None:
        return {"WWW-Authenticate": "Bearer"}


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Unprocessable(AppError):
    status_code = 422
    code = "unprocessable"


class TooManyRequests(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def headers(self) -> dict[str, str] | None:
        return {"Retry-After": str(self.retry_after)}


class ServiceUnavailable(AppError):
    """依赖的外部服务（如 OpenIM）暂时不可用，客户端可以稍后重试。"""

    status_code = 503
    code = "service_unavailable"


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# 各路由声明的错误响应，使 OpenAPI（以及生成的前端类型）包含统一的错误结构。
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 422)
}


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message), status_code=exc.status_code, headers=exc.headers()
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = jsonable_encoder(exc.errors(), exclude={"input", "ctx", "url"})
        return JSONResponse(
            error_body("validation_error", "请求参数不合法", details=details), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(
            error_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers
        )
