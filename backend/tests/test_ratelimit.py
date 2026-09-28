import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.ratelimit import LOGIN_FAILURES, LOGIN_PER_IP, Limit, RateLimiter
from tests.factories import (
    ADMIN_PASSWORD,
    PLATFORM_PASSWORD,
    STAFF_PASSWORD,
    create_platform_admin,
    create_staff,
    login,
    provision,
)


async def staff_login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"tenant_code": "acme", "username": username, "password": password},
    )


async def test_repeated_failures_lock_only_that_account(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")
    await create_staff(client, admin, "alice")

    for _ in range(LOGIN_FAILURES.limit):
        assert (await staff_login(client, "admin", "wrong-password")).status_code == 401
    locked = await staff_login(client, "admin", ADMIN_PASSWORD)

    assert locked.status_code == 429
    assert "登录失败次数过多" in locked.json()["error"]["message"]
    assert int(locked.headers["Retry-After"]) > 0
    assert (await staff_login(client, "alice", STAFF_PASSWORD)).status_code == 200


async def test_successful_login_resets_the_failure_count(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    for _ in range(2):
        for _ in range(LOGIN_FAILURES.limit - 1):
            assert (await staff_login(client, "admin", "wrong-password")).status_code == 401
        assert (await staff_login(client, "admin", ADMIN_PASSWORD)).status_code == 200


async def test_login_attempts_are_limited_per_ip(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    statuses = [
        (await staff_login(client, f"nobody{i}", "wrong-password")).status_code
        for i in range(LOGIN_PER_IP.limit + 1)
    ]
    assert statuses == [401] * LOGIN_PER_IP.limit + [429]


async def test_platform_accounts_are_locked_after_repeated_failures(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_platform_admin(app)
    body = {"username": "ops", "password": "wrong-password"}
    for _ in range(LOGIN_FAILURES.limit):
        assert (await client.post("/platform/v1/auth/login", json=body)).status_code == 401

    body["password"] = PLATFORM_PASSWORD
    assert (await client.post("/platform/v1/auth/login", json=body)).status_code == 429


async def test_limiter_allows_requests_when_redis_is_down() -> None:
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    limiter = RateLimiter(redis)
    rule = Limit("test", 1, 60)
    try:
        for _ in range(3):
            await limiter.check(rule, "subject")
        assert (await limiter.peek(rule, "subject")).count == 0
    finally:
        await redis.aclose()


def test_prod_settings_require_real_secrets() -> None:
    with pytest.raises(ValueError) as excinfo:
        Settings(env="prod", cookie_secure=True)
    message = str(excinfo.value)
    for name in (
        "EDP_JWT_SECRET",
        "EDP_PLATFORM_JWT_SECRET",
        "EDP_VISITOR_JWT_SECRET",
        "EDP_OPENIM_SECRET",
        "EDP_OPENIM_WEBHOOK_SECRET",
        "EDP_STORAGE_SECRET_KEY",
        "EDP_FILE_URL_SECRET",
    ):
        assert name in message

    Settings(
        env="prod",
        cookie_secure=True,
        jwt_secret="a" * 40,
        platform_jwt_secret="b" * 40,
        visitor_jwt_secret="c" * 40,
        openim_secret="d" * 40,
        openim_webhook_secret="e" * 40,
        storage_secret_key="f" * 40,
        file_url_secret="g" * 40,
    )
