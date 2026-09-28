"""Thu thập giá chứng khoán Việt Nam từ các API công khai.

Hai nguồn, đều miễn phí và không cần API key:

1. **VNDirect dchart** — trả về chuỗi OHLCV theo ngày, là nguồn chính vì có
   lịch sử để tính biến động.
2. **SSI iBoard** — trả về giá tham chiếu/trần/sàn, dùng làm nguồn dự phòng.

Lưu ý quan trọng về đơn vị: API dchart trả giá theo **nghìn đồng**, phải nhân
1000 mới ra VND (đã đối chiếu chéo: dchart 64.7 ↔ SSI refPrice 64.700).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from finagent.collectors.base import PricePoint

logger = logging.getLogger(__name__)

#: API dchart của VNDirect trả giá theo nghìn đồng.
DCHART_SCALE = 1000.0
DCHART_URL = "https://dchart-api.vndirect.com.vn/dchart/history"
SSI_URL = "https://iboard-query.ssi.com.vn/stock/{symbol}"

REQUEST_TIMEOUT = 20

#: UA giả trình duyệt là bắt buộc: máy chủ dchart chặn UA ``python-requests`` (403).
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

#: dchart trả 406 nếu gửi ``Accept: application/json`` — chỉ được nhận ``*/*``.
DCHART_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
SSI_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json"}


def _fetch_via_dchart(symbol: str, session: requests.Session) -> PricePoint:
    """Lấy chuỗi OHLCV gần nhất và suy ra giá đóng cửa mới nhất."""
    now = int(datetime.now(timezone.utc).timestamp())
    # Lùi 120 ngày để luôn có ít nhất vài phiên kể cả dịp nghỉ dài.
    start = now - 120 * 24 * 3600
    response = session.get(
        DCHART_URL,
        params={"resolution": "1D", "symbol": symbol.upper(), "from": start, "to": now},
        headers=DCHART_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("s") != "ok":
        raise RuntimeError(f"dchart báo lỗi trạng thái {payload.get('s')!r} cho {symbol}")

    closes = payload.get("c") or []
    if not closes:
        raise RuntimeError(f"dchart không có dữ liệu cho {symbol}")

    latest_close = float(closes[-1]) * DCHART_SCALE
    volumes = payload.get("v") or []

    change_pct = None
    if len(closes) > 1 and float(closes[-2]):
        previous = float(closes[-2])
        change_pct = round((float(closes[-1]) - previous) / previous * 100, 4)

    return PricePoint(
        symbol=symbol.upper(),
        asset_class="vn_stock",
        price=latest_close,
        currency="VND",
        change_pct_24h=change_pct,
        volume=float(volumes[-1]) if volumes and volumes[-1] else None,
        source="vndirect_dchart",
    )


def _fetch_via_ssi(symbol: str, session: requests.Session) -> PricePoint:
    """Dự phòng: lấy giá tham chiếu từ SSI iBoard (đã là VND)."""
    response = session.get(SSI_URL.format(symbol=symbol.upper()), headers=SSI_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json().get("data") or {}
    reference = data.get("refPrice") or data.get("priorClosePrice")
    if not reference:
        raise RuntimeError(f"SSI không trả về giá tham chiếu cho {symbol}")

    return PricePoint(
        symbol=symbol.upper(),
        asset_class="vn_stock",
        price=float(reference),
        currency="VND",
        change_pct_24h=None,  # iBoard không kèm % thay đổi
        volume=None,
        source="ssi_iboard",
    )


def fetch_price(symbol: str, session: requests.Session | None = None) -> PricePoint:
    """Lấy giá một mã cổ phiếu, tự động chuyển nguồn khi nguồn chính lỗi."""
    http = session or requests
    errors: list[str] = []

    try:
        return _fetch_via_dchart(symbol, http)
    except Exception as exc:  # noqa: BLE001 - chuyển nguồn dự phòng là chủ đích
        errors.append(f"dchart: {exc}")
        logger.debug("dchart thất bại cho %s: %s", symbol, exc)

    try:
        return _fetch_via_ssi(symbol, http)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ssi: {exc}")

    raise RuntimeError(f"Không lấy được giá {symbol} — " + "; ".join(errors))


def collect(symbols: list[str], worker: str = "unknown") -> tuple[list[dict], list[str]]:
    """Lấy giá cho nhiều mã; một mã lỗi không làm hỏng cả lượt."""
    items: list[dict] = []
    errors: list[str] = []
    with requests.Session() as session:
        for symbol in symbols:
            try:
                items.append(fetch_price(symbol, session=session).to_dict())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Không lấy được giá VN %s: %s", symbol, exc)
                errors.append(f"{symbol}: {exc}")
    return items, errors


def fetch_history(symbol: str, days: int = 120, session: requests.Session | None = None) -> list[dict]:
    """Lấy chuỗi OHLCV để vẽ biểu đồ hoặc tính chỉ báo kỹ thuật."""
    http = session or requests
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - days * 24 * 3600
    response = http.get(
        DCHART_URL,
        params={"resolution": "1D", "symbol": symbol.upper(), "from": start, "to": now},
        headers=DCHART_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    timestamps = payload.get("t") or []
    return [
        {
            "date": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
            "open": payload["o"][i] * DCHART_SCALE,
            "high": payload["h"][i] * DCHART_SCALE,
            "low": payload["l"][i] * DCHART_SCALE,
            "close": payload["c"][i] * DCHART_SCALE,
            "volume": payload["v"][i],
        }
        for i, ts in enumerate(timestamps)
    ]
