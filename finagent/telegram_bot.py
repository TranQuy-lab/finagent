"""Bot Telegram: báo cáo cho người dùng và xin duyệt lệnh.

Hai phần tách biệt trong cùng một module:

* :class:`TelegramNotifier` — gửi tin **đồng bộ** bằng HTTP, dùng từ scheduler
  và bộ máy ra quyết định (vốn chạy đồng bộ).
* :func:`run_bot` — bot **bất đồng bộ** lắng nghe nút bấm Duyệt/Từ chối của
  người dùng rồi thực thi lệnh tương ứng.

Nhờ tách đôi như vậy, phần gửi tin không phụ thuộc vòng lặp sự kiện của bot.
"""

from __future__ import annotations

import asyncio
import logging

import requests

from finagent.config import settings
from finagent.decision import engine

logger = logging.getLogger(__name__)

API_BASE = "{base}/bot{token}/{method}"
REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Định dạng tin nhắn
# ---------------------------------------------------------------------------

def _money(amount: float, currency: str) -> str:
    """Định dạng tiền tệ cho dễ đọc."""
    if currency == "VND":
        return f"{amount:,.0f} ₫"
    return f"{amount:,.2f} {currency}"


SIGNAL_EMOJI = {
    "Buy": "🟢", "Overweight": "🟢", "Hold": "⚪",
    "Underweight": "🔴", "Sell": "🔴", "REVIEW": "⚠️",
}


def format_proposal(proposal: dict) -> str:
    """Soạn nội dung tin nhắn xin duyệt một đề xuất."""
    emoji = SIGNAL_EMOJI.get(proposal["signal"], "⚪")
    action_text = {"buy": "MUA", "sell": "BÁN", "hold": "ĐỨNG NGOÀI"}.get(proposal["action"], proposal["action"])
    amount = float(proposal["amount"])
    currency = proposal.get("currency") or "VND"

    lines = [
        f"{emoji} *ĐỀ XUẤT {action_text}* — `{proposal['symbol']}`",
        "",
        f"• Tín hiệu: *{proposal['signal']}* (tin cậy {float(proposal['confidence']):.0%})",
        f"• Giá: {_money(float(proposal['price']), currency)}",
        f"• Khối lượng: {float(proposal['quantity']):g}",
        f"• Giá trị lệnh: *{_money(amount, currency)}*",
        f"• Nguồn tín hiệu: {proposal.get('source', 'n/a')}",
        f"• Chế độ: {'MÔ PHỎNG' if settings.trading_mode == 'paper' else 'THẬT'}",
    ]
    rationale = (proposal.get("rationale") or "").strip()
    if rationale:
        lines += ["", "📝 *Căn cứ:*", rationale[:900]]

    lines += ["", f"⏳ Đề xuất hết hạn sau {settings.approval_timeout // 60} phút."]
    return "\n".join(lines)


def format_positions(broker) -> str:
    """Bảng vị thế và số dư từng ví tiền tệ."""
    positions = broker.list_positions()
    wallets = broker.all_cash()

    lines = ["💼 *DANH MỤC HIỆN TẠI*", "", "*Số dư từng ví:*"]
    for currency, amount in sorted(wallets.items()):
        lines.append(f"• {currency}: *{_money(amount, currency)}*")

    lines.append("")
    if not positions:
        lines.append("_Chưa có vị thế nào._")
    else:
        lines.append("*Vị thế đang mở:*")
        for position in positions:
            currency = position.get("currency") or "VND"
            value = float(position["quantity"]) * float(position["avg_price"])
            lines.append(
                f"• `{position['symbol']}` — {float(position['quantity']):g} "
                f"@ {_money(float(position['avg_price']), currency)} "
                f"= {_money(value, currency)}"
            )
    return "\n".join(lines)


def format_status_report() -> str:
    """Báo cáo tổng quan: máy con, số liệu thu thập, đề xuất."""
    from finagent.celery_app import cluster_status
    from finagent.storage import get_conn, news_topic_counts

    conn = get_conn()
    price_count = conn.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"]
    news_count = conn.execute("SELECT COUNT(*) n FROM news").fetchone()["n"]
    order_count = conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]

    topics = news_topic_counts()
    topic_text = ", ".join(f"{k}: {v}" for k, v in sorted(topics.items())) or "chưa có"

    lines = [
        "📊 *TRẠNG THÁI HỆ THỐNG FINAGENT*",
        "",
        f"• Mẫu giá đã thu thập: *{price_count}*",
        f"• Bài tin tức: *{news_count}*",
        f"• Lệnh đã đặt: *{order_count}*",
        f"• Tin theo chủ đề: {topic_text}",
    ]

    try:
        status = cluster_status()
        lines += [
            "",
            f"🖥️ *Cụm máy con* — slot dùng {status['slots_in_use']}/{status['slots_total']}",
        ]
        if status["workers"]:
            for worker in status["workers"]:
                lines.append(f"• `{worker['name']}` — {worker.get('last_kind', '?')}")
        else:
            lines.append("_Chưa có máy con nào đăng ký._")
    except Exception as exc:  # noqa: BLE001 - Redis lỗi không nên chặn báo cáo
        lines += ["", f"⚠️ Không đọc được trạng thái cụm: {exc}"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gửi tin đồng bộ (dùng từ scheduler / engine)
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Gửi tin nhắn Telegram bằng HTTP đồng bộ."""

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 api_base: str | None = None) -> None:
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        # Cho phép đổi địa chỉ API: dùng Bot API tự lưu trữ, hoặc trỏ vào máy chủ
        # giả lập trong test để kiểm chứng luồng gửi tin mà không cần token thật.
        self.api_base = (api_base or settings.telegram_api_base).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, payload: dict) -> dict | None:
        if not self.enabled:
            logger.debug("Bỏ qua gửi Telegram vì thiếu token/chat_id.")
            return None
        try:
            response = requests.post(
                API_BASE.format(base=self.api_base, token=self.token, method=method),
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            data = response.json()
            if not data.get("ok"):
                logger.warning("Telegram %s lỗi: %s", method, data.get("description"))
                return None
            return data.get("result")
        except Exception as exc:  # noqa: BLE001 - mất mạng không được làm sập hệ thống
            logger.warning("Không gọi được Telegram %s: %s", method, exc)
            return None

    def send(self, text: str, with_buttons_for: int | None = None) -> int | None:
        """Gửi tin nhắn; nếu có ``with_buttons_for`` thì kèm nút Duyệt/Từ chối."""
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if with_buttons_for is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": "✅ DUYỆT MUA/BÁN", "callback_data": f"approve:{with_buttons_for}"},
                    {"text": "❌ TỪ CHỐI", "callback_data": f"reject:{with_buttons_for}"},
                ]]
            }
        result = self._call("sendMessage", payload)
        if result is None:
            return None
        message_id = result.get("message_id")
        logger.info("Đã gửi tin Telegram (message_id=%s)", message_id)
        return message_id

    def notify_proposal(self, proposal: dict) -> int | None:
        """Gửi đề xuất kèm nút duyệt, trả về message_id."""
        return self.send(format_proposal(proposal), with_buttons_for=int(proposal["id"]))


# ---------------------------------------------------------------------------
# Bot bất đồng bộ: lắng nghe nút bấm và lệnh
# ---------------------------------------------------------------------------

def _handle_decision(proposal_id: int, approved: bool, user: str) -> str:
    """Xử lý quyết định của người dùng: duyệt thì đặt lệnh ngay.

    Chống xử lý trùng: nếu đề xuất đã được quyết định từ trước (người dùng bấm
    hai lần, hoặc Telegram gửi lại callback), trả về thông báo và **không** đặt
    thêm lệnh.
    """
    if approved:
        status_text = f"✅ *ĐÃ DUYỆT* đề xuất #{proposal_id}"
    else:
        status_text = f"❌ *ĐÃ TỪ CHỐI* đề xuất #{proposal_id}"

    result = engine.resolve_proposal(proposal_id, approved, decided_by=user)
    if result is None:
        return f"⚠️ Không tìm thấy đề xuất #{proposal_id}."

    if result.get("already_decided"):
        return (
            f"⚠️ Đề xuất #{proposal_id} đã được xử lý từ trước "
            f"(trạng thái: *{result['status']}*). Không đặt thêm lệnh."
        )

    if not approved:
        return f"{status_text} (`{result['symbol']}`). Không đặt lệnh."

    outcome = engine.execute_approved(result)
    if outcome["ok"]:
        currency = outcome.get("currency") or "VND"
        return (
            f"✅ *ĐÃ ĐẶT LỆNH* cho đề xuất #{proposal_id}\n\n"
            f"• Mã: `{outcome['symbol']}`\n"
            f"• Hướng: {outcome['side'].upper()}\n"
            f"• Khối lượng: {outcome['quantity']:g}\n"
            f"• Giá khớp: {_money(outcome['price'], currency)}\n"
            f"• Tổng giá trị: {_money(outcome['amount'], currency)}\n"
            f"• Mã lệnh: `{outcome['broker_ref']}`\n"
            f"• Chế độ: {'MÔ PHỎNG' if outcome['status'] == 'filled' else outcome['status']}"
        )
    return f"⚠️ Lệnh #{proposal_id} bị từ chối: {outcome['message']}"


def run_bot() -> None:
    """Chạy bot ở chế độ polling (chặn cho tới khi dừng).

    Nếu chưa cấu hình Telegram, hàm **thoát gọn** thay vì ném lỗi. Ném lỗi sẽ
    khiến systemd khởi động lại liên tục, đầy log bằng cùng một thông báo và che
    mất các vấn đề thật khác.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

    if not settings.telegram_enabled:
        logger.warning(
            "Chưa cấu hình TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID — bỏ qua việc chạy bot. "
            "Điền vào /opt/finagent/.env rồi chạy lại: sudo systemctl start finagent-bot"
        )
        return

    allowed_chat = str(settings.telegram_chat_id)

    def _authorized(update: Update) -> bool:
        """Chỉ chấp nhận lệnh từ đúng chat đã cấu hình."""
        chat = update.effective_chat
        return chat is not None and str(chat.id) == allowed_chat

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await update.message.reply_text(
            "🤖 *FinAgent* đã sẵn sàng.\n\n"
            "Lệnh khả dụng:\n"
            "/status — trạng thái hệ thống và cụm máy con\n"
            "/positions — danh mục hiện tại\n"
            "/pending — các đề xuất đang chờ duyệt\n"
            "/scan — quét thị trường và tạo đề xuất mới\n"
            "/help — trợ giúp",
            parse_mode="Markdown",
        )

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await update.message.reply_text(format_status_report(), parse_mode="Markdown")

    async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        from finagent.broker import get_broker

        await update.message.reply_text(format_positions(get_broker()), parse_mode="Markdown")

    async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        pending = engine.pending_proposals()
        if not pending:
            await update.message.reply_text("Không có đề xuất nào đang chờ duyệt.")
            return
        for proposal in pending:
            await update.message.reply_text(
                format_proposal(proposal),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ DUYỆT", callback_data=f"approve:{proposal['id']}"),
                    InlineKeyboardButton("❌ TỪ CHỐI", callback_data=f"reject:{proposal['id']}"),
                ]]),
            )

    async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        await update.message.reply_text("🔍 Đang quét thị trường, vui lòng chờ…")
        from finagent.scheduler import scan_market

        # Chạy trong luồng riêng để không chặn vòng lặp sự kiện của bot.
        created = await asyncio.to_thread(scan_market, True)
        await update.message.reply_text(
            f"✅ Đã quét xong. Số đề xuất mới chờ duyệt: *{created}*",
            parse_mode="Markdown",
        )

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or str(query.message.chat_id) != allowed_chat:
            return
        await query.answer()

        action, _, raw_id = (query.data or "").partition(":")
        try:
            proposal_id = int(raw_id)
        except ValueError:
            await query.edit_message_text("⚠️ Nút bấm không hợp lệ.")
            return

        user = f"telegram:{query.from_user.id}" if query.from_user else "telegram"
        message = _handle_decision(proposal_id, action == "approve", user)
        try:
            await query.edit_message_text(message, parse_mode="Markdown")
        except Exception:  # noqa: BLE001 - tin nhắn cũ có thể không sửa được
            await query.message.reply_text(message, parse_mode="Markdown")

    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("positions", cmd_positions))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CallbackQueryHandler(on_button))

    logger.info("Bot Telegram đang chạy ở chế độ polling…")
    application.run_polling(allowed_updates=["message", "callback_query"])
