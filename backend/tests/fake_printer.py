"""模拟的云打印机厂商（芯烨云和飞鹅云的接口，设计文档 §29.3）。

uv run python -m tests.fake_printer --port 8904

- 单元测试里挂在 httpx.MockTransport 上（transport()）；浏览器验收时作为 HTTP 服务运行，后端的
  EDP_PRINT_XPYUN_URL、EDP_PRINT_FEIE_URL 都指向它（按路径区分两家）。
- 校验签名（SHA1(账号 + 密钥 + 时间戳)），记录收到的每一次打印（打印机编号、内容、份数），
  编号以 XPY 开头的算芯烨云的有效编号、以 FEIE 开头的算飞鹅云的；可以把打印机设成离线或缺纸，
  可以让接下来几次请求失败（模拟网络抖动）。
- 控制接口：GET /_fake/prints（收到的打印）、POST /_fake/reset、
  POST /_fake/printers/{sn}/status {"status": "online" | "offline" | "abnormal"}、
  POST /_fake/fail {"count": n}。
"""

import argparse
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

XPYUN_USER = "dev@example.com"
XPYUN_KEY = "xpyun-dev-key"
FEIE_USER = "feie@example.com"
FEIE_KEY = "feie-dev-ukey"
# 芯烨云的内容上限（GBK 计）。
XPYUN_MAX_BYTES = 12 * 1024


@dataclass
class Printed:
    brand: str
    sn: str
    content: str
    copies: int
    order_id: str
    at: float

    def dump(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "sn": self.sn,
            "content": self.content,
            "copies": self.copies,
            "order_id": self.order_id,
        }


@dataclass
class FakePrinterCloud:
    printers: dict[tuple[str, str], str] = field(default_factory=dict)  # (brand, sn) -> name
    status: dict[str, str] = field(default_factory=dict)  # sn -> online | offline | abnormal
    prints: list[Printed] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    # 接下来这么多次请求返回 500（模拟网络抖动）。
    fail_count: int = 0
    counter: int = 0
    # 已打印的云端订单号（离线的打印机收到的订单不算打印）。
    printed_ids: set[str] = field(default_factory=set)

    # ---- 控制 ----

    def reset(self) -> None:
        self.printers.clear()
        self.status.clear()
        self.prints.clear()
        self.requests.clear()
        self.printed_ids.clear()
        self.fail_count = 0

    def set_status(self, sn: str, status: str) -> None:
        self.status[sn] = status

    def contents(self, sn: str | None = None) -> list[str]:
        return [p.content for p in self.prints if sn is None or p.sn == sn]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- 请求分发 ----

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_count > 0:
            self.fail_count -= 1
            return httpx.Response(500, text="upstream unavailable")
        path = request.url.path
        if "/api/openapi/xprinter/" in path:
            return self._xpyun(path.rsplit("/", 1)[-1], request)
        if path.rstrip("/").endswith("/Api/Open"):
            return self._feie(request)
        return httpx.Response(404, json={"error": "unknown endpoint"})

    def _order_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{int(time.time())}{self.counter:04d}"

    def _record(self, brand: str, sn: str, content: str, copies: int, prefix: str) -> str:
        order_id = self._order_id(prefix)
        self.prints.append(Printed(brand, sn, content, copies, order_id, time.time()))
        if self.status.get(sn, "online") == "online":
            self.printed_ids.add(order_id)
        return order_id

    # ---- 芯烨云 ----

    @staticmethod
    def _xpyun_reply(code: int, data: Any, msg: str = "ok") -> httpx.Response:
        return httpx.Response(
            200, json={"code": code, "msg": msg, "data": data, "serverExecutedTime": 1}
        )

    def _xpyun(self, action: str, request: httpx.Request) -> httpx.Response:
        if "application/json" not in request.headers.get("content-type", ""):
            return self._xpyun_reply(-1, None, "REQUEST_HEADER_ERROR")
        try:
            body = json.loads(request.content or b"{}")
        except json.JSONDecodeError:
            return self._xpyun_reply(-2, None, "REQUEST_PARAM_INVALID")
        user, timestamp, sign = body.get("user"), body.get("timestamp"), body.get("sign")
        if user != XPYUN_USER:
            return self._xpyun_reply(-4, None, "REQUEST_USER_NOT_REGISTER")
        expected = hashlib.sha1(f"{user}{XPYUN_KEY}{timestamp}".encode()).hexdigest()
        if sign != expected:
            return self._xpyun_reply(-3, None, "REQUEST_SIGN_FAILED")
        if action == "addPrinters":
            success, fail, fail_msg = [], [], []
            for item in body.get("items") or []:
                sn = str(item.get("sn", ""))
                if not sn.startswith("XPY"):
                    fail.append(sn)
                    fail_msg.append(f"{sn}:1008")
                elif ("xpyun", sn) in self.printers:
                    fail.append(sn)
                    fail_msg.append(f"{sn}:1009")
                else:
                    self.printers[("xpyun", sn)] = str(item.get("name", ""))
                    success.append(sn)
            return self._xpyun_reply(0, {"success": success, "fail": fail, "failMsg": fail_msg})
        if action == "delPrinters":
            for sn in body.get("snlist") or []:
                self.printers.pop(("xpyun", sn), None)
            return self._xpyun_reply(0, {"success": body.get("snlist") or [], "fail": []})
        sn = str(body.get("sn", ""))
        if action == "queryOrderState":
            order_id = str(body.get("orderId", ""))
            known = any(p.order_id == order_id for p in self.prints)
            if not known:
                return self._xpyun_reply(1005, False, "ORDER_NOT_FOUND")
            return self._xpyun_reply(0, order_id in self.printed_ids)
        if ("xpyun", sn) not in self.printers:
            return self._xpyun_reply(1002, None, "PRINTER_NOT_REGISTER")
        if action == "queryPrinterStatus":
            status = self.status.get(sn, "online")
            value = {"online": 1, "offline": 0, "abnormal": 2}[status]
            return self._xpyun_reply(0, value)
        if action == "print":
            content = str(body.get("content", ""))
            if len(content.encode("gbk", errors="replace")) > XPYUN_MAX_BYTES:
                return self._xpyun_reply(1007, None, "PRINT_CONTENT_MORE_THAN_12K_BYTES")
            copies = int(body.get("copies") or 1)
            if not 1 <= copies <= 5:
                return self._xpyun_reply(-2, None, "REQUEST_PARAM_INVALID")
            if int(body.get("mode") or 0) == 0 and self.status.get(sn, "online") == "offline":
                return self._xpyun_reply(1003, None, "PRINTER_OFFLINE")
            return self._xpyun_reply(0, self._record("xpyun", sn, content, copies, "SMI"))
        return self._xpyun_reply(-2, None, "REQUEST_PARAM_INVALID")

    # ---- 飞鹅云 ----

    @staticmethod
    def _feie_reply(ret: int, data: Any, msg: str = "ok") -> httpx.Response:
        return httpx.Response(
            200, json={"ret": ret, "msg": msg, "data": data, "serverExecutedTime": 1}
        )

    def _feie(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        user, stime, sig = form.get("user"), form.get("stime", ""), form.get("sig")
        if user != FEIE_USER:
            return self._feie_reply(-2, None, "用户不存在")
        expected = hashlib.sha1(f"{user}{FEIE_KEY}{stime}".encode()).hexdigest()
        if sig != expected:
            return self._feie_reply(-3, None, "签名错误")
        apiname = form.get("apiname", "")
        if apiname == "Open_printerAddlist":
            ok, no = [], []
            for item in (form.get("printerContent") or "").split("|"):
                parts = item.split("#")
                sn = parts[0]
                if not sn.startswith("FEIE") or len(parts) < 2 or not parts[1]:
                    no.append(f"{item}（错误：打印机编号或 KEY 不正确）")
                elif ("feie", sn) in self.printers:
                    no.append(f"{item}（错误：该打印机已被添加过）")
                else:
                    self.printers[("feie", sn)] = parts[2] if len(parts) > 2 else ""
                    ok.append(item)
            return self._feie_reply(0, {"ok": ok, "no": no})
        if apiname == "Open_printerDelList":
            for sn in (form.get("snlist") or "").split("-"):
                self.printers.pop(("feie", sn), None)
            return self._feie_reply(0, {"ok": [form.get("snlist")], "no": []})
        if apiname == "Open_queryOrderState":
            order_id = form.get("orderid", "")
            return self._feie_reply(0, order_id in self.printed_ids)
        sn = form.get("sn", "")
        if ("feie", sn) not in self.printers:
            return self._feie_reply(1002, None, "打印机未添加")
        if apiname == "Open_queryPrinterStatus":
            status = self.status.get(sn, "online")
            text = {
                "online": "在线，工作状态正常。",
                "offline": "离线。",
                "abnormal": "在线，工作状态不正常。",
            }[status]
            return self._feie_reply(0, text)
        if apiname == "Open_printMsg":
            content = form.get("content", "")
            copies = int(form.get("times") or 1)
            return self._feie_reply(0, self._record("feie", sn, content, copies, "FEIE"))
        return self._feie_reply(-2, None, "参数错误")

    # ---- 作为 HTTP 服务运行（浏览器验收） ----

    def asgi(self) -> Any:
        fake = self

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                return
            raw = b""
            while True:
                message = await receive()
                raw += message.get("body", b"")
                if not message.get("more_body"):
                    break
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            path = scope["path"]
            if path.startswith("/_fake/"):
                status, content = fake.control(scope["method"], path, raw)
            else:
                request = httpx.Request(
                    scope["method"],
                    f"http://fake-printer{path}",
                    headers=headers,
                    content=raw,
                )
                response = fake.handle(request)
                status, content = response.status_code, response.content
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": content})

        return app

    def control(self, method: str, path: str, raw: bytes) -> tuple[int, bytes]:
        body: dict[str, Any] = json.loads(raw) if raw else {}
        if method == "GET" and path == "/_fake/prints":
            return 200, json.dumps([p.dump() for p in self.prints], ensure_ascii=False).encode()
        if method == "POST" and path == "/_fake/reset":
            self.reset()
            return 200, b"{}"
        if method == "POST" and path == "/_fake/fail":
            self.fail_count = int(body.get("count", 1))
            return 200, b"{}"
        if method == "POST" and path.startswith("/_fake/printers/") and path.endswith("/status"):
            sn = path[len("/_fake/printers/") : -len("/status")]
            self.set_status(sn, str(body.get("status", "online")))
            return 200, b"{}"
        return 404, b'{"error": "unknown control endpoint"}'


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="模拟的云打印机厂商")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8904)
    args = parser.parse_args()
    fake = FakePrinterCloud()
    print(f"fake printer cloud on http://{args.host}:{args.port}")
    uvicorn.run(fake.asgi(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
