"""Test các tính năng "trí tuệ" mới bật: chỉ số chuẩn, backtest, nhật ký, ngữ cảnh danh mục.

Những tính năng này đều là phần mở rộng quanh TradingAgents, nên cần test riêng để
chắc chắn cấu hình được truyền đúng và không âm thầm tắt đi.
"""

from __future__ import annotations

import pytest

from finagent.decision import backtest, engine, vendor


# ---------------------------------------------------------------------------
# Chỉ số chuẩn
# ---------------------------------------------------------------------------

class TestBenchmark:
    def test_co_phieu_vn_so_voi_vnindex(self):
        assert vendor.benchmark_for("vn_stock") == "VNINDEX"

    def test_crypto_so_voi_btc(self):
        assert vendor.benchmark_for("crypto") == "BTCUSDT"

    def test_vang_dung_chi_so_vn(self):
        assert vendor.benchmark_for("gold") == "VNINDEX"

    def test_nhan_dien_chi_so_vn(self):
        for symbol in ("VNINDEX", "VN30", "HNXINDEX", "UPCOMINDEX"):
            assert vendor.detect_asset_class(symbol) == "vn_index"

    def test_chi_so_khong_bi_coi_la_co_phieu(self):
        """Nếu coi chỉ số là cổ phiếu, hệ thống sẽ sinh đề xuất mua chỉ số — vô nghĩa."""
        assert vendor.detect_asset_class("VNINDEX") != "vn_stock"

    def test_loc_chi_so_khoi_danh_sach_giao_dich(self):
        filtered = vendor.tradable_symbols(["FPT", "VNINDEX", "BTCUSDT", "VN30"])

        assert filtered == ["FPT", "BTCUSDT"]

    def test_chi_so_bao_khong_giao_dich_duoc(self):
        output = vendor.get_stock_data("VNINDEX", "2026-08-01", "2026-09-20")

        assert "chỉ số" in output.lower()
        assert "không phải tài sản giao dịch" in output


# ---------------------------------------------------------------------------
# Cấu hình gửi sang TradingAgents
# ---------------------------------------------------------------------------

class TestGraphConfig:
    """Mọi tính năng đã bật phải thực sự có mặt trong cấu hình gửi đi.

    Bài học từ lỗi thật: một tính năng "đã bật" trong tài liệu nhưng không được
    truyền vào config thì im lặng không hoạt động, và không ai phát hiện.
    """

    @pytest.fixture(scope="class")
    def config(self):
        return engine.build_graph_config("vn_stock")

    def test_bat_nhat_ky_quyet_dinh(self, config):
        assert config["memory_log_path"]
        assert "decision_memory" in config["memory_log_path"]

    def test_gioi_han_so_muc_nhat_ky(self, config):
        assert config["memory_log_max_entries"] is not None

    def test_bat_checkpoint(self, config):
        assert config["checkpoint_enabled"] is True

    def test_chi_so_chuan_theo_thi_truong(self, config):
        assert config["benchmark_ticker"] == "VNINDEX"

    def test_chi_so_chuan_crypto(self):
        config = engine.build_graph_config("crypto")

        assert config["benchmark_ticker"] == "BTCUSDT"

    def test_thoi_gian_nam_giu(self, config):
        assert config["holding_period_days"] >= 1

    def test_ngu_canh_tin_tuc_duoc_mo_rong(self, config):
        assert config["news_article_limit"] >= 10
        assert config["global_news_article_limit"] >= 10
        assert config["global_news_lookback_days"] >= 7

    def test_moi_nhom_du_lieu_tro_ve_finagent(self, config):
        from finagent.decision.vendor import VENDOR_NAME

        for category, name in config["data_vendors"].items():
            assert name == VENDOR_NAME, f"{category} chưa trỏ về FinAgent"

    def test_doc_duoc_cac_tham_so_quan_trong(self, config):
        """Các khoá TradingAgents đọc phải tồn tại, nếu không nó dùng mặc định."""
        for key in ("llm_provider", "deep_think_llm", "quick_think_llm", "output_language",
                    "max_debate_rounds", "max_risk_discuss_rounds", "temperature", "results_dir"):
            assert key in config, f"thiếu khoá {key}"


    def test_so_vong_tranh_luan_cong_them(self, monkeypatch, temp_db):
        from finagent.config import settings

        monkeypatch.setattr(settings, "max_debate_rounds", 1, raising=False)
        monkeypatch.setattr(settings, "extra_debate_rounds", 2, raising=False)

        config = engine.build_graph_config("vn_stock")

        assert config["max_debate_rounds"] == 3
        assert config["max_risk_discuss_rounds"] == 1 + 2


# ---------------------------------------------------------------------------
# Nhật ký quyết định
# ---------------------------------------------------------------------------

class TestMemorySummary:
    def test_chua_co_nhat_ky_thi_bao_chua_co(self, temp_db, tmp_path, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "memory_log_path", tmp_path / "chua-co.md", raising=False)
        info = engine.memory_summary()

        assert info["exists"] is False
        assert info["entries"] == 0

    def test_doc_duoc_nhat_ky_rong(self, temp_db, tmp_path, monkeypatch):
        from finagent.config import settings

        path = tmp_path / "memory.md"
        path.write_text("")
        monkeypatch.setattr(settings, "memory_log_path", path, raising=False)

        info = engine.memory_summary()

        assert info["exists"] is True
        assert info["entries"] == 0

    def test_danh_sach_rong_nghia_la_khong_cham_ma_nao(self, temp_db):
        """``settle_all([])`` phải không làm gì, KHÔNG phải chạy toàn bộ danh mục.

        Đây là lỗi thật đã gặp: dùng ``symbols or default`` khiến danh sách rỗng bị
        hiểu thành "chạy tất cả", và một lệnh gọi rỗng bất ngờ tốn hàng loạt lời
        gọi LLM.
        """
        assert engine.settle_all([]) == []

    def test_none_nghia_la_dung_danh_sach_mac_dinh(self, temp_db, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "crypto_symbols", ["BTCUSDT"], raising=False)
        monkeypatch.setattr(settings, "vn_symbols", ["FPT"], raising=False)
        called: list[str] = []
        monkeypatch.setattr(engine, "settle_decisions",
                            lambda symbol: called.append(symbol) or {"settled": True, "symbol": symbol})

        engine.settle_all(None)

        assert called == ["BTCUSDT", "FPT"]


# ---------------------------------------------------------------------------
# Ngữ cảnh danh mục
# ---------------------------------------------------------------------------

class TestPortfolioContext:
    def test_dung_duoc_ngu_canh_tu_broker(self, temp_db):
        from finagent.broker import get_broker, reset_broker

        reset_broker()
        get_broker().buy("FPT", 100, 64_700.0)

        context = engine.build_portfolio_context()

        assert context is not None
        assert context.cash is not None
        held = context.position_in("FPT")
        assert held is not None
        assert held.quantity == 100
        reset_broker()

    def test_danh_muc_trong_van_tra_ve_ngu_canh(self, temp_db):
        """Danh mục trống khác với "không có ngữ cảnh" — phải giữ được sự khác biệt."""
        from finagent.broker import reset_broker

        reset_broker()
        context = engine.build_portfolio_context()

        assert context is not None
        assert context.positions == []
        assert context.cash is not None
        reset_broker()

    def test_broker_hong_thi_tra_ve_none_chu_khong_nem_loi(self, temp_db, monkeypatch):
        """Broker hỏng không được làm sập cả lượt phân tích vì thiếu ngữ cảnh danh mục."""
        import finagent.broker as broker_module
        import finagent.decision.engine as engine_module

        def boom():
            raise RuntimeError("broker hỏng")

        monkeypatch.setattr(broker_module, "get_broker", boom)

        assert engine_module.build_portfolio_context() is None


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class TestBacktestGrid:
    def test_sinh_luoi_ngay(self):
        dates = backtest.build_date_grid("2026-08-01", "2026-08-29", every_n_days=7)

        assert len(dates) == 5
        assert dates[0] == "2026-08-01"

    def test_khoang_mot_ngay_tra_ve_mot_ngay(self):
        """Khoảng chỉ có một ngày vẫn phải chạy được ngày đó."""
        assert backtest.build_date_grid("2026-08-01", "2026-08-01", every_n_days=7) == ["2026-08-01"]

    def test_moi_ngay_deu_trong_khoang(self):
        dates = backtest.build_date_grid("2026-07-01", "2026-09-01", every_n_days=7)

        assert all("2026-07-01" <= d <= "2026-09-01" for d in dates)


class TestBacktestReport:
    def _report(self, **overrides):
        base = dict(run_id="thu", symbols=["FPT"], dates=["2026-08-01", "2026-09-01"])
        base.update(overrides)
        return backtest.BacktestReport(**base)

    def test_bao_loi_thi_hien_loi(self):
        text = self._report(error="thiếu ngày").render()

        assert "lỗi" in text.lower()
        assert "thiếu ngày" in text

    def test_hien_thi_ma_va_khoang_ngay(self):
        text = self._report().render()

        assert "FPT" in text
        assert "2026-08-01" in text

    def test_hold_khong_hien_ti_le_dung_huong(self):
        """Hold không dự đoán hướng — không được bịa ra tỷ lệ đúng."""
        report = self._report(scores=[{"rating": "Hold", "count": 10,
                                       "mean_alpha": 0.001, "hit_rate": None}])

        text = report.render()

        assert "không dự đoán hướng" in text

    def test_bao_khi_khong_tinh_duoc_alpha(self):
        report = self._report(benchmark_skipped={"BTCUSDT"})

        text = report.render()

        assert "BTCUSDT" in text
        assert "lợi nhuận thô" in text

    def test_bao_khi_con_o_chua_cham(self):
        text = self._report(pending=5).render()

        assert "5 ô còn chờ" in text

    def test_to_dict_chuyen_duoc(self):
        data = self._report(cells_run=3).to_dict()

        assert data["cells_run"] == 3
        assert data["symbols"] == ["FPT"]


class TestBacktestValidation:
    def test_thieu_ngay_thi_bao_loi(self):
        report = backtest.run(symbols=["FPT"], start=None, end=None)

        assert report.ok is False
        assert "ngày" in report.error.lower()

    def test_chi_co_chi_so_thi_bao_loi(self):
        """Không có mã nào giao dịch được thì không chạy backtest."""
        report = backtest.run(symbols=["VNINDEX", "VN30"], start="2026-08-01", end="2026-09-01")

        assert report.ok is False
        assert "Không có mã" in report.error


# ---------------------------------------------------------------------------
# Nguồn dữ liệu bổ trợ
# ---------------------------------------------------------------------------

class TestSupplementaryVendors:
    def test_chua_co_fred_key_thi_bao_ro_cach_bat(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "fred_api_key", "", raising=False)

        output = vendor.get_macro_indicators("cpi", "2026-09-28", 30)

        assert "FRED_API_KEY" in output
        assert "miễn phí" in output

    def test_tat_polymarket_thi_bao_da_tat(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "polymarket_enabled", False, raising=False)

        output = vendor.get_prediction_markets("Fed rate cut", 5, "2026-09-28")

        assert "đã tắt" in output

    def test_hai_nguon_bo_tro_luon_tra_ve_chuoi(self, monkeypatch):
        """Dữ liệu bổ trợ hỏng không được làm sập lượt phân tích."""
        from finagent.config import settings

        monkeypatch.setattr(settings, "fred_api_key", "khoa-gia", raising=False)
        monkeypatch.setattr(settings, "polymarket_enabled", True, raising=False)

        assert isinstance(vendor.get_macro_indicators("cpi", "2026-09-28", 30), str)
        assert isinstance(vendor.get_prediction_markets("Fed rate cut", 5, "2026-09-28"), str)


# ---------------------------------------------------------------------------
# Nhà cung cấp LLM
# ---------------------------------------------------------------------------

class TestLlmProvider:
    """Cấu hình nhà cung cấp phải khớp với thứ TradingAgents thực sự đọc.

    Đã từng sai ở đây: gói miễn phí của Google chỉ cho 20 request/ngày cho mỗi
    model, mà một lượt phân tích tốn khoảng 20 lời gọi — vừa đủ một lượt rồi hết.
    """

    def test_co_anh_xa_khoa_cho_endpoint_tuong_thich_openai(self):
        from finagent.config import PROVIDER_KEY_ENV

        assert PROVIDER_KEY_ENV.get("openai_compatible") == "OPENAI_COMPATIBLE_API_KEY"

    def test_endpoint_tuong_thich_openai_khong_bi_coi_la_thieu_khoa(self):
        """Provider này bắt buộc có base_url nhưng khoá là tuỳ chọn."""
        from finagent.config import KEYLESS_PROVIDERS

        # openai_compatible không nằm trong nhóm miễn khoá, nhưng phải có tên biến
        # khoá để thông báo lỗi chỉ đúng chỗ cần điền.
        assert "openai_compatible" not in KEYLESS_PROVIDERS
        from finagent.config import PROVIDER_KEY_ENV

        assert "openai_compatible" in PROVIDER_KEY_ENV

    def test_doc_duoc_khoa_tu_bien_moi_truong(self, monkeypatch):
        from finagent.config import _resolve_llm_api_key

        monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai_compatible")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "khoa-thu")

        assert _resolve_llm_api_key() == "khoa-thu"

    def test_doi_nha_cung_cap_thi_khong_lay_nham_khoa_cu(self, monkeypatch):
        """Chuyển sang opencode mà vẫn còn GOOGLE_API_KEY thì không được dùng nhầm nó
        khi khoá mới đã có."""
        from finagent.config import _resolve_llm_api_key

        monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai_compatible")
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "khoa-moi")
        monkeypatch.setenv("GOOGLE_API_KEY", "khoa-cu")

        assert _resolve_llm_api_key() == "khoa-moi"

    def test_cau_hinh_llm_vao_graph(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "llm_provider", "openai_compatible", raising=False)
        monkeypatch.setattr(settings, "llm_backend_url", "https://opencode.ai/zen/v1", raising=False)
        monkeypatch.setattr(settings, "deep_think_llm", "deepseek-v4.1-flash", raising=False)

        config = engine.build_graph_config("vn_stock")

        assert config["llm_provider"] == "openai_compatible"
        assert config["backend_url"] == "https://opencode.ai/zen/v1"
        assert config["deep_think_llm"] == "deepseek-v4.1-flash"

    def test_khong_gioi_han_max_tokens_cho_model_suy_luan(self):
        """Model suy luận tiêu phần lớn token cho bước reasoning trước khi trả lời.

        Giới hạn chặt sẽ cho câu trả lời rỗng — đã quan sát được: một lời gọi 50
        token trả về nội dung rỗng vì cả 50 token đều vào ``reasoning_tokens``.
        Nên cấu hình mẫu phải để trống, tức không giới hạn.
        """
        from pathlib import Path

        example = Path(__file__).resolve().parent.parent / ".env.example"
        lines = [
            line for line in example.read_text().splitlines()
            if line.startswith("TRADINGAGENTS_MAX_TOKENS=")
        ]

        # Không đặt thì TradingAgents dùng None = không giới hạn.
        assert all(line.split("=", 1)[1].strip() == "" for line in lines)


# ---------------------------------------------------------------------------
# Tốc độ quét thị trường
# ---------------------------------------------------------------------------

class TestScanSpeed:
    """Quét tuần tự 7 mã hết hơn một tiếng rưỡi — quá chậm để dùng thật.

    Nhóm test này khoá lại hai cơ chế tăng tốc: lọc trước bằng ML, và chạy song song.
    """

    def test_loc_truoc_bo_ma_trung_tinh(self, temp_db, monkeypatch):
        from finagent import scheduler
        from finagent.config import settings
        from finagent.decision import ml_model, vendor

        monkeypatch.setattr(settings, "ml_neutral_band", 0.05, raising=False)
        monkeypatch.setattr(vendor, "daily_history", lambda symbol, days=400: [{"price": 1.0}])
        # Nửa mã có tín hiệu mạnh, nửa còn lại trung tính.
        probabilities = {"FPT": 0.80, "HPG": 0.50, "VCB": 0.20}
        monkeypatch.setattr(
            ml_model, "predict_from_history",
            lambda history: {"available": True, "probability": probabilities.pop("__next__", 0.5)}
            if False else {"available": True, "probability": 0.5},
        )

        # Dùng hàm giả để trả xác suất theo từng mã một cách xác định.
        calls = iter([0.80, 0.50, 0.20])
        monkeypatch.setattr(
            ml_model, "predict_from_history",
            lambda history: {"available": True, "probability": next(calls)},
        )

        import finagent.broker as broker_module

        class _NoPositions:
            def list_positions(self):
                return []

        monkeypatch.setattr(broker_module, "get_broker", lambda: _NoPositions())

        kept = scheduler._prefilter_symbols(["FPT", "HPG", "VCB"])

        assert kept == ["FPT", "VCB"], "mã trung tính (0,50) phải bị bỏ qua"

    def test_luon_giu_ma_dang_giu_vi_the(self, temp_db, monkeypatch):
        """Đang giữ tiền trong một mã thì phải phân tích kỹ, không được lọc bỏ.

        Bỏ qua mã đang giữ vị thế là rủi ro thật: câu hỏi không còn là 'có nên vào
        không' mà là 'có nên thoát không'.
        """
        from finagent import scheduler
        from finagent.config import settings
        from finagent.decision import ml_model, vendor

        monkeypatch.setattr(settings, "ml_neutral_band", 0.05, raising=False)
        monkeypatch.setattr(vendor, "daily_history", lambda symbol, days=400: [{"price": 1.0}])
        # HPG trung tính hoàn toàn, nhưng đang giữ vị thế.
        monkeypatch.setattr(
            ml_model, "predict_from_history",
            lambda history: {"available": True, "probability": 0.50},
        )

        import finagent.broker as broker_module

        class _Holding:
            def list_positions(self):
                return [{"symbol": "HPG", "quantity": 100, "avg_price": 20_000.0}]

        monkeypatch.setattr(broker_module, "get_broker", lambda: _Holding())

        kept = scheduler._prefilter_symbols(["FPT", "HPG"])

        assert "HPG" in kept, "mã đang giữ vị thế không được bị lọc bỏ"

    def test_loc_het_thi_phan_tich_toan_bo(self, temp_db, monkeypatch):
        """Thà chậm còn hơn bỏ sót: không giữ lại được mã nào thì phân tích hết."""
        from finagent import scheduler
        from finagent.config import settings
        from finagent.decision import ml_model, vendor

        monkeypatch.setattr(settings, "ml_neutral_band", 0.05, raising=False)
        monkeypatch.setattr(vendor, "daily_history", lambda symbol, days=400: [{"price": 1.0}])
        monkeypatch.setattr(
            ml_model, "predict_from_history",
            lambda history: {"available": True, "probability": 0.50},
        )

        import finagent.broker as broker_module

        class _NoPositions:
            def list_positions(self):
                return []

        monkeypatch.setattr(broker_module, "get_broker", lambda: _NoPositions())

        symbols = ["FPT", "HPG"]

        assert scheduler._prefilter_symbols(symbols) == symbols

    def test_khong_doc_duoc_vi_the_thi_phan_tich_het(self, temp_db, monkeypatch):
        """Không biết đang giữ gì thì phải phân tích tất, không được đoán bừa."""
        from finagent import scheduler

        import finagent.broker as broker_module

        def boom():
            raise RuntimeError("broker hỏng")

        monkeypatch.setattr(broker_module, "get_broker", boom)
        symbols = ["FPT", "HPG"]

        assert scheduler._prefilter_symbols(symbols) == symbols

    def test_ml_khong_chay_duoc_thi_van_phan_tich(self, temp_db, monkeypatch):
        from finagent import scheduler
        from finagent.decision import vendor

        monkeypatch.setattr(
            vendor, "daily_history",
            lambda symbol, days=400: (_ for _ in ()).throw(RuntimeError("hết dữ liệu")),
        )

        import finagent.broker as broker_module

        class _NoPositions:
            def list_positions(self):
                return []

        monkeypatch.setattr(broker_module, "get_broker", lambda: _NoPositions())

        symbols = ["FPT"]

        assert scheduler._prefilter_symbols(symbols) == symbols

    def test_analyze_loi_thi_tra_ve_none_chu_khong_nem(self, temp_db, monkeypatch):
        from finagent import scheduler
        from finagent.decision import engine

        monkeypatch.setattr(
            engine, "build_proposal",
            lambda symbol, run_llm=True: (_ for _ in ()).throw(RuntimeError("sập")),
        )

        assert scheduler._safe_analyse("FPT", True) is None

    def test_nhip_quet_dai_hon_thoi_gian_quet(self):
        """Nhịp quét phải dài hơn một lượt quét, nếu không bộ lập lịch chạy chồng."""
        from finagent.config import settings

        assert settings.monitor_interval >= 900, (
            "một lượt quét tốn hàng chục phút; nhịp quá ngắn sẽ khiến bộ lập lịch "
            "thử chạy chồng và ghi cảnh báo liên tục"
        )

    def test_cac_tham_so_toc_do_co_that(self):
        from finagent.config import settings

        assert hasattr(settings, "llm_prefilter")
        assert hasattr(settings, "scan_parallelism")
        assert settings.scan_parallelism >= 1
