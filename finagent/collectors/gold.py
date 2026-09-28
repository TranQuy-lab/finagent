"""Thu thập giá vàng trong nước và thế giới.

Nguồn: ``vang.today`` — API công khai, miễn phí, tổng hợp giá SJC, DOJI, PNJ,
Bảo Tín Minh Châu và giá vàng thế giới XAU/USD.

Đề tài yêu cầu theo dõi giá vàng nên bộ thu thập này chạy song song với crypto
và chứng khoán; dữ liệu được đưa vào cùng một kho để tầng suy luận dùng chung.
"""

from __future__ import annotations

import logging

import requests

from finagent.collectors.base import PricePoint

logger = logging.getLogger(__name__)

GOLD_API_URL = "https://vang.today/api/prices"
REQUEST_TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) FinAgent/1.0", "Accept": "application/json"}

#: Các thương hiệu vàng tiêu biểu cần theo dõi (khoá trong phản hồi của API).
#: Đây là vàng miếng/ nhẫn 9999 bán ra tại các thương hiệu lớn.
DEFAULT_GOLD_KEYS = ["SJL1L10", "DOJINHTV", "PQHNVM", "BT9999NTT", "XAUUSD"]

#: Tên hiển thị thân thiện cho từng khoá.
GOLD_LABELS = {
    "SJL1L10": "SJC 9999",
    "SJ9999": "SJC Ring",
    "DOJINHTV": "DOJI",
    "DOHNL": "DOJI Hà Nội",
    "DOHCML": "DOJI HCM",
    "PQHNVM": "PNJ Hà Nội",
    "PQHN24NTT": "PNJ 24K",
    "BT9999NTT": "Bảo Tín 9999",
    "BTSJC": "Bảo Tín SJC",
    "VIETTINMSJC": "Viettin SJC",
    "VNGSJC": "VN Gold SJC",
    "XAUUSD": "Vàng thế giới XAU/USD",
}


def fetch_all(session: requests.Session | None = None) -> dict:
    """Lấy toàn bộ bảng giá vàng thô từ API."""
    http = session or requests
    response = http.get(GOLD_API_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError("vang.today trả về success=false")
    return payload


def collect(
    keys: list[str] | None = None,
    worker: str = "unknown",
) -> tuple[list[dict], list[str]]:
    """Lấy giá vàng cho các thương hiệu được chọn.

    Giá sử dụng là giá **bán ra** — mức giá nhà đầu tư thực sự phải trả khi mua.
    """
    wanted = keys or DEFAULT_GOLD_KEYS
    errors: list[str] = []
    items: list[dict] = []

    with requests.Session() as session:
        try:
            payload = fetch_all(session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không lấy được bảng giá vàng: %s", exc)
            return [], [f"vang.today: {exc}"]

    prices = payload.get("prices") or {}
    for key in wanted:
        entry = prices.get(key)
        if not entry:
            errors.append(f"{key}: không có trong phản hồi")
            continue

        sell_price = entry.get("sell") or entry.get("buy")
        if not sell_price:
            errors.append(f"{key}: giá rỗng")
            continue

        is_world = key == "XAUUSD"
        items.append(
            PricePoint(
                symbol=key,
                asset_class="gold",
                price=float(sell_price),
                currency="USD" if is_world else "VND",
                # Vàng trong nước đổi giá trong ngày; dùng mức chênh mua-bán làm tín hiệu.
                change_pct_24h=_spread_pct(entry),
                volume=None,
                source="vang.today",
            ).to_dict()
        )

    return items, errors


def _spread_pct(entry: dict) -> float | None:
    """Chênh lệch giá mua-bán theo % — chỉ số thanh khoản hữu ích của thị trường vàng."""
    buy = entry.get("buy")
    sell = entry.get("sell")
    if not buy or not sell or not buy:
        return None
    try:
        return round((float(sell) - float(buy)) / float(buy) * 100, 4)
    except (TypeError, ZeroDivisionError):
        return None


def label_for(key: str) -> str:
    """Tên hiển thị của một thương hiệu vàng."""
    return GOLD_LABELS.get(key, key)
