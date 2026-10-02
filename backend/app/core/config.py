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
_DEV_DATA_ENCRYPTION_KEY = "dev-only-data-encryption-key-change-me-0123456789"


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
    # 运营后台的刷新令牌（httpOnly Cookie）：页面刷新后不用重新登录；登录后最多这么久要重新登录。
    platform_refresh_ttl_seconds: int = 12 * 3600
    # 平台运营账号是否必须启用二次验证（TOTP）；不设置时生产环境必须启用（设计文档 §13）。
    platform_mfa_required: bool | None = None

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
    # 用量按日汇总时划分日期的时区（计费时区）。
    usage_timezone: str = "Asia/Shanghai"

    # 大模型（OpenAI 兼容协议，设计文档 §11.5）。llm_base_url 为空时不启用 AI 接待与坐席助手。
    # base_url 形如 https://api.deepseek.com/v1、https://dashscope.aliyuncs.com/compatible-mode/v1。
    llm_provider: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_chat_model: str = ""
    # 分类、摘要等轻量任务使用的模型；为空时与 llm_chat_model 相同。
    llm_fast_model: str = ""
    # 备用供应商：主供应商重试后仍失败时使用。
    llm_fallback_provider: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_fallback_chat_model: str = ""
    # 向量模型（知识库语义检索）；llm_embed_model 为空时只用关键词检索。维度须与数据库一致（1024）。
    llm_embed_base_url: str = ""
    llm_embed_api_key: SecretStr = SecretStr("")
    llm_embed_model: str = ""
    llm_embed_dim: int = 1024
    # 接口支持 dimensions 参数时（如 text-embedding-v3、embedding-3）设为 true。
    llm_embed_send_dimensions: bool = False
    llm_timeout_seconds: float = 30.0
    # 价格（每千 tokens 多少分），用于估算费用；向量模型按输入价格计。
    llm_price_input: float = 0.0
    llm_price_output: float = 0.0
    llm_embed_price: float = 0.0
    # 对话模型支持函数调用（tools）时设为 true，AI 接待才会让模型调用工具。
    llm_supports_tools: bool = False
    # 重排序模型（/rerank，如 bge-reranker-v2-m3）；为空时不重排。接口地址默认与向量模型相同。
    llm_rerank_model: str = ""
    llm_rerank_base_url: str = ""
    llm_rerank_api_key: SecretStr = SecretStr("")
    # 每个租户同时进行的大模型调用上限（平台可以按租户调整），超出时最多排队这么多秒。
    llm_tenant_concurrency: int = 8
    llm_queue_seconds: float = 10.0
    # 客户连续发消息时，等这么久没有新消息再合并回复。
    ai_debounce_seconds: float = 2.0
    # 判断模型（TypeSafe Jev，设计文档 §32）：运营后台没有把"意图判断"路由到供应商时使用；
    # judge_base_url 为空时不用。价格为每千 tokens 多少分（Jev 输入约 0.03 分，输出不计费）。
    judge_base_url: str = ""
    judge_api_key: SecretStr = SecretStr("")
    judge_model: str = "jev-latest"
    judge_price_input: float = 0.0
    judge_timeout_seconds: float = 5.0
    # 意图判断：客户连续发消息时等这么久再判断（比 AI 回复短，AI 回复时通常已经有结果）。
    intent_debounce_seconds: float = 1.0
    # 知识导入：上传文件的大小上限，抓取帮助中心时每个网页的超时与大小上限、最多抓取的页数。
    kb_import_max_bytes: int = 20 * 1024 * 1024
    kb_crawl_timeout_seconds: float = 10.0
    kb_crawl_max_page_bytes: int = 2 * 1024 * 1024
    kb_crawl_max_pages: int = 100
    # 语音转文字（OpenAI 兼容的 /audio/transcriptions）；为空时不转写，AI 只知道客户发了语音。
    asr_base_url: str = ""
    asr_api_key: SecretStr = SecretStr("")
    asr_model: str = ""
    # 微信客服的 AMR 语音转成 MP3 供网页播放；找不到 ffmpeg 时保留原文件。
    ffmpeg_path: str = "ffmpeg"

    # 聊天附件病毒扫描（ClamAV clamd 的 INSTREAM 接口）；为空时不扫描。
    clamav_host: str = ""
    clamav_port: int = 3310
    clamav_timeout_seconds: float = 30.0

    # 邮件渠道（设计文档 §10.8）：每个邮箱多久收一次信、连接超时、单封邮件和单个附件的大小上限；
    # mail_allow_private_hosts 允许内网和本机的邮件服务器、不加密的连接（只用于开发和测试）。
    mail_poll_seconds: int = 60
    mail_timeout_seconds: float = 30.0
    mail_max_message_bytes: int = 30 * 1024 * 1024
    mail_max_attachment_bytes: int = 20 * 1024 * 1024
    mail_allow_private_hosts: bool = False

    # 渠道凭证（企业微信永久授权码等）的加密密钥，任意长度的随机字符串。
    data_encryption_key: SecretStr = SecretStr(_DEV_DATA_ENCRYPTION_KEY)

    # 接口传输加密（设计文档 §25.15）。transport_encryption：required 只接受加密的请求，optional
    # 加密和不加密的都接受，off 关闭；不设置时生产环境为 required，其他环境为 optional（测试和
    # 浏览器验收的脚本直接调接口）。transport_signing_key：握手签名用的 ECDSA P-256 私钥（PEM），
    # 生产环境必须配置；其他环境不配置时由 data_encryption_key 派生。
    transport_encryption: Literal["required", "optional", "off"] | None = None
    transport_signing_key: SecretStr = SecretStr("")
    transport_session_ttl_seconds: int = 12 * 3600
    transport_handshakes_per_minute: int = 300

    # 企业微信服务商（设计文档 §7.4，代开发应用）：模板 ID（suite_id）与 Secret，以及模板和
    # 代开发应用共用的回调 Token、EncodingAESKey。wecom_suite_id 为空时不启用企业微信接入。
    wecom_suite_id: str = ""
    wecom_suite_secret: SecretStr = SecretStr("")
    wecom_token: SecretStr = SecretStr("")
    wecom_encoding_aes_key: SecretStr = SecretStr("")
    # 企业微信接口地址；联调、测试时可以指向模拟服务（tests/fake_wecom.py）。
    wecom_api_url: str = "https://qyapi.weixin.qq.com"
    # 授权安装页、网页授权（企业微信内免登）和扫码登录页的地址。
    wecom_install_url: str = "https://open.work.weixin.qq.com/3rdapp/install"
    wecom_oauth_url: str = "https://open.weixin.qq.com/connect/oauth2/authorize"
    wecom_sso_url: str = "https://login.work.weixin.qq.com/wwlogin/sso/login"
    # 员工控制台的对外地址：授权完成、登录后跳回这里，应用消息里的链接也指向这里。
    console_public_url: str = "http://localhost:5173"
    # AI 公司助理接入的 IM 平台接口地址（设计文档 §27.3.1）；联调时可以指向模拟服务。
    telegram_api_url: str = "https://api.telegram.org"
    feishu_api_url: str = "https://open.feishu.cn"
    dingtalk_api_url: str = "https://api.dingtalk.com"
    whatsapp_api_url: str = "https://graph.facebook.com/v21.0"
    imbot_timeout_seconds: float = 15.0

    # 云打印机（设计文档 §29.3）：芯烨云、飞鹅云的接口地址（测试和验收指向模拟厂商
    # tests/fake_printer.py）和超时；print_immediate 为真时打印任务排队后由 API 进程马上尝试发送
    # 一次（关掉后只由调度任务发送）。
    print_xpyun_url: str = "https://open.xpyun.net"
    print_feie_url: str = "https://api.feieyun.cn"
    print_timeout_seconds: float = 10.0
    print_immediate: bool = True

    # 按租户限流（设计文档 §9.3 租户公平）：每个租户每分钟的员工接口请求、访客接口请求、OpenIM 回调
    # 和大模型调用上限，平台可以在运营后台按租户调整；0 表示不限。访客另有每人每分钟的上限。
    tenant_api_per_minute: int = 6000
    tenant_visitor_per_minute: int = 6000
    tenant_webhook_per_minute: int = 6000
    tenant_llm_per_minute: int = 600
    visitor_per_minute: int = 120

    # 可观测性（设计文档 §19.3）。metrics_port：Prometheus 指标端口，每个进程（API、实时消费、
    # 调度）单独监听，0 表示不开启；otel_endpoint：OpenTelemetry 链路追踪的 OTLP/HTTP 地址
    # （如 http://otel-collector:4318），为空时不上报；log_format 为 json 时日志每行一个 JSON，
    # 带租户与链路 ID，便于 Loki 检索。
    metrics_port: int = 0
    otel_endpoint: str = ""
    otel_sample_ratio: float = 1.0
    log_format: Literal["text", "json"] = "text"

    @property
    def wecom_enabled(self) -> bool:
        return bool(self.wecom_suite_id)

    @property
    def transport_mode(self) -> Literal["required", "optional", "off"]:
        if self.transport_encryption is not None:
            return self.transport_encryption
        return "required" if self.env == "prod" else "optional"

    @property
    def platform_mfa_enforced(self) -> bool:
        if self.platform_mfa_required is not None:
            return self.platform_mfa_required
        return self.env == "prod"

    @model_validator(mode="after")
    def _check_wecom(self) -> "Settings":
        if not self.wecom_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("EDP_WECOM_SUITE_SECRET", self.wecom_suite_secret),
                ("EDP_WECOM_TOKEN", self.wecom_token),
                ("EDP_WECOM_ENCODING_AES_KEY", self.wecom_encoding_aes_key),
            )
            if not value.get_secret_value()
        ]
        if missing:
            raise ValueError(f"{', '.join(missing)} must be set when EDP_WECOM_SUITE_ID is set")
        if len(self.wecom_encoding_aes_key.get_secret_value()) != 43:
            raise ValueError("EDP_WECOM_ENCODING_AES_KEY must be 43 characters")
        return self

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
            "EDP_DATA_ENCRYPTION_KEY": (self.data_encryption_key, _DEV_DATA_ENCRYPTION_KEY),
        }
        unset = [
            name for name, (value, dev) in dev_defaults.items() if value.get_secret_value() == dev
        ]
        if not self.transport_signing_key.get_secret_value():
            unset.append("EDP_TRANSPORT_SIGNING_KEY")
        if unset:
            raise ValueError(f"{', '.join(unset)} must be set in prod")
        if not self.cookie_secure:
            raise ValueError("EDP_COOKIE_SECURE must be true in prod")
        if self.mail_allow_private_hosts:
            # 会让租户配置的邮件服务器访问内网、明文发送邮箱授权码。
            raise ValueError("EDP_MAIL_ALLOW_PRIVATE_HOSTS must be false in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
