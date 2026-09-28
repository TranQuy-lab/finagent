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
from concurrent.futures import ThreadPoolExecutor

from finagent import storage
from finagent.celery_app import cluster_status, dispatch
from finagent.collectors import news as news_collector
from finagent.config import settings
from finagent.decision import engine, token_meter
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

def _safe_analyse(symbol: str, run_llm: bool):
    """Phân tích một mã, không để lỗi của mã này chặn các mã khác.

    Chạy được trong luồng riêng: chỉ đọc cơ sở dữ liệu rồi gọi mạng, không giữ
    trạng thái dùng chung. Việc ghi vào cơ sở dữ liệu do luồng chính làm sau.
    """
    try:
        return engine.build_proposal(symbol, run_llm=run_llm)
    except Exception:  # noqa: BLE001 - một mã lỗi không chặn các mã khác
        logger.exception("Lỗi khi phân tích %s", symbol)
        return None


def _prefilter_symbols(symbols: list[str]) -> list[str]:
    """Chỉ giữ lại những mã đáng bỏ ra 13 phút chạy LLM.

    Một lượt phân tích đầy đủ tốn khoảng 13 phút và một lượng hạn mức API đáng kể.
    Chạy cho cả 7 mã một cách mù quáng là lãng phí phần lớn thời gian, vì phần lớn
    mã đang ở vùng trung tính và kết luận sẽ là "đứng ngoài" dù phân tích kỹ thế nào.

    Giữ lại mã khi thoả **một trong hai**:

    1. **Tín hiệu ML ra khỏi vùng trung tính** — có gì đó đáng xem.
    2. **Đang giữ vị thế ở mã đó** — lúc này luôn cần phân tích kỹ, vì câu hỏi
       không còn là "có nên vào không" mà là "có nên thoát không". Bỏ qua mã đang
       giữ tiền là rủi ro thật, không phải tiết kiệm.

    Nếu không lọc được mã nào (ví dụ chưa có dữ liệu giá), trả về **toàn bộ** danh
    sách — thà chậm còn hơn bỏ sót.
    """
    from finagent.broker import get_broker
    from finagent.decision import ml_model, vendor

    band = settings.ml_neutral_band
    kept: list[str] = []
    skipped: list[str] = []

    try:
        broker = get_broker()
        held = {position["symbol"] for position in broker.list_positions()}
    except Exception as exc:  # noqa: BLE001 - không biết đang giữ gì thì phân tích hết
        logger.warning("Không đọc được vị thế để lọc trước: %s — phân tích toàn bộ.", exc)
        return symbols

    for symbol in symbols:
        if symbol.upper() in {s.upper() for s in held}:
            kept.append(symbol)
            continue

        try:
            history = vendor.daily_history(symbol, days=400)
            result = ml_model.predict_from_history(history)
        except Exception as exc:  # noqa: BLE001 - không chạy được ML thì cứ phân tích
            logger.debug("Lọc trước %s: không chạy được ML (%s) — vẫn phân tích.", symbol, exc)
            kept.append(symbol)
            continue

        if not result.get("available"):
            kept.append(symbol)
            continue

        probability = float(result.get("probability", 0.5))
        if abs(probability - 0.5) >= band:
            kept.append(symbol)
        else:
            skipped.append(symbol)

    if not kept:
        logger.info("Lọc trước: không mã nào vượt vùng trung tính — phân tích toàn bộ để chắc chắn.")
        return symbols

    logger.info(
        "Lọc trước bằng ML: phân tích %d/%d mã (%s). Bỏ qua %d mã trung tính: %s",
        len(kept), len(symbols), ", ".join(kept), len(skipped),
        ", ".join(skipped) if skipped else "không có",
    )
    return kept


def scan_market(run_llm: bool = True, notify: bool = True) -> int:
    """Phân tích các mã đang theo dõi và gửi đề xuất cần duyệt qua Telegram.

    Trả về số đề xuất đã gửi cho người dùng.
    """
    # Vàng chỉ có tin tức, không có chuỗi nến ngày nên không đưa vào mô hình.
    symbols = list(settings.crypto_symbols) + list(settings.vn_symbols)

    notifier = TelegramNotifier() if notify else None
    sent = 0
    started = time.monotonic()

    # Ngoài giờ giao dịch thì chứng khoán Việt Nam không có gì mới để phân tích.
    # Bỏ trước khi lọc bằng ML để khỏi chạy cả bước lọc cho mã chắc chắn bị loại.
    symbols = filter_by_market_hours(symbols)

    # Lọc trước bằng ML rẻ tiền để khỏi tốn 13 phút LLM cho mã đang ở vùng trung tính.
    if run_llm and settings.llm_prefilter:
        symbols = _prefilter_symbols(symbols)

    # Mỗi mã là một lượt phân tích đầy đủ 12 tác nhân, tốn khoảng 13 phút. Chạy
    # tuần tự cả 7 mã mất hơn một tiếng rưỡi. Các lời gọi này chờ mạng là chính nên
    # chạy song song cho tốc độ gần như nhân lên theo số luồng.
    parallelism = max(1, settings.scan_parallelism) if run_llm else 1

    if parallelism > 1 and len(symbols) > 1:
        logger.info(
            "Quét %d mã, chạy song song %d luồng.", len(symbols), parallelism
        )
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            analysed = list(pool.map(lambda s: _safe_analyse(s, run_llm), symbols))
    else:
        analysed = [_safe_analyse(symbol, run_llm) for symbol in symbols]

    logger.info(
        "Đã phân tích %d mã trong %.1f phút.", len(symbols), (time.monotonic() - started) / 60
    )

    for proposal in analysed:
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
    from datetime import datetime, timedelta

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="Asia/Ho_Chi_Minh")

    # Chạy ngay lượt đầu thay vì chờ hết một chu kỳ.
    #
    # ``add_job`` kiểu ``interval`` mặc định chờ hết chu kỳ đầu mới chạy lần đầu.
    # Với nhịp quét 30 phút, khởi động lại dịch vụ là phải ngồi chờ nửa tiếng mới
    # thấy kết quả — người dùng không phân biệt được là đang chờ hay đã hỏng.
    #
    # Thu thập chạy sau 10 giây, quét chạy sau 2 phút: phải có dữ liệu giá và tin
    # tức trong kho trước thì quét mới có ý nghĩa.
    now = datetime.now()

    # Bật bộ đếm token cho cả tiến trình. Nhờ vậy log mỗi lượt quét đều có số token
    # thật, không phải ước lượng.
    token_meter.install_global_callback()

    # Thu thập tin tức thường xuyên hơn vì tin tức thay đổi liên tục.
    scheduler.add_job(
        collect_once, "interval", seconds=settings.news_interval,
        id="collect", name="Thu thập dữ liệu từ máy con", max_instances=1, coalesce=True,
        next_run_time=now + timedelta(seconds=10),
    )
    # Quét thị trường và ra quyết định theo nhịp chậm hơn (tốn token LLM).
    scheduler.add_job(
        lambda: scan_market(run_llm=True), "interval", seconds=settings.monitor_interval,
        id="scan", name="Quét thị trường và ra quyết định", max_instances=1, coalesce=True,
        next_run_time=now + timedelta(seconds=120),
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


# ---------------------------------------------------------------------------
# Giờ giao dịch
# ---------------------------------------------------------------------------

def _vietnam_now():
    """Thời điểm hiện tại theo giờ Việt Nam (UTC+7)."""
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=7)))


def in_market_hours(now=None) -> bool:
    """Sàn chứng khoán Việt Nam có đang mở cửa không.

    Mở 9:00–15:00 các ngày trong tuần. Nghỉ trưa 11:30–13:00 vẫn tính là trong giờ
    vì dữ liệu phiên sáng còn dùng được, và phiên chiều bắt đầu ngay sau đó.

    Không xét ngày lễ — lịch nghỉ lễ thay đổi theo năm và không có nguồn miễn phí
    đáng tin. Ngày lễ chỉ khiến hệ thống phân tích thừa một lượt, không gây hại.
    """
    from datetime import time as clock

    current = now or _vietnam_now()
    if current.weekday() >= 5:      # thứ Bảy, Chủ nhật
        return False

    try:
        open_h, open_m = (int(part) for part in settings.market_open.split(":"))
        close_h, close_m = (int(part) for part in settings.market_close.split(":"))
    except ValueError:
        logger.warning(
            "FINAGENT_MARKET_OPEN/CLOSE sai định dạng (%r, %r) — coi như luôn trong giờ.",
            settings.market_open, settings.market_close,
        )
        return True

    return clock(open_h, open_m) <= current.time() <= clock(close_h, close_m)


def filter_by_market_hours(symbols: list[str]) -> list[str]:
    """Bỏ chứng khoán Việt Nam ra khỏi danh sách khi sàn đã đóng cửa.

    Crypto và vàng giữ nguyên: crypto chạy 24/7, còn vàng trong nước niêm yết giá
    tham khảo cả ngày.
    """
    if not settings.market_hours_only or in_market_hours():
        return symbols

    from finagent.decision import vendor

    kept = [s for s in symbols if vendor.detect_asset_class(s) != "vn_stock"]
    dropped = len(symbols) - len(kept)

    if dropped:
        logger.info(
            "Ngoài giờ giao dịch (%s–%s): bỏ %d mã chứng khoán, còn %d mã.",
            settings.market_open, settings.market_close, dropped, len(kept),
        )
    return kept
