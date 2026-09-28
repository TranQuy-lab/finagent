"""Mô hình dự đoán ML nhỏ — phương án dự phòng khi không gọi được LLM.

Đây là "mô hình dự đoán ML nhỏ" mà đề tài nhắc tới: hồi quy logistic huấn luyện
trên chính chuỗi giá do các máy con thu thập, dùng các đặc trưng kỹ thuật cơ bản.

Toàn bộ đặc trưng chỉ dùng dữ liệu **quá khứ** tại thời điểm dự đoán (không nhìn
về tương lai), nhờ vậy kết quả đánh giá không bị rò rỉ dữ liệu.

Cài đặt thuần ``numpy`` (đã có sẵn theo pandas) để không phát sinh thêm phụ thuộc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

#: Số phiên tối thiểu để huấn luyện — dưới ngưỡng này mô hình không đáng tin.
MIN_SAMPLES = 40

FEATURE_NAMES = (
    "return_1d",
    "return_5d",
    "return_20d",
    "rsi_14",
    "price_vs_sma20",
    "volatility_20",
    "volume_ratio",
)


def _returns(closes: np.ndarray) -> np.ndarray:
    """Chuỗi tỷ suất sinh lời theo ngày."""
    return np.diff(closes) / closes[:-1]


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Chỉ số sức mạnh tương đối, chuẩn hoá về khoảng 0..1."""
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    values = np.full(len(closes), 0.5)

    if len(deltas) < period:
        return values

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        if avg_loss == 0:
            values[index + 1] = 1.0
        else:
            rs = avg_gain / avg_loss
            values[index + 1] = 1 - 1 / (1 + rs)
    return values


def build_features(closes: np.ndarray, volumes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dựng ma trận đặc trưng và nhãn từ chuỗi giá.

    Trả về ``(X, y)`` với ``y = 1`` nếu phiên kế tiếp tăng giá.
    """
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes, dtype=float)
    n = len(closes)
    if n < MIN_SAMPLES:
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0)

    rsi = _rsi(closes)
    rows: list[list[float]] = []
    labels: list[int] = []

    for i in range(20, n - 1):  # -1 để luôn có nhãn của phiên kế tiếp
        window20 = closes[i - 20:i + 1]
        sma20 = window20.mean()
        rets = _returns(closes[i - 20:i + 1])
        volume_window = volumes[i - 20:i + 1]
        avg_volume = volume_window.mean()

        rows.append([
            closes[i] / closes[i - 1] - 1,
            closes[i] / closes[i - 5] - 1,
            closes[i] / closes[i - 20] - 1,
            rsi[i],
            closes[i] / sma20 - 1 if sma20 else 0.0,
            rets.std() if len(rets) else 0.0,
            volumes[i] / avg_volume - 1 if avg_volume else 0.0,
        ])
        labels.append(1 if closes[i + 1] > closes[i] else 0)

    return np.array(rows, dtype=float), np.array(labels, dtype=int)


@dataclass
class SmallMLModel:
    """Hồi quy logistic có chuẩn hoá đặc trưng và điều chuẩn L2."""

    weights: np.ndarray | None = None
    mean: np.ndarray | None = None
    std: np.ndarray | None = None
    train_accuracy: float = 0.0
    samples: int = 0
    feature_importances: dict[str, float] = field(default_factory=dict)

    @property
    def trained(self) -> bool:
        return self.weights is not None

    def fit(self, X: np.ndarray, y: np.ndarray, epochs: int = 400, lr: float = 0.1, l2: float = 1e-3):
        """Huấn luyện bằng gradient descent; trả về chính mô hình để tiện nối chuỗi."""
        if len(X) < MIN_SAMPLES:
            logger.info("Chỉ có %d mẫu (<%d) — bỏ qua huấn luyện ML.", len(X), MIN_SAMPLES)
            return self

        self.mean = X.mean(axis=0)
        self.std = X.std(axis=0)
        self.std[self.std == 0] = 1.0          # tránh chia cho 0 với đặc trưng hằng
        Xn = (X - self.mean) / self.std

        n, d = Xn.shape
        w = np.zeros(d)
        b = 0.0

        for _ in range(epochs):
            z = Xn @ w + b
            p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
            error = p - y
            grad_w = Xn.T @ error / n + l2 * w
            grad_b = error.mean()
            w -= lr * grad_w
            b -= lr * grad_b

        self.weights = np.append(w, b)         # phần tử cuối là bias
        self.samples = n
        self.train_accuracy = float(((p > 0.5).astype(int) == y).mean())
        self.feature_importances = dict(zip(FEATURE_NAMES, np.abs(w).round(4).tolist()))
        return self

    def predict_proba(self, x: np.ndarray) -> float:
        """Xác suất phiên kế tiếp tăng giá, trong khoảng 0..1."""
        if not self.trained:
            return 0.5
        xn = (np.asarray(x, dtype=float) - self.mean) / self.std
        z = float(xn @ self.weights[:-1] + self.weights[-1])
        return float(1 / (1 + np.exp(-np.clip(z, -30, 30))))

    def latest_features(self, closes: np.ndarray, volumes: np.ndarray) -> np.ndarray | None:
        """Vector đặc trưng của phiên mới nhất (dùng để dự đoán phiên tới)."""
        closes = np.asarray(closes, dtype=float)
        volumes = np.asarray(volumes, dtype=float)
        if len(closes) < 21:
            return None

        rsi = _rsi(closes)
        i = len(closes) - 1
        sma20 = closes[i - 20:i + 1].mean()
        rets = _returns(closes[i - 20:i + 1])
        avg_volume = volumes[i - 20:i + 1].mean()

        return np.array([
            closes[i] / closes[i - 1] - 1,
            closes[i] / closes[i - 5] - 1,
            closes[i] / closes[i - 20] - 1,
            rsi[i],
            closes[i] / sma20 - 1 if sma20 else 0.0,
            rets.std() if len(rets) else 0.0,
            volumes[i] / avg_volume - 1 if avg_volume else 0.0,
        ], dtype=float)


def predict_from_history(prices: list[dict]) -> dict:
    """Huấn luyện nhanh trên lịch sử giá và dự đoán phiên kế tiếp.

    ``prices`` là danh sách dict có khoá ``price`` (và tuỳ chọn ``volume``),
    sắp xếp cũ → mới. Trả về điểm dự đoán kèm độ tin cậy.
    """
    if len(prices) < MIN_SAMPLES:
        return {
            "available": False,
            "probability": 0.5,
            "reason": f"Chưa đủ dữ liệu ({len(prices)}/{MIN_SAMPLES} phiên).",
        }

    closes = np.array([float(p["price"]) for p in prices])
    volumes = np.array([float(p.get("volume") or 0.0) for p in prices])

    X, y = build_features(closes, volumes)
    if len(X) < MIN_SAMPLES:
        return {"available": False, "probability": 0.5, "reason": "Không dựng được đủ đặc trưng."}

    model = SmallMLModel().fit(X, y)
    latest = model.latest_features(closes, volumes)
    if latest is None:
        return {"available": False, "probability": 0.5, "reason": "Thiếu phiên gần nhất."}

    probability = model.predict_proba(latest)
    return {
        "available": True,
        "probability": round(probability, 4),
        "train_accuracy": round(model.train_accuracy, 4),
        "samples": model.samples,
        "feature_importances": model.feature_importances,
        "reason": (
            f"Hồi quy logistic trên {model.samples} phiên, "
            f"độ chính xác tập huấn luyện {model.train_accuracy:.1%}."
        ),
    }
