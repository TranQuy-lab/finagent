"""Broker Binance — đặt lệnh thật trên sàn (mặc định là Testnet).

Đây là adapter đầu tiên nối hệ thống vào một sàn giao dịch thật. Chọn Binance vì
API công khai, tài liệu rõ, và có **Testnet** — API giống hệt bản thật nhưng dùng
tiền giả, nên kiểm chứng được trọn luồng đặt lệnh mà không mất tiền.

Hai điều khác biệt so với broker mô phỏng:

1. **Kích thước lệnh do sàn quy định.** Binance chấp nhận bội số của ``stepSize``
   và từ chối lệnh dưới ``minNotional``. Phải hỏi sàn trước khi đặt, không được
   đoán — nếu không sẽ bị trả lỗi ``LOT_SIZE`` hoặc ``MIN_NOTIONAL``.
2. **Sàn không biết giá vốn.** Binance chỉ cho biết đang giữ bao nhiêu, không cho
   biết đã mua ở giá nào. Nên số lượng lấy từ sàn (nguồn sự thật), còn giá vốn
   bình quân giữ trong SQLite cục bộ (sàn không cung cấp được).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN

import requests

from finagent.broker.base import OrderResult
from finagent.collectors.base import utcnow_iso
from finagent.config import settings
from finagent.storage import get_conn, transaction

logger = logging.getLogger(__name__)

TESTNET_BASE_URL = "https://testnet.binance.vision"
LIVE_BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT = 20

#: Sàn từ chối nếu thời điểm yêu cầu lệch quá ngưỡng này (mili giây).
RECV_WINDOW_MS = 5000


class BinanceError(RuntimeError):
    """Lỗi do sàn trả về, kèm mã lỗi để tra cứu."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def split_symbol(symbol: str) -> tuple[str, str]:
    """Tách ``BTCUSDT`` thành ``("BTC", "USDT")``."""
    upper = symbol.upper().replace("-", "")
    for quote in ("USDT", "FDUSD", "TUSD", "USDC", "BUSD", "BTC", "ETH", "BNB"):
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)], quote
    raise ValueError(f"Không tách được mã {symbol!r} thành tài sản gốc và tài sản báo giá.")


def round_step(quantity: float, step_size: str) -> float:
    """Làm tròn xuống theo bội số ``stepSize`` mà sàn yêu cầu.

    Dùng ``Decimal`` chứ không dùng số thực: ``0.1 + 0.2`` trong số thực cho
    ``0.30000000000000004``, và sai số đó đủ để sàn từ chối lệnh.
    """
    step = Decimal(step_size)
    if step <= 0:
        return quantity
    value = Decimal(str(quantity))
    stepped = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    return float(stepped)


class BinanceBroker:
    """Broker thật, nói chuyện với Binance Spot API.

    Mặc định trỏ vào **Testnet**. Muốn dùng tiền thật phải đặt
    ``BINANCE_TESTNET=false`` **và** ``BINANCE_LIVE_CONFIRM=YES`` — hai điều kiện,
    để không ai vô tình giao dịch tiền thật.
    """

    mode = "live"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        fee_rate: float | None = None,
        timeout: int = REQUEST_TIMEOUT,
        base_url: str | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError(
                "Thiếu BINANCE_API_KEY hoặc BINANCE_API_SECRET. "
                "Lấy khoá Testnet miễn phí tại https://testnet.binance.vision"
            )

        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        # ``base_url`` cho phép trỏ vào sàn giả lập trong test, hoặc một endpoint
        # Binance khác. Bỏ trống thì chọn theo testnet/thật.
        self.base_url = (base_url or (TESTNET_BASE_URL if testnet else LIVE_BASE_URL)).rstrip("/")
        self.fee_rate = settings.binance_fee_rate if fee_rate is None else fee_rate
        self.timeout = timeout

        self._session = requests.Session()
        self._session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "User-Agent": "FinAgent/1.0",
        })
        self._symbol_cache: dict[str, dict] = {}
        self._time_offset_ms = 0

        if testnet:
            logger.info("Binance: dùng TESTNET (tiền giả) — %s", self.base_url)
        else:
            logger.warning("Binance: dùng TÀI KHOẢN THẬT (tiền thật) — %s", self.base_url)

    # -- Kết nối ------------------------------------------------------------

    @classmethod
    def from_settings(cls) -> BinanceBroker:
        """Tạo broker từ cấu hình, có kiểm tra an toàn cho chế độ tiền thật.

        Cửa chặn gồm hai điều kiện: phải tắt Testnet **và** phải đặt
        ``BINANCE_LIVE_CONFIRM=YES``. Chấp nhận cả chữ hoa lẫn chữ thường, nhưng
        không chấp nhận giá trị khác — người dùng phải chủ động viết ra.
        """
        if not settings.binance_testnet and settings.binance_live_confirm.strip().upper() != "YES":
            raise RuntimeError(
                "BINANCE_TESTNET=false nhưng chưa xác nhận giao dịch tiền thật.\n"
                "Đây là cửa chặn cố ý. Nếu thực sự muốn dùng tiền thật, đặt thêm:\n"
                "    BINANCE_LIVE_CONFIRM=YES\n"
                "Nên thử trên Testnet trước: BINANCE_TESTNET=true"
            )
        return cls(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.binance_testnet,
            base_url=settings.binance_base_url or None,
        )

    def _sync_time(self) -> None:
        """Tính lệch đồng hồ so với sàn.

        Máy ảo có thể lệch giờ; lệch quá ``recvWindow`` là mọi lệnh bị từ chối
        với lỗi ``-1021``. Đồng bộ một lần rồi bù vào mọi yêu cầu sau.
        """
        try:
            response = self._session.get(f"{self.base_url}/api/v3/time", timeout=self.timeout)
            response.raise_for_status()
            server_time = int(response.json()["serverTime"])
        except Exception as exc:  # noqa: BLE001 - không đồng bộ được vẫn thử chạy tiếp
            logger.warning("Không lấy được giờ sàn Binance: %s", exc)
            return

        local_ms = int(time.time() * 1000)
        self._time_offset_ms = server_time - local_ms
        if abs(self._time_offset_ms) > 1000:
            logger.warning(
                "Đồng hồ máy chủ lệch %d ms so với Binance — đã tự bù.",
                self._time_offset_ms,
            )

    def _timestamp(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _signed_request(self, method: str, path: str, params: dict | None = None) -> dict:
        """Gọi endpoint cần xác thực: ký HMAC-SHA256 trên chuỗi truy vấn."""
        query = dict(params or {})
        query["timestamp"] = self._timestamp()
        query["recvWindow"] = RECV_WINDOW_MS
        query_string = urllib.parse.urlencode(query)

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = f"{self.base_url}{path}?{query_string}&signature={signature}"
        try:
            response = self._session.request(method, url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise BinanceError(f"Không kết nối được Binance: {exc}") from exc

        return self._parse(response)

    def _public_request(self, path: str, params: dict | None = None) -> dict:
        """Gọi endpoint công khai, không cần ký."""
        try:
            response = self._session.get(
                f"{self.base_url}{path}", params=params or {}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise BinanceError(f"Không kết nối được Binance: {exc}") from exc
        return self._parse(response)

    @staticmethod
    def _parse(response: requests.Response) -> dict:
        """Đọc phản hồi; chuyển lỗi của sàn thành ngoại lệ có mã."""
        try:
            payload = response.json()
        except ValueError:
            raise BinanceError(f"Sàn trả về dữ liệu không phải JSON (HTTP {response.status_code})")

        if response.status_code >= 400 or (isinstance(payload, dict) and "code" in payload
                                           and int(payload.get("code", 0)) < 0):
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("msg") if isinstance(payload, dict) else str(payload)
            raise BinanceError(f"Binance lỗi {code}: {message}", code=code)
        return payload

    # -- Thông tin sàn ------------------------------------------------------

    def symbol_info(self, symbol: str) -> dict:
        """Lấy giới hạn của một mã: bước khối lượng, giá trị lệnh tối thiểu."""
        symbol = symbol.upper()
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        payload = self._public_request("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = payload.get("symbols") or []
        if not symbols:
            raise BinanceError(f"Sàn không có mã {symbol}")

        entry = symbols[0]
        if entry.get("status") != "TRADING":
            raise BinanceError(f"Mã {symbol} hiện không giao dịch được (trạng thái {entry.get('status')}).")

        info = {
            "symbol": symbol,
            "base_asset": entry["baseAsset"],
            "quote_asset": entry["quoteAsset"],
            "step_size": "0.00000001",
            "min_qty": 0.0,
            "min_notional": 0.0,
        }
        for rule in entry.get("filters", []):
            kind = rule.get("filterType")
            if kind == "LOT_SIZE":
                info["step_size"] = rule.get("stepSize", info["step_size"])
                info["min_qty"] = float(rule.get("minQty", 0) or 0)
            elif kind in ("NOTIONAL", "MIN_NOTIONAL"):
                info["min_notional"] = float(rule.get("minNotional", 0) or 0)

        self._symbol_cache[symbol] = info
        return info

    def account(self) -> dict:
        """Thông tin tài khoản, gồm số dư các tài sản."""
        return self._signed_request("GET", "/api/v3/account")

    def balances(self) -> dict[str, float]:
        """Số dư khả dụng (đã trừ phần đang bị lệnh treo giữ)."""
        result: dict[str, float] = {}
        for entry in self.account().get("balances") or []:
            free = float(entry.get("free", 0) or 0)
            locked = float(entry.get("locked", 0) or 0)
            if free or locked:
                result[entry["asset"]] = free
        return result

    # -- Giao diện Broker ---------------------------------------------------

    def get_cash(self, currency: str = "USDT") -> float:
        """Số dư khả dụng của một loại tiền trên sàn."""
        return self.balances().get(currency.upper(), 0.0)

    def all_cash(self) -> dict[str, float]:
        return self.balances()

    def get_position(self, symbol: str) -> dict | None:
        """Vị thế của một mã: khối lượng lấy từ sàn, giá vốn lấy từ sổ cục bộ."""
        info = self.symbol_info(symbol)
        quantity = self.balances().get(info["base_asset"], 0.0)
        if quantity <= 0:
            return None

        row = get_conn().execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol.upper(),)
        ).fetchone()
        avg_price = float(row["avg_price"]) if row else 0.0
        return {
            "symbol": symbol.upper(),
            "asset_class": "crypto",
            "currency": info["quote_asset"],
            "quantity": quantity,
            "avg_price": avg_price,
            "avg_price_known": row is not None,
        }

    def list_positions(self) -> list[dict]:
        """Mọi tài sản đang giữ trên sàn có giá trị đáng kể."""
        balances = self.balances()
        positions: list[dict] = []
        for asset, quantity in balances.items():
            if asset in ("USDT", "FDUSD", "TUSD", "USDC", "BUSD"):
                continue
            if quantity <= 0:
                continue
            symbol = f"{asset}USDT"
            row = get_conn().execute(
                "SELECT * FROM positions WHERE symbol = ?", (symbol,)
            ).fetchone()
            positions.append({
                "symbol": symbol,
                "asset_class": "crypto",
                "currency": "USDT",
                "quantity": quantity,
                "avg_price": float(row["avg_price"]) if row else 0.0,
                "avg_price_known": row is not None,
            })
        return sorted(positions, key=lambda p: p["symbol"])

    def portfolio_value(self, price_lookup, usdt_vnd: float | None = None) -> float:
        """Tổng giá trị tài khoản quy về VND.

        Tiền mặt lấy trực tiếp từ sàn; các tài sản khác nhân với giá thị trường
        do máy con thu thập (``price_lookup``).
        """
        rate = settings.usdt_vnd_rate if usdt_vnd is None else usdt_vnd
        balances = self.balances()

        total = 0.0
        for asset, quantity in balances.items():
            if quantity <= 0:
                continue
            if asset in ("USDT", "FDUSD", "TUSD", "USDC", "BUSD"):
                total += quantity
                continue
            price = price_lookup(f"{asset}USDT")
            if price:
                total += quantity * float(price)

        return total * rate

    def buy(self, symbol: str, quantity: float, price: float, asset_class: str = "crypto") -> OrderResult:
        """Đặt lệnh mua thị trường (market) trên sàn.

        ``price`` chỉ dùng để ghi sổ giá vốn; sàn khớp theo giá thị trường thực tế,
        nên giá khớp trong kết quả là giá sàn trả về chứ không phải giá đề xuất.
        """
        return self._place_order(symbol, "BUY", quantity, price)

    def sell(self, symbol: str, quantity: float, price: float, asset_class: str = "crypto") -> OrderResult:
        """Đặt lệnh bán thị trường (market) trên sàn."""
        return self._place_order(symbol, "SELL", quantity, price)

    # -- Đặt lệnh -----------------------------------------------------------

    def _place_order(self, symbol: str, side: str, quantity: float, reference_price: float) -> OrderResult:
        symbol = symbol.upper()
        try:
            info = self.symbol_info(symbol)
        except BinanceError as exc:
            return self._reject(symbol, side.lower(), quantity, reference_price, str(exc))

        stepped = round_step(quantity, info["step_size"])
        if stepped <= 0 or stepped < info["min_qty"]:
            return self._reject(
                symbol, side.lower(), stepped, reference_price,
                f"Khối lượng {stepped:g} dưới mức tối thiểu {info['min_qty']:g} của sàn.",
            )

        if reference_price > 0 and stepped * reference_price < info["min_notional"]:
            return self._reject(
                symbol, side.lower(), stepped, reference_price,
                f"Giá trị lệnh {stepped * reference_price:,.2f} {info['quote_asset']} "
                f"dưới mức tối thiểu {info['min_notional']:,.2f} của sàn.",
            )

        # Bán thì không được vượt quá số đang có trên sàn.
        if side == "SELL":
            held = self.balances().get(info["base_asset"], 0.0)
            if stepped > held:
                stepped = round_step(held, info["step_size"])
            if stepped <= 0:
                return self._reject(
                    symbol, "sell", stepped, reference_price,
                    f"Không có {info['base_asset']} khả dụng để bán.",
                )

        try:
            order = self._signed_request("POST", "/api/v3/order", {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": self._format_quantity(stepped, info["step_size"]),
            })
        except BinanceError as exc:
            logger.error("Binance từ chối lệnh %s %s: %s", side, symbol, exc)
            return self._reject(symbol, side.lower(), stepped, reference_price, str(exc))

        return self._record_fill(symbol, side, order, reference_price, info)

    @staticmethod
    def _format_quantity(quantity: float, step_size: str) -> str:
        """Định dạng khối lượng đúng số chữ số thập phân mà ``stepSize`` yêu cầu.

        Binance từ chối nếu gửi thừa chữ số thập phân (ví dụ ``0.00100000`` khi
        bước là ``0.001``), nên phải cắt đúng độ dài.
        """
        decimals = max(0, -Decimal(step_size).as_tuple().exponent)
        return f"{quantity:.{decimals}f}"

    def _record_fill(self, symbol: str, side: str, order: dict,
                     reference_price: float, info: dict) -> OrderResult:
        """Ghi nhận lệnh đã khớp vào sổ cục bộ (giá vốn) và trả kết quả."""
        filled = float(order.get("executedQty", 0) or 0)
        quote_spent = float(order.get("cummulativeQuoteQty", 0) or 0)
        # Giá khớp bình quân thật = tổng tiền / tổng lượng.
        avg_price = (quote_spent / filled) if filled else reference_price

        status_map = {
            "FILLED": "filled",
            "PARTIALLY_FILLED": "filled",
            "NEW": "pending",
            "PENDING_NEW": "pending",
        }
        status = status_map.get(order.get("status", ""), "pending")

        if filled > 0:
            self._update_local_book(symbol, side, filled, avg_price, info)

        logger.info(
            "Binance khớp lệnh %s %s: %.8f @ %.4f %s (orderId=%s, %s)",
            side, symbol, filled, avg_price, info["quote_asset"], order.get("orderId"), status,
        )
        return OrderResult(
            ok=status != "pending",
            symbol=symbol,
            side=side.lower(),
            quantity=filled,
            price=avg_price,
            status=status,
            mode=self.mode,
            broker_ref=str(order.get("orderId", "")),
            currency=info["quote_asset"],
            message="" if status != "pending" else "Lệnh đã gửi, chờ khớp.",
        )

    def _update_local_book(self, symbol: str, side: str, quantity: float,
                           price: float, info: dict) -> None:
        """Cập nhật giá vốn bình quân trong SQLite (sàn không cung cấp dữ liệu này)."""
        with transaction() as conn:
            if side == "BUY":
                existing = conn.execute(
                    "SELECT * FROM positions WHERE symbol = ?", (symbol,)
                ).fetchone()
                now = utcnow_iso()
                if existing:
                    old_qty = float(existing["quantity"])
                    old_avg = float(existing["avg_price"])
                    new_qty = old_qty + quantity
                    new_avg = (old_qty * old_avg + quantity * price) / new_qty if new_qty else price
                    conn.execute(
                        "UPDATE positions SET quantity = ?, avg_price = ?, updated_at = ? WHERE symbol = ?",
                        (new_qty, new_avg, now, symbol),
                    )
                else:
                    conn.execute(
                        """INSERT INTO positions
                           (symbol, asset_class, currency, quantity, avg_price, opened_at, updated_at)
                           VALUES (?, 'crypto', ?, ?, ?, ?, ?)""",
                        (symbol, info["quote_asset"], quantity, price, now, now),
                    )
            else:
                existing = conn.execute(
                    "SELECT * FROM positions WHERE symbol = ?", (symbol,)
                ).fetchone()
                if existing:
                    remaining = float(existing["quantity"]) - quantity
                    if remaining <= 1e-12:
                        conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
                    else:
                        conn.execute(
                            "UPDATE positions SET quantity = ?, updated_at = ? WHERE symbol = ?",
                            (remaining, utcnow_iso(), symbol),
                        )

    def test_connection(self) -> dict:
        """Kiểm tra khoá API và kết nối — dùng cho lệnh ``finagent broker-check``."""
        self._sync_time()
        account = self.account()
        return {
            "endpoint": self.base_url,
            "testnet": self.testnet,
            "can_trade": account.get("canTrade"),
            "account_type": account.get("accountType"),
            "balances_nonzero": {
                asset: float(entry.get("free", 0) or 0)
                for asset, entry in (
                    (e["asset"], e) for e in account.get("balances") or []
                )
                if float(entry.get("free", 0) or 0) > 0
            },
        }

    def _reject(self, symbol: str, side: str, quantity: float, price: float, message: str) -> OrderResult:
        logger.warning("Binance từ chối lệnh %s %s: %s", side, symbol, message)
        return OrderResult(
            ok=False, symbol=symbol, side=side, quantity=quantity, price=price,
            status="rejected", mode=self.mode, message=message,
        )
