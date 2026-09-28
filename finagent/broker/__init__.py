"""Chọn broker theo cấu hình.

Toàn bộ hệ thống lấy broker qua :func:`get_broker` thay vì tự khởi tạo, nhờ vậy
đổi từ mô phỏng sang giao dịch thật chỉ cần sửa ``FINAGENT_TRADING_MODE`` trong
``.env`` — không phải sửa chỗ nào trong mã nghiệp vụ.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from finagent.broker.base import Broker, OrderResult

logger = logging.getLogger(__name__)

__all__ = ["Broker", "OrderResult", "get_broker", "reset_broker", "describe_broker"]


@lru_cache(maxsize=1)
def _build_broker():
    """Khởi tạo broker một lần cho mỗi tiến trình (tạo kết nối khá tốn kém)."""
    from finagent.config import settings

    mode = settings.trading_mode.lower()

    if mode == "live":
        from finagent.broker.binance import BinanceBroker

        logger.warning("Chế độ giao dịch: THẬT (Binance, testnet=%s)", settings.binance_testnet)
        return BinanceBroker.from_settings()

    if mode != "paper":
        raise ValueError(
            f"FINAGENT_TRADING_MODE không hợp lệ: {mode!r} (chỉ nhận 'paper' hoặc 'live')."
        )

    from finagent.broker.paper import PaperBroker

    logger.info("Chế độ giao dịch: MÔ PHỎNG (không dùng tiền thật)")
    return PaperBroker()


def get_broker() -> Broker:
    """Broker đang dùng, theo ``FINAGENT_TRADING_MODE``."""
    return _build_broker()


def reset_broker() -> None:
    """Xoá broker đã lưu đệm — dùng khi cấu hình đổi lúc đang chạy, hoặc trong test."""
    _build_broker.cache_clear()


def describe_broker() -> dict:
    """Mô tả ngắn broker đang dùng, để người dùng biết mình đang ở chế độ nào."""
    from finagent.config import settings

    if settings.trading_mode.lower() == "live":
        return {
            "mode": "live",
            "venue": "Binance Testnet (tiền giả)" if settings.binance_testnet else "Binance (TIỀN THẬT)",
            "endpoint": "https://testnet.binance.vision" if settings.binance_testnet
                        else "https://api.binance.com",
            "real_money": not settings.binance_testnet,
        }
    return {
        "mode": "paper",
        "venue": "Mô phỏng cục bộ (SQLite)",
        "endpoint": "không kết nối ra ngoài",
        "real_money": False,
    }
