"""Cầu nối dữ liệu: đăng ký vendor ``finagent`` vào TradingAgents.

TradingAgents gốc chỉ có vendor Mỹ (yfinance, alpha_vantage, SEC, FRED), nên với
chứng khoán Việt Nam và crypto USDT nó sẽ không có dữ liệu. Module này cắm thêm
một vendor đọc từ chính kho dữ liệu mà các máy con đã thu thập, giúp các tác
nhân LLM suy luận trên dữ liệu thật của đề tài.

Cách hoạt động: chèn hàm vào ``VENDOR_METHODS`` của TradingAgents, sau đó đặt
``data_vendors`` trong config trỏ về ``finagent``.
"""

from __future__ import annotations

import functools
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import requests

from finagent.collectors import vnstock as vn_collector
from finagent.storage import recent_news

logger = logging.getLogger(__name__)

VENDOR_NAME = "finagent"

#: Phiên bản TradingAgents mà các bản vá trong module này được viết cho.
#: PyPI có gói cùng tên nhưng phiên bản khác, nên phải kiểm tra để lỗi hiện ra
#: rõ ràng thay vì âm thầm hỏng.
TESTED_TRADINGAGENTS_VERSION = "0.5.1"

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

#: Mã thương hiệu vàng trong nước (khớp khoá của bộ thu thập giá vàng).
GOLD_BRAND_SYMBOLS = {
    "SJL1L10", "SJ9999", "DOJINHTV", "DOHNL", "DOHCML",
    "PQHNVM", "PQHN24NTT", "BT9999NTT", "BTSJC", "VIETTINMSJC", "VNGSJC", "XAUUSD",
}

#: Chỉ số thị trường Việt Nam. Dùng làm chỉ số chuẩn để chấm điểm alpha.
#: Không phải tài sản giao dịch được — không mã nào trong đây sinh đề xuất mua bán.
VN_INDEX_SYMBOLS = {"VNINDEX", "VN30", "HNXINDEX", "UPCOMINDEX"}

#: Các đồng tiền cơ sở được coi là crypto khi ghép với USDT/USD.
_CRYPTO_BASES = re.compile(r"^(BTC|ETH|SOL|BNB|XRP|ADA|DOGE|TON|AVAX|MATIC|LTC|LINK|DOT)")


# ---------------------------------------------------------------------------
# Nhận diện loại tài sản từ mã
# ---------------------------------------------------------------------------

def detect_asset_class(symbol: str) -> str:
    """Suy ra loại tài sản của một mã.

    ``BTCUSDT``/``ETHUSDT`` → crypto; mã thương hiệu vàng trong nước (``SJL1L10``,
    ``DOJINHTV``…) và ``XAUUSD`` → vàng; còn lại là cổ phiếu Việt Nam.
    """
    upper = symbol.upper().replace("-", "")

    if upper in VN_INDEX_SYMBOLS:
        return "vn_index"
    if upper in GOLD_BRAND_SYMBOLS or upper in ("XAUUSD", "XAU"):
        return "gold"
    if _CRYPTO_BASES.search(upper) and (upper.endswith("USDT") or upper.endswith("USD")):
        return "crypto"
    return "vn_stock"


def to_binance_symbol(symbol: str) -> str:
    """Chuẩn hoá mã crypto về dạng Binance, ví dụ ``BTC-USD`` → ``BTCUSDT``."""
    upper = symbol.upper().replace("-", "")
    if upper.endswith("USDT"):
        return upper
    if upper.endswith("USD"):
        return upper[:-3] + "USDT"
    return upper + "USDT"


def as_int(value, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    """Ép một tham số do mô hình sinh ra về số nguyên an toàn.

    Mô hình ngôn ngữ rất hay trả tham số dạng chuỗi (``"30"``) dù lược đồ khai báo
    số nguyên. Với Python, ``"30" * 3`` cho ``"303030"`` chứ không phải 90, và
    SQLite thì báo ``datatype mismatch`` — nên phải ép kiểu ở mọi chỗ nhận tham số
    từ mô hình.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def as_date(value, default: str) -> str:
    """Chuẩn hoá tham số ngày về ``YYYY-MM-DD``; trả về mặc định nếu không hợp lệ."""
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Lấy dữ liệu giá
# ---------------------------------------------------------------------------

def _crypto_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Lấy nến ngày từ Binance."""
    pair = to_binance_symbol(symbol)
    start_ms = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp() * 1000)

    response = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": pair, "interval": "1d", "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise RuntimeError(f"Binance không có nến cho {pair} trong khoảng yêu cầu")

    frame = pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume",
                 "close_time", "qav", "trades", "tbb", "tbq", "ignore"],
    )
    frame["date"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True).dt.date
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = frame[column].astype(float)
    return frame[["date", "open", "high", "low", "close", "volume"]]


def _vn_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Lấy nến ngày của cổ phiếu Việt Nam qua VNDirect dchart."""
    days = max((datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days + 5, 10)
    rows = vn_collector.fetch_history(symbol, days=days)
    if not rows:
        raise RuntimeError(f"Không có dữ liệu giá cho {symbol}")
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame[(frame["date"] >= datetime.fromisoformat(start_date).date())
                 & (frame["date"] <= datetime.fromisoformat(end_date).date())]


def daily_history(symbol: str, days: int = 400) -> list[dict]:
    """Lịch sử nến **ngày** của một mã, dạng ``[{price, volume, date}]`` cũ → mới.

    Dùng chung cho mô hình ML và cho biểu đồ. Luôn lấy trực tiếp từ nguồn thay vì
    kho nội bộ, vì kho chỉ lưu ảnh chụp giá theo chu kỳ quét (trong ngày) chứ
    không phải nến ngày.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    asset_class = detect_asset_class(symbol)

    if asset_class == "gold":
        raise ValueError(
            f"{symbol} là vàng — nguồn miễn phí chỉ có giá hiện tại, không có chuỗi nến ngày. "
            "Vàng được giám sát bằng tin tức thay vì mô hình ML."
        )

    if asset_class == "crypto":
        frame = _crypto_ohlcv(symbol, start.isoformat(), end.isoformat())
        return [
            {"date": str(row["date"]), "price": float(row["close"]), "volume": float(row["volume"])}
            for _, row in frame.sort_values("date").iterrows()
        ]

    rows = vn_collector.fetch_history(symbol, days=days)
    return [
        {"date": row["date"], "price": float(row["close"]), "volume": float(row["volume"])}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Hàm vendor — chữ ký phải khớp cách TradingAgents gọi
# ---------------------------------------------------------------------------

def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """Trả về bảng OHLCV dạng văn bản cho tác nhân phân tích kỹ thuật."""
    today = datetime.now(timezone.utc).date().isoformat()
    start_date = as_date(start_date, (datetime.now(timezone.utc).date() - timedelta(days=120)).isoformat())
    end_date = as_date(end_date, today)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    asset_class = detect_asset_class(symbol)
    if asset_class == "gold":
        return (
            f"# {symbol} là vàng — hệ thống chỉ có giá hiện tại, không có chuỗi OHLCV.\n"
            "Hãy dùng dữ liệu tin tức (get_news) và giá vàng mới nhất trong kho để phân tích."
        )
    if asset_class == "vn_index":
        return (
            f"# {symbol} là chỉ số thị trường, không phải tài sản giao dịch được.\n"
            "Dùng số liệu này làm bối cảnh thị trường chung, không đưa ra đề xuất mua bán nó."
        )
    try:
        if asset_class == "crypto":
            frame = _crypto_ohlcv(symbol, start_date, end_date)
        else:
            frame = _vn_ohlcv(symbol, start_date, end_date)
    except Exception as exc:  # noqa: BLE001
        return f"LỖI DỮ LIỆU: không lấy được giá {symbol} ({exc}). Hãy nêu rõ trong báo cáo là thiếu dữ liệu giá."

    if frame.empty:
        return f"Không có phiên giao dịch nào cho {symbol} trong khoảng {start_date} → {end_date}."

    frame = frame.sort_values("date")
    lines = [
        f"# Dữ liệu giá {symbol} ({asset_class}) từ {start_date} đến {end_date}",
        f"# Đơn vị: {'USDT' if asset_class == 'crypto' else 'VND'}",
        "date,open,high,low,close,volume",
    ]
    for _, row in frame.iterrows():
        lines.append(
            f"{row['date']},{row['open']:.4f},{row['high']:.4f},"
            f"{row['low']:.4f},{row['close']:.4f},{int(row['volume'])}"
        )

    first_close, last_close = float(frame.iloc[0]["close"]), float(frame.iloc[-1]["close"])
    change = (last_close - first_close) / first_close * 100 if first_close else 0.0
    lines.append("")
    lines.append(f"# Biến động cả kỳ: {change:+.2f}% ({first_close:.4f} → {last_close:.4f})")

    # Nối thêm tầng vi mô: sổ lệnh, khối ngoại, dòng tiền chủ động.
    #
    # Ghép vào đây thay vì tạo công cụ mới, vì danh sách công cụ của TradingAgents
    # được khai báo cứng trong khung — thêm hàm mới sẽ không được các tác nhân gọi.
    # Hàm này thì tác nhân phân tích thị trường đã gọi sẵn.
    #
    # Đây là phần "tổng hợp thông tin": giá chỉ cho biết chuyện đã xảy ra, còn sổ
    # lệnh và dòng tiền cho biết áp lực đang hình thành.
    micro = _microstructure_section(symbol, asset_class)
    if micro:
        lines += ["", micro]

    return "\n".join(lines)


def _microstructure_section(symbol: str, asset_class: str) -> str:
    """Khối dữ liệu vi mô để nối vào báo cáo giá. Lỗi thì trả về chuỗi rỗng."""
    parts: list[str] = []
    try:
        from finagent.collectors.microstructure import (
            fetch_microstructure,
            format_microstructure,
        )

        parts.append(format_microstructure(fetch_microstructure(symbol, asset_class)))
    except Exception as exc:  # noqa: BLE001 - dữ liệu bổ trợ, không được làm hỏng
        logger.warning("Không lấy được dữ liệu vi mô cho %s: %s", symbol, exc)

    # Chứng khoán Việt Nam có hai nguồn giá độc lập, nên đối chiếu được với nhau.
    # Crypto chỉ có Binance nên không có gì để đối chiếu.
    if asset_class == "vn_stock":
        try:
            from finagent.collectors.vnstock import cross_check_price

            check = cross_check_price(symbol)
            icon = {"high": "✅", "low": "⚠️", "conflict": "❌", "none": "❌"}.get(
                check["confidence"], "⚠️"
            )
            parts.append(
                f"# Kiểm chứng giá từ hai nguồn độc lập\n"
                f"{icon} {check['note']}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không đối chiếu được giá %s: %s", symbol, exc)

    return "\n\n".join(part for part in parts if part)


def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
    """Tính chỉ báo kỹ thuật bằng ``stockstats`` (đã có sẵn trong TradingAgents)."""
    from stockstats import wrap

    look_back_days = as_int(look_back_days, default=30, minimum=5, maximum=250)
    curr_date = as_date(curr_date, datetime.now(timezone.utc).date().isoformat())

    end = datetime.fromisoformat(curr_date)
    start = end - timedelta(days=max(look_back_days * 3, 90))  # lấy dư để chỉ báo đủ ấm

    asset_class = detect_asset_class(symbol)
    if asset_class == "gold":
        return f"Không tính được chỉ báo kỹ thuật cho {symbol}: vàng không có chuỗi nến trong hệ thống."
    try:
        if asset_class == "crypto":
            frame = _crypto_ohlcv(symbol, start.date().isoformat(), end.date().isoformat())
        else:
            frame = _vn_ohlcv(symbol, start.date().isoformat(), end.date().isoformat())
    except Exception as exc:  # noqa: BLE001
        return f"LỖI DỮ LIỆU: không tính được chỉ báo {indicator} cho {symbol} ({exc})."

    if frame.empty:
        return f"Không có dữ liệu để tính {indicator} cho {symbol}."

    prepared = frame.rename(columns={"date": "date"}).copy()
    prepared["date"] = pd.to_datetime(prepared["date"])
    prepared = prepared.sort_values("date")

    stats = wrap(prepared)
    key = indicator.strip().lower()
    try:
        series = stats[key]
    except Exception:  # noqa: BLE001
        return f"Chỉ báo {indicator!r} không được hỗ trợ. Hãy dùng rsi, macd, sma_20, ema_10, boll, atr."

    tail = prepared.tail(look_back_days).copy()
    tail["value"] = list(series.tail(look_back_days))
    lines = [f"# Chỉ báo {key.upper()} của {symbol} (tính đến {curr_date})", "date,value"]
    lines += [f"{row['date'].date()},{row['value']:.4f}" for _, row in tail.iterrows()]

    latest = float(tail["value"].iloc[-1])
    lines.append("")
    lines.append(f"# Giá trị mới nhất: {latest:.4f}")
    return "\n".join(lines)


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Tin tức đã thu thập, lọc theo chủ đề gắn với mã đang phân tích."""
    asset_class = detect_asset_class(ticker)
    topic = {"crypto": "crypto", "gold": "gold"}.get(asset_class, "stock")
    items = recent_news(limit=40, topic=topic)
    if not items:
        items = recent_news(limit=30)
    return _format_news(items, title=f"Tin tức liên quan {ticker} (chủ đề {topic})")


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    """Tin tức vĩ mô tổng hợp do các máy con thu thập."""
    items = recent_news(limit=as_int(limit, default=20, minimum=1, maximum=200))
    return _format_news(items, title="Tin tức vĩ mô và thị trường (do các máy con thu thập)")


def get_fundamentals(ticker: str, curr_date: str = "") -> str:
    """Cảnh báo thiếu dữ liệu cơ bản để tác nhân không bịa số liệu."""
    return (
        f"KHÔNG CÓ DỮ LIỆU CƠ BẢN cho {ticker} trong hệ thống FinAgent. "
        "Hệ thống chỉ thu thập giá và tin tức. Tuyệt đối không suy đoán các chỉ số "
        "tài chính như P/E, EPS, doanh thu; hãy dựa vào giá, chỉ báo kỹ thuật và tin tức."
    )


def _not_available(method: str, symbol: str = "") -> str:
    """Câu trả lời chuẩn cho dữ liệu mà hệ thống không có.

    Trả về thông báo rõ ràng thay vì để TradingAgents rơi sang vendor Mỹ
    (yfinance/SEC) — những vendor đó không có dữ liệu Việt Nam, chỉ tốn thời gian
    chờ mạng rồi đổ ra lỗi 404 khó hiểu.
    """
    target = f" cho {symbol}" if symbol else ""
    return (
        f"KHÔNG CÓ DỮ LIỆU{target}: FinAgent không cung cấp '{method}'. "
        "Hãy dựa vào dữ liệu giá, chỉ báo kỹ thuật và tin tức đang có; "
        "không được suy đoán số liệu còn thiếu."
    )


# Các hàm dưới đây chỉ trả về thông báo "không có dữ liệu", nên nhận mọi tham số
# qua ``*args, **kwargs``. TradingAgents gọi mỗi phương thức với số tham số khác
# nhau và có thể đổi giữa các phiên bản — nhận thoáng giúp hệ thống không vỡ vì
# lệch chữ ký, trong khi thông tin trả về vẫn đúng như thiết kế.

def get_balance_sheet(ticker: str = "", *args, **kwargs) -> str:
    return _not_available("bảng cân đối kế toán", ticker)


def get_cashflow(ticker: str = "", *args, **kwargs) -> str:
    return _not_available("báo cáo lưu chuyển tiền tệ", ticker)


def get_income_statement(ticker: str = "", *args, **kwargs) -> str:
    return _not_available("báo cáo kết quả kinh doanh", ticker)


def get_insider_transactions(ticker: str = "", *args, **kwargs) -> str:
    return _not_available("giao dịch nội bộ", ticker)


def get_macro_indicators(indicator: str = "", curr_date: str = "", look_back_days: int | None = None,
                         *args, **kwargs) -> str:
    """Chỉ số vĩ mô — chuyển tiếp sang FRED khi có khoá.

    Vĩ mô Mỹ (lãi suất Fed, CPI, lợi suất trái phiếu) tác động trực tiếp tới tỷ giá
    USD/VND, giá vàng và crypto — nên đây là ngữ cảnh có giá trị cho thị trường
    Việt Nam, không phải dữ liệu xa lạ.
    """
    from finagent.config import settings

    if not settings.fred_api_key:
        return (
            "KHÔNG CÓ DỮ LIỆU VĨ MÔ: chưa cấu hình FRED_API_KEY. "
            "Đăng ký miễn phí tại https://fred.stlouisfed.org/docs/api/api_key.html "
            "rồi thêm FRED_API_KEY vào .env để có chỉ số lãi suất Fed, CPI, lợi suất trái phiếu."
        )
    try:
        from tradingagents.dataflows.vendors.fred import get_macro_data

        return get_macro_data(indicator, curr_date, look_back_days)
    except Exception as exc:  # noqa: BLE001 - thiếu dữ liệu vĩ mô không được làm hỏng lượt phân tích
        logger.warning("FRED thất bại cho %r: %s", indicator, exc)
        return f"KHÔNG LẤY ĐƯỢC DỮ LIỆU VĨ MÔ {indicator!r}: {exc}"


def get_prediction_markets(topic: str = "", limit: int | None = None, curr_date: str | None = None,
                           *args, **kwargs) -> str:
    """Xác suất sự kiện tương lai — chuyển tiếp sang Polymarket (miễn phí, không cần khoá).

    Ví dụ hữu ích: xác suất Fed giảm lãi suất, xác suất suy thoái — đều ảnh hưởng
    tới vàng và crypto.
    """
    from finagent.config import settings

    if not settings.polymarket_enabled:
        return _not_available(f"thị trường dự đoán {topic!r} (đã tắt bằng FINAGENT_POLYMARKET=false)")
    try:
        from tradingagents.dataflows.vendors.polymarket import (
            get_prediction_markets as polymarket_markets,
        )

        return polymarket_markets(topic, limit, curr_date)
    except Exception as exc:  # noqa: BLE001 - dữ liệu bổ trợ, không được làm hỏng lượt phân tích
        logger.warning("Polymarket thất bại cho %r: %s", topic, exc)
        return f"KHÔNG LẤY ĐƯỢC DỮ LIỆU DỰ ĐOÁN {topic!r}: {exc}"


def _format_news(items: list[dict], title: str) -> str:
    """Định dạng danh sách tin tức thành văn bản cho LLM đọc."""
    if not items:
        return f"# {title}\nKhông có tin tức nào trong kho dữ liệu."

    lines = [f"# {title}", f"# Tổng số: {len(items)} bài", ""]
    for index, item in enumerate(items, 1):
        topics = ", ".join(item.get("topics") or []) or "khác"
        lines.append(f"{index}. [{item.get('source', '?')}] {item.get('title', '')}")
        lines.append(f"   Thời gian: {item.get('published_at', '')} | Chủ đề: {topics}")
        if item.get("summary"):
            lines.append(f"   Tóm tắt: {item['summary'][:300]}")
        lines.append(f"   Nguồn: {item.get('url', '')}")
    return "\n".join(lines)


def build_verified_market_snapshot(
    symbol: str,
    curr_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> str:
    """Ảnh chụp thị trường "đã kiểm chứng" dựng từ dữ liệu FinAgent.

    TradingAgents gốc lấy ảnh chụp này từ Yahoo Finance và tác nhân được dạy coi
    nó là nguồn sự thật. Với cổ phiếu Việt Nam, Yahoo không có dữ liệu nên phải
    thay bằng bản dựng từ chính kho dữ liệu của hệ thống — nếu không, tác nhân sẽ
    nhận lỗi 404 và mất đi nguồn đối chiếu chính.
    """
    look_back_days = as_int(look_back_days, default=30, minimum=5, maximum=250)
    end = datetime.fromisoformat(as_date(curr_date, datetime.now(timezone.utc).date().isoformat())).date()
    # Lấy dư gấp đôi để chỉ báo đủ "ấm" trước khi cắt về đúng cửa sổ yêu cầu.
    start = end - timedelta(days=max(look_back_days * 2, 60))

    parts = [
        f"# ẢNH CHỤP THỊ TRƯỜNG ĐÃ KIỂM CHỨNG — {symbol}",
        f"# Nguồn: FinAgent (do các máy con thu thập) | Ngày: {curr_date}",
        "# Đây là nguồn sự thật cho mọi con số OHLCV và chỉ báo dưới đây.",
        "",
        get_stock_data(symbol, start.isoformat(), end.isoformat()),
        "",
    ]

    for indicator in (indicators or ("rsi", "macd", "sma_20")):
        parts.append(get_indicators(symbol, indicator, curr_date, look_back_days))
        parts.append("")

    return "\n".join(parts)


#: Thông báo dùng chung khi hệ thống không thu thập dữ liệu mạng xã hội.
_SOCIAL_UNAVAILABLE = (
    "KHÔNG CÓ DỮ LIỆU MẠNG XÃ HỘI: FinAgent không thu thập StockTwits hay Reddit "
    "cho {ticker}. Các cộng đồng đó bàn về cổ phiếu Mỹ, không phản ánh thị trường "
    "Việt Nam. Hãy đánh giá tâm lý dựa trên tin tức trong nước đã thu thập; "
    "tuyệt đối không suy đoán số liệu mạng xã hội."
)


#: Tên công ty cho các mã theo dõi mặc định.
#: TradingAgents tra tên qua Yahoo và trả về rỗng với cổ phiếu Việt Nam — khi đó
#: tác nhân không biết mình đang phân tích công ty nào và dễ suy diễn sai ngành
#: (đúng vấn đề #814 mà cơ chế này sinh ra để chống). Cấp tên sẵn vừa chặn được
#: lệnh gọi mạng vô ích, vừa giữ đúng danh tính doanh nghiệp.
VN_COMPANY_NAMES: dict[str, str] = {
    "FPT": "Công ty Cổ phần FPT",
    "VNM": "Công ty Cổ phần Sữa Việt Nam (Vinamilk)",
    "HPG": "Công ty Cổ phần Tập đoàn Hoà Phát",
    "VCB": "Ngân hàng TMCP Ngoại thương Việt Nam (Vietcombank)",
    "TCB": "Ngân hàng TMCP Kỹ thương Việt Nam (Techcombank)",
    "VIC": "Tập đoàn Vingroup",
    "VHM": "Công ty Cổ phần Vinhomes",
    "MSN": "Tập đoàn Masan",
    "MWG": "Công ty Cổ phần Đầu tư Thế Giới Di Động",
    "SSI": "Công ty Cổ phần Chứng khoán SSI",
    "VND": "Công ty Cổ phần Chứng khoán VNDirect",
    "ACB": "Ngân hàng TMCP Á Châu",
    "MBB": "Ngân hàng TMCP Quân đội",
    "CTG": "Ngân hàng TMCP Công thương Việt Nam",
    "GAS": "Tổng Công ty Khí Việt Nam",
    "SAB": "Tổng Công ty Cổ phần Bia - Rượu - Nước giải khát Sài Gòn",
    "PNJ": "Công ty Cổ phần Vàng bạc Đá quý Phú Nhuận",
}

#: Danh tính cho các đồng crypto phổ biến.
CRYPTO_NAMES: dict[str, str] = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "BNB": "BNB",
    "XRP": "XRP",
    "ADA": "Cardano",
    "DOGE": "Dogecoin",
    "TON": "Toncoin",
    "AVAX": "Avalanche",
    "LINK": "Chainlink",
    "DOT": "Polkadot",
    "MATIC": "Polygon",
    "LTC": "Litecoin",
}


@functools.lru_cache(maxsize=256)
def resolve_instrument_identity(ticker: str) -> dict:
    """Danh tính của mã, tra từ dữ liệu nội bộ thay vì gọi Yahoo.

    Giữ nguyên hợp đồng của TradingAgents (trả về dict, rỗng nếu không biết) nên
    phần còn lại của đồ thị không phải sửa gì.
    """
    symbol = ticker.upper().replace("-", "")
    asset_class = detect_asset_class(ticker)

    if asset_class == "crypto":
        base = re.match(r"^[A-Z]+?(?=USDT|USD$)", symbol)
        base_code = base.group(0) if base else symbol
        name = CRYPTO_NAMES.get(base_code, base_code)
        return {
            "company_name": f"{name} (cặp {symbol})",
            "sector": "Cryptocurrency",
            "industry": "Digital Assets",
            "exchange": "Binance",
            "quote_type": "CRYPTOCURRENCY",
        }

    if asset_class == "gold":
        from finagent.collectors import gold as gold_collector

        return {
            "company_name": gold_collector.label_for(ticker),
            "sector": "Precious Metals",
            "industry": "Gold",
            "exchange": "Trong nước",
            "quote_type": "COMMODITY",
        }

    return {
        "company_name": VN_COMPANY_NAMES.get(symbol, f"{symbol} (công ty niêm yết tại Việt Nam)"),
        "sector": "Cổ phiếu Việt Nam",
        "exchange": "HOSE/HNX",
        "quote_type": "EQUITY",
    }


def _social_unavailable(ticker: str = "") -> str:
    return _SOCIAL_UNAVAILABLE.format(ticker=ticker)


def fetch_stocktwits_messages(ticker: str = "", *args, **kwargs) -> str:
    """Chặn lệnh gọi StockTwits — không liên quan thị trường Việt Nam."""
    return _SOCIAL_UNAVAILABLE.format(ticker=ticker)


def fetch_reddit_posts(ticker: str = "", *args, **kwargs) -> str:
    """Chặn lệnh gọi Reddit — không liên quan thị trường Việt Nam."""
    return _SOCIAL_UNAVAILABLE.format(ticker=ticker)


def get_closes(symbol: str, start_date: str, end_date: str):
    """Chuỗi giá đóng cửa phục vụ việc đối chiếu kết quả về sau.

    Trả về ``pandas.Series`` rỗng với những mã hệ thống không có dữ liệu (ví dụ
    chỉ số chuẩn SPY của Mỹ). Nhờ vậy quyết định chỉ được chấm điểm khi thật sự
    có đủ dữ liệu, thay vì chấm sai dựa trên dữ liệu không tồn tại.
    """
    import pandas as pd

    try:
        if detect_asset_class(symbol) == "gold":
            return pd.Series(dtype=float)
        frame = _crypto_ohlcv(symbol, start_date, end_date) if detect_asset_class(symbol) == "crypto" \
            else _vn_ohlcv(symbol, start_date, end_date)
    except Exception:  # noqa: BLE001 - thiếu dữ liệu là chuyện bình thường ở đây
        return pd.Series(dtype=float)

    if frame is None or frame.empty:
        return pd.Series(dtype=float)

    frame = frame.sort_values("date")
    return pd.Series(frame["close"].to_numpy(), index=pd.to_datetime(frame["date"]))


# ---------------------------------------------------------------------------
# Đăng ký vendor vào TradingAgents
# ---------------------------------------------------------------------------

_registered = False


def installed_tradingagents_version() -> str:
    """Phiên bản TradingAgents đang được cài, hoặc ``"không rõ"``."""
    try:
        from importlib.metadata import version

        return version("tradingagents")
    except Exception:  # noqa: BLE001
        return "không rõ"


def check_tradingagents_version() -> str | None:
    """Cảnh báo nếu TradingAgents không đúng phiên bản đã kiểm thử.

    Trả về chuỗi cảnh báo, hoặc ``None`` nếu khớp. Không ném lỗi: hệ thống vẫn
    nên chạy, nhưng người dùng phải biết vì sao bản vá có thể không hoạt động.
    """
    installed = installed_tradingagents_version()
    if installed.startswith(TESTED_TRADINGAGENTS_VERSION):
        return None
    return (
        f"TradingAgents đang cài là {installed}, nhưng bản đã kiểm thử là "
        f"{TESTED_TRADINGAGENTS_VERSION}. Các bản vá dữ liệu Việt Nam có thể không "
        f"khớp. Hãy cài đúng bản trong dự án: pip install -e vendor/TradingAgents"
    )


def _install_opencode_go_headers() -> None:
    """Gửi header mà endpoint OpenCode Go bắt buộc.

    Go **từ chối** mọi yêu cầu thiếu header ``x-opencode-session``, trả về lỗi
    ``MissingSessionID``. Đây không phải tuỳ chọn: thiếu nó là không gọi được gì.

    Ngoài ra Go yêu cầu client tự giới thiệu bằng User-Agent riêng thay vì tên thư
    viện HTTP chung, để họ theo dõi lạm dụng. Dùng tên mặc định của thư viện có thể
    bị coi là traffic bất thường.

    Cách cắm: ``OpenAIClient`` chỉ chuyển tiếp các khoá trong ``_PASSTHROUGH_KWARGS``
    xuống ``ChatOpenAI``, và danh sách đó không có ``default_headers``. Nên phải mở
    danh sách ra, rồi bổ sung header từ ``build_llm_kwargs``.
    """
    from finagent.config import settings

    if not settings.llm_backend_url or "opencode.ai" not in settings.llm_backend_url:
        return

    try:
        from tradingagents.llm_clients import factory, openai_client

        # 1) Cho phép chuyển tiếp default_headers xuống ChatOpenAI.
        if "default_headers" not in openai_client._PASSTHROUGH_KWARGS:
            openai_client._PASSTHROUGH_KWARGS = (
                *openai_client._PASSTHROUGH_KWARGS, "default_headers",
            )

        # 2) Bổ sung header khi dựng kwargs cho provider OpenAI-compatible.
        #
        # Luôn bọc lại từ hàm GỐC, không bọc chồng lên bản đã bọc. Nhờ vậy hàm cài
        # lại được nhiều lần (cần cho test, và để đổi mã phiên lúc đang chạy) mà
        # không tạo ra chuỗi bọc vô hạn.
        original = getattr(factory.build_llm_kwargs, "_finagent_original", None)
        if original is None:
            original = factory.build_llm_kwargs

        def build_llm_kwargs_with_headers(config: dict) -> dict:
            kwargs = original(config)
            if str(config.get("llm_provider", "")).lower() == "openai_compatible":
                kwargs["default_headers"] = {
                    # Go đòi User-Agent riêng, không nhận tên thư viện HTTP chung.
                    "User-Agent": settings.llm_user_agent,
                    # Go đòi mã phiên để định tuyến và tối ưu prompt caching. Dùng
                    # một mã ổn định cho cả tiến trình để các lời gọi trong cùng một
                    # lượt phân tích được coi là một phiên.
                    "x-opencode-session": settings.llm_session_id,
                }
            return kwargs

        build_llm_kwargs_with_headers._finagent_headers = True
        build_llm_kwargs_with_headers._finagent_original = original
        factory.build_llm_kwargs = build_llm_kwargs_with_headers

        # trading_graph nhập tên hàm trực tiếp nên phải vá cả ở đó.
        from tradingagents.graph import trading_graph

        trading_graph.build_llm_kwargs = build_llm_kwargs_with_headers

        logger.info(
            "Đã cắm header OpenCode Go (phiên %s).", settings.llm_session_id,
        )
    except Exception as exc:  # noqa: BLE001 - không cắm được thì vẫn chạy tiếp
        logger.warning("Không cắm được header OpenCode Go: %s", exc)


def install_patches() -> None:
    """Thay các lệnh gọi cứng tới nhà cung cấp Mỹ bằng bản dùng dữ liệu Việt Nam.

    Ba chỗ trong TradingAgents gọi thẳng Yahoo/StockTwits/Reddit mà **không** đi
    qua bộ định tuyến vendor, nên đăng ký vendor thôi là chưa đủ:

    1. ``agents.tools.build_verified_market_snapshot`` — ảnh chụp giá mà tác nhân
       phân tích kỹ thuật được dạy coi là nguồn sự thật.
    2. ``sentiment_analyst.fetch_stocktwits_messages`` / ``fetch_reddit_posts`` —
       dữ liệu mạng xã hội Mỹ, không liên quan thị trường Việt Nam.
    3. ``graph.settlement.get_closes`` — dùng để chấm điểm quyết định cũ.

    Không vá thì mỗi lượt phân tích vẫn tốn vài lệnh gọi mạng chắc chắn thất bại,
    vừa chậm vừa đổ ra log lỗi gây nhiễu.
    """
    from tradingagents.agents import context as ta_context
    from tradingagents.agents import tools as ta_tools
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.graph import settlement, trading_graph

    ta_tools.build_verified_market_snapshot = build_verified_market_snapshot
    sentiment_analyst.fetch_stocktwits_messages = fetch_stocktwits_messages
    sentiment_analyst.fetch_reddit_posts = fetch_reddit_posts
    settlement.get_closes = get_closes

    # `resolve_instrument_identity` được import trực tiếp vào hai module nên phải
    # thay ở cả hai nơi, không chỉ ở nơi định nghĩa.
    ta_context.resolve_instrument_identity = resolve_instrument_identity
    trading_graph.resolve_instrument_identity = resolve_instrument_identity

    logger.info("Đã thay các điểm gọi cứng tới nhà cung cấp Mỹ bằng dữ liệu FinAgent.")


    _install_opencode_go_headers()

def register_vendor() -> None:
    """Chèn vendor ``finagent`` vào bảng định tuyến của TradingAgents.

    Đăng ký **mọi** phương thức mà TradingAgents có, kể cả những phương thức hệ
    thống không hỗ trợ. Việc đó là chủ đích: nếu để trống, TradingAgents sẽ rơi
    sang vendor Mỹ (yfinance, SEC, FRED) — những vendor không có dữ liệu Việt Nam,
    chỉ làm chậm mỗi lượt phân tích và đổ ra lỗi 404 khó hiểu. Các phương thức
    không hỗ trợ trả về thông báo rõ ràng để tác nhân biết là thiếu dữ liệu.

    Hàm này idempotent nên gọi lại vẫn an toàn.
    """
    global _registered
    if _registered:
        return

    mismatch = check_tradingagents_version()
    if mismatch:
        logger.warning(mismatch)

    from tradingagents.dataflows import router

    implementations = {
        # Có dữ liệu thật
        "get_stock_data": get_stock_data,
        "get_indicators": get_indicators,
        "get_news": get_news,
        "get_global_news": get_global_news,
        "get_fundamentals": get_fundamentals,
        # Không có dữ liệu — trả về thông báo thay vì gọi mạng nước ngoài
        "get_balance_sheet": get_balance_sheet,
        "get_cashflow": get_cashflow,
        "get_income_statement": get_income_statement,
        "get_insider_transactions": get_insider_transactions,
        "get_macro_indicators": get_macro_indicators,
        "get_prediction_markets": get_prediction_markets,
    }
    for method, implementation in implementations.items():
        router.VENDOR_METHODS.setdefault(method, {})[VENDOR_NAME] = implementation

    if VENDOR_NAME not in router.VENDOR_LIST:
        router.VENDOR_LIST.append(VENDOR_NAME)

    # Vá các lệnh gọi cứng tới nhà cung cấp Mỹ (không đi qua bộ định tuyến vendor).
    try:
        install_patches()
    except Exception as exc:  # noqa: BLE001 - TradingAgents đổi API thì vẫn phải chạy được
        logger.warning("Không vá được các điểm gọi nhà cung cấp Mỹ: %s", exc)

    _registered = True
    logger.info(
        "Đã đăng ký vendor %r cho %d phương thức của TradingAgents.",
        VENDOR_NAME, len(implementations),
    )


def vendor_config() -> dict:
    """Cấu hình ``data_vendors``: mọi danh mục đều dùng vendor ``finagent``.

    Trỏ hết về finagent để **không còn lệnh gọi mạng nào tới vendor Mỹ**. Những
    danh mục hệ thống không có dữ liệu vẫn trỏ về đây và nhận thông báo
    "không có dữ liệu", nhờ vậy tác nhân biết rõ giới hạn thay vì nhận về lỗi mạng.
    """
    return {
        "core_stock_apis": VENDOR_NAME,
        "technical_indicators": VENDOR_NAME,
        "fundamental_data": VENDOR_NAME,
        "news_data": VENDOR_NAME,
        "macro_data": VENDOR_NAME,
        "prediction_markets": VENDOR_NAME,
    }


def benchmark_for(asset_class: str) -> str:
    """Chỉ số chuẩn để chấm điểm alpha cho một nhóm tài sản.

    Backtest so lợi nhuận với chỉ số này; không có chỉ số đúng thị trường thì điểm
    alpha vô nghĩa. Cổ phiếu Việt Nam so với VN-Index, crypto so với Bitcoin.
    """
    from finagent.config import settings

    if asset_class == "crypto":
        return settings.benchmark_crypto
    return settings.benchmark_vn


def tradable_symbols(symbols: list[str]) -> list[str]:
    """Lọc bỏ chỉ số — chỉ số dùng làm bối cảnh, không sinh đề xuất mua bán."""
    return [s for s in symbols if detect_asset_class(s) != "vn_index"]
