"""Test mô hình ML nhỏ: dựng đặc trưng, huấn luyện, dự đoán."""

from __future__ import annotations

import numpy as np
import pytest

from finagent.decision import ml_model


class TestBuildFeatures:
    def test_tra_ve_dung_so_dac_trung(self, price_series):
        closes, volumes = price_series
        X, y = ml_model.build_features(closes, volumes)

        assert X.shape[1] == len(ml_model.FEATURE_NAMES)
        assert len(X) == len(y)
        assert len(X) > 0

    def test_nhan_chi_la_0_hoac_1(self, price_series):
        closes, volumes = price_series
        _, y = ml_model.build_features(closes, volumes)
        assert set(np.unique(y)).issubset({0, 1})

    def test_du_lieu_qua_ngan_thi_bo_qua(self):
        """Dưới ngưỡng tối thiểu phải trả về rỗng thay vì dựng đặc trưng rác."""
        closes = np.linspace(100, 110, ml_model.MIN_SAMPLES - 5)
        volumes = np.ones(len(closes)) * 1000

        X, y = ml_model.build_features(closes, volumes)

        assert X.shape[0] == 0
        assert len(y) == 0

    def test_khong_ro_ri_du_lieu_tuong_lai(self, price_series):
        """Đặc trưng tại phiên i chỉ được dùng dữ liệu tới phiên i.

        Cắt bớt đuôi chuỗi không được làm thay đổi các dòng đặc trưng đã có.
        """
        closes, volumes = price_series
        X_full, _ = ml_model.build_features(closes, volumes)

        cut = len(closes) - 40
        X_cut, _ = ml_model.build_features(closes[:cut], volumes[:cut])

        # Số dòng chênh lệch đúng bằng phần đuôi bị cắt (trừ đi cửa sổ khởi động).
        overlap = min(len(X_cut), len(X_full))
        assert overlap > 0
        np.testing.assert_allclose(X_cut[:overlap], X_full[:overlap], rtol=1e-9)


class TestSmallMLModel:
    def test_huan_luyen_va_du_doan(self, price_series):
        closes, volumes = price_series
        X, y = ml_model.build_features(closes, volumes)

        model = ml_model.SmallMLModel().fit(X, y)

        assert model.trained
        assert model.samples == len(X)
        assert 0.0 <= model.train_accuracy <= 1.0

        probability = model.predict_proba(X[-1])
        assert 0.0 <= probability <= 1.0

    def test_chua_huan_luyen_thi_tra_ve_0_5(self):
        model = ml_model.SmallMLModel()
        assert not model.trained
        assert model.predict_proba(np.zeros(len(ml_model.FEATURE_NAMES))) == 0.5

    def test_khong_huan_luyen_khi_thieu_du_lieu(self):
        X = np.random.default_rng(0).normal(size=(5, len(ml_model.FEATURE_NAMES)))
        y = np.array([0, 1, 0, 1, 0])

        model = ml_model.SmallMLModel().fit(X, y)

        assert not model.trained

    def test_moi_dac_trung_deu_co_trong_bao_cao(self, price_series):
        closes, volumes = price_series
        X, y = ml_model.build_features(closes, volumes)
        model = ml_model.SmallMLModel().fit(X, y)

        assert set(model.feature_importances) == set(ml_model.FEATURE_NAMES)


class TestPredictFromHistory:
    def test_du_lieu_du_thi_co_ket_qua(self, price_series):
        closes, _ = price_series
        prices = [{"price": float(c), "volume": 1_000_000.0} for c in closes]

        result = ml_model.predict_from_history(prices)

        assert result["available"] is True
        assert 0.0 <= result["probability"] <= 1.0
        assert result["samples"] > 0

    def test_du_lieu_thieu_thi_bao_khong_kha_dung(self):
        prices = [{"price": 100.0 + i} for i in range(10)]

        result = ml_model.predict_from_history(prices)

        assert result["available"] is False
        assert result["probability"] == 0.5
        assert "Chưa đủ dữ liệu" in result["reason"]

    def test_du_lieu_rong_khong_gay_loi(self):
        result = ml_model.predict_from_history([])
        assert result["available"] is False


class TestRSI:
    def test_rsi_luon_trong_khoang_0_1(self, price_series):
        closes, _ = price_series
        values = ml_model._rsi(closes)
        assert values.min() >= 0.0
        assert values.max() <= 1.0

    def test_chuoi_tang_deu_cho_rsi_cao(self):
        closes = np.linspace(100, 200, 60)
        values = ml_model._rsi(closes)
        assert values[-1] > 0.9
