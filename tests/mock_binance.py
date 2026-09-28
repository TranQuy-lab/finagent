"""Sàn Binance giả lập để kiểm chứng adapter mà không cần khoá API thật.

Điểm quan trọng: máy chủ này **kiểm tra chữ ký HMAC-SHA256** của mọi yêu cầu cần
xác thực, đúng như Binance thật. Nhờ vậy bài test chứng minh được phần ký request
là đúng — chứ không chỉ kiểm tra rằng hàm trả về một dict nào đó.

Nó cũng mô phỏng khớp lệnh: trừ/cộng số dư theo lệnh, trả về ``executedQty`` và
``cummulativeQuoteQty``, để kiểm chứng được trọn luồng mua → có vị thế → bán.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class MockBinanceState:
    """Trạng thái tài khoản giả và nhật ký yêu cầu đã nhận."""

    api_key: str = "test-api-key"
    api_secret: str = "test-api-secret"
    base_asset: str = "BTC"
    quote_asset: str = "USDT"
    step_size: str = "0.00001000"
    min_notional: float = 5.0
    balances: dict[str, float] = field(default_factory=lambda: {"USDT": 10_000.0, "BTC": 0.0})
    price: float = 83_000.0

    orders: list[dict] = field(default_factory=list)
    signed_requests: list[dict] = field(default_factory=list)
    signature_failures: int = 0
    auth_failures: int = 0
    #: Đặt để giả lập sàn từ chối lệnh (ví dụ hết số dư).
    reject_message: str | None = None
    _next_order_id: int = 1000

    @property
    def symbol(self) -> str:
        return f"{self.base_asset}{self.quote_asset}"

    def next_order_id(self) -> int:
        self._next_order_id += 1
        return self._next_order_id


class _Handler(BaseHTTPRequestHandler):
    state: MockBinanceState
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        return

    # -- Tiện ích -----------------------------------------------------------

    def _send(self, body: dict, status: int = 200) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, code: int, message: str, status: int = 400) -> None:
        self._send({"code": code, "msg": message}, status=status)

    def _verify_signature(self, query: dict) -> bool:
        """Kiểm tra chữ ký đúng như Binance thật: HMAC-SHA256 trên chuỗi truy vấn."""
        provided = query.pop("signature", None)
        if not provided:
            return False
        # Dựng lại chuỗi truy vấn theo đúng thứ tự tham số đã nhận.
        query_string = urllib.parse.urlencode(query)
        expected = hmac.new(
            self.state.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, provided)

    def _authenticate(self, query: dict) -> bool:
        if self.headers.get("X-MBX-APIKEY") != self.state.api_key:
            self.state.auth_failures += 1
            self._error(-2015, "Invalid API-key, IP, or permissions for action.", status=401)
            return False
        if not self._verify_signature(query):
            self.state.signature_failures += 1
            self._error(-1022, "Signature for this request is not valid.")
            return False
        return True

    # -- Endpoint -----------------------------------------------------------

    def do_GET(self) -> None:                   # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if parsed.path == "/api/v3/ping":
            self._send({})
            return

        if parsed.path == "/api/v3/time":
            self._send({"serverTime": 1_790_570_906_410})
            return

        if parsed.path == "/api/v3/exchangeInfo":
            self._send({"symbols": [{
                "symbol": self.state.symbol,
                "status": "TRADING",
                "baseAsset": self.state.base_asset,
                "quoteAsset": self.state.quote_asset,
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": self.state.step_size,
                     "maxQty": "9000", "stepSize": self.state.step_size},
                    {"filterType": "NOTIONAL", "minNotional": str(self.state.min_notional),
                     "applyMinToMarket": True},
                ],
            }]})
            return

        if parsed.path == "/api/v3/account":
            if not self._authenticate(query):
                return
            self.state.signed_requests.append({"method": "GET", "path": parsed.path})
            self._send({
                "canTrade": True,
                "accountType": "SPOT",
                "balances": [
                    {"asset": asset, "free": f"{amount:.8f}", "locked": "0.00000000"}
                    for asset, amount in self.state.balances.items()
                ],
            })
            return

        self._error(-1121, f"Unknown endpoint {parsed.path}", status=404)

    def do_POST(self) -> None:                  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        # Binance nhận tham số ở query string ngay cả với POST.
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if not query and raw:
            query = dict(urllib.parse.parse_qsl(raw.decode()))

        if parsed.path == "/api/v3/order":
            if not self._authenticate(query):
                return
            self._handle_order(query)
            return

        self._error(-1121, f"Unknown endpoint {parsed.path}", status=404)

    def _handle_order(self, query: dict) -> None:
        self.state.signed_requests.append({"method": "POST", "path": "/api/v3/order", **query})

        if self.state.reject_message:
            self._error(-2010, self.state.reject_message)
            return

        symbol = query.get("symbol", "")
        side = query.get("side", "").upper()
        quantity = float(query.get("quantity", 0) or 0)

        if symbol != self.state.symbol:
            self._error(-1121, f"Invalid symbol {symbol}.")
            return

        step = Decimal(self.state.step_size)
        if Decimal(str(quantity)) % step != 0:
            self._error(-1111, "Quantity does not meet the LOT_SIZE filter.")
            return
        if quantity <= 0:
            self._error(-1013, "Filter failure: LOT_SIZE")
            return

        notional = quantity * self.state.price
        if notional < self.state.min_notional:
            self._error(-1013, f"Filter failure: NOTIONAL (min {self.state.min_notional})")
            return

        if side == "BUY":
            cost = notional
            if self.state.balances.get(self.state.quote_asset, 0.0) < cost:
                self._error(-2010, "Account has insufficient balance for requested action.")
                return
            self.state.balances[self.state.quote_asset] -= cost
            self.state.balances[self.state.base_asset] = (
                self.state.balances.get(self.state.base_asset, 0.0) + quantity
            )
        elif side == "SELL":
            if self.state.balances.get(self.state.base_asset, 0.0) < quantity:
                self._error(-2010, "Account has insufficient balance for requested action.")
                return
            self.state.balances[self.state.base_asset] -= quantity
            self.state.balances[self.state.quote_asset] = (
                self.state.balances.get(self.state.quote_asset, 0.0) + notional
            )
        else:
            self._error(-1104, "Invalid side.")
            return

        order = {
            "symbol": symbol,
            "orderId": self.state.next_order_id(),
            "side": side,
            "type": "MARKET",
            "status": "FILLED",
            "executedQty": f"{quantity:.8f}",
            "cummulativeQuoteQty": f"{notional:.8f}",
        }
        self.state.orders.append(order)
        self._send(order)


class MockBinanceServer:
    """Sàn giả lập chạy trong luồng nền, dùng được như context manager."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, **state_kwargs) -> None:
        self.state = MockBinanceState(**state_kwargs)
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self._server.server_address[0]}:{self._server.server_address[1]}"

    def start(self) -> MockBinanceServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> MockBinanceServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


if __name__ == "__main__":
    server = MockBinanceServer(port=8125).start()
    print(f"Sàn Binance giả lập đang chạy tại {server.base_url}")
    print(f"  api_key    = {server.state.api_key}")
    print(f"  api_secret = {server.state.api_secret}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
