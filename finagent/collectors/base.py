"""Kiểu dữ liệu dùng chung cho mọi bộ thu thập.

Chuẩn hoá dữ liệu về một hình dạng duy nhất giúp tầng ra quyết định và lưu trữ
không cần biết dữ liệu đến từ Binance, VNDirect hay một bản tin RSS.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow_iso() -> str:
    """Thời điểm hiện tại theo ISO-8601 UTC (dạng dùng thống nhất toàn hệ thống)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class PricePoint:
    """Một mẫu giá của một mã tài sản tại một thời điểm."""

    symbol: str
    asset_class: str          # "crypto" | "vn_stock" | "gold"
    price: float
    currency: str
    captured_at: str = field(default_factory=utcnow_iso)
    change_pct_24h: float | None = None
    volume: float | None = None
    source: str = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NewsItem:
    """Một bài tin tức đã thu thập."""

    title: str
    url: str
    source: str
    published_at: str = field(default_factory=utcnow_iso)
    summary: str = ""
    #: Nhóm chủ đề suy ra từ tiêu đề, ví dụ "gold", "real_estate", "stock".
    topics: list[str] = field(default_factory=list)
    collected_by: str = "unknown"

    @property
    def uid(self) -> str:
        """Mã định danh ổn định để chống trùng lặp giữa các máy con."""
        raw = f"{self.url}|{self.title}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["uid"] = self.uid
        return data


@dataclass
class CollectionResult:
    """Kết quả một lượt thu thập do một máy con trả về cho máy chủ."""

    worker: str
    kind: str                 # "price" | "news"
    items: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0
    finished_at: str = field(default_factory=utcnow_iso)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return asdict(self)
