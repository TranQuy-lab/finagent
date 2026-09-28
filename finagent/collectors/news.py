"""Thu thập tin tức tài chính từ các bản tin RSS công khai.

Đây là phần "dùng tài nguyên máy con để thu thập thông tin đầu vào" trong đề
tài: mỗi máy con nhận một phần danh sách nguồn, cào song song, rồi trả về máy
chủ. Nhóm chủ đề (vàng, bất động sản, chứng khoán, vĩ mô) được suy ra ngay tại
máy con để máy chủ đỡ phải xử lý.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from finagent.collectors.base import NewsItem

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20

#: Nguồn RSS công khai, miễn phí. Nhãn dùng để hiển thị và gán chủ đề.
#: Toàn bộ URL dưới đây đã kiểm chứng trả về HTTP 200 kèm bản tin.
NEWS_FEEDS: dict[str, str] = {
    "VnExpress Kinh doanh": "https://vnexpress.net/rss/kinh-doanh.rss",
    "VnExpress Bất động sản": "https://vnexpress.net/rss/bat-dong-san.rss",
    "VnExpress Chứng khoán": "https://vnexpress.net/rss/chung-khoan.rss",
    "CafeF Thị trường chứng khoán": "https://cafef.vn/thi-truong-chung-khoan.rss",
    "CafeF Bất động sản": "https://cafef.vn/bat-dong-san.rss",
    "CafeF Tài chính ngân hàng": "https://cafef.vn/tai-chinh-ngan-hang.rss",
    "Vietstock Chứng khoán": "https://vietstock.vn/830/chung-khoan.rss",
    "Vietstock Bất động sản": "https://vietstock.vn/761/bat-dong-san.rss",
    "Vietstock Kinh tế": "https://vietstock.vn/733/kinh-te.rss",
}

#: Từ khoá suy ra chủ đề. Kèm cả tiếng Anh vì nhiều bản tin trộn thuật ngữ.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gold": ("vàng", "sjc", "doji", "pnj", "gold", "kim loại quý"),
    "real_estate": ("bất động sản", "nhà đất", "địa ốc", "chung cư", "real estate", "quy hoạch"),
    "stock": ("chứng khoán", "cổ phiếu", "vn-index", "hose", "hnx", "upcom", "stock"),
    "crypto": ("bitcoin", "ethereum", "tiền số", "tiền ảo", "crypto", "btc", "eth"),
    "macro": ("lạm phát", "lãi suất", "gdp", "tỷ giá", "fed", "ngân hàng nhà nước", "vĩ mô"),
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Bỏ thẻ HTML khỏi phần mô tả của bản tin."""
    return _TAG_RE.sub("", text or "").strip()


def detect_topics(text: str) -> list[str]:
    """Suy ra danh sách chủ đề từ tiêu đề/mô tả."""
    haystack = (text or "").lower()
    return [topic for topic, words in TOPIC_KEYWORDS.items() if any(word in haystack for word in words)]


def _parse_published(entry) -> str:
    """Chuẩn hoá thời gian đăng bài về ISO-8601 UTC."""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return datetime(*parsed[:6], tzinfo=timezone.utc).replace(microsecond=0).isoformat()


def fetch_feed(source: str, url: str, max_items: int, worker: str = "unknown") -> tuple[list[dict], list[str]]:
    """Đọc một bản tin RSS; trả về ``(bài viết, lỗi)``."""
    errors: list[str] = []
    items: list[dict] = []
    try:
        # Tải qua requests trước để kiểm soát timeout và User-Agent, tránh bị chặn.
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "FinAgent/1.0"})
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as exc:  # noqa: BLE001 - một nguồn lỗi không được làm hỏng cả lượt
        logger.warning("Không đọc được nguồn %s: %s", source, exc)
        return [], [f"{source}: {exc}"]

    for entry in feed.entries[:max_items]:
        title = _strip_html(getattr(entry, "title", ""))
        link = getattr(entry, "link", "")
        if not title or not link:
            continue
        summary = _strip_html(getattr(entry, "summary", ""))[:500]
        item = NewsItem(
            title=title,
            url=link,
            source=source,
            published_at=_parse_published(entry),
            summary=summary,
            topics=detect_topics(f"{title} {summary}"),
            collected_by=worker,
        )
        items.append(item.to_dict())

    return items, errors


def collect(
    max_items: int,
    worker: str = "unknown",
    feeds: dict[str, str] | None = None,
    sources: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Thu thập tin tức từ nhiều nguồn.

    ``sources`` cho phép máy chủ chia nhỏ danh sách nguồn cho từng máy con,
    nhờ đó nhiều máy cùng cào mà không trùng lặp.
    """
    selected = feeds or NEWS_FEEDS
    if sources:
        selected = {name: url for name, url in selected.items() if name in sources}

    items: list[dict] = []
    errors: list[str] = []
    for source, url in selected.items():
        feed_items, feed_errors = fetch_feed(source, url, max_items, worker=worker)
        items.extend(feed_items)
        errors.extend(feed_errors)
    return items, errors


def chunk_sources(num_chunks: int) -> list[list[str]]:
    """Chia danh sách nguồn thành ``num_chunks`` phần gần bằng nhau.

    Dùng để máy chủ phân công mỗi máy con phụ trách một nhóm nguồn riêng.
    """
    names = list(NEWS_FEEDS.keys())
    if num_chunks <= 0:
        return [names]
    chunks: list[list[str]] = [[] for _ in range(num_chunks)]
    for index, name in enumerate(names):
        chunks[index % num_chunks].append(name)
    return [chunk for chunk in chunks if chunk]
