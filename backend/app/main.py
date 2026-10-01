from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.db.models  # noqa: F401  注册全部模型，保证 ORM 能解析跨模块外键
from app.context import AppContext
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.ratelimit import RateLimiter
from app.integrations.llm import LLMClient
from app.integrations.openim import OpenIMClient
from app.modules.ai.router import router as ai_router
from app.modules.audit.router import router as audit_router
from app.modules.billing.router import platform_router as platform_billing_router
from app.modules.billing.router import router as billing_router
from app.modules.channels.router import router as channels_router
from app.modules.conversation.hooks import router as openim_hooks_router
from app.modules.conversation.router import router as conversation_router
from app.modules.customer.router import router as customer_router
from app.modules.files.router import router as files_router
from app.modules.health.router import router as health_router
from app.modules.history.router import router as history_router
from app.modules.iam.router import auth_router
from app.modules.iam.router import router as iam_router
from app.modules.integration.open_router import router as open_router
from app.modules.integration.router import platform_router as platform_integration_router
from app.modules.integration.router import router as integration_router
from app.modules.kb.router import router as kb_router
from app.modules.lifecycle.router import platform_router as platform_lifecycle_router
from app.modules.lifecycle.router import public_router as signup_router
from app.modules.lifecycle.router import router as tenant_router
from app.modules.notifications.router import router as notifications_router
from app.modules.orders.production_router import router as production_router
from app.modules.orders.public import router as order_public_router
from app.modules.orders.router import router as orders_router
from app.modules.platform.router import router as platform_ops_router
from app.modules.products.router import router as products_router
from app.modules.quickreply.router import router as quick_reply_router
from app.modules.reports.router import router as reports_router
from app.modules.routing.router import router as routing_router
from app.modules.security.router import platform_router as platform_security_router
from app.modules.security.router import router as security_router
from app.modules.sessions.router import router as sessions_router
from app.modules.tenancy.router import router as platform_router
from app.modules.todos.router import router as todos_router
from app.modules.transport.middleware import TransportMiddleware
from app.modules.transport.router import router as transport_router
from app.modules.transport.sessions import SessionStore
from app.modules.usage.router import platform_router as platform_usage_router
from app.modules.usage.router import router as usage_router
from app.modules.visitor.router import router as visitor_router
from app.modules.warehouse.router import router as warehouse_router
from app.modules.wecom.callbacks import router as wecom_hooks_router
from app.modules.wecom.router import router as wecom_router
from app.observability import logs, metrics, tracing
from app.observability.http import ObservabilityMiddleware


def create_app(
    settings: Settings | None = None,
    *,
    im: OpenIMClient | None = None,
    llm: LLMClient | None = None,
    wecom_transport: httpx.AsyncBaseTransport | None = None,
    storage_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """应用工厂。开发环境：uvicorn app.main:create_app --factory --reload。

    im、llm 和几个 transport 供测试注入（内存版 OpenIM、模拟大模型、模拟企业微信、内存对象存储；
    llm_transport 用于运营后台配置的供应商）；默认按配置连接。
    """
    settings = settings or get_settings()
    logs.configure(settings, force=False)
    tracing.setup(settings, "api")
    ctx = AppContext.create(
        settings,
        im=im,
        llm=llm,
        wecom_transport=wecom_transport,
        storage_transport=storage_transport,
        llm_transport=llm_transport,
        web_transport=web_transport,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        metrics.start_metrics_server(ctx.settings.metrics_port)
        yield
        await ctx.aclose()

    app = FastAPI(title="EDP API", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx
    app.state.settings = ctx.settings
    app.state.db = ctx.db
    app.state.redis = ctx.redis
    app.state.rate_limiter = ctx.limiter or RateLimiter(ctx.redis)
    app.state.im = ctx.im
    app.state.im_provisioner = ctx.provisioner
    app.state.transport_store = SessionStore(ctx.redis)

    install_error_handlers(app)
    # 接口传输加密（§25.15）：在 CORS 里面，出错的响应也带 CORS 头。
    app.add_middleware(TransportMiddleware, settings=ctx.settings, store=app.state.transport_store)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ctx.settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 最外层：记录每个请求（包括被 CORS 拒绝的）的指标与服务端 span。
    app.add_middleware(ObservabilityMiddleware)

    app.include_router(health_router)
    app.include_router(transport_router)
    app.include_router(auth_router)
    app.include_router(iam_router)
    app.include_router(customer_router)
    app.include_router(channels_router)
    app.include_router(conversation_router)
    app.include_router(sessions_router)
    app.include_router(todos_router)
    app.include_router(orders_router)
    app.include_router(production_router)
    app.include_router(warehouse_router)
    app.include_router(history_router)
    app.include_router(order_public_router)
    app.include_router(products_router)
    app.include_router(integration_router)
    app.include_router(open_router)
    app.include_router(routing_router)
    app.include_router(quick_reply_router)
    app.include_router(kb_router)
    app.include_router(notifications_router)
    app.include_router(ai_router)
    app.include_router(files_router)
    app.include_router(visitor_router)
    app.include_router(openim_hooks_router)
    app.include_router(wecom_router)
    app.include_router(wecom_hooks_router)
    app.include_router(reports_router)
    app.include_router(usage_router)
    app.include_router(audit_router)
    app.include_router(billing_router)
    app.include_router(signup_router)
    app.include_router(tenant_router)
    app.include_router(security_router)
    app.include_router(platform_router)
    app.include_router(platform_usage_router)
    app.include_router(platform_billing_router)
    app.include_router(platform_lifecycle_router)
    app.include_router(platform_ops_router)
    app.include_router(platform_security_router)
    app.include_router(platform_integration_router)
    return app
