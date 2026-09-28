"""Lưu trữ trạng thái của máy chủ bằng SQLite.

Máy chủ là nơi duy nhất ghi dữ liệu: giá, tin tức, đề xuất, lệnh và vị thế.
Máy con chỉ trả kết quả về, không đụng tới cơ sở dữ liệu.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from finagent.config import settings
from finagent.collectors.base import utcnow_iso

logger = logging.getLogger(__name__)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    asset_class  TEXT NOT NULL,
    price        REAL NOT NULL,
    currency     TEXT NOT NULL,
    change_pct   REAL,
    volume       REAL,
    source       TEXT,
    captured_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_time ON prices(symbol, captured_at DESC);

CREATE TABLE IF NOT EXISTS news (
    uid          TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    source       TEXT,
    published_at TEXT,
    summary      TEXT,
    topics       TEXT,
    collected_by TEXT,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news(collected_at DESC);

CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    asset_class   TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'VND',
    action        TEXT NOT NULL,
    signal        TEXT NOT NULL,
    confidence    REAL NOT NULL,
    -- Nguồn ra tín hiệu: "llm", "ml" hay "llm+ml" — phục vụ đối chiếu về sau.
    source        TEXT NOT NULL DEFAULT 'ml',
    price         REAL NOT NULL,
    quantity      REAL NOT NULL,
    amount        REAL NOT NULL,
    rationale     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    telegram_msg  INTEGER,
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id  INTEGER,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    quantity     REAL NOT NULL,
    price        REAL NOT NULL,
    status       TEXT NOT NULL,
    mode         TEXT NOT NULL,
    broker_ref   TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS positions (
    symbol       TEXT PRIMARY KEY,
    asset_class  TEXT NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'VND',
    quantity     REAL NOT NULL,
    avg_price    REAL NOT NULL,
    opened_at    TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Ví đa tiền tệ: mỗi loại tiền một số dư riêng. Cần thiết vì crypto niêm yết
-- bằng USDT còn chứng khoán/vàng niêm yết bằng VND — không thể trộn chung.
CREATE TABLE IF NOT EXISTS portfolio (
    currency   TEXT PRIMARY KEY,
    cash       REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    worker     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # cho phép đọc/ghi đồng thời
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """Kết nối SQLite riêng cho mỗi luồng (SQLite không chia sẻ giữa luồng)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect(settings.db_path)
        _local.conn = conn
    return conn


@contextmanager
def transaction():
    """Ngữ cảnh giao dịch: commit khi thành công, rollback khi lỗi."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Tạo bảng và nạp số dư ban đầu cho từng ví tiền tệ."""
    with transaction() as conn:
        conn.executescript(SCHEMA)
        for currency, amount in (
            ("VND", settings.paper_starting_cash),
            ("USDT", settings.paper_starting_usdt),
        ):
            conn.execute(
                """INSERT OR IGNORE INTO portfolio (currency, cash, updated_at)
                   VALUES (?, ?, ?)""",
                (currency, amount, utcnow_iso()),
            )


def get_cash(currency: str) -> float:
    """Số dư tiền mặt của một loại tiền tệ."""
    row = get_conn().execute(
        "SELECT cash FROM portfolio WHERE currency = ?", (currency.upper(),)
    ).fetchone()
    return float(row["cash"]) if row else 0.0


def add_cash(currency: str, delta: float, conn: sqlite3.Connection | None = None) -> None:
    """Cộng/trừ tiền mặt của một ví (``delta`` âm là chi ra)."""
    target = conn or get_conn()
    target.execute(
        """INSERT INTO portfolio (currency, cash, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(currency) DO UPDATE SET
               cash = cash + excluded.cash,
               updated_at = excluded.updated_at""",
        (currency.upper(), delta, utcnow_iso()),
    )


def all_cash() -> dict[str, float]:
    """Toàn bộ số dư theo từng loại tiền tệ."""
    rows = get_conn().execute("SELECT currency, cash FROM portfolio").fetchall()
    return {row["currency"]: float(row["cash"]) for row in rows}


# ---------------------------------------------------------------------------
# Ghi dữ liệu thu thập
# ---------------------------------------------------------------------------

def save_prices(items: list[dict]) -> int:
    """Lưu các mẫu giá; trả về số bản ghi đã ghi."""
    if not items:
        return 0
    rows = [
        (
            item["symbol"], item["asset_class"], float(item["price"]), item["currency"],
            item.get("change_pct_24h"), item.get("volume"), item.get("source"),
            item.get("captured_at") or utcnow_iso(),
        )
        for item in items
    ]
    with transaction() as conn:
        conn.executemany(
            """INSERT INTO prices
               (symbol, asset_class, price, currency, change_pct, volume, source, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def save_news(items: list[dict]) -> int:
    """Lưu tin tức, bỏ qua bài đã có (chống trùng giữa nhiều máy con)."""
    if not items:
        return 0
    rows = [
        (
            item["uid"], item["title"], item["url"], item.get("source"),
            item.get("published_at"), item.get("summary"),
            json.dumps(item.get("topics") or [], ensure_ascii=False),
            item.get("collected_by"), utcnow_iso(),
        )
        for item in items
    ]
    with transaction() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM news").fetchone()["n"]
        conn.executemany(
            """INSERT OR IGNORE INTO news
               (uid, title, url, source, published_at, summary, topics, collected_by, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        after = conn.execute("SELECT COUNT(*) AS n FROM news").fetchone()["n"]
    return after - before


def log_worker_event(worker: str, kind: str, detail: str = "") -> None:
    """Ghi nhật ký hoạt động của máy con để phục vụ báo cáo đề tài."""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO worker_events (worker, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            (worker, kind, detail[:500], utcnow_iso()),
        )


# ---------------------------------------------------------------------------
# Đọc dữ liệu cho tầng ra quyết định
# ---------------------------------------------------------------------------

def latest_price(symbol: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM prices WHERE symbol = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    return dict(row) if row else None


def price_history(symbol: str, limit: int = 120) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM prices WHERE symbol = ? ORDER BY captured_at DESC, id DESC LIMIT ?",
        (symbol.upper(), limit),
    ).fetchall()
    return [dict(r) for r in rows][::-1]


def recent_news(limit: int = 30, topic: str | None = None) -> list[dict]:
    """Tin tức mới nhất, tuỳ chọn lọc theo chủ đề.

    ``limit`` được ép về số nguyên một cách phòng thủ: giá trị này có thể đến từ
    tham số do mô hình ngôn ngữ sinh ra, mà mô hình thường trả về chuỗi (``"20"``)
    — SQLite sẽ báo ``datatype mismatch`` nếu đưa chuỗi vào mệnh đề ``LIMIT``.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 500))       # chặn dưới để không rỗng, chặn trên để không ngốn RAM

    conn = get_conn()
    if topic:
        rows = conn.execute(
            """SELECT * FROM news WHERE topics LIKE ?
               ORDER BY collected_at DESC LIMIT ?""",
            (f'%"{topic}"%', limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM news ORDER BY collected_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["topics"] = json.loads(item.get("topics") or "[]")
        except json.JSONDecodeError:
            item["topics"] = []
        result.append(item)
    return result


def news_topic_counts() -> dict[str, int]:
    """Đếm số bài theo chủ đề — dùng cho báo cáo tổng quan."""
    counts: dict[str, int] = {}
    for row in get_conn().execute("SELECT topics FROM news").fetchall():
        try:
            for topic in json.loads(row["topics"] or "[]"):
                counts[topic] = counts.get(topic, 0) + 1
        except json.JSONDecodeError:
            continue
    return counts
