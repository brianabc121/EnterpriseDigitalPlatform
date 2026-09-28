from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

import app.db.models  # noqa: F401  注册全部模型，保证 ORM 能解析跨模块外键
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.ratelimit import RateLimiter
from app.db.session import Database
from app.integrations.openim import OpenIMClient
from app.modules.channels.router import router as channels_router
from app.modules.conversation.provisioning import IMProvisioner
from app.modules.customer.router import router as customer_router
from app.modules.health.router import router as health_router
from app.modules.iam.router import auth_router
from app.modules.iam.router import router as iam_router
from app.modules.tenancy.router import router as platform_router
from app.modules.visitor.router import router as visitor_router


def create_app(settings: Settings | None = None, *, im: OpenIMClient | None = None) -> FastAPI:
    """应用工厂。开发环境：uvicorn app.main:create_app --factory --reload。

    im 供测试注入（例如接到内存版 OpenIM）；默认按配置连接 OpenIM。
    """
    settings = settings or get_settings()
    db = Database(settings)
    redis = Redis.from_url(settings.redis_url)
    im = im or OpenIMClient(
        settings.openim_api_url,
        secret=settings.openim_secret.get_secret_value(),
        admin_user_id=settings.openim_admin_user_id,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await im.aclose()
        await redis.aclose()
        await db.dispose()

    app = FastAPI(title="EDP API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.redis = redis
    app.state.rate_limiter = RateLimiter(redis)
    app.state.im = im
    app.state.im_provisioner = IMProvisioner(im)

    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(iam_router)
    app.include_router(customer_router)
    app.include_router(channels_router)
    app.include_router(visitor_router)
    app.include_router(platform_router)
    return app
