"""Giao diện broker — cho phép đổi từ mô phỏng sang tài khoản thật.

Toàn bộ hệ thống chỉ nói chuyện với ``Broker``. Muốn giao dịch thật, viết thêm
một lớp con (SSI, TCBS, Binance…) rồi đổi ``FINAGENT_TRADING_MODE``; phần còn
lại của hệ thống không phải sửa gì.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class OrderResult:
    """Kết quả một lệnh gửi tới broker."""

    ok: bool
    symbol: str
    side: str                 # "buy" | "sell"
    quantity: float
    price: float
    status: str               # "filled" | "rejected" | "pending"
    mode: str                 # "paper" | "live"
    broker_ref: str | None = None
    message: str = ""
    currency: str = "VND"     # loại tiền tệ dùng để khớp lệnh

    @property
    def amount(self) -> float:
        return self.quantity * self.price


@runtime_checkable
class Broker(Protocol):
    """Hợp đồng tối thiểu mà mọi broker phải thoả."""

    mode: str

    def get_cash(self, currency: str = "VND") -> float:
        """Số tiền mặt khả dụng của một ví tiền tệ."""

    def get_position(self, symbol: str) -> dict | None:
        """Vị thế đang nắm giữ của một mã, ``None`` nếu chưa có."""

    def list_positions(self) -> list[dict]:
        """Toàn bộ vị thế đang mở."""

    def buy(self, symbol: str, quantity: float, price: float, asset_class: str = "vn_stock") -> OrderResult:
        """Mua vào."""

    def sell(self, symbol: str, quantity: float, price: float, asset_class: str = "vn_stock") -> OrderResult:
        """Bán ra."""

    def portfolio_value(self, price_lookup, usdt_vnd: float | None = None) -> float:
        """Tổng giá trị danh mục quy về VND; ``price_lookup(symbol) -> float | None``."""
