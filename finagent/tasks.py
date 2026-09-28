"""Các tác vụ Celery chạy trên máy con.

Máy chủ đẩy tác vụ vào Redis, máy con nhặt và thực thi. Mọi tác vụ đều đi qua
``crawl_slot()`` để tổng số việc chạy song song trên toàn cụm không vượt quá
``FINAGENT_MAX_CONCURRENCY`` (mặc định 10 slot).
"""

from __future__ import annotations

import logging
import os
import socket
import time

from finagent.celery_app import celery_app, crawl_slot, get_redis
from finagent.collectors import crypto, gold, news, vnstock
from finagent.config import settings

logger = logging.getLogger(__name__)


def _worker_name() -> str:
    """Tên định danh máy con: ưu tiên biến môi trường, mặc định là hostname."""
    return os.getenv("FINAGENT_WORKER_NAME") or socket.gethostname()


def _register_worker(kind: str, detail: str = "") -> None:
    """Đăng ký máy con vào Redis để máy chủ biết máy nào đang online."""
    name = _worker_name()
    client = get_redis()
    try:
        client.sadd("finagent:workers", name)
        client.hset(
            f"finagent:worker:{name}",
            mapping={
                "hostname": socket.gethostname(),
                "last_seen": str(int(time.time())),
                "last_kind": kind,
                "detail": detail[:200],
                "pid": str(os.getpid()),
            },
        )
        client.expire(f"finagent:worker:{name}", 600)
    except Exception as exc:  # noqa: BLE001 - mất Redis tạm thời không nên làm chết tác vụ
        logger.warning("Không đăng ký được máy con %s: %s", name, exc)


def _timed(fn, *args, **kwargs) -> tuple[list[dict], list[str], int]:
    """Chạy một hàm thu thập và đo thời gian thực thi (ms)."""
    started = time.monotonic()
    items, errors = fn(*args, **kwargs)
    return items, errors, int((time.monotonic() - started) * 1000)


# ---------------------------------------------------------------------------
# Tác vụ thu thập
# ---------------------------------------------------------------------------

@celery_app.task(name="finagent.tasks.crawl_crypto")
def crawl_crypto(symbols: list[str] | None = None) -> dict:
    """Máy con lấy giá crypto từ Binance."""
    worker = _worker_name()
    with crawl_slot() as acquired:
        if not acquired:
            return {"worker": worker, "kind": "price", "skipped": True, "items": [], "errors": ["hết slot"]}
        _register_worker("crawl_crypto", ",".join(symbols or settings.crypto_symbols))
        items, errors, duration = _timed(
            crypto.collect, symbols or settings.crypto_symbols, worker=worker
        )
    logger.info("[%s] crypto: %d mẫu giá, %d lỗi, %dms", worker, len(items), len(errors), duration)
    return {"worker": worker, "kind": "price", "items": items, "errors": errors, "duration_ms": duration}


@celery_app.task(name="finagent.tasks.crawl_vn_stock")
def crawl_vn_stock(symbols: list[str] | None = None) -> dict:
    """Máy con lấy giá chứng khoán Việt Nam."""
    worker = _worker_name()
    with crawl_slot() as acquired:
        if not acquired:
            return {"worker": worker, "kind": "price", "skipped": True, "items": [], "errors": ["hết slot"]}
        _register_worker("crawl_vn_stock", ",".join(symbols or settings.vn_symbols))
        items, errors, duration = _timed(
            vnstock.collect, symbols or settings.vn_symbols, worker=worker
        )
    logger.info("[%s] vn_stock: %d mẫu giá, %d lỗi, %dms", worker, len(items), len(errors), duration)
    return {"worker": worker, "kind": "price", "items": items, "errors": errors, "duration_ms": duration}


@celery_app.task(name="finagent.tasks.crawl_gold")
def crawl_gold(keys: list[str] | None = None) -> dict:
    """Máy con lấy giá vàng trong nước và thế giới."""
    worker = _worker_name()
    with crawl_slot() as acquired:
        if not acquired:
            return {"worker": worker, "kind": "price", "skipped": True, "items": [], "errors": ["hết slot"]}
        _register_worker("crawl_gold", ",".join(keys or settings.gold_symbols))
        items, errors, duration = _timed(gold.collect, keys or settings.gold_symbols, worker=worker)
    logger.info("[%s] gold: %d mẫu giá, %d lỗi, %dms", worker, len(items), len(errors), duration)
    return {"worker": worker, "kind": "price", "items": items, "errors": errors, "duration_ms": duration}


@celery_app.task(name="finagent.tasks.crawl_news")
def crawl_news(sources: list[str] | None = None, max_items: int | None = None) -> dict:
    """Máy con cào tin tức từ một nhóm nguồn RSS được máy chủ chỉ định."""
    worker = _worker_name()
    with crawl_slot() as acquired:
        if not acquired:
            return {"worker": worker, "kind": "news", "skipped": True, "items": [], "errors": ["hết slot"]}
        _register_worker("crawl_news", ",".join(sources or ["tất cả nguồn"]))
        items, errors, duration = _timed(
            news.collect,
            max_items or settings.news_max_items,
            worker=worker,
            sources=sources,
        )
    logger.info("[%s] news: %d bài, %d lỗi, %dms", worker, len(items), len(errors), duration)
    return {"worker": worker, "kind": "news", "items": items, "errors": errors, "duration_ms": duration}


@celery_app.task(name="finagent.tasks.ping")
def ping() -> dict:
    """Tác vụ kiểm tra kết nối — dùng khi cài đặt máy con mới."""
    worker = _worker_name()
    _register_worker("ping", "kiểm tra kết nối")
    return {
        "worker": worker,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "pong": True,
    }
