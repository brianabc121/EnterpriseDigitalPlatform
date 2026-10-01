from typing import Literal

from pydantic import BaseModel, Field


class TransportKey(BaseModel):
    algorithm: Literal["ECDSA-P256-SHA256"] = "ECDSA-P256-SHA256"
    key_id: str = Field(description="公钥的指纹（SHA-256 的前 16 位十六进制）")
    public_key: str = Field(description="服务器公钥：SubjectPublicKeyInfo DER 的 base64")
    mode: Literal["required", "optional", "off"] = Field(description="是否强制加密")


class HandshakeIn(BaseModel):
    client_key: str = Field(
        min_length=80,
        max_length=100,
        description="浏览器的一次性 ECDH P-256 公钥（未压缩的点，base64url）",
    )


class HandshakeOut(BaseModel):
    session: str = Field(description="会话号：放在请求头 X-EDP-Transport 里")
    server_key: str = Field(description="服务器的一次性 ECDH P-256 公钥（未压缩的点，base64url）")
    expires_at: int = Field(description="会话的过期时间（Unix 秒）")
    server_time: int = Field(description="服务器的当前时间（Unix 秒）：浏览器按它校正请求的时间戳")
    signature: str = Field(
        description="服务器签名密钥对握手内容的 ECDSA P-256 / SHA-256 签名（r || s，base64url）"
    )
    key_id: str
