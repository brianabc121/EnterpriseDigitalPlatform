from fastapi import Request

from app.context import AppContext
from app.core.config import Settings
from app.core.ratelimit import RateLimiter
from app.db.session import Database


def get_database(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def get_context(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx
