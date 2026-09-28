"""Cấu hình chung cho bộ test.

Các test được chia hai nhóm:

* **Đơn vị** — chạy hoàn toàn ngoại tuyến, không cần mạng/Redis/API key.
* **Tích hợp** — đánh dấu ``integration``, cần mạng thật hoặc Redis đang chạy.

Chạy nhanh (bỏ qua tích hợp)::

    pytest -m "not integration"

Chạy tất cả::

    pytest
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: cần mạng thật hoặc Redis đang chạy")


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Cơ sở dữ liệu SQLite tạm, tách biệt hoàn toàn giữa các test."""
    import finagent.config as config_module
    import finagent.storage as storage_module

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config_module.settings, "db_path", db_path, raising=False)

    # Bỏ kết nối cũ đang gắn với luồng để buộc mở lại theo đường dẫn mới.
    if hasattr(storage_module._local, "conn"):
        storage_module._local.conn.close()
        del storage_module._local.conn

    storage_module.init_db()
    yield db_path

    if hasattr(storage_module._local, "conn"):
        storage_module._local.conn.close()
        del storage_module._local.conn


@pytest.fixture()
def price_series():
    """Chuỗi giá tăng dần có nhiễu, đủ dài để huấn luyện mô hình ML."""
    import numpy as np

    rng = np.random.default_rng(42)
    steps = rng.normal(0.001, 0.02, 300)
    closes = 100 * np.cumprod(1 + steps)
    volumes = rng.integers(1_000_000, 5_000_000, 300).astype(float)
    return closes, volumes


@pytest.fixture(scope="module")
def temp_db_module(tmp_path_factory):
    """Cơ sở dữ liệu tạm dùng chung cho cả một module test.

    Cần thiết cho những test tốn kém mà vẫn muốn tách biệt dữ liệu — ví dụ chạy
    trọn đồ thị đa tác nhân một lần rồi chia sẻ kết quả cho nhiều khẳng định.
    """
    import finagent.config as config_module
    import finagent.storage as storage_module

    db_path = tmp_path_factory.mktemp("db") / "module.db"
    original_path = config_module.settings.db_path
    config_module.settings.db_path = db_path

    _reset_connection(storage_module)
    storage_module.init_db()
    yield db_path

    _reset_connection(storage_module)
    config_module.settings.db_path = original_path


def _reset_connection(storage_module) -> None:
    """Đóng và xoá kết nối SQLite đang gắn với luồng hiện tại."""
    if hasattr(storage_module._local, "conn"):
        storage_module._local.conn.close()
        del storage_module._local.conn
