"""Test tích hợp — cần mạng thật và/hoặc Redis đang chạy.

Chạy riêng nhóm này::

    pytest -m integration

Bỏ qua khi chỉ muốn test nhanh::

    pytest -m "not integration"
"""

from __future__ import annotations

import pytest

from finagent.decision import vendor

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Nhận diện mã — không cần mạng nhưng gắn với ngữ nghĩa tài sản thật
# ---------------------------------------------------------------------------

class TestAssetDetection:
    def test_crypto(self):
        for symbol in ("BTCUSDT", "ETHUSDT", "BTC-USD", "ETHUSD", "SOLUSDT"):
            assert vendor.detect_asset_class(symbol) == "crypto"

    def test_vang(self):
        for symbol in ("XAUUSD", "SJL1L10", "DOJINHTV", "PQHNVM", "BT9999NTT"):
            assert vendor.detect_asset_class(symbol) == "gold"

    def test_co_phieu_viet_nam(self):
        for symbol in ("FPT", "VNM", "HPG", "VCB", "TCB"):
            assert vendor.detect_asset_class(symbol) == "vn_stock"

    def test_chuan_hoa_ma_binance(self):
        assert vendor.to_binance_symbol("BTC-USD") == "BTCUSDT"
        assert vendor.to_binance_symbol("btcusdt") == "BTCUSDT"
        assert vendor.to_binance_symbol("ETH") == "ETHUSDT"


# ---------------------------------------------------------------------------
# Thu thập dữ liệu thật
# ---------------------------------------------------------------------------

class TestLiveCollection:
    def test_gia_crypto_tu_binance(self):
        from finagent.collectors import crypto

        items, errors = crypto.collect(["BTCUSDT"])

        assert not errors, f"Lỗi khi lấy giá: {errors}"
        assert items[0]["price"] > 0
        assert items[0]["currency"] == "USDT"

    def test_gia_chung_khoan_vn(self):
        from finagent.collectors import vnstock

        items, errors = vnstock.collect(["FPT"])

        assert not errors, f"Lỗi khi lấy giá: {errors}"
        # Giá phải theo thang VND (hàng chục nghìn), không phải nghìn đồng.
        assert items[0]["price"] > 1000
        assert items[0]["currency"] == "VND"

    def test_gia_vang(self):
        from finagent.collectors import gold

        items, errors = gold.collect(["SJL1L10"])

        assert not errors, f"Lỗi khi lấy giá vàng: {errors}"
        assert items[0]["price"] > 1_000_000

    def test_tin_tuc_tu_rss(self):
        from finagent.collectors import news

        items, errors = news.collect(max_items=3, worker="test")

        assert len(items) > 0, f"Không lấy được bài nào. Lỗi: {errors}"
        assert all(item["title"] for item in items)
        assert all(item["url"].startswith("http") for item in items)


# ---------------------------------------------------------------------------
# Vendor cắm vào TradingAgents
# ---------------------------------------------------------------------------

class TestVendorBridge:
    def test_dang_ky_thanh_cong(self):
        vendor.register_vendor()

        from tradingagents.dataflows import router
        assert vendor.VENDOR_NAME in router.VENDOR_LIST
        assert vendor.VENDOR_NAME in router.VENDOR_METHODS["get_stock_data"]

    def test_lay_duoc_du_lieu_gia_crypto(self):
        output = vendor.get_stock_data("BTCUSDT", "2026-09-01", "2026-09-20")

        assert "date,open,high,low,close,volume" in output
        assert "LỖI DỮ LIỆU" not in output

    def test_lay_duoc_du_lieu_gia_vn(self):
        output = vendor.get_stock_data("FPT", "2026-08-01", "2026-09-20")

        assert "date,open,high,low,close,volume" in output

    def test_vang_bao_thieu_chuoi_nen_thay_vi_loi(self):
        """Vàng không có nến ngày — phải báo rõ ràng, không được ném lỗi."""
        output = vendor.get_stock_data("SJL1L10", "2026-09-01", "2026-09-20")

        assert "vàng" in output.lower()

    def test_chi_bao_ky_thuat(self):
        output = vendor.get_indicators("FPT", "rsi", "2026-09-20", 20)

        assert "RSI" in output.upper()

    def test_chi_bao_khong_ho_tro_bao_loi_mem(self):
        output = vendor.get_indicators("FPT", "khong_ton_tai", "2026-09-20", 10)

        assert "không được hỗ trợ" in output.lower()

    def test_du_lieu_co_ban_noi_ro_khong_co(self):
        output = vendor.get_fundamentals("FPT", "2026-09-20")

        assert "KHÔNG CÓ DỮ LIỆU" in output
        assert "không suy đoán" in output.lower()

    def test_cau_hinh_vendor_tro_ve_finagent(self):
        config = vendor.vendor_config()

        assert config["core_stock_apis"] == "finagent"
        assert config["news_data"] == "finagent"


# ---------------------------------------------------------------------------
# Lịch sử nến ngày cho mô hình ML
# ---------------------------------------------------------------------------

class TestDailyHistory:
    def test_lich_su_crypto_du_dai(self):
        history = vendor.daily_history("BTCUSDT", days=200)

        assert len(history) > 100
        assert all(row["price"] > 0 for row in history)

    def test_lich_su_vn_co_du_lieu(self):
        history = vendor.daily_history("FPT", days=200)

        assert len(history) > 50

    def test_vang_bao_loi_ro_rang(self):
        with pytest.raises(ValueError, match="vàng"):
            vendor.daily_history("SJL1L10")


# ---------------------------------------------------------------------------
# Lớp phân tán (cần Redis)
# ---------------------------------------------------------------------------

class TestDistributed:
    def test_semaphore_gioi_han_slot(self):
        """Chỉ được cấp tối đa ``capacity`` slot cùng lúc."""
        import redis as redis_lib

        from finagent.celery_app import SlotPool
        from finagent.config import settings

        client = redis_lib.Redis.from_url(settings.redis_url)
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Redis không chạy: {exc}")

        pool = SlotPool(client, "test:slots", capacity=3, ttl=30)
        client.delete("test:slots")

        tokens = [pool.acquire(wait_seconds=1) for _ in range(3)]
        assert all(tokens), "Phải giành được đủ 3 slot"
        assert pool.in_use() == 3

        # Slot thứ 4 phải bị từ chối vì đã đầy.
        assert pool.acquire(wait_seconds=1) is None

        # Trả lại một slot thì slot mới giành được.
        pool.release(tokens[0])
        assert pool.acquire(wait_seconds=1) is not None

        client.delete("test:slots")

    def test_slot_tu_het_han_khi_may_con_chet(self):
        """Máy con chết đột ngột không được làm kẹt slot vĩnh viễn."""
        import time

        import redis as redis_lib

        from finagent.celery_app import SlotPool
        from finagent.config import settings

        client = redis_lib.Redis.from_url(settings.redis_url)
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Redis không chạy: {exc}")

        pool = SlotPool(client, "test:ttl", capacity=1, ttl=1)
        client.delete("test:ttl")

        assert pool.acquire(wait_seconds=1) is not None
        assert pool.acquire(wait_seconds=1) is None      # đã đầy

        time.sleep(1.5)                                   # slot tự hết hạn
        assert pool.acquire(wait_seconds=2) is not None, "Slot phải được giải phóng sau khi hết hạn"

        client.delete("test:ttl")
