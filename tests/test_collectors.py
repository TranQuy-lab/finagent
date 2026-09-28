"""Test các bộ thu thập dữ liệu (phần chạy được ngoại tuyến)."""

from __future__ import annotations

from finagent.collectors import news
from finagent.collectors.base import NewsItem, PricePoint


class TestNewsTopics:
    def test_nhan_dien_chu_de_vang(self):
        topics = news.detect_topics("Giá vàng SJC hôm nay tăng mạnh")
        assert "gold" in topics

    def test_nhan_dien_chu_de_bat_dong_san(self):
        topics = news.detect_topics("Thị trường bất động sản phía Nam khởi sắc")
        assert "real_estate" in topics

    def test_nhan_dien_chu_de_chung_khoan(self):
        topics = news.detect_topics("VN-Index vượt mốc 1.300 điểm")
        assert "stock" in topics

    def test_nhan_dien_chu_de_crypto(self):
        topics = news.detect_topics("Bitcoin lập đỉnh mới, Ethereum theo sau")
        assert "crypto" in topics

    def test_nhan_dien_chu_de_vi_mo(self):
        topics = news.detect_topics("Fed quyết định lãi suất, lạm phát hạ nhiệt")
        assert "macro" in topics

    def test_tieu_de_khong_lien_quan_thi_rong(self):
        assert news.detect_topics("Kết quả bóng đá tối qua") == []

    def test_van_ban_rong_khong_gay_loi(self):
        assert news.detect_topics("") == []
        assert news.detect_topics(None) == []

    def test_mot_bai_co_the_thuoc_nhieu_chu_de(self):
        topics = news.detect_topics("Chứng khoán và bất động sản dẫn dắt thị trường")
        assert "stock" in topics and "real_estate" in topics


class TestStripHtml:
    def test_bo_the_html(self):
        assert news._strip_html("<p>Xin <b>chào</b></p>") == "Xin chào"

    def test_van_ban_rong(self):
        assert news._strip_html("") == ""
        assert news._strip_html(None) == ""


class TestChunkSources:
    def test_chia_deu_cac_nguon(self):
        chunks = news.chunk_sources(3)

        assert len(chunks) == 3
        total = sum(len(chunk) for chunk in chunks)
        assert total == len(news.NEWS_FEEDS)

    def test_khong_trung_nguon_giua_cac_may(self):
        chunks = news.chunk_sources(3)
        flattened = [source for chunk in chunks for source in chunk]

        assert len(flattened) == len(set(flattened)), "Một nguồn không được giao cho hai máy con"

    def test_so_may_lon_hon_so_nguon(self):
        chunks = news.chunk_sources(50)

        assert len(chunks) <= len(news.NEWS_FEEDS)
        assert all(chunk for chunk in chunks), "Không trả về nhóm rỗng"

    def test_so_may_bang_mot(self):
        chunks = news.chunk_sources(1)

        assert len(chunks) == 1
        assert len(chunks[0]) == len(news.NEWS_FEEDS)

    def test_so_may_khong_hop_le(self):
        chunks = news.chunk_sources(0)
        assert len(chunks) == 1


class TestNewsFeeds:
    def test_tat_ca_nguon_deu_dung_https(self):
        for name, url in news.NEWS_FEEDS.items():
            assert url.startswith("https://"), f"{name} không dùng HTTPS"

    def test_co_it_nhat_mot_nguon_moi_chu_de(self):
        """Phải có nguồn cho từng chủ đề đề tài yêu cầu."""
        from collections import Counter

        # Bản thân tên nguồn đã cho biết chủ đề bao phủ.
        names = " ".join(news.NEWS_FEEDS).lower()
        assert "chứng khoán" in names
        assert "bất động sản" in names


class TestNewsItem:
    def test_uid_on_dinh_voi_cung_du_lieu(self):
        item1 = NewsItem(title="Tin A", url="https://a.vn/1", source="A")
        item2 = NewsItem(title="Tin A", url="https://a.vn/1", source="B")

        assert item1.uid == item2.uid, "Cùng bài thì phải cùng mã, bất kể nguồn"

    def test_uid_khac_nhau_voi_bai_khac(self):
        item1 = NewsItem(title="Tin A", url="https://a.vn/1", source="A")
        item2 = NewsItem(title="Tin B", url="https://a.vn/2", source="A")

        assert item1.uid != item2.uid

    def test_to_dict_co_uid(self):
        data = NewsItem(title="Tin A", url="https://a.vn/1", source="A").to_dict()
        assert "uid" in data


class TestPricePoint:
    def test_to_dict_giu_du_truong(self):
        point = PricePoint(
            symbol="FPT", asset_class="vn_stock", price=64_700.0,
            currency="VND", change_pct_24h=-0.9, volume=3_543_900.0, source="test",
        )
        data = point.to_dict()

        assert data["symbol"] == "FPT"
        assert data["asset_class"] == "vn_stock"
        assert data["price"] == 64_700.0
        assert data["currency"] == "VND"


class TestGoldSpread:
    def test_tinh_chenh_lech_mua_ban(self):
        from finagent.collectors import gold

        spread = gold._spread_pct({"buy": 100.0, "sell": 105.0})

        assert spread == 5.0

    def test_thieu_gia_thi_tra_ve_none(self):
        from finagent.collectors import gold

        assert gold._spread_pct({"buy": 0, "sell": 100}) is None
        assert gold._spread_pct({}) is None

    def test_co_ten_hien_thi_cho_cac_thuong_hieu_chinh(self):
        from finagent.collectors import gold

        for key in gold.DEFAULT_GOLD_KEYS:
            assert gold.label_for(key)
