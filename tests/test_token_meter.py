"""Test bộ đếm token và lọc theo giờ giao dịch.

Bộ đếm token tồn tại vì khung TradingAgents không đếm, mà sàn cũng không có endpoint
thống kê. Đo được thì mới biết một lượt quét tốn bao nhiêu và có nên chạy thường
xuyên không.

Lọc theo giờ giao dịch tồn tại vì một lượt phân tích tốn khoảng 217.000 token cho
mỗi mã — phân tích chứng khoán lúc 3 giờ sáng là đốt token cho số liệu đã cũ.
"""

from __future__ import annotations

import datetime as dt

import pytest

from finagent.decision.token_meter import TokenMeter, build_callbacks, measure


class TestTokenMeter:
    def test_cong_don_nhieu_loi_goi(self):
        meter = TokenMeter()
        meter.record({"prompt_tokens": 1000, "completion_tokens": 200})
        meter.record({"prompt_tokens": 500, "completion_tokens": 100})

        assert meter.calls == 2
        assert meter.prompt_tokens == 1500
        assert meter.completion_tokens == 300
        assert meter.total_tokens == 1800

    def test_dem_ca_token_suy_luan(self):
        """Model suy luận tiêu phần lớn token cho bước reasoning — phải thấy được."""
        meter = TokenMeter()
        meter.record({
            "prompt_tokens": 100, "completion_tokens": 500,
            "completion_tokens_details": {"reasoning_tokens": 450},
        })

        assert meter.reasoning_tokens == 450

    def test_loi_goi_khong_co_so_lieu_van_duoc_dem(self):
        """Một số lời gọi không trả về số liệu; phải biết là có bao nhiêu như vậy."""
        meter = TokenMeter()
        meter.record({})
        meter.record({"prompt_tokens": 10, "completion_tokens": 5})

        assert meter.calls == 1
        assert meter.unmeasured_calls == 1

    def test_ghi_nho_prompt_lon_nhat(self):
        """Prompt phình bất thường là dấu hiệu dữ liệu lọt nguyên trang vào ngữ cảnh."""
        meter = TokenMeter()
        meter.record({"prompt_tokens": 500, "completion_tokens": 10})
        meter.record({"prompt_tokens": 30_000, "completion_tokens": 10})
        meter.record({"prompt_tokens": 800, "completion_tokens": 10})

        assert meter.largest_prompt == 30_000

    def test_tach_theo_model(self):
        meter = TokenMeter()
        meter.record({"prompt_tokens": 100, "completion_tokens": 0}, "model-a")
        meter.record({"prompt_tokens": 200, "completion_tokens": 0}, "model-b")

        assert meter.per_model == {"model-a": 100, "model-b": 200}

    def test_chiu_duoc_ten_truong_khac_nhau(self):
        """Nhà cung cấp dùng input_tokens/output_tokens thay vì prompt/completion."""
        meter = TokenMeter()
        meter.record({"input_tokens": 300, "output_tokens": 70})

        assert meter.prompt_tokens == 300
        assert meter.completion_tokens == 70

    def test_tom_tat_khi_chua_co_gi(self):
        assert "Chưa ghi nhận" in TokenMeter().summary()

    def test_tom_tat_chia_theo_so_ma(self):
        meter = TokenMeter()
        meter.record({"prompt_tokens": 1000, "completion_tokens": 0})

        assert "mỗi mã" in meter.summary(symbols=4)


class TestMeasureContext:
    def test_bat_va_tat_do(self):
        assert build_callbacks() == []
        with measure() as meter:
            assert len(build_callbacks()) == 1
            assert meter is not None
        assert build_callbacks() == []

    def test_tat_do_sau_khi_co_loi(self):
        """Bộ đếm phải được dọn kể cả khi khối lệnh ném lỗi, nếu không lần sau sẽ
        ghi nhầm vào bộ đếm cũ."""
        with pytest.raises(RuntimeError):
            with measure():
                raise RuntimeError("sập")

        assert build_callbacks() == []

    def test_callback_la_lop_con_cua_langchain(self):
        """LangChain dùng pydantic kiểm tra kiểu và TỪ CHỐI callback không kế thừa
        BaseCallbackHandler — đã mắc đúng lỗi này khi tích hợp lần đầu."""
        from langchain_core.callbacks import BaseCallbackHandler

        from finagent.decision.token_meter import _MeterCallback

        assert isinstance(_MeterCallback(TokenMeter()), BaseCallbackHandler)


class TestMarketHours:
    TZ = dt.timezone(dt.timedelta(hours=7))

    def _at(self, year, month, day, hour, minute=0):
        return dt.datetime(year, month, day, hour, minute, tzinfo=self.TZ)

    def test_trong_gio_ngay_thuong(self):
        from finagent import scheduler

        assert scheduler.in_market_hours(self._at(2026, 9, 28, 10, 0)) is True

    def test_dem_khuya_thi_dong(self):
        from finagent import scheduler

        assert scheduler.in_market_hours(self._at(2026, 9, 28, 3, 0)) is False

    def test_sau_gio_dong_cua(self):
        from finagent import scheduler

        assert scheduler.in_market_hours(self._at(2026, 9, 28, 16, 0)) is False

    def test_cuoi_tuan_dong_cua(self):
        from finagent import scheduler

        # 3/10/2026 là thứ Bảy.
        assert scheduler.in_market_hours(self._at(2026, 10, 3, 10, 0)) is False

    def test_dung_ngay_dong_cua(self):
        from finagent import scheduler

        assert scheduler.in_market_hours(self._at(2026, 9, 28, 9, 0)) is True
        assert scheduler.in_market_hours(self._at(2026, 9, 28, 15, 0)) is True

    def test_loc_bo_chung_khoan_ngoai_gio(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "market_hours_only", True, raising=False)
        monkeypatch.setattr(scheduler, "in_market_hours", lambda now=None: False)

        assert scheduler.filter_by_market_hours(["BTCUSDT", "FPT", "VNM"]) == ["BTCUSDT"]

    def test_giu_nguyen_trong_gio(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "market_hours_only", True, raising=False)
        monkeypatch.setattr(scheduler, "in_market_hours", lambda now=None: True)
        symbols = ["BTCUSDT", "FPT"]

        assert scheduler.filter_by_market_hours(symbols) == symbols

    def test_tat_loc_thi_giu_het(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "market_hours_only", False, raising=False)
        monkeypatch.setattr(scheduler, "in_market_hours", lambda now=None: False)
        symbols = ["BTCUSDT", "FPT"]

        assert scheduler.filter_by_market_hours(symbols) == symbols

    def test_crypto_khong_bi_gio_giao_dich_chan(self, monkeypatch):
        """Crypto chạy 24/7 nên không được lọc theo giờ sàn chứng khoán."""
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "market_hours_only", True, raising=False)
        monkeypatch.setattr(scheduler, "in_market_hours", lambda now=None: False)

        assert scheduler.filter_by_market_hours(["BTCUSDT", "ETHUSDT"]) == ["BTCUSDT", "ETHUSDT"]


class TestScanTimes:
    """Quét theo mốc giờ thay vì theo chu kỳ.

    Đây là khác biệt lớn về chi phí: một lượt quét 4 mã tốn khoảng 867.000 token,
    quét mỗi 30 phút suốt ngày là hơn 1,2 tỷ token mỗi tháng.
    """

    def test_doc_moc_gio_binh_thuong(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "scan_times", ["09:45", "14:00"], raising=False)

        assert scheduler._scan_times() == [(9, 45), (14, 0)]

    def test_sap_xep_va_bo_trung(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "scan_times", ["14:00", "09:45", "09:45"], raising=False)

        assert scheduler._scan_times() == [(9, 45), (14, 0)]

    def test_bo_qua_moc_sai_dinh_dang(self, monkeypatch):
        """Một dấu phẩy thừa trong .env không đáng làm sập bộ lập lịch."""
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "scan_times", ["09:45", "abc", "", "14:00"], raising=False)

        assert scheduler._scan_times() == [(9, 45), (14, 0)]

    def test_bo_qua_gio_ngoai_khoang(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "scan_times", ["25:00", "10:99", "12:30"], raising=False)

        assert scheduler._scan_times() == [(12, 30)]

    def test_danh_sach_rong_thi_quay_ve_chu_ky(self, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings

        monkeypatch.setattr(settings, "scan_times", [], raising=False)

        assert scheduler._scan_times() == []

    def test_mac_dinh_hai_moc_moi_ngay(self):
        """Mặc định phải là hai mốc — nhiều hơn là tốn token vô ích."""
        import importlib

        from finagent import config as config_module

        importlib.reload(config_module)
        assert len(config_module.settings.scan_times) == 2

    def test_hai_moc_nam_trong_gio_giao_dich(self):
        """Quét ngoài giờ giao dịch là phân tích số liệu đã cũ."""
        from finagent.config import settings

        open_h = int(settings.market_open.split(":")[0])
        close_h = int(settings.market_close.split(":")[0])
        for text in settings.scan_times:
            hour = int(str(text).split(":")[0])
            assert open_h <= hour <= close_h, f"mốc {text} nằm ngoài giờ giao dịch"


class TestOpenCodeGoHeaders:
    """Endpoint OpenCode Go BẮT BUỘC có header x-opencode-session.

    Thiếu nó thì mọi yêu cầu bị từ chối với lỗi MissingSessionID — không phải suy
    giảm chất lượng mà là không gọi được gì. Đã xác nhận bằng cách gọi thật.
    """

    def test_co_cau_hinh_header(self):
        from finagent.config import settings

        assert settings.llm_user_agent, "phải có User-Agent riêng cho client"
        assert settings.llm_session_id, "phải có mã phiên"

    def test_endpoint_mac_dinh_la_go(self):
        """Go và Zen là hai endpoint khác nhau; trỏ nhầm là trả tiền theo token."""
        from finagent.config import settings

        if "opencode.ai" in (settings.llm_backend_url or ""):
            assert "/zen/go/" in settings.llm_backend_url or "go." in settings.llm_backend_url, (
                "endpoint OpenCode phải là /zen/go/v1 (gói Go), không phải /zen/v1 (Zen)"
            )

    def test_cam_header_dung_cach(self, monkeypatch):
        """Kiểm tra hàm cắm header chạy được và không ném lỗi với cấu hình Go."""
        from finagent.config import settings
        from finagent.decision import vendor

        monkeypatch.setattr(settings, "llm_backend_url", "https://opencode.ai/zen/go/v1", raising=False)
        monkeypatch.setattr(settings, "llm_user_agent", "FinAgent/1.0", raising=False)
        monkeypatch.setattr(settings, "llm_session_id", "phien-thu", raising=False)

        # Gọi trực tiếp hàm cắm, không qua install_patches (vốn chỉ chạy một lần).
        vendor._install_opencode_go_headers()

        from tradingagents.graph import trading_graph

        config = {"llm_provider": "openai_compatible"}
        kwargs = trading_graph.build_llm_kwargs(config)

        assert "default_headers" in kwargs
        assert kwargs["default_headers"]["x-opencode-session"] == "phien-thu"
        assert kwargs["default_headers"]["User-Agent"] == "FinAgent/1.0"

    def test_khong_cam_header_cho_nha_cung_cap_khac(self, monkeypatch):
        """Cắm header của Go vào nhà cung cấp khác là gửi rác cho họ."""
        from finagent.config import settings
        from finagent.decision import vendor

        monkeypatch.setattr(settings, "llm_backend_url", "https://opencode.ai/zen/go/v1", raising=False)
        vendor._install_opencode_go_headers()

        from tradingagents.graph import trading_graph

        kwargs = trading_graph.build_llm_kwargs({"llm_provider": "openai"})

        assert "default_headers" not in kwargs
