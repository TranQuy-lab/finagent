"""Test bộ máy ra quyết định: hợp nhất tín hiệu và tính khối lượng lệnh."""

from __future__ import annotations

import pytest

from finagent.decision import engine


class TestCombineSignals:
    def test_ca_hai_deu_mua_thi_tang_do_tin_cay(self):
        ml = {"available": True, "probability": 0.80, "reason": "ml"}
        llm = {"available": True, "signal": "Buy", "reason": "llm"}

        signal, confidence, source, _ = engine.combine_signals(ml, llm)

        assert signal == "Buy"
        assert source == "llm+ml"
        assert confidence > 0.5

    def test_ca_hai_deu_ban(self):
        ml = {"available": True, "probability": 0.20, "reason": "ml"}
        llm = {"available": True, "signal": "Sell", "reason": "llm"}

        signal, _, source, _ = engine.combine_signals(ml, llm)

        assert signal == "Sell"
        assert source == "llm+ml"

    def test_mau_thuan_thi_dung_ngoai(self):
        """LLM mua nhưng ML dự báo giảm → phải thận trọng, không giao dịch."""
        ml = {"available": True, "probability": 0.20, "reason": "ml"}
        llm = {"available": True, "signal": "Buy", "reason": "llm"}

        signal, confidence, _, _ = engine.combine_signals(ml, llm)

        assert signal == "Hold"
        assert confidence < 0.5

    def test_llm_giu_thi_ket_qua_la_giu(self):
        ml = {"available": True, "probability": 0.80, "reason": "ml"}
        llm = {"available": True, "signal": "Hold", "reason": "llm"}

        signal, _, _, _ = engine.combine_signals(ml, llm)

        assert signal == "Hold"

    def test_chi_co_ml(self):
        ml = {"available": True, "probability": 0.90, "reason": "ml"}

        signal, confidence, source, _ = engine.combine_signals(ml, {"available": False})

        assert signal == "Buy"
        assert source == "ml"
        assert confidence > 0.5

    def test_chi_co_llm(self):
        llm = {"available": True, "signal": "Overweight", "reason": "llm"}

        signal, _, source, _ = engine.combine_signals({"available": False}, llm)

        assert signal == "Overweight"
        assert source == "llm"

    def test_khong_co_nguon_nao_thi_giu(self):
        signal, confidence, source, _ = engine.combine_signals({}, {})

        assert signal == "Hold"
        assert confidence == 0.0
        assert source == "none"

    def test_xac_suat_50_50_cho_tin_hieu_giu(self):
        ml = {"available": True, "probability": 0.50, "reason": "ml"}

        signal, _, _, _ = engine.combine_signals(ml, {"available": False})

        assert signal == "Hold"

    def test_do_tin_cay_ml_nam_trong_khoang_hop_le(self):
        for probability in (0.0, 0.25, 0.5, 0.75, 1.0):
            ml = {"available": True, "probability": probability, "reason": "ml"}
            _, confidence, _, _ = engine.combine_signals(ml, {"available": False})

            assert 0.0 <= confidence <= 1.0
            assert confidence >= 0.5, "Điểm tin cậy không được thấp hơn mức 50/50"


class TestSizeOrder:
    def test_dung_ngoai_thi_khoi_luong_bang_0(self):
        assert engine.size_order("hold", 100.0, 1e9, 1e9, 0.0, "vn_stock") == 0.0

    def test_mua_gioi_han_theo_ty_le_von(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "max_position_pct", 0.10, raising=False)

        quantity = engine.size_order("buy", 100_000.0, 1e9, 1e9, 0.0, "vn_stock")

        # 10% của 1 tỷ = 100 triệu; chia 100.000 = 1000 cổ phiếu.
        assert quantity == 1000.0

    def test_mua_khong_vuot_qua_tien_mat(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "max_position_pct", 0.50, raising=False)

        quantity = engine.size_order("buy", 100_000.0, 10_000_000.0, 1e9, 0.0, "vn_stock")

        # Tiền mặt chỉ 10 triệu → tối đa ~98 cổ phiếu, làm tròn lô 100 thành 0.
        assert quantity * 100_000 <= 10_000_000
        assert quantity in (0.0, 100.0)

    def test_co_phieu_vn_lam_tron_lo_100(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "max_position_pct", 0.10, raising=False)

        quantity = engine.size_order("buy", 64_700.0, 1e9, 1e9, 0.0, "vn_stock")

        assert quantity % 100 == 0

    def test_crypto_cho_phep_khoi_luong_le(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "max_position_pct", 0.10, raising=False)

        quantity = engine.size_order("buy", 83_000.0, 1e9, 1e9, 0.0, "crypto")

        assert quantity > 0
        assert quantity % 100 != 0     # không bị ép về lô 100

    def test_ban_thi_ban_het_vi_the(self):
        quantity = engine.size_order("sell", 100_000.0, 1e9, 1e9, 1500.0, "vn_stock")

        assert quantity == 1500.0

    def test_gia_bang_0_khong_gay_loi_chia(self):
        assert engine.size_order("buy", 0.0, 1e9, 1e9, 0.0, "vn_stock") == 0.0


class TestProposal:
    def _proposal(self, **overrides):
        base = dict(
            symbol="FPT", asset_class="vn_stock", action="buy", signal="Buy",
            confidence=0.80, price=64_700.0, quantity=100.0, amount=6_470_000.0,
        )
        base.update(overrides)
        return engine.Proposal(**base)

    def test_de_xuat_du_dieu_kien(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "min_signal_confidence", 0.55, raising=False)

        assert self._proposal().actionable is True

    def test_do_tin_cay_thap_thi_khong_hanh_dong(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "min_signal_confidence", 0.55, raising=False)

        assert self._proposal(confidence=0.30).actionable is False

    def test_lenh_nho_khong_can_duyet_rieng(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "require_approval_above", 5_000_000.0, raising=False)

        assert self._proposal(amount=1_000_000.0).needs_approval is False

    def test_lenh_lon_can_nguoi_dung_duyet(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "require_approval_above", 5_000_000.0, raising=False)

        assert self._proposal(amount=50_000_000.0).needs_approval is True


class TestSignalConfidence:
    def test_moi_muc_tin_hieu_deu_co_diem(self):
        for signal in ("Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW"):
            assert signal in engine.SIGNAL_CONFIDENCE

    def test_thang_bac_tin_hieu_giam_dan(self):
        assert engine.SIGNAL_CONFIDENCE["Buy"] > engine.SIGNAL_CONFIDENCE["Overweight"]
        assert engine.SIGNAL_CONFIDENCE["Overweight"] > engine.SIGNAL_CONFIDENCE["Hold"]
        assert engine.SIGNAL_CONFIDENCE["Hold"] > engine.SIGNAL_CONFIDENCE["Underweight"]
        assert engine.SIGNAL_CONFIDENCE["Underweight"] > engine.SIGNAL_CONFIDENCE["Sell"]
