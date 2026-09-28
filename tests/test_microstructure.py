"""Test tầng tổng hợp thông tin: vi mô thị trường và đối chiếu chéo nguồn giá.

Không gọi mạng thật. Mọi phản hồi của sàn được giả lập, để test chạy được cả khi
mất mạng và cho kết quả xác định.

Bài quan trọng nhất ở đây là ``TestSsiPriceFieldPriority`` — nó khoá lại một lỗi
thật đã từng có: hàm dự phòng SSI đọc ``refPrice`` (giá đóng cửa hôm trước) thay vì
``matchedPrice`` (giá khớp gần nhất), khiến hệ thống ghi giá cũ như giá hiện tại.
Lỗi chỉ lộ ra nhờ đối chiếu chéo với nguồn thứ hai.
"""

from __future__ import annotations

import pytest

from finagent.collectors import microstructure as ms
from finagent.collectors import vnstock


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Session giả: trả về phản hồi đã định trước thay vì gọi mạng.

    Phải truyền đối tượng này vào hàm cần test. Vá ``requests.Session`` không có
    tác dụng vì các hàm nhận session qua tham số, nên vá sai chỗ sẽ khiến test âm
    thầm gọi mạng thật và cho kết quả thay đổi theo thị trường.
    """

    def __init__(self, payload) -> None:
        self._payload = payload

    def get(self, *args, **kwargs):
        if isinstance(self._payload, Exception):
            raise self._payload
        return _FakeResponse(self._payload)


def _ssi_payload(**overrides) -> dict:
    """Phản hồi SSI iBoard thu gọn, đủ trường để kiểm tra."""
    data = {
        "matchedPrice": 63_400,
        "avgPrice": 63_483.9,
        "refPrice": 64_700,
        "priorClosePrice": 64_700,
        "ceiling": 69_200,
        "floor": 60_200,
        "priceChangePercent": -2.01,
        "nmTotalTradedQty": 3_537_400,
        "best1Bid": 63_300, "best1BidVol": 42_200,
        "best1Offer": 63_400, "best1OfferVol": 36_900,
        "best2Bid": 63_200, "best2BidVol": 272_600,
        "best2Offer": 63_500, "best2OfferVol": 26_000,
        "best3Bid": 63_100, "best3BidVol": 323_700,
        "best3Offer": 63_600, "best3OfferVol": 24_600,
        "buyForeignValue": 35_322_575_200,
        "sellForeignValue": 30_405_630_400,
        "remainForeignQtty": 351_630_791,
        "stockBUVol": 1_043_400,
        "stockSDVol": 2_505_800,
    }
    data.update(overrides)
    return {"code": "SUCCESS", "message": "ok", "data": data}


# ---------------------------------------------------------------------------
# Lỗi thật: thứ tự ưu tiên trường giá của SSI
# ---------------------------------------------------------------------------

class TestSsiPriceFieldPriority:
    def test_dung_gia_khop_chu_khong_dung_gia_tham_chieu(self):
        """``refPrice`` là giá đóng cửa hôm trước — dùng nó là ghi giá cũ."""
        point = vnstock._fetch_via_ssi("FPT", _FakeSession(_ssi_payload()))

        assert point.price == 63_400, (
            "phải lấy matchedPrice; lấy refPrice (64.700) nghĩa là đang ghi giá "
            "đóng cửa hôm trước như giá hiện tại"
        )
        assert point.source == "ssi_iboard"

    def test_khong_co_gia_khop_thi_dung_binh_quan(self):
        session = _FakeSession(_ssi_payload(matchedPrice=None, avgPrice=63_483.9))
        point = vnstock._fetch_via_ssi("FPT", session)

        assert point.price == pytest.approx(63_483.9)

    def test_ngoai_gio_chua_co_gi_thi_dung_tham_chieu_va_ghi_ro(self):
        """Chưa có giá khớp nào thì đành dùng giá tham chiếu, nhưng phải nói rõ."""
        session = _FakeSession(_ssi_payload(matchedPrice=None, avgPrice=None))
        point = vnstock._fetch_via_ssi("FPT", session)

        assert point.price == 64_700
        assert point.source == "ssi_iboard_refprice", "phải đánh dấu là giá tham chiếu"

    def test_khong_co_gia_nao_thi_bao_loi(self):
        session = _FakeSession(_ssi_payload(matchedPrice=None, avgPrice=None,
                                            refPrice=None, priorClosePrice=None))

        with pytest.raises(RuntimeError, match="không trả về giá"):
            vnstock._fetch_via_ssi("FPT", session)


# ---------------------------------------------------------------------------
# Đối chiếu chéo nguồn giá
# ---------------------------------------------------------------------------

class TestCrossCheck:
    def _patch_sources(self, monkeypatch, dchart_price, ssi_price, dchart_fails=False):
        def fake_dchart(symbol, session):
            if dchart_fails:
                raise RuntimeError("dchart sập")
            return vnstock.PricePoint(
                symbol=symbol, asset_class="vn_stock", price=dchart_price,
                currency="VND", change_pct_24h=None, volume=None, source="vndirect_dchart",
            )

        def fake_ssi(symbol, session):
            return vnstock.PricePoint(
                symbol=symbol, asset_class="vn_stock", price=ssi_price,
                currency="VND", change_pct_24h=None, volume=None, source="ssi_iboard",
            )

        monkeypatch.setattr(vnstock, "_fetch_via_dchart", fake_dchart)
        monkeypatch.setattr(vnstock, "_fetch_via_ssi", fake_ssi)

    def test_hai_nguon_khop_thi_tin_cay_cao(self, monkeypatch):
        self._patch_sources(monkeypatch, 63_400, 63_400)

        result = vnstock.cross_check_price("FPT")

        assert result["agreed"] is True
        assert result["confidence"] == "high"
        assert result["consensus_price"] == pytest.approx(63_400)

    def test_lech_nhau_thi_bao_xung_dot(self, monkeypatch):
        self._patch_sources(monkeypatch, 63_400, 64_700)     # lệch 2%

        result = vnstock.cross_check_price("FPT")

        assert result["agreed"] is False
        assert result["confidence"] == "conflict"
        assert "LỆCH NHAU" in result["note"]

    def test_lech_nho_trong_dung_sai_van_chap_nhan(self, monkeypatch):
        """Bước giá Việt Nam là 10–50 đồng nên lệch vài chục đồng là bình thường."""
        self._patch_sources(monkeypatch, 63_400, 63_450)     # lệch 0,08%

        result = vnstock.cross_check_price("FPT")

        assert result["agreed"] is True

    def test_mot_nguon_sap_thi_tin_cay_thap_chu_khong_bao_xung_dot(self, monkeypatch):
        self._patch_sources(monkeypatch, 0, 63_400, dchart_fails=True)

        result = vnstock.cross_check_price("FPT")

        assert result["agreed"] is True
        assert result["confidence"] == "low"
        assert "Chỉ một nguồn" in result["note"]

    def test_ca_hai_nguon_sap_thi_khong_nem_loi(self, monkeypatch):
        def boom(symbol, session):
            raise RuntimeError("sập")

        monkeypatch.setattr(vnstock, "_fetch_via_dchart", boom)
        monkeypatch.setattr(vnstock, "_fetch_via_ssi", boom)

        result = vnstock.cross_check_price("FPT")

        assert result["confidence"] == "none"
        assert result["agreed"] is False


# ---------------------------------------------------------------------------
# Tín hiệu vi mô
# ---------------------------------------------------------------------------

class TestMicrostructureSignals:
    def _micro(self, **overrides):
        base = dict(symbol="FPT", asset_class="vn_stock")
        base.update(overrides)
        return ms.Microstructure(**base)

    def test_khoi_ngoai_mua_rong(self):
        signals = self._micro(foreign_net_value=6_171_970_600).signals()

        assert any("mua ròng 6.2 tỷ" in s for s in signals)

    def test_khoi_ngoai_ban_rong(self):
        signals = self._micro(foreign_net_value=-21_197_778_700).signals()

        assert any("bán ròng 21.2 tỷ" in s for s in signals)

    def test_dong_tien_chu_dong_nghieng_ve_ban(self):
        signals = self._micro(active_buy_ratio=0.31).signals()

        assert any("bên bán" in s for s in signals)

    def test_hai_nguong_khac_nhau_theo_nhom_tai_san(self):
        """Crypto đo trong dải hẹp nên mất cân bằng nhỏ hơn nhiều so với SSI."""
        crypto = self._micro(asset_class="crypto", book_imbalance=0.12).signals()
        vn = self._micro(asset_class="vn_stock", book_imbalance=0.12).signals()

        assert any("Thanh khoản" in s for s in crypto), "12% phải đủ để báo với crypto"
        assert not any("Thanh khoản" in s for s in vn), "12% chưa đủ để báo với chứng khoán"

    def test_tin_hieu_khong_vo_nghia_khi_thieu_du_lieu(self):
        """Không có dữ liệu thì không được bịa ra tín hiệu nào."""
        assert self._micro().signals() == []

    def test_funding_rate_duong_bao_phe_long_tra_phi(self):
        signals = self._micro(asset_class="crypto", funding_rate=0.0005).signals()

        assert any("long đang trả phí" in s for s in signals)

    def test_phong_dai_funding_qua_nho_thi_bo_qua(self):
        signals = self._micro(asset_class="crypto", funding_rate=0.000001).signals()

        assert not any("funding" in s.lower() for s in signals)


class TestMicrostructureFormatting:
    def test_khong_co_nguon_thi_noi_ro_ly_do(self):
        micro = ms.Microstructure(symbol="SJL1L10", asset_class="gold",
                                  errors=["vàng không có sổ lệnh"])

        text = ms.format_microstructure(micro)

        assert "KHÔNG CÓ DỮ LIỆU VI MÔ" in text
        assert "vàng không có sổ lệnh" in text

    def test_co_du_lieu_thi_liet_ke_nguon(self):
        micro = ms.Microstructure(symbol="FPT", asset_class="vn_stock",
                                  foreign_net_value=1e9, sources_ok=["ssi_iboard"])

        text = ms.format_microstructure(micro)

        assert "ssi_iboard" in text
        assert "Khối ngoại ròng" in text

    def test_nhac_ro_day_khong_phai_gia_giao_dich(self):
        """Dữ liệu vi mô không được để mô hình nhầm là giá để đặt lệnh."""
        micro = ms.Microstructure(symbol="FPT", asset_class="vn_stock",
                                  book_imbalance=0.3, sources_ok=["ssi_iboard"])

        text = ms.format_microstructure(micro)

        assert "KHÔNG phải giá đã khớp" in text


class TestDepthChoice:
    """Vì sao dùng sổ lệnh sâu và dải ±0,5% thay vì 'N mức đầu tiên'."""

    def test_dai_gia_la_hang_so_co_giai_thich(self):
        assert ms.NEAR_TOUCH_BAND == 0.005
        doc = ms._fetch_book_crypto.__doc__ or ""

        assert "nhiễu" in doc.lower(), "phải ghi lại vì sao không dùng 'N mức'"

    def test_van_xin_so_lenh_sau(self, monkeypatch):
        """Xin ít mức thì tín hiệu là nhiễu — phải luôn xin sổ lệnh sâu."""
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["limit"] = params["limit"]
            return _FakeResponse({
                "bids": [["100.0", "1.0"]],
                "asks": [["100.1", "1.0"]],
            })

        # Hàm này gọi ``requests.get`` trực tiếp (không nhận session), nên vá ở đây
        # là đúng chỗ.
        monkeypatch.setattr(ms.requests, "get", fake_get)

        result = ms.Microstructure(symbol="BTCUSDT", asset_class="crypto")
        ms._fetch_book_crypto("BTCUSDT", 50, result)

        assert captured["limit"] >= 1000, "phải xin ít nhất 1000 mức"
