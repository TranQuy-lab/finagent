"""Test đường đi LLM đầy đủ (TradingAgents + vendor FinAgent) không cần API key.

Đây là nhóm test quan trọng nhất của phần suy luận: đường đi LLM chỉ chạy được khi
có khoá API thật, nên nếu không có gì thay thế thì nó hoàn toàn không được kiểm
chứng. Ở đây dùng một LLM giả lập tương thích OpenAI và **ghi lại mọi prompt**, nhờ
đó khẳng định được dữ liệu Việt Nam thật sự tới được mô hình.

Chạy::

    pytest tests/test_llm_path.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_llm import MockLLMServer  # noqa: E402

pytestmark = pytest.mark.integration

SYMBOL = "FPT"


@pytest.fixture(scope="module")
def llm_run(temp_db_module):
    """Chạy đồ thị đa tác nhân **một lần** rồi chia sẻ kết quả cho mọi test.

    Đồ thị này tốn vài giây mỗi lượt, nên chạy một lần ở phạm vi module thay vì
    lặp lại cho từng khẳng định. Trước khi chạy phải nạp sẵn tin tức vào kho,
    nếu không tác nhân tin tức sẽ không có gì để đọc và bài test mất ý nghĩa.
    """
    from finagent import storage
    from finagent.collectors import news as news_collector
    from finagent.config import settings
    from finagent.decision import engine
    from finagent.decision import vendor as vendor_module

    # Ghim nhà cung cấp về ``deepseek`` (tương thích OpenAI) thay vì dùng cấu hình
    # trong ``.env``: LLM giả lập nói giao thức OpenAI, còn nhà cung cấp khác như
    # Google dùng client riêng và KHÔNG nhận ``backend_url`` — để nguyên cấu hình
    # thật thì bài test sẽ gọi thẳng API bên ngoài và tốn tiền thật.
    saved = {
        "llm_provider": settings.llm_provider,
        "llm_api_key": settings.llm_api_key,
        "deep_think_llm": settings.deep_think_llm,
        "quick_think_llm": settings.quick_think_llm,
        "output_language": settings.output_language,
    }
    settings.llm_provider = "deepseek"
    settings.llm_api_key = "dummy-key-cho-llm-gia-lap"
    settings.deep_think_llm = "deepseek-flash"
    settings.quick_think_llm = "deepseek-flash"
    settings.output_language = "Vietnamese"

    items, _errors = news_collector.collect(max_items=10, worker="test-llm")
    storage.save_news(items)

    try:
        with MockLLMServer() as server:
            result = engine.analyze_llm(SYMBOL, backend_url=server.base_url)
            yield {
                "result": result,
                "server": server,
                "prompt": server.state.all_prompt_text(),
                "tools": list(server.state.tool_calls_made),
                "calls": server.state.call_count(),
                "vendor_config": vendor_module.vendor_config(),
            }
    finally:
        for key, value in saved.items():
            setattr(settings, key, value)


class TestGraphRuns:
    def test_do_thi_chay_thanh_cong(self, llm_run):
        assert llm_run["result"]["available"] is True

    def test_tra_ve_tin_hieu_nam_trong_thang_5_muc(self, llm_run):
        from finagent.decision.engine import SIGNAL_CONFIDENCE

        assert llm_run["result"]["signal"] in SIGNAL_CONFIDENCE

    def test_goi_llm_nhieu_lan_that_su(self, llm_run):
        """Đồ thị đa tác nhân phải gọi LLM nhiều lần, không phải một phát duy nhất."""
        assert llm_run["calls"] >= 10

    def test_quyet_dinh_cuoi_co_cau_truc(self, llm_run):
        """Quyết định cuối phải là structured output, không phải văn bản rời."""
        decision = str(llm_run["result"].get("decision") or "")
        assert "Rating" in decision


class TestVietnameseDataReachesLLM:
    """Khẳng định cầu nối dữ liệu hoạt động thật, không chỉ chạy cho có."""

    def test_bang_gia_ohlcv_toi_duoc_llm(self, llm_run):
        assert "open,high,low,close" in llm_run["prompt"]

    def test_gia_theo_dong_viet_nam_toi_duoc_llm(self, llm_run):
        assert "VND" in llm_run["prompt"]

    def test_chi_bao_ky_thuat_toi_duoc_llm(self, llm_run):
        assert "RSI" in llm_run["prompt"].upper()

    def test_tin_tuc_viet_nam_toi_duoc_llm(self, llm_run):
        sources = ("CafeF", "VnExpress", "Vietstock")
        assert any(source in llm_run["prompt"] for source in sources), "Không có tin tức Việt Nam nào trong prompt"

    def test_anh_chup_thi_truong_tu_he_thong(self, llm_run):
        """Ảnh chụp 'đã kiểm chứng' phải do FinAgent dựng, không phải Yahoo."""
        assert "ẢNH CHỤP THỊ TRƯỜNG ĐÃ KIỂM CHỨNG" in llm_run["prompt"]

    def test_danh_tinh_cong_ty_viet_nam(self, llm_run):
        """Phải có tên doanh nghiệp thật, tránh việc mô hình bịa ra công ty khác."""
        assert "Công ty Cổ phần FPT" in llm_run["prompt"]


class TestToolUsage:
    def test_goi_tool_lay_gia(self, llm_run):
        assert "get_stock_data" in llm_run["tools"]

    def test_goi_tool_anh_chup_kiem_chung(self, llm_run):
        assert "get_verified_market_snapshot" in llm_run["tools"]

    def test_goi_tool_chi_bao(self, llm_run):
        assert "get_indicators" in llm_run["tools"]

    def test_goi_tool_tin_tuc(self, llm_run):
        assert "get_news" in llm_run["tools"]

    def test_goi_tool_tin_vi_mo(self, llm_run):
        assert "get_global_news" in llm_run["tools"]


class TestNoUSVendorCalls:
    """Không được để lộ lệnh gọi nhà cung cấp Mỹ — chúng luôn thất bại và chỉ gây chậm."""

    def test_mang_xa_hoi_bao_khong_co_du_lieu(self, llm_run):
        assert "KHÔNG CÓ DỮ LIỆU MẠNG XÃ HỘI" in llm_run["prompt"]

    def test_moi_danh_muc_deu_tro_ve_finagent(self, llm_run):
        from finagent.decision.vendor import VENDOR_NAME

        for category, vendor_name in llm_run["vendor_config"].items():
            assert vendor_name == VENDOR_NAME, f"Danh mục {category} chưa trỏ về FinAgent"


class TestVendorRegistration:
    def test_moi_phuong_thuc_deu_co_ban_finagent(self):
        import finagent.decision.vendor as vendor_module
        from tradingagents.dataflows import router

        vendor_module.register_vendor()
        missing = [
            method
            for method in router.VENDOR_METHODS
            if vendor_module.VENDOR_NAME not in router.VENDOR_METHODS[method]
        ]
        assert not missing, f"Các phương thức chưa đăng ký FinAgent: {missing}"

    def test_dang_ky_lai_khong_gay_loi(self):
        import finagent.decision.vendor as vendor_module

        vendor_module.register_vendor()
        vendor_module.register_vendor()
        assert vendor_module._registered is True


class TestVendorStubMethods:
    """Các phương thức không có dữ liệu phải trả lời rõ ràng, không được ném lỗi."""

    def test_bang_can_doi_bao_khong_co(self):
        from finagent.decision import vendor

        assert "KHÔNG CÓ DỮ LIỆU" in vendor.get_balance_sheet("FPT")

    def test_luu_chuyen_tien_te_bao_khong_co(self):
        from finagent.decision import vendor

        assert "KHÔNG CÓ DỮ LIỆU" in vendor.get_cashflow("FPT")

    def test_ket_qua_kinh_doanh_bao_khong_co(self):
        from finagent.decision import vendor

        assert "KHÔNG CÓ DỮ LIỆU" in vendor.get_income_statement("FPT")

    def test_giao_dich_noi_bo_bao_khong_co(self):
        from finagent.decision import vendor

        assert "KHÔNG CÓ DỮ LIỆU" in vendor.get_insider_transactions("FPT")

    def test_chi_so_vi_mo_bao_khong_co(self):
        from finagent.decision import vendor

        assert "KHÔNG CÓ DỮ LIỆU" in vendor.get_macro_indicators("cpi")

    def test_cac_phuong_thuc_tra_ve_chuoi(self):
        """TradingAgents mong đợi mọi tool trả về chuỗi."""
        from finagent.decision import vendor

        for output in (
            vendor.get_balance_sheet("FPT"),
            vendor.get_cashflow("FPT"),
            vendor.get_income_statement("FPT"),
            vendor.get_insider_transactions("FPT"),
            vendor.get_macro_indicators("cpi"),
            vendor.get_prediction_markets("fed"),
        ):
            assert isinstance(output, str) and output


class TestInstrumentIdentity:
    def test_co_phieu_viet_nam_co_ten_that(self):
        from finagent.decision import vendor

        identity = vendor.resolve_instrument_identity("FPT")

        assert identity["company_name"] == "Công ty Cổ phần FPT"
        assert identity["quote_type"] == "EQUITY"

    def test_ma_khong_biet_van_tra_ve_danh_tinh(self):
        """Mã lạ không được trả về rỗng — nếu rỗng, mô hình dễ bịa ra công ty khác."""
        from finagent.decision import vendor

        identity = vendor.resolve_instrument_identity("XYZ")

        assert identity["company_name"]
        assert "XYZ" in identity["company_name"]

    def test_crypto_co_danh_tinh_rieng(self):
        from finagent.decision import vendor

        identity = vendor.resolve_instrument_identity("BTCUSDT")

        assert "Bitcoin" in identity["company_name"]
        assert identity["quote_type"] == "CRYPTOCURRENCY"

    def test_vang_co_danh_tinh_rieng(self):
        from finagent.decision import vendor

        identity = vendor.resolve_instrument_identity("SJL1L10")

        assert identity["quote_type"] == "COMMODITY"


class TestVendorSignatures:
    """Mọi hàm vendor phải chịu được đúng số tham số mà TradingAgents truyền.

    Đây là bài học từ một lỗi thật: khi chạy bằng Gemini, phương thức
    ``get_prediction_markets`` bị gọi với **3** tham số trong khi hàm chỉ nhận 2,
    khiến mỗi lượt phân tích đều mất phần dữ liệu dự đoán. LLM giả lập không phát
    hiện được vì nó chỉ gọi các tool lấy dữ liệu, không chạm tới nhánh này.
    """

    #: Số tham số thực tế mà ``agents/tools.py`` truyền cho từng phương thức.
    CALL_ARGUMENTS = {
        "get_stock_data": ("FPT", "2026-08-01", "2026-09-28"),
        "get_indicators": ("FPT", "rsi", "2026-09-28", 30),
        "get_fundamentals": ("FPT", "2026-09-28"),
        "get_balance_sheet": ("FPT", "quarterly", "2026-09-28"),
        "get_cashflow": ("FPT", "quarterly", "2026-09-28"),
        "get_income_statement": ("FPT", "quarterly", "2026-09-28"),
        "get_news": ("FPT", "2026-09-01", "2026-09-28"),
        "get_global_news": ("2026-09-28", 7, 20),
        "get_insider_transactions": ("FPT", None),
        "get_macro_indicators": ("cpi", "2026-09-28", 30),
        "get_prediction_markets": ("Fed rate cut", 6, "2026-09-28"),
    }

    @pytest.mark.parametrize("method", sorted(CALL_ARGUMENTS))
    def test_goi_duoc_voi_tham_so_that(self, method):
        from finagent.decision import vendor

        function = getattr(vendor, method)
        result = function(*self.CALL_ARGUMENTS[method])

        assert isinstance(result, str) and result

    @pytest.mark.parametrize("method", sorted(CALL_ARGUMENTS))
    def test_moi_phuong_thuc_deu_duoc_dang_ky(self, method):
        from finagent.decision import vendor
        from tradingagents.dataflows import router

        vendor.register_vendor()
        assert vendor.VENDOR_NAME in router.VENDOR_METHODS[method]


class TestLLMArgumentCoercion:
    """Mô hình thật hay trả tham số sai kiểu — phải ép kiểu, không được vỡ.

    Lỗi thật đã gặp: Gemini truyền ``limit`` dạng chuỗi ``"20"``, SQLite báo
    ``datatype mismatch`` và cả lượt phân tích đổ vỡ.
    """

    def test_ep_so_nguyen_tu_chuoi(self):
        from finagent.decision.vendor import as_int

        assert as_int("30", default=7) == 30

    def test_ep_so_nguyen_voi_gia_tri_khong_hop_le(self):
        from finagent.decision.vendor import as_int

        assert as_int("khong-phai-so", default=7) == 7
        assert as_int(None, default=7) == 7

    def test_ep_so_nguyen_luon_nam_trong_khoang(self):
        from finagent.decision.vendor import as_int

        assert as_int(0, default=7, minimum=1) == 1
        assert as_int(99999, default=7, maximum=100) == 100

    def test_chuoi_nhan_ba_lan_khong_thanh_so(self):
        """``"30" * 3`` cho ``"303030"`` — đúng cái bẫy cần tránh."""
        from finagent.decision.vendor import as_int

        assert as_int("30", default=1) * 3 == 90

    def test_ep_ngay_hop_le(self):
        from finagent.decision.vendor import as_date

        assert as_date("2026-09-28", default="2026-01-01") == "2026-09-28"

    def test_ep_ngay_khong_hop_le(self):
        from finagent.decision.vendor import as_date

        assert as_date("hom-nay", default="2026-01-01") == "2026-01-01"
        assert as_date(None, default="2026-01-01") == "2026-01-01"

    def test_recent_news_chiu_duoc_limit_dang_chuoi(self, temp_db):
        from finagent import storage

        storage.save_news([{
            "uid": "u1", "title": "Tin", "url": "https://a.vn/1", "source": "Test",
            "published_at": "2026-09-28T01:00:00+00:00", "summary": "",
            "topics": ["stock"], "collected_by": "w1",
        }])

        # Trước khi sửa, dòng này ném sqlite3.IntegrityError: datatype mismatch.
        assert len(storage.recent_news(limit="1")) == 1
        assert len(storage.recent_news(limit=None)) == 1
        assert len(storage.recent_news(limit="rac")) == 1
