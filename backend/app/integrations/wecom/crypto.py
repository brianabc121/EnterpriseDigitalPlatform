"""企业微信回调的加解密与验签（与官方 WXBizMsgCrypt 一致）。

- 密钥：AESKey = Base64Decode(EncodingAESKey + "=")，32 字节；IV 取密钥的前 16 字节。
- 明文：16 字节随机串 + 4 字节消息长度（网络字节序）+ 消息 + ReceiveId，按 32 字节做 PKCS#7 填充，
  AES-256-CBC 加密后 Base64 编码。
- 签名：Token、时间戳、随机数、密文四个字符串按字典序排序后拼接，取 SHA1 十六进制。
- ReceiveId：代开发应用和自建应用的回调是授权企业的 CorpID，模板（服务商）的指令回调是 SuiteId。
- XML 只按元素解析，拒绝带 DOCTYPE 或实体声明的内容（防 XML 实体攻击）。
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from typing import Any
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_BLOCK = 32


class CallbackError(Exception):
    """回调签名不对、密文无法解密或内容不是合法的 XML。"""


class CallbackCrypto:
    def __init__(self, token: str, encoding_aes_key: str) -> None:
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except binascii.Error as exc:
            raise ValueError("EncodingAESKey is not valid base64") from exc
        if len(key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self._token = token
        self._key = key

    def _cipher(self) -> Cipher[modes.CBC]:
        return Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16]))

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        joined = "".join(sorted([self._token, timestamp, nonce, encrypted]))
        return hashlib.sha1(joined.encode()).hexdigest()

    def verify(self, signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        expected = self.signature(timestamp, nonce, encrypted)
        if not hmac.compare_digest(expected, signature.lower()):
            raise CallbackError("signature mismatch")

    def encrypt(self, plaintext: str, receive_id: str) -> str:
        body = plaintext.encode()
        raw = (
            secrets.token_hex(8).encode()
            + struct.pack(">I", len(body))
            + body
            + receive_id.encode()
        )
        pad = _BLOCK - len(raw) % _BLOCK
        raw += bytes([pad]) * pad
        encryptor = self._cipher().encryptor()
        return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()

    def decrypt(self, encrypted: str) -> tuple[str, str]:
        """返回（明文，ReceiveId）。"""
        try:
            data = base64.b64decode(encrypted, validate=True)
        except binascii.Error as exc:
            raise CallbackError("ciphertext is not valid base64") from exc
        if not data or len(data) % 16:
            raise CallbackError("ciphertext has an invalid length")
        decryptor = self._cipher().decryptor()
        raw = decryptor.update(data) + decryptor.finalize()
        pad = raw[-1]
        if not 1 <= pad <= _BLOCK or len(raw) < 20 + pad:
            raise CallbackError("invalid padding")
        content = raw[16:-pad]
        (length,) = struct.unpack(">I", content[:4])
        if length > len(content) - 4:
            raise CallbackError("invalid message length")
        try:
            message = content[4 : 4 + length].decode()
            receive_id = content[4 + length :].decode()
        except UnicodeDecodeError as exc:
            raise CallbackError("plaintext is not utf-8") from exc
        return message, receive_id

    # ---- 回调 ----

    def verify_url(
        self, *, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> tuple[str, str]:
        """配置回调 URL 时的验证请求：验签、解密 echostr，返回（明文，ReceiveId）。"""
        self.verify(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)

    def open(
        self, *, msg_signature: str, timestamp: str, nonce: str, body: str
    ) -> tuple[dict[str, Any], str]:
        """回调请求：验签、解密，返回（事件内容，ReceiveId）。"""
        outer = parse_xml(body)
        encrypted = outer.get("Encrypt")
        if not isinstance(encrypted, str) or not encrypted:
            raise CallbackError("missing Encrypt")
        self.verify(msg_signature, timestamp, nonce, encrypted)
        plaintext, receive_id = self.decrypt(encrypted)
        return parse_xml(plaintext), receive_id

    def seal(
        self, plaintext: str, receive_id: str, *, timestamp: str | None = None, nonce: str = ""
    ) -> tuple[str, dict[str, str]]:
        """加密一段回调内容：返回请求体 XML 和 URL 参数（msg_signature、timestamp、nonce）。

        企业微信向平台推送回调时就是这个格式；模拟服务和测试用它构造回调。
        """
        timestamp = timestamp or str(int(time.time()))
        nonce = nonce or secrets.token_hex(5)
        encrypted = self.encrypt(plaintext, receive_id)
        signature = self.signature(timestamp, nonce, encrypted)
        body = to_xml({"ToUserName": receive_id, "Encrypt": encrypted, "AgentID": ""})
        return body, {"msg_signature": signature, "timestamp": timestamp, "nonce": nonce}


def parse_xml(text: str) -> dict[str, Any]:
    """把回调 XML 解析成字典：叶子元素取文本；有子元素的取字典，同名元素重复出现时取列表。"""
    head = text[:2048].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in text.upper():
        raise CallbackError("DOCTYPE and entities are not allowed")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise CallbackError("invalid xml") from exc
    value = _element(root)
    return value if isinstance(value, dict) else {}


def _element(element: ElementTree.Element) -> dict[str, Any] | str:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        value = _element(child)
        if child.tag in result:
            existing = result[child.tag]
            if not isinstance(existing, list):
                result[child.tag] = [existing]
            result[child.tag].append(value)
        else:
            result[child.tag] = value
    return result


def to_xml(data: dict[str, Any], root: str = "xml") -> str:
    """字典转回调格式的 XML（字符串用 CDATA，数字原样）。"""

    def render(tag: str, value: Any) -> str:
        if isinstance(value, dict):
            inner = "".join(render(k, v) for k, v in value.items())
            return f"<{tag}>{inner}</{tag}>"
        if isinstance(value, list):
            return "".join(render(tag, item) for item in value)
        if isinstance(value, bool):
            return f"<{tag}>{int(value)}</{tag}>"
        if isinstance(value, int):
            return f"<{tag}>{value}</{tag}>"
        text = str(value).replace("]]>", "]]]]><![CDATA[>")
        return f"<{tag}><![CDATA[{text}]]></{tag}>"

    return render(root, data)
