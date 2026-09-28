"""Broker mô phỏng (paper trading).

Khớp lệnh tức thì theo giá thị trường vừa thu thập, trừ tiền và ghi vị thế vào
SQLite. Đây là chế độ mặc định: chạy trọn luồng nghiệp vụ mà không rủi ro tiền thật.

**Ví đa tiền tệ.** Crypto niêm yết bằng USDT, chứng khoán và vàng niêm yết bằng
VND. Trộn chung một số dư sẽ sai ngay từ lệnh đầu tiên (mua 2.660 USDT ETH bằng
"VND"), nên mỗi loại tiền có ví riêng và mọi giao dịch chỉ đụng tới ví tương ứng.
"""

from __future__ import annotations

import logging
import math
import uuid

from finagent.broker.base import OrderResult
from finagent.collectors.base import utcnow_iso
from finagent.config import settings
from finagent.storage import add_cash, all_cash, get_cash, get_conn, transaction

logger = logging.getLogger(__name__)

#: Phí giao dịch mô phỏng (0,15% mỗi chiều — mức phổ biến ở công ty chứng khoán VN).
FEE_RATE = 0.0015

#: Loại tiền tệ dùng để định giá từng nhóm tài sản.
ASSET_CURRENCY = {
    "crypto": "USDT",
    "vn_stock": "VND",
    "gold": "VND",
}


def currency_for(asset_class: str) -> str:
    """Trả về loại tiền tệ dùng để giao dịch một nhóm tài sản."""
    return ASSET_CURRENCY.get(asset_class, "VND")


def _round_quantity(quantity: float, asset_class: str) -> float:
    """Làm tròn khối lượng theo lô giao dịch.

    Chứng khoán Việt Nam khớp theo lô 100; crypto cho phép lẻ tới 6 chữ số.
    """
    if asset_class == "crypto":
        return math.floor(quantity * 1e6) / 1e6
    return float(math.floor(quantity / 100) * 100)


class PaperBroker:
    """Broker mô phỏng, lưu trạng thái trong SQLite."""

    mode = "paper"

    def __init__(self, fee_rate: float = FEE_RATE) -> None:
        self.fee_rate = fee_rate

    # -- Truy vấn trạng thái ------------------------------------------------

    def get_cash(self, currency: str = "VND") -> float:
        """Số dư của một ví tiền tệ."""
        return get_cash(currency)

    def all_cash(self) -> dict[str, float]:
        """Số dư của toàn bộ ví."""
        return all_cash()

    def get_position(self, symbol: str) -> dict | None:
        row = get_conn().execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol.upper(),)
        ).fetchone()
        return dict(row) if row else None

    def list_positions(self) -> list[dict]:
        rows = get_conn().execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [dict(r) for r in rows]

    def portfolio_value(self, price_lookup, usdt_vnd: float | None = None) -> float:
        """Tổng giá trị danh mục quy về **VND**.

        ``price_lookup(symbol) -> float | None`` trả về giá thị trường mới nhất.
        Ví USDT được nhân tỷ giá ``usdt_vnd`` (mặc định lấy từ cấu hình).
        """
        rate = usdt_vnd if usdt_vnd is not None else settings.usdt_vnd_rate

        total = 0.0
        for currency, cash in self.all_cash().items():
            total += cash * rate if currency == "USDT" else cash

        for position in self.list_positions():
            price = price_lookup(position["symbol"])
            if price is None:
                price = float(position["avg_price"])   # không có giá mới thì dùng giá vốn
            value = float(position["quantity"]) * float(price)
            if position.get("currency") == "USDT":
                value *= rate
            total += value
        return total

    # -- Đặt lệnh -----------------------------------------------------------

    def buy(self, symbol: str, quantity: float, price: float, asset_class: str = "vn_stock") -> OrderResult:
        symbol = symbol.upper()
        currency = currency_for(asset_class)
        quantity = _round_quantity(quantity, asset_class)
        if quantity <= 0:
            return self._reject(symbol, "buy", quantity, price, "Khối lượng sau khi làm tròn bằng 0.")

        cost = quantity * price
        fee = cost * self.fee_rate
        cash = self.get_cash(currency)
        if cost + fee > cash:
            return self._reject(
                symbol, "buy", quantity, price,
                f"Ví {currency} không đủ tiền: cần {cost + fee:,.2f}, có {cash:,.2f}.",
            )

        with transaction() as conn:
            add_cash(currency, -(cost + fee), conn=conn)
            existing = conn.execute(
                "SELECT * FROM positions WHERE symbol = ?", (symbol,)
            ).fetchone()
            now = utcnow_iso()
            if existing:
                old_qty = float(existing["quantity"])
                old_avg = float(existing["avg_price"])
                new_qty = old_qty + quantity
                new_avg = (old_qty * old_avg + cost) / new_qty
                conn.execute(
                    "UPDATE positions SET quantity = ?, avg_price = ?, updated_at = ? WHERE symbol = ?",
                    (new_qty, new_avg, now, symbol),
                )
            else:
                conn.execute(
                    """INSERT INTO positions
                       (symbol, asset_class, currency, quantity, avg_price, opened_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (symbol, asset_class, currency, quantity, price, now, now),
                )

        return self._fill(symbol, "buy", quantity, price, currency)

    def sell(self, symbol: str, quantity: float, price: float, asset_class: str = "vn_stock") -> OrderResult:
        symbol = symbol.upper()
        position = self.get_position(symbol)
        if not position:
            return self._reject(symbol, "sell", quantity, price, "Không có vị thế để bán.")

        currency = position.get("currency") or currency_for(asset_class)
        held = float(position["quantity"])
        quantity = min(_round_quantity(quantity, asset_class), held)
        if quantity <= 0:
            return self._reject(symbol, "sell", quantity, price, "Khối lượng bán không hợp lệ.")

        proceeds = quantity * price
        fee = proceeds * self.fee_rate

        with transaction() as conn:
            add_cash(currency, proceeds - fee, conn=conn)
            remaining = held - quantity
            if remaining <= 1e-9:
                conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            else:
                conn.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE symbol = ?",
                    (remaining, utcnow_iso(), symbol),
                )

        return self._fill(symbol, "sell", quantity, price, currency)

    # -- Tiện ích nội bộ ----------------------------------------------------

    def _fill(self, symbol: str, side: str, quantity: float, price: float, currency: str) -> OrderResult:
        ref = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        logger.info(
            "Khớp lệnh mô phỏng %s %s %.6f @ %.4f %s (%s)",
            side, symbol, quantity, price, currency, ref,
        )
        return OrderResult(
            ok=True, symbol=symbol, side=side, quantity=quantity, price=price,
            status="filled", mode=self.mode, broker_ref=ref, currency=currency,
        )

    def _reject(self, symbol: str, side: str, quantity: float, price: float, message: str) -> OrderResult:
        logger.warning("Từ chối lệnh %s %s: %s", side, symbol, message)
        return OrderResult(
            ok=False, symbol=symbol, side=side, quantity=quantity, price=price,
            status="rejected", mode=self.mode, message=message,
        )
