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
    """Dự phòng: lấy giá khớp gần nhất từ SSI iBoard (đã là VND).

    **Thứ tự ưu tiên trường rất quan trọng.** iBoard trả về nhiều trường giá khác
    nhau, và chúng không cùng ý nghĩa:

    ======================  ====================================================
    Trường                  Ý nghĩa
    ======================  ====================================================
    ``matchedPrice``        Giá khớp gần nhất — đây mới là "giá hiện tại"
    ``avgPrice``            Giá khớp bình quân cả phiên
    ``refPrice``            Giá tham chiếu, tức **giá đóng cửa hôm trước**
    ``priorClosePrice``     Giá đóng cửa hôm trước
    ======================  ====================================================

    Bản đầu tiên của hàm này đọc ``refPrice``, tức là ghi giá đóng cửa hôm qua như
    thể là giá hôm nay — sai tới mức bằng cả biên độ một phiên. Lỗi chỉ lộ ra khi
    đối chiếu chéo với VNDirect: hai nguồn lệch nhau ~2,2% một cách có hệ thống
    trên gần như mọi mã, trong khi lệch thật chỉ khoảng 0,1–0,2%.
    """
    response = session.get(SSI_URL.format(symbol=symbol.upper()), headers=SSI_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json().get("data") or {}

    price = data.get("matchedPrice") or data.get("avgPrice")
    if not price:
        # Ngoài giờ giao dịch chưa có giá khớp nào thì đành dùng giá tham chiếu,
        # nhưng phải nói rõ nguồn gốc qua trường ``source``.
        price = data.get("refPrice") or data.get("priorClosePrice")
        if not price:
            raise RuntimeError(f"SSI không trả về giá nào cho {symbol}")
        used_reference = True
    else:
        used_reference = False

    return PricePoint(
        symbol=symbol.upper(),
        asset_class="vn_stock",
        price=float(price),
        currency="VND",
        change_pct_24h=(
            float(data["priceChangePercent"]) if data.get("priceChangePercent") is not None else None
        ),
        volume=float(data["nmTotalTradedQty"]) if data.get("nmTotalTradedQty") else None,
        source="ssi_iboard_refprice" if used_reference else "ssi_iboard",
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


#: Mức lệch tối đa được coi là hai nguồn khớp nhau. Chứng khoán Việt Nam niêm yết
#: theo bước giá 10–50 đồng, nên lệch vài chục đồng là bình thường (một nguồn lấy
#: giá khớp gần nhất, nguồn kia lấy giá tham chiếu). Lệch quá 0,5% mới đáng ngờ.
PRICE_AGREEMENT_TOLERANCE = 0.005


def cross_check_price(symbol: str, session: requests.Session | None = None) -> dict:
    """Lấy giá từ **cả hai** nguồn rồi đối chiếu, thay vì tin nguồn đầu tiên trả lời.

    ``fetch_price`` dừng ngay khi nguồn chính thành công — nhanh, nhưng không có gì
    bảo đảm con số đó đúng. Hàm này hỏi cả VNDirect lẫn SSI và so với nhau:

    - Hai nguồn khớp trong dung sai → độ tin cậy cao.
    - Hai nguồn lệch nhau → nghi ngờ có nguồn trả dữ liệu cũ hoặc sai mã.

    Đây là phần cốt lõi của tổng hợp thông tin: một con số từ một nguồn là dữ liệu,
    cùng con số từ hai nguồn khớp nhau mới là thông tin.
    """
    http = session or requests
    prices: dict[str, float] = {}
    errors: dict[str, str] = {}

    for name, fetch in (("vndirect_dchart", _fetch_via_dchart), ("ssi_iboard", _fetch_via_ssi)):
        try:
            prices[name] = float(fetch(symbol, http).price)
        except Exception as exc:  # noqa: BLE001 - một nguồn hỏng không chặn nguồn kia
            errors[name] = str(exc)
            logger.debug("Đối chiếu %s: nguồn %s thất bại: %s", symbol, name, exc)

    if not prices:
        return {
            "symbol": symbol, "agreed": False, "confidence": "none",
            "prices": {}, "errors": errors,
            "note": f"Không nguồn nào trả lời được cho {symbol}.",
        }

    if len(prices) == 1:
        only_source, only_price = next(iter(prices.items()))
        return {
            "symbol": symbol, "agreed": True, "confidence": "low",
            "prices": prices, "errors": errors, "consensus_price": only_price,
            "note": (
                f"Chỉ một nguồn trả lời ({only_source} = {only_price:,.0f} VND), "
                "chưa đối chiếu được với nguồn nào khác."
            ),
        }

    low, high = min(prices.values()), max(prices.values())
    spread = (high - low) / low if low else 0.0
    agreed = spread <= PRICE_AGREEMENT_TOLERANCE

    if agreed:
        note = (
            f"Hai nguồn khớp nhau: "
            + ", ".join(f"{name} {value:,.0f}" for name, value in sorted(prices.items()))
            + f" VND (lệch {spread:.3%})."
        )
    else:
        note = (
            "⚠️ HAI NGUỒN LỆCH NHAU: "
            + ", ".join(f"{name} {value:,.0f}" for name, value in sorted(prices.items()))
            + f" VND (lệch {spread:.2%}). Nên thận trọng: có thể một nguồn đang trả "
            "dữ liệu cũ, hoặc mã bị nhầm."
        )

    return {
        "symbol": symbol,
        "agreed": agreed,
        #: Độ tin cậy: hai nguồn khớp là "high", lệch nhau là "conflict".
        "confidence": "high" if agreed else "conflict",
        "prices": prices,
        "errors": errors,
        "spread": spread,
        # Khi khớp thì lấy trung bình; khi lệch thì lấy nguồn chính (dchart) vì đó
        # là nguồn vẫn dùng khi chạy bình thường — đổi hành vi lúc có tranh chấp sẽ
        # khiến kết quả khó lần lại.
        "consensus_price": (low + high) / 2 if agreed else prices.get("vndirect_dchart", low),
        "note": note,
    }


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
