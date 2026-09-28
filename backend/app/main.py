from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.db.models  # noqa: F401  注册全部模型，保证 ORM 能解析跨模块外键
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.db.session import Database
from app.modules.health.router import router as health_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。开发环境：uvicorn app.main:create_app --factory --reload。"""
    settings = settings or get_settings()
    db = Database(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await db.dispose()

    app = FastAPI(title="EDP API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db

    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    return app
