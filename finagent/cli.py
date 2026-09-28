"""Giao diện dòng lệnh của FinAgent.

Dùng cho cả máy chủ và máy con:

* Máy chủ: ``init``, ``status``, ``collect``, ``scan``, ``run``, ``bot``, ``positions``
* Máy con:  ``worker``, ``ping``

Ví dụ::

    finagent status          # xem cụm máy con và kho dữ liệu
    finagent collect         # một lượt thu thập phân tán
    finagent scan --no-llm   # quét thị trường chỉ bằng mô hình ML
    finagent run             # chạy bộ giám sát định kỳ
    finagent worker          # (trên máy con) lắng nghe hàng đợi và thu thập
"""

from __future__ import annotations

import argparse
import logging
import sys

from finagent import storage
from finagent.collectors.base import utcnow_iso
from finagent.config import settings


def _setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or settings.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Các lệnh
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    """Khởi tạo cơ sở dữ liệu và in cảnh báo cấu hình."""
    storage.init_db()
    print(f"✅ Đã khởi tạo cơ sở dữ liệu: {settings.db_path}")

    warnings = settings.validate()
    if warnings:
        print("\n⚠️  Cảnh báo cấu hình:")
        for warning in warnings:
            print(f"   • {warning}")
    else:
        print("✅ Cấu hình đầy đủ.")

    print("\n📋 Thông số chính:")
    print(f"   • Slot thu thập tối đa : {settings.max_concurrency}")
    print(f"   • Hàng đợi Celery      : {settings.crawl_queue}")
    print(f"   • Redis                : {settings.redis_url}")
    print(f"   • Chế độ giao dịch     : {settings.trading_mode}")
    print(f"   • Mô hình LLM          : {settings.llm_provider} / {settings.deep_think_llm}")
    print(f"   • Crypto theo dõi      : {', '.join(settings.crypto_symbols)}")
    print(f"   • Chứng khoán theo dõi : {', '.join(settings.vn_symbols)}")
    print(f"   • Vàng theo dõi        : {', '.join(settings.gold_symbols)}")
    return 0


def cmd_status(args) -> int:
    """In trạng thái cụm máy con và kho dữ liệu."""
    from finagent.celery_app import cluster_status

    conn = storage.get_conn()
    print("=" * 60)
    print("  TRẠNG THÁI HỆ THỐNG FINAGENT")
    print("=" * 60)

    print("\n🖥️  CỤM MÁY CON")
    try:
        status = cluster_status()
        print(f"   Slot đang dùng: {status['slots_in_use']}/{status['slots_total']}")
        print(f"   Hàng đợi      : {status['queue']}")
        if status["workers"]:
            for worker in status["workers"]:
                print(f"   • {worker['name']:<16} host={worker.get('hostname', '?'):<28} "
                      f"việc cuối={worker.get('last_kind', '?')}")
        else:
            print("   ⚠️  Chưa có máy con nào đăng ký.")
    except Exception as exc:  # noqa: BLE001
        print(f"   ❌ Không kết nối được Redis: {exc}")
        print("      Hãy chắc chắn Redis đang chạy và REDIS_URL đúng.")

    print("\n📦 KHO DỮ LIỆU")
    print(f"   Mẫu giá   : {conn.execute('SELECT COUNT(*) n FROM prices').fetchone()['n']}")
    print(f"   Bài tin   : {conn.execute('SELECT COUNT(*) n FROM news').fetchone()['n']}")
    print(f"   Đề xuất   : {conn.execute('SELECT COUNT(*) n FROM proposals').fetchone()['n']}")
    print(f"   Lệnh      : {conn.execute('SELECT COUNT(*) n FROM orders').fetchone()['n']}")

    topics = storage.news_topic_counts()
    if topics:
        print(f"   Chủ đề    : {', '.join(f'{k}={v}' for k, v in sorted(topics.items()))}")

    print("\n💼 DANH MỤC")
    from finagent.broker.paper import PaperBroker

    broker = PaperBroker()
    for currency, amount in sorted(broker.all_cash().items()):
        print(f"   Ví {currency:<5}: {amount:>18,.2f}")
    positions = broker.list_positions()
    if positions:
        for position in positions:
            currency = position.get("currency") or "VND"
            print(f"   • {position['symbol']:<10} {float(position['quantity']):>12g} "
                  f"@ {float(position['avg_price']):>12,.2f} {currency}")
    else:
        print("   (chưa có vị thế)")

    pending = conn.execute("SELECT COUNT(*) n FROM proposals WHERE status='pending'").fetchone()["n"]
    print(f"\n⏳ Đề xuất chờ duyệt: {pending}")
    return 0


def cmd_collect(args) -> int:
    """Chạy một lượt thu thập phân tán."""
    from finagent.scheduler import collect_once

    storage.init_db()
    print("🔄 Đang đẩy việc cho các máy con…")
    stats = collect_once(timeout=args.timeout)

    print(f"\n✅ Xong sau khi tổng hợp:")
    print(f"   Máy con tham gia : {', '.join(stats['workers']) or '(không có)'}")
    print(f"   Mẫu giá mới      : {stats['prices']}")
    print(f"   Bài tin mới      : {stats['news_new']}")
    print(f"   Bỏ qua (hết slot): {stats['skipped']}")
    print(f"   Lỗi              : {len(stats['errors'])}")
    for error in stats["errors"][:5]:
        print(f"     - {error[:140]}")

    if not stats["workers"]:
        print("\n⚠️  Không máy con nào phản hồi. Kiểm tra: finagent status")
        return 1
    return 0


def cmd_scan(args) -> int:
    """Quét thị trường và tạo đề xuất."""
    storage.init_db()
    from finagent.scheduler import scan_market

    run_llm = not args.no_llm
    if run_llm and not settings.llm_enabled:
        print(f"⚠️  Chưa có khoá API cho {settings.llm_provider!r} — tự động chuyển sang chế độ chỉ dùng ML.")
        run_llm = False

    print(f"🔍 Đang quét {len(settings.crypto_symbols) + len(settings.vn_symbols)} mã "
          f"(LLM: {'có' if run_llm else 'không'})…")
    sent = scan_market(run_llm=run_llm, notify=not args.no_notify)
    print(f"\n✅ Đã tạo đề xuất. Số đề xuất gửi duyệt: {sent}")
    print("   Xem chi tiết: finagent status")
    return 0


def cmd_positions(args) -> int:
    """In danh mục mô phỏng hiện tại."""
    from finagent.broker.paper import PaperBroker
    from finagent.telegram_bot import format_positions

    storage.init_db()
    print(format_positions(PaperBroker()))
    return 0


def cmd_pending(args) -> int:
    """Liệt kê đề xuất đang chờ duyệt."""
    storage.init_db()
    rows = storage.get_conn().execute(
        "SELECT * FROM proposals WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("Không có đề xuất nào đang chờ duyệt.")
        return 0
    print(f"{len(rows)} đề xuất đang chờ duyệt:\n")
    for row in rows:
        print(f"  #{row['id']:<4} {row['symbol']:<10} {row['action']:<5} {row['signal']:<11} "
              f"tin cậy={row['confidence']:.2f} kl={row['quantity']:g} "
              f"giá trị={row['amount']:,.0f} ({row['created_at']})")
    return 0


def cmd_run(args) -> int:
    """Chạy bộ giám sát định kỳ."""
    storage.init_db()
    from finagent.scheduler import run_scheduler

    warnings = settings.validate()
    for warning in warnings:
        print(f"⚠️  {warning}")
    run_scheduler()
    return 0


def cmd_bot(args) -> int:
    """Chạy bot Telegram (duyệt lệnh, xem trạng thái)."""
    storage.init_db()
    from finagent.telegram_bot import run_bot

    run_bot()
    return 0


def cmd_worker(args) -> int:
    """Chạy máy con: lắng nghe hàng đợi và thực thi việc thu thập."""
    import os
    import socket

    from finagent.celery_app import celery_app

    name = args.name or os.getenv("FINAGENT_WORKER_NAME") or socket.gethostname()
    os.environ["FINAGENT_WORKER_NAME"] = name

    print(f"🖥️  Máy con '{name}' đang khởi động…")
    print(f"   Redis   : {settings.redis_url}")
    print(f"   Hàng đợi: {settings.crawl_queue}")
    print(f"   Slot tối đa toàn cụm: {settings.max_concurrency}")
    print(f"   Số tiến trình con    : {settings.worker_concurrency}")

    argv = [
        "worker",
        f"--loglevel={settings.log_level}",
        f"--concurrency={settings.worker_concurrency}",
        "-Q", settings.crawl_queue,
        f"-n={name}@%h",
    ]
    celery_app.worker_main(argv)
    return 0


def cmd_ping(args) -> int:
    """Gửi một tác vụ kiểm tra để xác nhận máy con đã kết nối."""
    from finagent.tasks import ping

    storage.init_db()
    print("📡 Đang gửi tác vụ kiểm tra tới hàng đợi…")
    try:
        result = ping.apply_async(queue=settings.crawl_queue).get(timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Không nhận được phản hồi: {exc}")
        print("   Kiểm tra Redis và xem máy con đã chạy chưa.")
        return 1
    print(f"✅ Máy con phản hồi: {result['worker']} (host={result['hostname']}, pid={result['pid']})")
    return 0


# ---------------------------------------------------------------------------
# Bộ phân tích tham số
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finagent",
        description="FinAgent — hệ thống đa tác nhân thu thập tin tức và ra quyết định tài chính.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--log-level", default=None, help="Mức log: DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Khởi tạo cơ sở dữ liệu và kiểm tra cấu hình").set_defaults(func=cmd_init)
    sub.add_parser("status", help="Xem trạng thái cụm máy con và kho dữ liệu").set_defaults(func=cmd_status)
    sub.add_parser("positions", help="Xem danh mục mô phỏng").set_defaults(func=cmd_positions)
    sub.add_parser("pending", help="Liệt kê đề xuất đang chờ duyệt").set_defaults(func=cmd_pending)

    p_collect = sub.add_parser("collect", help="Chạy một lượt thu thập phân tán")
    p_collect.add_argument("--timeout", type=int, default=120, help="Thời gian chờ máy con (giây)")
    p_collect.set_defaults(func=cmd_collect)

    p_scan = sub.add_parser("scan", help="Quét thị trường và tạo đề xuất")
    p_scan.add_argument("--no-llm", action="store_true", help="Chỉ dùng mô hình ML, không gọi LLM")
    p_scan.add_argument("--no-notify", action="store_true", help="Không gửi Telegram")
    p_scan.set_defaults(func=cmd_scan)

    sub.add_parser("run", help="Chạy bộ giám sát định kỳ (máy chủ)").set_defaults(func=cmd_run)
    sub.add_parser("bot", help="Chạy bot Telegram").set_defaults(func=cmd_bot)

    p_worker = sub.add_parser("worker", help="Chạy máy con thu thập")
    p_worker.add_argument("--name", default=None, help="Tên máy con (mặc định lấy hostname)")
    p_worker.set_defaults(func=cmd_worker)

    p_ping = sub.add_parser("ping", help="Kiểm tra kết nối tới máy con")
    p_ping.add_argument("--timeout", type=int, default=30, help="Thời gian chờ (giây)")
    p_ping.set_defaults(func=cmd_ping)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n⏹️  Đã dừng theo yêu cầu.")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI nên báo lỗi gọn gàng
        logging.exception("Lệnh thất bại")
        print(f"\n❌ Lỗi: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
