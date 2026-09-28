"""Test luồng Telegram: báo cáo đề xuất, người dùng duyệt, máy chủ đặt lệnh.

Đây là phần "người dùng thao tác quyết định mua hay không" trong đề tài. Test dùng
máy chủ Telegram giả lập nên không cần bot token thật mà vẫn kiểm chứng được trọn
vẹn: nội dung tin nhắn, nút bấm, và hệ quả của việc bấm nút.

Chạy::

    pytest tests/test_telegram.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_telegram import MockTelegramServer  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture()
def telegram(temp_db):
    """Máy chủ Telegram giả lập, đã nối vào cấu hình FinAgent."""
    from finagent.config import settings

    with MockTelegramServer() as server:
        settings.telegram_bot_token = server.token
        settings.telegram_chat_id = "123456"
        settings.telegram_api_base = server.api_base
        yield server
        settings.telegram_api_base = "https://api.telegram.org"


def _make_proposal(**overrides):
    """Một đề xuất mẫu đã lưu vào cơ sở dữ liệu."""
    from finagent.decision import engine

    base = dict(
        symbol="FPT", asset_class="vn_stock", action="buy", signal="Buy",
        confidence=0.82, price=64_700.0, quantity=100.0, amount=6_470_000.0,
        rationale="ML dự báo tăng, tin tức tích cực.", currency="VND",
    )
    base.update(overrides)
    proposal = engine.Proposal(**base)
    engine.save_proposal(proposal)
    return proposal


class TestNotifier:
    def test_gui_duoc_tin_nhan(self, telegram):
        from finagent.telegram_bot import TelegramNotifier

        message_id = TelegramNotifier().send("Xin chào")

        assert message_id is not None
        assert len(telegram.state.messages) == 1
        assert telegram.state.messages[0]["text"] == "Xin chào"

    def test_tat_khi_thieu_token(self, temp_db):
        from finagent.config import settings
        from finagent.telegram_bot import TelegramNotifier

        settings.telegram_bot_token = ""
        notifier = TelegramNotifier(token="", chat_id="")

        assert notifier.enabled is False
        assert notifier.send("không gửi được") is None

    def test_gui_de_xuat_kem_nut_duyet(self, telegram):
        from finagent.telegram_bot import TelegramNotifier

        proposal = _make_proposal()
        message_id = TelegramNotifier().notify_proposal(proposal.to_dict())

        assert message_id is not None
        buttons = telegram.state.buttons_of()
        assert len(buttons) == 1 and len(buttons[0]) == 2

        callbacks = {button["callback_data"] for button in buttons[0]}
        assert callbacks == {f"approve:{proposal.id}", f"reject:{proposal.id}"}

    def test_tin_nhan_chua_du_thong_tin_de_quyet_dinh(self, telegram):
        from finagent.telegram_bot import TelegramNotifier

        proposal = _make_proposal()
        TelegramNotifier().notify_proposal(proposal.to_dict())

        text = telegram.state.last_message()["text"]
        assert "FPT" in text
        assert "MUA" in text
        assert "64,700" in text
        assert "MÔ PHỎNG" in text          # phải nói rõ đang chạy mô phỏng
        assert "82%" in text               # độ tin cậy
        assert "Căn cứ" in text            # lý do đề xuất


class TestApprovalFlow:
    def test_duyet_thi_dat_lenh(self, telegram):
        from finagent.broker.paper import PaperBroker
        from finagent.telegram_bot import _handle_decision

        proposal = _make_proposal()
        before = PaperBroker().get_cash("VND")

        message = _handle_decision(proposal.id, approved=True, user="telegram:1")

        assert "ĐÃ ĐẶT LỆNH" in message
        assert PaperBroker().get_position("FPT") is not None
        assert PaperBroker().get_cash("VND") < before

    def test_tu_choi_thi_khong_dat_lenh(self, telegram):
        from finagent.broker.paper import PaperBroker
        from finagent.telegram_bot import _handle_decision

        proposal = _make_proposal()
        message = _handle_decision(proposal.id, approved=False, user="telegram:1")

        assert "TỪ CHỐI" in message
        assert PaperBroker().get_position("FPT") is None
        assert len(PaperBroker().list_positions()) == 0

    def test_de_xuat_khong_ton_tai_bao_ro(self, telegram):
        from finagent.telegram_bot import _handle_decision

        message = _handle_decision(999_999, approved=True, user="telegram:1")

        assert "Không tìm thấy" in message

    def test_duyet_hai_lan_khong_dat_lenh_hai_lan(self, telegram):
        """Bấm Duyệt lần thứ hai không được mua thêm."""
        from finagent.broker.paper import PaperBroker
        from finagent.telegram_bot import _handle_decision

        proposal = _make_proposal()
        _handle_decision(proposal.id, approved=True, user="telegram:1")
        quantity_after_first = PaperBroker().get_position("FPT")["quantity"]

        _handle_decision(proposal.id, approved=True, user="telegram:1")

        assert PaperBroker().get_position("FPT")["quantity"] == quantity_after_first

    def test_ghi_nhan_nguoi_quyet_dinh(self, telegram):
        from finagent import storage
        from finagent.telegram_bot import _handle_decision

        proposal = _make_proposal()
        _handle_decision(proposal.id, approved=True, user="telegram:42")

        row = storage.get_conn().execute(
            "SELECT status, decided_by FROM proposals WHERE id = ?", (proposal.id,)
        ).fetchone()

        assert row["status"] == "approved"
        assert row["decided_by"] == "telegram:42"


class TestMessageFormatting:
    def test_danh_muc_hien_thi_tung_vi(self, temp_db):
        from finagent.broker.paper import PaperBroker
        from finagent.telegram_bot import format_positions

        text = format_positions(PaperBroker())

        assert "USDT" in text, "Phải hiển thị ví USDT riêng"
        assert "VND" in text

    def test_danh_muc_hien_thi_vi_the_dung_tien_te(self, temp_db):
        from finagent.broker.paper import PaperBroker
        from finagent.telegram_bot import format_positions

        PaperBroker().buy("BTCUSDT", 0.01, 83_000.0, asset_class="crypto")
        text = format_positions(PaperBroker())

        assert "BTCUSDT" in text
        assert "USDT" in text

    def test_dinh_dang_tien_vnd(self):
        from finagent.telegram_bot import _money

        assert _money(1_000_000, "VND") == "1,000,000 ₫"

    def test_dinh_dang_tien_usdt(self):
        from finagent.telegram_bot import _money

        assert _money(1_234.5, "USDT") == "1,234.50 USDT"

    def test_moi_muc_tin_hieu_deu_co_bieu_tuong(self):
        from finagent.decision.engine import SIGNAL_CONFIDENCE
        from finagent.telegram_bot import SIGNAL_EMOJI

        for signal in SIGNAL_CONFIDENCE:
            assert signal in SIGNAL_EMOJI
