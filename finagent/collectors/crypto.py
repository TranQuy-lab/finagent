"""Thu thập giá crypto từ API công khai của Binance.

Không cần API key, không cần đăng ký — phù hợp để các máy con chạy miễn phí.
"""

from __future__ import annotations

import logging

import requests

from finagent.collectors.base import PricePoint

logger = logging.getLogger(__name__)

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
REQUEST_TIMEOUT = 15


def fetch_price(symbol: str, session: requests.Session | None = None) -> PricePoint:
    """Lấy giá và mức thay đổi 24h của một cặp giao dịch, ví dụ ``BTCUSDT``."""
    http = session or requests
    response = http.get(
        BINANCE_TICKER_URL,
        params={"symbol": symbol.upper()},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Binance trả về HTTP {response.status_code} cho {symbol}: {response.text[:200]}"
        )

    payload = response.json()
    price = float(payload["lastPrice"])
    change_pct = float(payload.get("priceChangePercent", 0.0))

    return PricePoint(
        symbol=symbol.upper(),
        asset_class="crypto",
        price=price,
        currency="USDT",
        change_pct_24h=round(change_pct, 4),
        volume=float(payload.get("quoteVolume", 0.0)) or None,
        source="binance",
    )


def collect(symbols: list[str], worker: str = "unknown") -> tuple[list[dict], list[str]]:
    """Lấy giá cho nhiều cặp; trả về ``(danh sách giá, danh sách lỗi)``.

    Một mã lỗi không làm hỏng cả lượt thu thập — các mã còn lại vẫn được ghi nhận.
    """
    items: list[dict] = []
    errors: list[str] = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": "FinAgent/1.0"})
        for symbol in symbols:
            try:
                items.append(fetch_price(symbol, session=session).to_dict())
            except (requests.RequestException, KeyError, ValueError, RuntimeError) as exc:
                logger.warning("Không lấy được giá %s: %s", symbol, exc)
                errors.append(f"{symbol}: {exc}")
    return items, errors
