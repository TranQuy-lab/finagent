"""Test tầng lưu trữ và broker mô phỏng (chạy trên cơ sở dữ liệu tạm)."""

from __future__ import annotations

import pytest

from finagent import storage
from finagent.broker.paper import FEE_RATE, PaperBroker, _round_quantity


class TestStoragePrices:
    def test_luu_va_doc_lai_gia(self, temp_db):
        count = storage.save_prices([{
            "symbol": "FPT", "asset_class": "vn_stock", "price": 64_700.0,
            "currency": "VND", "change_pct_24h": -0.9, "volume": 3_500_000.0,
            "source": "test", "captured_at": "2026-09-28T01:00:00+00:00",
        }])

        assert count == 1
        latest = storage.latest_price("FPT")
        assert latest is not None
        assert latest["price"] == 64_700.0

    def test_tra_ve_none_khi_chua_co_gia(self, temp_db):
        assert storage.latest_price("KHONGCO") is None

    def test_luu_danh_sach_rong_khong_loi(self, temp_db):
        assert storage.save_prices([]) == 0


class TestStorageNews:
    def _item(self, title="Tin A", url="https://a.vn/1", topics=None):
        return {
            "uid": f"uid-{url}", "title": title, "url": url, "source": "Test",
            "published_at": "2026-09-28T01:00:00+00:00", "summary": "...",
            "topics": topics or ["stock"], "collected_by": "worker-1",
        }

    def test_luu_tin_moi(self, temp_db):
        assert storage.save_news([self._item()]) == 1

    def test_khu_trung_lap_theo_uid(self, temp_db):
        storage.save_news([self._item()])
        # Cùng uid nhưng do máy con khác gửi → không được ghi thêm.
        duplicate = self._item()
        duplicate["collected_by"] = "worker-2"

        assert storage.save_news([duplicate]) == 0
        assert storage.get_conn().execute("SELECT COUNT(*) n FROM news").fetchone()["n"] == 1

    def test_loc_tin_theo_chu_de(self, temp_db):
        storage.save_news([
            self._item(url="https://a.vn/1", topics=["gold"]),
            self._item(url="https://a.vn/2", topics=["stock"]),
        ])

        gold_news = storage.recent_news(topic="gold")

        assert len(gold_news) == 1
        assert gold_news[0]["topics"] == ["gold"]

    def test_dem_tin_theo_chu_de(self, temp_db):
        storage.save_news([
            self._item(url="https://a.vn/1", topics=["gold", "macro"]),
            self._item(url="https://a.vn/2", topics=["gold"]),
        ])

        counts = storage.news_topic_counts()

        assert counts["gold"] == 2
        assert counts["macro"] == 1


class TestWorkerEvents:
    def test_ghi_nhat_ky_may_con(self, temp_db):
        storage.log_worker_event("worker-1", "thu thập price", "12 bản ghi, 800ms")

        row = storage.get_conn().execute(
            "SELECT * FROM worker_events WHERE worker='worker-1'"
        ).fetchone()

        assert row is not None
        assert row["kind"] == "thu thập price"


class TestRoundQuantity:
    def test_co_phieu_lam_tron_lo_100(self):
        assert _round_quantity(1550.0, "vn_stock") == 1500.0

    def test_co_phieu_duoi_100_thanh_0(self):
        assert _round_quantity(50.0, "vn_stock") == 0.0

    def test_crypto_giu_phan_le(self):
        assert _round_quantity(0.1234567, "crypto") == 0.123456


class TestPaperBroker:
    def test_so_du_ban_dau_cua_vi_vnd(self, temp_db):
        from finagent.config import settings

        assert PaperBroker().get_cash("VND") == settings.paper_starting_cash

    def test_so_du_ban_dau_cua_vi_usdt(self, temp_db):
        from finagent.config import settings

        assert PaperBroker().get_cash("USDT") == settings.paper_starting_usdt

    def test_mua_thanh_cong_va_tru_tien(self, temp_db):
        broker = PaperBroker()
        cash_before = broker.get_cash("VND")

        result = broker.buy("FPT", 100, 64_700.0)

        assert result.ok and result.status == "filled"
        cost = 100 * 64_700.0
        assert broker.get_cash("VND") == pytest.approx(cash_before - cost * (1 + FEE_RATE))

    def test_mua_tao_vi_the(self, temp_db):
        broker = PaperBroker()
        broker.buy("FPT", 100, 64_700.0)

        position = broker.get_position("FPT")

        assert position is not None
        assert position["quantity"] == 100
        assert position["avg_price"] == 64_700.0

    def test_mua_hai_lan_bi_nh_dung_gia_von(self, temp_db):
        broker = PaperBroker()
        broker.buy("FPT", 100, 60_000.0)
        broker.buy("FPT", 100, 70_000.0)

        position = broker.get_position("FPT")

        assert position["quantity"] == 200
        assert position["avg_price"] == pytest.approx(65_000.0)

    def test_khong_du_tien_thi_tu_choi(self, temp_db):
        broker = PaperBroker()

        result = broker.buy("FPT", 100_000, 64_700.0)   # ~6,5 tỷ, vượt 1 tỷ

        assert result.ok is False
        assert result.status == "rejected"
        assert "không đủ tiền" in result.message.lower()

    def test_ban_khi_khong_co_vi_the_thi_tu_choi(self, temp_db):
        result = PaperBroker().sell("FPT", 100, 64_700.0)

        assert result.ok is False
        assert "Không có vị thế" in result.message

    def test_ban_het_thi_xoa_vi_the(self, temp_db):
        broker = PaperBroker()
        broker.buy("FPT", 100, 64_700.0)

        broker.sell("FPT", 100, 70_000.0)

        assert broker.get_position("FPT") is None

    def test_ban_mot_phan_thi_giu_phan_con_lai(self, temp_db):
        broker = PaperBroker()
        broker.buy("FPT", 200, 64_700.0)

        broker.sell("FPT", 100, 70_000.0)

        assert broker.get_position("FPT")["quantity"] == 100

    def test_khoi_luong_lam_tron_ve_0_thi_tu_choi(self, temp_db):
        result = PaperBroker().buy("FPT", 50, 64_700.0)

        assert result.ok is False
        assert "bằng 0" in result.message

    def test_gia_tri_danh_muc_gom_tien_va_vi_the(self, temp_db):
        broker = PaperBroker()
        broker.buy("FPT", 100, 64_700.0)

        value = broker.portfolio_value(lambda symbol: 64_700.0)

        # Giá thị trường bằng giá vốn nên chỉ mất phí; cộng thêm ví USDT quy về VND.
        from finagent.config import settings

        cost = 100 * 64_700.0
        usdt_in_vnd = settings.paper_starting_usdt * settings.usdt_vnd_rate
        assert value == pytest.approx(broker.get_cash("VND") + cost + usdt_in_vnd)

    def test_mua_nhieu_ma_khac_nhau(self, temp_db):
        broker = PaperBroker()
        broker.buy("FPT", 100, 64_700.0)
        broker.buy("VNM", 100, 59_800.0)

        assert len(broker.list_positions()) == 2


class TestMultiCurrency:
    """Crypto niêm yết bằng USDT, chứng khoán và vàng bằng VND.

    Đây là bài học từ một lỗi thật: nếu dùng chung một số dư, lệnh mua ETH
    (2.660 USDT) sẽ bị trừ nhầm vào ví tiền đồng.
    """

    def test_mua_crypto_tru_dung_vi_usdt(self, temp_db):
        broker = PaperBroker()
        usdt_before = broker.get_cash("USDT")
        vnd_before = broker.get_cash("VND")

        result = broker.buy("BTCUSDT", 0.01, 83_000.0, asset_class="crypto")

        assert result.ok
        assert result.currency == "USDT"
        assert broker.get_cash("USDT") < usdt_before
        assert broker.get_cash("VND") == vnd_before, "Ví tiền đồng không được bị đụng tới"

    def test_mua_chung_khoan_khong_dung_vi_usdt(self, temp_db):
        broker = PaperBroker()
        usdt_before = broker.get_cash("USDT")

        broker.buy("FPT", 100, 64_700.0, asset_class="vn_stock")

        assert broker.get_cash("USDT") == usdt_before

    def test_vi_the_luu_dung_loai_tien(self, temp_db):
        broker = PaperBroker()
        broker.buy("BTCUSDT", 0.01, 83_000.0, asset_class="crypto")
        broker.buy("FPT", 100, 64_700.0, asset_class="vn_stock")

        assert broker.get_position("BTCUSDT")["currency"] == "USDT"
        assert broker.get_position("FPT")["currency"] == "VND"

    def test_crypto_khong_bi_gioi_han_boi_vi_vnd(self, temp_db):
        """Ví USDT có 5.000, phải mua được lượng crypto trong hạn mức đó."""
        broker = PaperBroker()

        result = broker.buy("BTCUSDT", 0.05, 83_000.0, asset_class="crypto")  # 4.150 USDT

        assert result.ok, "Phải mua được bằng ví USDT dù ví VND không liên quan"

    def test_ban_crypto_tra_tien_ve_vi_usdt(self, temp_db):
        broker = PaperBroker()
        broker.buy("BTCUSDT", 0.01, 83_000.0, asset_class="crypto")
        usdt_after_buy = broker.get_cash("USDT")

        broker.sell("BTCUSDT", 0.01, 90_000.0, asset_class="crypto")

        assert broker.get_cash("USDT") > usdt_after_buy

    def test_tong_danh_muc_quy_ve_vnd(self, temp_db):
        broker = PaperBroker()

        value = broker.portfolio_value(lambda symbol: None, usdt_vnd=25_000.0)

        from finagent.config import settings
        expected = settings.paper_starting_cash + settings.paper_starting_usdt * 25_000.0
        assert value == pytest.approx(expected)

    def test_currency_for_tung_nhom_tai_san(self, temp_db):
        from finagent.broker.paper import currency_for

        assert currency_for("crypto") == "USDT"
        assert currency_for("vn_stock") == "VND"
        assert currency_for("gold") == "VND"
        assert currency_for("khong_biet") == "VND"
