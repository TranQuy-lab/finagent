"""Tổng hợp thông tin vi mô — những tín hiệu đọc được từ sổ lệnh và dòng tiền.

Phần còn thiếu trước đây: hệ thống chỉ biết **giá đã khớp**, tức là thông tin đã
cũ. Giá chỉ cho biết chuyện gì vừa xảy ra, không cho biết **áp lực đang hình
thành**. Module này đọc những thứ đi trước giá:

- **Sổ lệnh** — bên mua hay bên bán đang dày hơn.
- **Khối ngoại** — nhà đầu tư nước ngoài đang mua ròng hay bán ròng.
- **Dòng tiền chủ động** — lệnh mua khớp vào giá bán (mua chủ động) so với lệnh
  bán khớp vào giá mua (bán chủ động).
- **Định vị thị trường crypto** — phí funding, open interest, tỷ lệ long/short.

Khác biệt quan trọng so với giá: nếu giá tăng nhưng khối ngoại bán ròng và bán chủ
động áp đảo, đà tăng đó đang được duy trì bởi dòng tiền yếu và dễ đảo chiều.

Mọi hàm ở đây đều **không được ném lỗi ra ngoài**. Đây là dữ liệu bổ trợ: thiếu nó
thì phân tích kém sắc hơn, chứ không được làm hỏng cả lượt phân tích.

## Nguồn dữ liệu

============  ========================================  ==================
Nhóm          Nguồn                                     Nội dung
============  ========================================  ==================
Crypto        ``api.binance.com/api/v3/depth``          Sổ lệnh
Crypto        ``fapi.binance.com``                      Funding, OI, long/short
Chứng khoán   ``iboard-query.ssi.com.vn/stock/<mã>``    Sổ lệnh, khối ngoại, dòng tiền
============  ========================================  ==================

Vàng không có nguồn dữ liệu vi mô nào — trong nước chỉ niêm yết giá, không có sổ
lệnh công khai.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import requests

from finagent.collectors.base import utcnow_iso

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"
SSI_BASE = "https://iboard-query.ssi.com.vn"

#: Dải giá dùng để đo thanh khoản quanh giá khớp (xem ``_fetch_book_crypto``).
#: 0,5% là mức đo được độ lệch chuẩn 2,2% khi gọi liên tiếp — đủ ổn định để dùng.
#: Dải hẹp hơn (±0,05%) lại nhiễu trở lại vì quá ít lệnh trong dải.
NEAR_TOUCH_BAND = 0.005

#: Ngưỡng mất cân bằng đáng chú ý. Khác nhau theo nhóm tài sản vì cách đo khác
#: nhau: dải hẹp cho ra vài phần trăm, còn sổ lệnh 3 mức của SSI cho hàng chục.
CRYPTO_IMBALANCE_THRESHOLD = 0.10
VN_IMBALANCE_THRESHOLD = 0.15

#: SSI chặn yêu cầu không có User-Agent trình duyệt.
SSI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class Microstructure:
    """Ảnh chụp áp lực thị trường tại một thời điểm.

    Mọi trường đều có thể ``None``: nguồn nào không cung cấp thì để trống, thay vì
    điền số 0 gây hiểu nhầm là "đã đo được và bằng không".
    """

    symbol: str
    asset_class: str
    captured_at: str = field(default_factory=utcnow_iso)

    # --- Sổ lệnh ---
    #: Độ lệch sổ lệnh trong khoảng [-1, 1]: dương là bên mua dày hơn.
    book_imbalance: float | None = None
    bid_volume: float | None = None
    ask_volume: float | None = None
    spread_pct: float | None = None

    # --- Khối ngoại (chứng khoán Việt Nam) ---
    foreign_buy_value: float | None = None
    foreign_sell_value: float | None = None
    #: Mua ròng trừ bán ròng. Dương là mua ròng.
    foreign_net_value: float | None = None
    #: Room còn lại cho nhà đầu tư nước ngoài.
    foreign_room: float | None = None

    # --- Dòng tiền chủ động (chứng khoán Việt Nam) ---
    active_buy_volume: float | None = None
    active_sell_volume: float | None = None
    #: Tỷ lệ mua chủ động trên tổng, khoảng [0, 1]. Trên 0,5 là bên mua chủ động.
    active_buy_ratio: float | None = None

    # --- Định vị thị trường (crypto) ---
    #: Phí funding: dương nghĩa là phe long đang trả tiền cho phe short.
    funding_rate: float | None = None
    open_interest: float | None = None
    long_short_ratio: float | None = None
    #: Tỷ lệ khối lượng lệnh taker mua trên bán.
    taker_buy_sell_ratio: float | None = None

    # --- Tham chiếu giá ---
    ceiling: float | None = None
    floor: float | None = None
    average_price: float | None = None
    reference_price: float | None = None

    #: Nguồn nào đã trả lời được, để biết vì sao thiếu trường nào.
    sources_ok: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def available(self) -> bool:
        """Có ít nhất một tín hiệu dùng được hay không."""
        return bool(self.sources_ok)

    def signals(self) -> list[str]:
        """Các tín hiệu đọc được, dạng câu ngắn để đưa vào ngữ cảnh cho LLM."""
        out: list[str] = []

        # Ngưỡng khác nhau theo nhóm tài sản vì cách đo khác nhau: crypto đo trong
        # dải ±0,5% quanh giá khớp nên mất cân bằng chỉ vài phần trăm, còn SSI chỉ
        # cho 3 mức nên con số lớn hơn nhiều. Dùng chung một ngưỡng sẽ hoặc là bỏ
        # sót tín hiệu crypto, hoặc là báo động giả liên tục với chứng khoán.
        threshold = (
            CRYPTO_IMBALANCE_THRESHOLD if self.asset_class == "crypto"
            else VN_IMBALANCE_THRESHOLD
        )
        if self.book_imbalance is not None and abs(self.book_imbalance) >= threshold:
            side = "mua" if self.book_imbalance > 0 else "bán"
            if self.bid_volume is not None and self.ask_volume is not None:
                # Khối lượng crypto nhỏ (BTC) còn chứng khoán lớn (cổ phiếu), nên
                # định dạng theo độ lớn thay vì cố định một kiểu.
                def _vol(value: float) -> str:
                    return f"{value:,.0f}" if value >= 100 else f"{value:,.2f}"
                out.append(
                    f"Thanh khoản nghiêng về bên {side} "
                    f"(lệch {self.book_imbalance:+.1%}, "
                    f"chào mua {_vol(self.bid_volume)} / chào bán {_vol(self.ask_volume)})"
                )
            else:
                out.append(f"Thanh khoản nghiêng về bên {side} (lệch {self.book_imbalance:+.1%})")

        if self.foreign_net_value is not None:
            ty = self.foreign_net_value / 1e9
            action = "mua ròng" if self.foreign_net_value > 0 else "bán ròng"
            out.append(f"Khối ngoại {action} {abs(ty):,.1f} tỷ đồng")
            if self.foreign_room is not None:
                out.append(f"Room ngoại còn lại {self.foreign_room:,.0f} cổ phiếu")

        if self.active_buy_ratio is not None:
            side = "mua" if self.active_buy_ratio > 0.5 else "bán"
            out.append(
                f"Dòng tiền chủ động nghiêng về bên {side} "
                f"(mua chủ động {self.active_buy_ratio:.0%} khối lượng khớp)"
            )

        if self.funding_rate is not None:
            # Phí funding dương đáng kể nghĩa là phe long đang phải trả tiền —
            # thị trường nghiêng về long và dễ bị siết.
            if abs(self.funding_rate) >= 0.0001:
                side = "long đang trả phí cho short" if self.funding_rate > 0 else "short đang trả phí cho long"
                out.append(f"Phí funding {self.funding_rate:+.4%} — {side}")

        if self.long_short_ratio is not None and abs(self.long_short_ratio - 1) >= 0.15:
            side = "long" if self.long_short_ratio > 1 else "short"
            out.append(f"Tài khoản lớn nghiêng về {side} (tỷ lệ {self.long_short_ratio:.2f})")

        if self.spread_pct is not None and self.spread_pct > 0:
            out.append(f"Chênh lệch mua-bán {self.spread_pct:.3%}")

        if self.ceiling and self.reference_price:
            room_up = (self.ceiling - self.reference_price) / self.reference_price
            out.append(f"Còn {room_up:.1%} tới giá trần")

        return out


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------

def _fetch_book_crypto(symbol: str, limit: int, result: Microstructure) -> None:
    """Sổ lệnh Binance → thanh khoản hai bên trong một dải giá quanh giá khớp.

    **Vì sao không dùng "N mức đầu tiên".** Cách đo hiển nhiên là cộng khối lượng
    của N mức gần giá khớp nhất. Đo thực tế cho thấy cách đó là **nhiễu thuần**:
    gọi liên tiếp 8 lần trong vài giây, độ lệch chuẩn của tỉ lệ mất cân bằng là
    31% ở 5 mức, 45% ở 50 mức, 38% ở 100 mức — tín hiệu **đảo dấu** giữa các lần
    gọi. Đưa số đó cho mô hình còn tệ hơn là không đưa gì, vì nó tạo ra tự tin giả.

    Lý do: "N mức" là thước đo tuỳ tiện. Khi giá dịch chuyển, một mức có thể chứa
    khối lượng rất khác nhau, và các lệnh lớn đặt rồi huỷ ngay (spoofing) nằm
    chính ở những mức đầu.

    **Cách đo thay thế.** Cộng khối lượng chào mua trong dải ``±0,5%`` dưới giá
    khớp và chào bán trong ``±0,5%`` trên giá khớp, trên sổ lệnh sâu (1000 mức).
    Đây là vùng giá giao dịch thật sự diễn ra. Đo lại 8 lần liên tiếp: độ lệch
    chuẩn còn **2,2%** — ổn định hơn hai chục lần.

    Ngưỡng đọc tín hiệu cũng phải khác: vì dải hẹp nên mất cân bằng thường chỉ vài
    phần trăm, chứ không phải hàng chục phần trăm như cách đo nông.
    """
    response = requests.get(
        f"{SPOT_BASE}/api/v3/depth",
        params={"symbol": symbol, "limit": max(limit, 1000)},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    bids = [(float(price), float(qty)) for price, qty in data.get("bids", [])]
    asks = [(float(price), float(qty)) for price, qty in data.get("asks", [])]
    if not bids or not asks:
        return

    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    if not mid:
        return

    lower, upper = mid * (1 - NEAR_TOUCH_BAND), mid * (1 + NEAR_TOUCH_BAND)
    bid_volume = sum(qty for price, qty in bids if price >= lower)
    ask_volume = sum(qty for price, qty in asks if price <= upper)
    total = bid_volume + ask_volume

    result.bid_volume = bid_volume
    result.ask_volume = ask_volume
    result.book_imbalance = (bid_volume - ask_volume) / total if total else None
    result.spread_pct = (best_ask - best_bid) / mid
    result.sources_ok.append("binance_spot_depth")


def _fetch_positioning_crypto(symbol: str, result: Microstructure) -> None:
    """Phí funding, open interest, tỷ lệ long/short từ thị trường hợp đồng.

    Chỉ có ý nghĩa với hợp đồng tương lai; mã không có hợp đồng sẽ trả 404 và ta
    bỏ qua chứ không coi là lỗi.
    """
    for name, path, params, apply in (
        ("funding", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1},
         lambda rows: setattr(result, "funding_rate", float(rows[0]["fundingRate"]))),
        ("open_interest", "/fapi/v1/openInterest", {"symbol": symbol},
         lambda data: setattr(result, "open_interest", float(data["openInterest"]))),
        ("long_short", "/futures/data/topLongShortAccountRatio",
         {"symbol": symbol, "period": "1h", "limit": 1},
         lambda rows: setattr(result, "long_short_ratio", float(rows[0]["longShortRatio"]))),
        ("taker", "/futures/data/takerlongshortRatio",
         {"symbol": symbol, "period": "1h", "limit": 1},
         lambda rows: setattr(result, "taker_buy_sell_ratio", float(rows[0]["buySellRatio"]))),
    ):
        try:
            response = requests.get(f"{FUTURES_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                continue        # mã không có hợp đồng tương lai
            response.raise_for_status()
            apply(response.json())
            result.sources_ok.append(f"binance_futures_{name}")
        except Exception as exc:  # noqa: BLE001 - bổ trợ, không được làm hỏng
            logger.debug("Binance futures %s cho %s thất bại: %s", name, symbol, exc)
            result.errors.append(f"{name}: {exc}")


# ---------------------------------------------------------------------------
# Chứng khoán Việt Nam
# ---------------------------------------------------------------------------

def _ssi_stock(symbol: str) -> dict:
    """Dữ liệu chi tiết một mã từ SSI (kèm sổ lệnh, khối ngoại, dòng tiền)."""
    response = requests.get(
        f"{SSI_BASE}/stock/{symbol.upper()}", headers=SSI_HEADERS, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "SUCCESS":
        raise RuntimeError(f"SSI trả về {payload.get('code')}: {payload.get('message')}")
    return payload.get("data") or {}


def _fetch_book_vn(data: dict, result: Microstructure, levels: int = 3) -> None:
    """Sổ lệnh SSI (3 mức) → độ lệch khối lượng chào mua và chào bán."""
    bid_volume = 0.0
    ask_volume = 0.0
    best_bid = best_ask = None

    for level in range(1, levels + 1):
        bid = data.get(f"best{level}Bid")
        bid_vol = data.get(f"best{level}BidVol")
        ask = data.get(f"best{level}Offer")
        ask_vol = data.get(f"best{level}OfferVol")

        if bid and bid_vol:
            bid_volume += float(bid_vol)
            best_bid = best_bid or float(bid)
        if ask and ask_vol:
            ask_volume += float(ask_vol)
            best_ask = best_ask or float(ask)

    if bid_volume or ask_volume:
        total = bid_volume + ask_volume
        result.bid_volume = bid_volume
        result.ask_volume = ask_volume
        result.book_imbalance = (bid_volume - ask_volume) / total if total else None

    if best_bid and best_ask:
        mid = (best_bid + best_ask) / 2
        result.spread_pct = (best_ask - best_bid) / mid if mid else None


def _fetch_foreign_vn(data: dict, result: Microstructure) -> None:
    """Khối ngoại mua/bán và room còn lại."""
    buy = data.get("buyForeignValue")
    sell = data.get("sellForeignValue")
    room = data.get("remainForeignQtty")

    if buy is not None:
        result.foreign_buy_value = float(buy)
    if sell is not None:
        result.foreign_sell_value = float(sell)
    if buy is not None and sell is not None:
        result.foreign_net_value = float(buy) - float(sell)
    if room is not None:
        result.foreign_room = float(room)


def _fetch_flow_vn(data: dict, result: Microstructure) -> None:
    """Dòng tiền chủ động: mua khớp vào giá bán so với bán khớp vào giá mua.

    SSI gọi là ``stockBUVol`` (buy-up, mua chủ động) và ``stockSDVol`` (sell-down,
    bán chủ động). Tỷ lệ này cho biết ai đang sốt ruột hơn.
    """
    active_buy = data.get("stockBUVol")
    active_sell = data.get("stockSDVol")

    if active_buy is not None:
        result.active_buy_volume = float(active_buy)
    if active_sell is not None:
        result.active_sell_volume = float(active_sell)
    if active_buy is not None and active_sell is not None:
        total = float(active_buy) + float(active_sell)
        result.active_buy_ratio = float(active_buy) / total if total else None


def _fetch_reference_vn(data: dict, result: Microstructure) -> None:
    """Giá trần, giá sàn, giá tham chiếu và giá bình quân."""
    for key, attribute in (
        ("ceiling", "ceiling"), ("floor", "floor"),
        ("avgPrice", "average_price"), ("refPrice", "reference_price"),
    ):
        value = data.get(key)
        if value:
            setattr(result, attribute, float(value))


# ---------------------------------------------------------------------------
# Điểm vào
# ---------------------------------------------------------------------------

def fetch_microstructure(symbol: str, asset_class: str, depth_limit: int = 50) -> Microstructure:
    """Gom mọi tín hiệu vi mô có được cho một mã.

    Không bao giờ ném lỗi: nguồn nào hỏng thì ghi vào ``errors`` và đi tiếp. Một
    nguồn sập không được làm hỏng cả lượt phân tích.
    """
    result = Microstructure(symbol=symbol.upper(), asset_class=asset_class)

    if asset_class == "crypto":
        for name, fetch in (
            ("thanh khoản", lambda: _fetch_book_crypto(symbol.upper(), depth_limit, result)),
            ("định vị", lambda: _fetch_positioning_crypto(symbol.upper(), result)),
        ):
            try:
                fetch()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vi mô crypto %s cho %s thất bại: %s", name, symbol, exc)
                result.errors.append(f"{name}: {exc}")

    elif asset_class == "vn_stock":
        try:
            data = _ssi_stock(symbol)
            _fetch_book_vn(data, result)
            _fetch_foreign_vn(data, result)
            _fetch_flow_vn(data, result)
            _fetch_reference_vn(data, result)
            result.sources_ok.append("ssi_iboard")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vi mô SSI cho %s thất bại: %s", symbol, exc)
            result.errors.append(f"ssi: {exc}")

    # Vàng: không có nguồn vi mô nào, trả về ảnh chụp rỗng một cách êm ái.
    return result


def format_microstructure(micro: Microstructure) -> str:
    """Định dạng ảnh chụp vi mô thành văn bản cho các tác nhân LLM đọc."""
    if not micro.available:
        ly_do = "; ".join(micro.errors) if micro.errors else "không có nguồn dữ liệu vi mô"
        return f"KHÔNG CÓ DỮ LIỆU VI MÔ cho {micro.symbol} ({ly_do})."

    lines = [f"# Dữ liệu vi mô — {micro.symbol} (lúc {micro.captured_at})", ""]
    signals = micro.signals()

    if signals:
        lines.append("## Tín hiệu đọc được")
        lines += [f"- {signal}" for signal in signals]
    else:
        lines.append("## Không có tín hiệu nào vượt ngưỡng đáng chú ý")

    lines += ["", "## Số liệu thô", ""]
    for label, value, unit in (
        ("Mất cân bằng thanh khoản", micro.book_imbalance, ""),
        ("Khối lượng chào mua trong dải", micro.bid_volume, ""),
        ("Khối lượng chào bán trong dải", micro.ask_volume, ""),
        ("Chênh lệch mua-bán", micro.spread_pct, ""),
        ("Khối ngoại mua", micro.foreign_buy_value, "VND"),
        ("Khối ngoại bán", micro.foreign_sell_value, "VND"),
        ("Khối ngoại ròng", micro.foreign_net_value, "VND"),
        ("Room ngoại còn", micro.foreign_room, "cổ phiếu"),
        ("Mua chủ động", micro.active_buy_volume, ""),
        ("Bán chủ động", micro.active_sell_volume, ""),
        ("Tỷ lệ mua chủ động", micro.active_buy_ratio, ""),
        ("Phí funding", micro.funding_rate, ""),
        ("Open interest", micro.open_interest, ""),
        ("Tỷ lệ long/short", micro.long_short_ratio, ""),
        ("Tỷ lệ taker mua/bán", micro.taker_buy_sell_ratio, ""),
        ("Giá trần", micro.ceiling, "VND"),
        ("Giá sàn", micro.floor, "VND"),
        ("Giá bình quân", micro.average_price, "VND"),
        ("Giá tham chiếu", micro.reference_price, "VND"),
    ):
        if value is None:
            continue
        if isinstance(value, float):
            lines.append(f"- {label}: {value:,.4f} {unit}".rstrip())
        else:
            lines.append(f"- {label}: {value} {unit}".rstrip())

    lines += ["", f"Nguồn trả lời được: {', '.join(micro.sources_ok)}"]
    if micro.errors:
        lines.append(f"Nguồn lỗi: {'; '.join(micro.errors)}")

    lines += [
        "",
        "Lưu ý: đây là dữ liệu bổ trợ đọc từ sổ lệnh và dòng tiền, KHÔNG phải giá đã "
        "khớp. Dùng để đánh giá áp lực đang hình thành, không dùng làm giá giao dịch.",
    ]
    return "\n".join(lines)
