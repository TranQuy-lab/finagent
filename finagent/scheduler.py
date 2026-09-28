"""Bộ điều phối của máy chủ: chia việc cho máy con, tổng hợp, rồi ra quyết định.

Một chu kỳ đầy đủ gồm bốn bước:

1. **Chia việc** — máy chủ đẩy các tác vụ thu thập vào Redis. Danh sách nguồn tin
   được chia nhỏ theo số máy con đang online để không máy nào cào trùng máy nào.
2. **Chờ kết quả** — gom kết quả máy con trả về, có thời hạn chờ.
3. **Lưu trữ** — giá và tin tức được ghi vào SQLite, khử trùng lặp.
4. **Ra quyết định** — chạy ML/LLM trên dữ liệu mới, tạo đề xuất, gửi Telegram.
"""

from __future__ import annotations

import logging
import time

from finagent import storage
from finagent.celery_app import cluster_status, dispatch
from finagent.collectors import news as news_collector
from finagent.config import settings
from finagent.decision import engine
from finagent.telegram_bot import TelegramNotifier
from finagent.tasks import crawl_crypto, crawl_gold, crawl_news, crawl_vn_stock

logger = logging.getLogger(__name__)

#: Chờ tối đa bao lâu cho một lượt thu thập trước khi coi như máy con không phản hồi.
RESULT_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Bước 1–3: thu thập
# ---------------------------------------------------------------------------

def _online_worker_count() -> int:
    """Số máy con đang hoạt động, tối thiểu 1 để phép chia luôn hợp lệ."""
    try:
        return max(len(cluster_status()["workers"]), 1)
    except Exception:  # noqa: BLE001 - Redis lỗi thì coi như một máy
        return 1


def dispatch_collection() -> list:
    """Đẩy toàn bộ tác vụ thu thập vào hàng đợi cho máy con.

    Nguồn tin được chia theo số máy con đang online, nhờ vậy thêm máy con là
    tăng tốc độ cào mà không sinh việc trùng lặp.
    """
    workers = _online_worker_count()
    source_groups = news_collector.chunk_sources(workers)

    tasks = [
        dispatch(crawl_crypto, settings.crypto_symbols),
        dispatch(crawl_vn_stock, settings.vn_symbols),
        dispatch(crawl_gold, settings.gold_symbols),
    ]
    for group in source_groups:
        tasks.append(dispatch(crawl_news, group, settings.news_max_items))

    logger.info(
        "Đã đẩy %d tác vụ cho %d máy con (nhóm nguồn tin: %s).",
        len(tasks), workers, [len(g) for g in source_groups],
    )
    return tasks


def gather_results(tasks: list, timeout: int = RESULT_TIMEOUT) -> dict:
    """Chờ và tổng hợp kết quả từ máy con; lưu vào cơ sở dữ liệu.

    Trả về thống kê: số mẫu giá, số bài tin mới, số lỗi, danh sách máy con.
    """
    prices = 0
    news_new = 0
    errors: list[str] = []
    workers: set[str] = set()
    skipped = 0
    deadline = time.monotonic() + timeout

    for task in tasks:
        remaining = max(deadline - time.monotonic(), 1)
        try:
            payload = task.get(timeout=remaining)
        except Exception as exc:  # noqa: BLE001 - một tác vụ hỏng không chặn cả lượt
            errors.append(f"tác vụ không trả kết quả: {exc}")
            continue

        if not isinstance(payload, dict):
            continue

        worker = payload.get("worker", "unknown")
        workers.add(worker)

        if payload.get("skipped"):
            skipped += 1
            continue

        items = payload.get("items") or []
        kind = payload.get("kind")
        errors.extend(payload.get("errors") or [])

        if kind == "price":
            prices += storage.save_prices(items)
        elif kind == "news":
            news_new += storage.save_news(items)

        storage.log_worker_event(
            worker, f"thu thập {kind}", f"{len(items)} bản ghi, {payload.get('duration_ms', 0)}ms"
        )

    stats = {
        "prices": prices,
        "news_new": news_new,
        "errors": errors,
        "workers": sorted(workers),
        "skipped": skipped,
    }
    logger.info(
        "Tổng hợp xong: +%d giá, +%d tin mới, %d lỗi, %d máy con tham gia, %d bỏ qua vì hết slot.",
        prices, news_new, len(errors), len(workers), skipped,
    )
    return stats


def collect_once(timeout: int = RESULT_TIMEOUT) -> dict:
    """Chạy trọn một lượt thu thập (chia việc → chờ → lưu)."""
    return gather_results(dispatch_collection(), timeout=timeout)


# ---------------------------------------------------------------------------
# Bước 4: ra quyết định
# ---------------------------------------------------------------------------

def scan_market(run_llm: bool = True, notify: bool = True) -> int:
    """Phân tích các mã đang theo dõi và gửi đề xuất cần duyệt qua Telegram.

    Trả về số đề xuất đã gửi cho người dùng.
    """
    # Vàng chỉ có tin tức, không có chuỗi nến ngày nên không đưa vào mô hình.
    symbols = list(settings.crypto_symbols) + list(settings.vn_symbols)

    notifier = TelegramNotifier() if notify else None
    sent = 0

    for symbol in symbols:
        try:
            proposal = engine.build_proposal(symbol, run_llm=run_llm)
        except Exception:  # noqa: BLE001 - một mã lỗi không chặn các mã khác
            logger.exception("Lỗi khi phân tích %s", symbol)
            continue

        if proposal is None:
            continue

        engine.save_proposal(proposal)
        logger.info(
            "Đề xuất %s: %s (%s, tin cậy %.2f, %s)",
            proposal.symbol, proposal.action, proposal.signal,
            proposal.confidence, proposal.source,
        )

        if not proposal.actionable:
            continue

        if notifier and notifier.enabled:
            message_id = notifier.notify_proposal(proposal.to_dict())
            if message_id:
                sent += 1
                storage.get_conn().execute(
                    "UPDATE proposals SET telegram_msg = ? WHERE id = ?",
                    (message_id, proposal.id),
                )
                storage.get_conn().commit()
        else:
            logger.warning(
                "Đề xuất %s đủ điều kiện nhưng Telegram chưa cấu hình — bỏ qua thông báo.",
                proposal.symbol,
            )
            sent += 1

    return sent


def expire_stale_proposals() -> int:
    """Đánh dấu hết hạn các đề xuất chờ duyệt quá lâu."""
    from finagent.collectors.base import utcnow_iso
    from finagent.storage import transaction

    cutoff_seconds = settings.approval_timeout
    with transaction() as conn:
        rows = conn.execute(
            "SELECT id, created_at FROM proposals WHERE status = 'pending'"
        ).fetchall()

        expired = 0
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
            except ValueError:
                continue
            if (now - created).total_seconds() > cutoff_seconds:
                conn.execute(
                    "UPDATE proposals SET status = 'expired', decided_at = ?, decided_by = 'system' WHERE id = ?",
                    (utcnow_iso(), row["id"]),
                )
                expired += 1

    if expired:
        logger.info("Đã đánh dấu hết hạn %d đề xuất quá thời gian chờ duyệt.", expired)
    return expired


# ---------------------------------------------------------------------------
# Vòng lặp định kỳ
# ---------------------------------------------------------------------------

def run_cycle(run_llm: bool = True) -> dict:
    """Một chu kỳ đầy đủ: thu thập → hết hạn đề xuất cũ → quét thị trường."""
    stats = collect_once()
    expire_stale_proposals()
    stats["proposals_sent"] = scan_market(run_llm=run_llm)
    return stats


def run_scheduler() -> None:
    """Chạy bộ giám sát định kỳ cho tới khi bị dừng."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="Asia/Ho_Chi_Minh")

    # Thu thập tin tức thường xuyên hơn vì tin tức thay đổi liên tục.
    scheduler.add_job(
        collect_once, "interval", seconds=settings.news_interval,
        id="collect", name="Thu thập dữ liệu từ máy con", max_instances=1, coalesce=True,
    )
    # Quét thị trường và ra quyết định theo nhịp chậm hơn (tốn token LLM).
    scheduler.add_job(
        lambda: scan_market(run_llm=True), "interval", seconds=settings.monitor_interval,
        id="scan", name="Quét thị trường và ra quyết định", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        expire_stale_proposals, "interval", seconds=300,
        id="expire", name="Hết hạn đề xuất cũ", max_instances=1, coalesce=True,
    )

    logger.info(
        "Bộ giám sát đã chạy: thu thập mỗi %ds, quét thị trường mỗi %ds.",
        settings.news_interval, settings.monitor_interval,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Đã dừng bộ giám sát.")
