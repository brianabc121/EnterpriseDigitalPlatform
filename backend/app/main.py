from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.db.models  # noqa: F401  注册全部模型，保证 ORM 能解析跨模块外键
from app.context import AppContext
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.ratelimit import RateLimiter
from app.integrations.openim import OpenIMClient
from app.modules.channels.router import router as channels_router
from app.modules.conversation.hooks import router as openim_hooks_router
from app.modules.conversation.router import router as conversation_router
from app.modules.customer.router import router as customer_router
from app.modules.health.router import router as health_router
from app.modules.iam.router import auth_router
from app.modules.iam.router import router as iam_router
from app.modules.routing.router import router as routing_router
from app.modules.sessions.router import router as sessions_router
from app.modules.tenancy.router import router as platform_router
from app.modules.visitor.router import router as visitor_router


def create_app(settings: Settings | None = None, *, im: OpenIMClient | None = None) -> FastAPI:
    """应用工厂。开发环境：uvicorn app.main:create_app --factory --reload。

    im 供测试注入（例如接到内存版 OpenIM）；默认按配置连接 OpenIM。
    """
    ctx = AppContext.create(settings or get_settings(), im=im)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await ctx.aclose()

    app = FastAPI(title="EDP API", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.state.settings = ctx.settings
    app.state.db = ctx.db
    app.state.redis = ctx.redis
    app.state.rate_limiter = RateLimiter(ctx.redis)
    app.state.im = ctx.im
    app.state.im_provisioner = ctx.provisioner

    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ctx.settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(iam_router)
    app.include_router(customer_router)
    app.include_router(channels_router)
    app.include_router(conversation_router)
    app.include_router(sessions_router)
    app.include_router(routing_router)
    app.include_router(visitor_router)
    app.include_router(openim_hooks_router)
    app.include_router(platform_router)
    return app
