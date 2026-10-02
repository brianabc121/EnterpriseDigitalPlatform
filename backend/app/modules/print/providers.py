"""云打印机厂商的接口（设计文档 §29.3）：芯烨云（XPrinter）和飞鹅云。

两家都是"开发者账号 + 密钥 + 时间戳做 SHA1 签名"，然后按打印机编号（SN）下发内容。平台只用到：
添加打印机（接入时验证编号和账号）、打印、查询打印机状态、查询是否已打印、删除打印机。
厂商地址可配置（EDP_PRINT_XPYUN_URL、EDP_PRINT_FEIE_URL），测试和验收指向 tests/fake_printer.py；
生产环境先固定解析结果再连（core/urls.resolve_outbound）。
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.errors import Unprocessable
from app.core.urls import resolve_outbound
from app.modules.print.models import PrinterBrand, PrinterStatus

TIMEOUT_SECONDS = 10.0
ERROR_LIMIT = 200


class PrinterError(Exception):
    """厂商拒绝或者连不上。permanent 为真表示账号、密钥或编号不对——重试也没用。"""

    def __init__(self, message: str, *, permanent: bool = False, code: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.permanent = permanent
        self.code = code


@dataclass(frozen=True)
class Probe:
    status: PrinterStatus
    detail: str


class CloudPrinter(Protocol):
    brand: PrinterBrand

    async def register(self, sn: str, name: str, *, device_key: str = "") -> None: ...

    async def print(self, sn: str, content: str, copies: int) -> str: ...

    async def status(self, sn: str) -> Probe: ...

    async def printed(self, cloud_order_id: str) -> bool: ...

    async def remove(self, sn: str) -> None: ...


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


@dataclass
class _Client:
    http: httpx.AsyncClient
    base_url: str
    account: str
    key: str
    allow_private: bool
    timeout: float = TIMEOUT_SECONDS

    async def _post(self, url: str, *, json: Any = None, data: Any = None) -> Any:
        try:
            target = await resolve_outbound(url, allow_private=self.allow_private)
        except Unprocessable as exc:
            raise PrinterError(f"厂商地址不可用：{exc}", permanent=True) from exc
        headers = dict(target.headers)
        if json is not None:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        try:
            response = await self.http.post(
                target.request_url,
                json=json,
                data=data,
                headers=headers,
                extensions=target.extensions,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise PrinterError(f"连接厂商失败：{type(exc).__name__}"[:ERROR_LIMIT]) from exc
        if response.status_code != 200:
            raise PrinterError(f"厂商返回 HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise PrinterError("厂商返回的不是 JSON") from exc
        if not isinstance(body, dict):
            raise PrinterError("厂商返回的格式不对")
        return body


class XpyunClient(_Client):
    """芯烨云：POST JSON 到 /api/openapi/xprinter/<接口>，返回 code（0 成功）、msg、data。"""

    brand = PrinterBrand.XPYUN
    # 账号、密钥、编号不对：-3 签名失败、-4 / 1012 用户未注册、1001 编号和用户不匹配、1002 未注册、
    # 1008 编号无效。
    PERMANENT = frozenset({-3, -4, 1001, 1002, 1008, 1012})

    def _common(self) -> dict[str, str]:
        timestamp = str(int(time.time()))
        return {
            "user": self.account,
            "timestamp": timestamp,
            "sign": _sha1(self.account + self.key + timestamp),
        }

    async def _call(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url.rstrip('/')}/api/openapi/xprinter/{path}"
        body = await self._post(url, json={**payload, **self._common()})
        code = body.get("code")
        if code != 0:
            message = str(body.get("msg") or "厂商返回错误")
            raise PrinterError(f"{message}（{code}）", permanent=code in self.PERMANENT, code=code)
        return body.get("data")

    async def register(self, sn: str, name: str, *, device_key: str = "") -> None:
        data = await self._call("addPrinters", {"items": [{"sn": sn, "name": name}]})
        data = data if isinstance(data, dict) else {}
        if sn in (data.get("success") or []):
            return
        reasons = [m for m in (data.get("failMsg") or []) if str(m).startswith(f"{sn}:")]
        code = str(reasons[0]).split(":", 1)[1] if reasons else ""
        if code == "1009":  # 已经添加过
            return
        raise PrinterError(
            f"添加打印机失败（{code or '编号无效'}）", permanent=True, code=code or None
        )

    async def print(self, sn: str, content: str, copies: int) -> str:
        # mode=1：打印机不在线时厂商先收下，上线后自动打。
        data = await self._call(
            "print", {"sn": sn, "content": content, "copies": copies, "mode": 1}
        )
        return str(data)

    async def status(self, sn: str) -> Probe:
        data = await self._call("queryPrinterStatus", {"sn": sn})
        value = data.get("status") if isinstance(data, dict) else data
        if value == 1:
            return Probe(PrinterStatus.ONLINE, "在线")
        if value == 2:
            return Probe(PrinterStatus.ABNORMAL, "在线但异常，一般是缺纸")
        return Probe(PrinterStatus.OFFLINE, "离线")

    async def printed(self, cloud_order_id: str) -> bool:
        return bool(await self._call("queryOrderState", {"orderId": cloud_order_id}))

    async def remove(self, sn: str) -> None:
        await self._call("delPrinters", {"snlist": [sn]})


class FeieClient(_Client):
    """飞鹅云：表单 POST 到 /Api/Open/，apiname 区分接口，返回 ret（0 成功）、msg、data。"""

    brand = PrinterBrand.FEIE
    PERMANENT_HINTS = ("签名", "sig", "用户", "user", "不存在", "未绑定", "未添加", "无效")

    def _common(self, apiname: str) -> dict[str, str]:
        stime = str(int(time.time()))
        return {
            "user": self.account,
            "stime": stime,
            "sig": _sha1(self.account + self.key + stime),
            "apiname": apiname,
        }

    async def _call(self, apiname: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url.rstrip('/')}/Api/Open/"
        body = await self._post(url, data={**payload, **self._common(apiname)})
        ret = body.get("ret")
        if ret != 0:
            message = str(body.get("msg") or "厂商返回错误")
            permanent = any(hint in message for hint in self.PERMANENT_HINTS)
            raise PrinterError(f"{message}（{ret}）", permanent=permanent, code=ret)
        return body.get("data")

    async def register(self, sn: str, name: str, *, device_key: str = "") -> None:
        data = await self._call(
            "Open_printerAddlist", {"printerContent": f"{sn}#{device_key}#{name}"}
        )
        data = data if isinstance(data, dict) else {}
        if any(str(item).startswith(sn) for item in data.get("ok") or []):
            return
        failed = [str(item) for item in data.get("no") or [] if str(item).startswith(sn)]
        reason = failed[0] if failed else ""
        if "已" in reason and "添加" in reason:  # 已被添加过
            return
        detail = reason.split("#")[-1] if reason else "编号或 KEY 无效"
        raise PrinterError(f"添加打印机失败（{detail}）", permanent=True)

    async def print(self, sn: str, content: str, copies: int) -> str:
        data = await self._call("Open_printMsg", {"sn": sn, "content": content, "times": copies})
        return str(data)

    async def status(self, sn: str) -> Probe:
        data = str(await self._call("Open_queryPrinterStatus", {"sn": sn}))
        if "离线" in data:
            return Probe(PrinterStatus.OFFLINE, "离线")
        if "不正常" in data:
            return Probe(PrinterStatus.ABNORMAL, "在线但异常，一般是缺纸")
        return Probe(PrinterStatus.ONLINE, "在线")

    async def printed(self, cloud_order_id: str) -> bool:
        return bool(await self._call("Open_queryOrderState", {"orderid": cloud_order_id}))

    async def remove(self, sn: str) -> None:
        await self._call("Open_printerDelList", {"snlist": sn})


def client_for(
    http: httpx.AsyncClient,
    brand: str,
    *,
    account: str,
    key: str,
    xpyun_url: str,
    feie_url: str,
    allow_private: bool,
    timeout: float = TIMEOUT_SECONDS,
) -> CloudPrinter:
    if brand == PrinterBrand.XPYUN:
        return XpyunClient(http, xpyun_url, account, key, allow_private, timeout)
    if brand == PrinterBrand.FEIE:
        return FeieClient(http, feie_url, account, key, allow_private, timeout)
    raise ValueError(f"unknown printer brand {brand}")
