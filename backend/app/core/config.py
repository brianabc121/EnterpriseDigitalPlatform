from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仅供本地开发使用的默认密钥；生产环境必须通过环境变量覆盖（见 _check_prod）。
_DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-0123456789abcdef"
_DEV_PLATFORM_JWT_SECRET = "dev-only-platform-jwt-secret-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDP_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    # edp_app 处理租户员工请求（受 RLS 约束）；edp_platform 处理平台运营；owner 只用于迁移。
    database_url_app: str = "postgresql+asyncpg://edp_app:edp_app@localhost:5432/edp"
    database_url_platform: str = "postgresql+asyncpg://edp_platform:edp_platform@localhost:5432/edp"
    database_url_owner: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/edp"

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    platform_jwt_secret: SecretStr = SecretStr(_DEV_PLATFORM_JWT_SECRET)
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 7 * 24 * 3600
    platform_token_ttl_seconds: int = 2 * 3600

    cookie_secure: bool = False
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:5174"]

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.env != "prod":
            return self
        if (
            self.jwt_secret.get_secret_value() == _DEV_JWT_SECRET
            or self.platform_jwt_secret.get_secret_value() == _DEV_PLATFORM_JWT_SECRET
        ):
            raise ValueError("EDP_JWT_SECRET and EDP_PLATFORM_JWT_SECRET must be set in prod")
        if not self.cookie_secure:
            raise ValueError("EDP_COOKIE_SECURE must be true in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
