from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仅供本地开发使用的默认密钥；生产环境必须通过环境变量覆盖（见 _check_prod）。
_DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-0123456789abcdef"
_DEV_PLATFORM_JWT_SECRET = "dev-only-platform-jwt-secret-change-me-0123456789"
_DEV_VISITOR_JWT_SECRET = "dev-only-visitor-jwt-secret-change-me-0123456789a"
# 与 deploy/compose/openim/docker-compose.yml 的默认值一致。
_DEV_OPENIM_SECRET = "openim-dev-secret"
_DEV_OPENIM_WEBHOOK_SECRET = "dev-openim-webhook-secret"
_DEV_STORAGE_SECRET_KEY = "edp-dev-storage-secret"  # 与 deploy/compose/docker-compose.yml 一致
_DEV_FILE_URL_SECRET = "dev-only-file-url-secret-change-me-0123456789abc"


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

    visitor_jwt_secret: SecretStr = SecretStr(_DEV_VISITOR_JWT_SECRET)
    visitor_token_ttl_seconds: int = 365 * 24 * 3600

    cookie_secure: bool = False
    # 5173 控制台、5174 运营后台、5175 访客 Widget（开发环境）
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
    ]

    redis_url: str = "redis://localhost:6379/0"

    # OpenIM：平台后端调用 REST 的地址，以及浏览器（SDK）访问的公开地址。
    openim_api_url: str = "http://localhost:10002"
    openim_public_api_url: str = "http://localhost:10002"
    openim_public_ws_url: str = "ws://localhost:10001"
    openim_secret: SecretStr = SecretStr(_DEV_OPENIM_SECRET)
    openim_admin_user_id: str = "imAdmin"
    # OpenIM 回调路径中的共享密钥（OpenIM 回调不签名）。
    openim_webhook_secret: SecretStr = SecretStr(_DEV_OPENIM_WEBHOOK_SECRET)

    # S3 兼容对象存储（开发环境为 deploy/compose 里的 MinIO）。storage_endpoint 供后端访问，
    # storage_public_endpoint 用于签发给浏览器的上传、下载地址。
    storage_endpoint: str = "http://localhost:9000"
    storage_public_endpoint: str = "http://localhost:9000"
    storage_access_key: str = "edp"
    storage_secret_key: SecretStr = SecretStr(_DEV_STORAGE_SECRET_KEY)
    storage_bucket: str = "edp-files"
    storage_region: str = "us-east-1"
    # 平台签发的文件链接（/api/v1/files/...）的签名密钥。
    file_url_secret: SecretStr = SecretStr(_DEV_FILE_URL_SECRET)
    # 平台对外地址，用于拼接文件链接。
    public_api_url: str = "http://localhost:8000"
    # 访客 Widget 的地址：它自己的来源总是允许接入（渠道设置了允许嵌入的网站时）。
    widget_public_url: str = "http://localhost:5175"

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.env != "prod":
            return self
        dev_defaults = {
            "EDP_JWT_SECRET": (self.jwt_secret, _DEV_JWT_SECRET),
            "EDP_PLATFORM_JWT_SECRET": (self.platform_jwt_secret, _DEV_PLATFORM_JWT_SECRET),
            "EDP_VISITOR_JWT_SECRET": (self.visitor_jwt_secret, _DEV_VISITOR_JWT_SECRET),
            "EDP_OPENIM_SECRET": (self.openim_secret, _DEV_OPENIM_SECRET),
            "EDP_OPENIM_WEBHOOK_SECRET": (self.openim_webhook_secret, _DEV_OPENIM_WEBHOOK_SECRET),
            "EDP_STORAGE_SECRET_KEY": (self.storage_secret_key, _DEV_STORAGE_SECRET_KEY),
            "EDP_FILE_URL_SECRET": (self.file_url_secret, _DEV_FILE_URL_SECRET),
        }
        unset = [
            name for name, (value, dev) in dev_defaults.items() if value.get_secret_value() == dev
        ]
        if unset:
            raise ValueError(f"{', '.join(unset)} must be set in prod")
        if not self.cookie_secure:
            raise ValueError("EDP_COOKIE_SECURE must be true in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
