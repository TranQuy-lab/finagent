"""Test adapter Binance — chạy trên sàn giả lập có kiểm tra chữ ký HMAC.

Nhóm test này chứng minh ba điều quan trọng trước khi cho hệ thống giao dịch thật:

1. **Chữ ký request đúng.** Sàn giả lập tự tính lại HMAC-SHA256 và từ chối nếu
   sai — đúng như Binance thật. Nếu phần ký sai, mọi test có xác thực đều đổ.
2. **Tuân thủ giới hạn của sàn.** Khối lượng phải là bội số ``stepSize`` và giá
   trị lệnh phải đạt ``minNotional``, nếu không sàn trả lỗi.
3. **Chốt an toàn tiền thật.** Không thể vô tình bật giao dịch tiền thật.

Chạy::

    pytest tests/test_binance.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_binance import MockBinanceServer  # noqa: E402

from finagent.broker.binance import (  # noqa: E402
    BinanceBroker,
    BinanceError,
    round_step,
    split_symbol,
)

pytestmark = pytest.mark.integration


def _seed_price_history(symbol: str, count: int = 80, start: float = 40_000.0,
                        step: float = 1.004) -> None:
    """Gieo một chuỗi giá tăng dần để mô hình ML có đủ mẫu.

    Chuỗi tăng đều để mô hình chắc chắn nghiêng về tín hiệu mua — bài test quan tâm
    tới khối lượng lệnh, không quan tâm mô hình đoán đúng hay sai.
    """
    import datetime as _dt

    from finagent import storage

    base = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    price = start
    rows = []
    for offset in range(count):
        price *= step
        rows.append({
            "symbol": symbol,
            "asset_class": "crypto",
            "price": round(price, 2),
            "currency": "USDT",
            "change_pct_24h": 0.4,
            "volume": 1000.0 + offset,
            "source": "test",
            "captured_at": (base + _dt.timedelta(days=offset)).isoformat(),
        })
    storage.save_prices(rows)


def _rising_history(count: int = 90, start: float = 40_000.0, step: float = 1.004) -> list[dict]:
    """Chuỗi nến tăng đều, dùng thay cho dữ liệu mạng trong test.

    Tăng đều để mô hình ML chắc chắn nghiêng về tín hiệu mua — bài test quan tâm
    tới khối lượng lệnh, không quan tâm mô hình đoán đúng hay sai.
    """
    import datetime as _dt

    base = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    price = start
    rows = []
    for offset in range(count):
        price *= step
        rows.append({
            "date": (base + _dt.timedelta(days=offset)).strftime("%Y-%m-%d"),
            "price": round(price, 2),
            "volume": 1000.0 + offset,
        })
    return rows


@pytest.fixture()
def exchange(temp_db):
    """Sàn giả lập + broker trỏ vào đó, dùng chung một cơ sở dữ liệu tạm."""
    with MockBinanceServer(price=83_000.0) as server:
        broker = BinanceBroker(
            api_key=server.state.api_key,
            api_secret=server.state.api_secret,
            testnet=True,
            base_url=server.base_url,
        )
        broker._sync_time()
        yield server, broker


# ---------------------------------------------------------------------------
# Hàm tiện ích thuần
# ---------------------------------------------------------------------------

class TestSplitSymbol:
    def test_tach_usdt(self):
        assert split_symbol("BTCUSDT") == ("BTC", "USDT")

    def test_tach_eth(self):
        assert split_symbol("ETHUSDT") == ("ETH", "USDT")

    def test_chap_nhan_gach_ngang(self):
        assert split_symbol("BTC-USDT") == ("BTC", "USDT")

    def test_ma_khong_hop_le_thi_bao_loi(self):
        with pytest.raises(ValueError, match="Không tách được"):
            split_symbol("FPT")


class TestRoundStep:
    def test_lam_tron_xuong_theo_buoc(self):
        assert round_step(0.00012345, "0.00001000") == pytest.approx(0.00012)

    def test_dung_buoc_lon(self):
        assert round_step(123.456, "1.00000000") == 123.0

    def test_khong_vuot_qua_gia_tri_goc(self):
        """Làm tròn xuống, không bao giờ làm tròn lên — nếu lên sẽ vượt số dư."""
        for quantity in (0.000019, 0.999999, 12.3456789):
            assert round_step(quantity, "0.00001000") <= quantity + 1e-12

    def test_tranh_sai_so_dau_phay_dong(self):
        """``0.1 + 0.2`` trong số thực cho 0.30000000000000004 — phải xử lý được."""
        result = round_step(0.1 + 0.2, "0.10000000")
        assert result == pytest.approx(0.3)
        assert f"{result:.8f}" == "0.30000000"


# ---------------------------------------------------------------------------
# Xác thực và kết nối
# ---------------------------------------------------------------------------

class TestAuthentication:
    def test_chu_ky_hmac_dung(self, exchange):
        """Sàn tự kiểm tra chữ ký; nếu phần ký sai thì đã bị từ chối."""
        server, broker = exchange

        account = broker.account()

        assert account["canTrade"] is True
        assert server.state.signature_failures == 0
        assert server.state.auth_failures == 0

    def test_sai_api_key_bi_tu_choi(self, exchange):
        server, _ = exchange
        wrong = BinanceBroker(
            api_key="sai-key", api_secret=server.state.api_secret, base_url=server.base_url
        )

        with pytest.raises(BinanceError, match="Invalid API-key"):
            wrong.account()

    def test_sai_api_secret_bi_tu_choi(self, exchange):
        server, _ = exchange
        wrong = BinanceBroker(
            api_key=server.state.api_key, api_secret="sai-secret", base_url=server.base_url
        )

        with pytest.raises(BinanceError, match="Signature"):
            wrong.account()

        assert server.state.signature_failures == 1

    def test_thieu_khoa_thi_bao_loi_ro(self):
        with pytest.raises(ValueError, match="Thiếu BINANCE_API_KEY"):
            BinanceBroker(api_key="", api_secret="")

    def test_kiem_tra_ket_noi(self, exchange):
        _, broker = exchange

        result = broker.test_connection()

        assert result["testnet"] is True
        assert result["can_trade"] is True
        assert result["balances_nonzero"]["USDT"] == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# Đọc trạng thái tài khoản
# ---------------------------------------------------------------------------

class TestAccountState:
    def test_doc_so_du(self, exchange):
        _, broker = exchange

        assert broker.get_cash("USDT") == pytest.approx(10_000.0)
        assert broker.get_cash("BTC") == 0.0

    def test_tien_te_khong_co_tra_ve_0(self, exchange):
        _, broker = exchange

        assert broker.get_cash("KHONGCO") == 0.0

    def test_chua_co_vi_the_thi_tra_ve_none(self, exchange):
        _, broker = exchange

        assert broker.get_position("BTCUSDT") is None

    def test_danh_sach_vi_the_rong_khi_chua_mua(self, exchange):
        _, broker = exchange

        assert broker.list_positions() == []

    def test_gia_tri_danh_muc_quy_ve_vnd(self, exchange):
        server, broker = exchange

        # 10.000 USDT, chưa có BTC.
        value = broker.portfolio_value(lambda symbol: 83_000.0, usdt_vnd=25_000.0)

        assert value == pytest.approx(10_000.0 * 25_000.0)


# ---------------------------------------------------------------------------
# Đặt lệnh
# ---------------------------------------------------------------------------

class TestBuyOrder:
    def test_mua_thanh_cong(self, exchange):
        server, broker = exchange

        result = broker.buy("BTCUSDT", 0.001, 83_000.0)

        assert result.ok is True
        assert result.status == "filled"
        assert result.mode == "live"
        assert result.quantity == pytest.approx(0.001)
        assert result.price == pytest.approx(83_000.0)
        assert result.broker_ref

    def test_mua_tru_dung_so_du_tren_san(self, exchange):
        server, broker = exchange

        broker.buy("BTCUSDT", 0.001, 83_000.0)

        assert server.state.balances["USDT"] == pytest.approx(10_000.0 - 83.0)
        assert server.state.balances["BTC"] == pytest.approx(0.001)

    def test_mua_tao_vi_the_co_gia_von(self, exchange):
        _, broker = exchange

        broker.buy("BTCUSDT", 0.001, 83_000.0)

        position = broker.get_position("BTCUSDT")
        assert position is not None
        assert position["quantity"] == pytest.approx(0.001)
        assert position["avg_price"] == pytest.approx(83_000.0)
        assert position["avg_price_known"] is True

    def test_mua_hai_lan_bi_nh_dung_gia_von(self, exchange):
        server, broker = exchange
        server.state.price = 80_000.0
        broker.buy("BTCUSDT", 0.001, 80_000.0)
        server.state.price = 90_000.0
        broker.buy("BTCUSDT", 0.001, 90_000.0)

        position = broker.get_position("BTCUSDT")

        assert position["quantity"] == pytest.approx(0.002)
        assert position["avg_price"] == pytest.approx(85_000.0)

    def test_gia_khop_lay_tu_san_khong_phai_gia_de_xuat(self, exchange):
        """Sàn khớp theo giá thị trường thật, không theo giá trong đề xuất."""
        server, broker = exchange
        server.state.price = 81_500.0

        result = broker.buy("BTCUSDT", 0.001, 83_000.0)   # đề xuất 83.000

        assert result.price == pytest.approx(81_500.0)     # khớp thật 81.500


class TestSellOrder:
    def test_ban_giam_vi_the(self, exchange):
        server, broker = exchange
        broker.buy("BTCUSDT", 0.002, 83_000.0)

        result = broker.sell("BTCUSDT", 0.001, 83_000.0)

        assert result.ok is True
        assert broker.get_position("BTCUSDT")["quantity"] == pytest.approx(0.001)

    def test_ban_het_thi_xoa_vi_the(self, exchange):
        server, broker = exchange
        broker.buy("BTCUSDT", 0.001, 83_000.0)

        broker.sell("BTCUSDT", 0.001, 83_000.0)

        assert broker.get_position("BTCUSDT") is None

    def test_ban_cong_tien_ve_vi(self, exchange):
        server, broker = exchange
        broker.buy("BTCUSDT", 0.001, 83_000.0)
        usdt_sau_mua = server.state.balances["USDT"]

        broker.sell("BTCUSDT", 0.001, 83_000.0)

        assert server.state.balances["USDT"] > usdt_sau_mua

    def test_ban_khi_khong_co_gi_thi_tu_choi(self, exchange):
        _, broker = exchange

        result = broker.sell("BTCUSDT", 0.001, 83_000.0)

        assert result.ok is False
        assert "khả dụng" in result.message


class TestExchangeLimits:
    def test_khoi_luong_lam_tron_theo_buoc_cua_san(self, exchange):
        server, broker = exchange

        # 0.00012345 không phải bội số của 0.00001 → phải bị làm tròn xuống.
        broker.buy("BTCUSDT", 0.00012345, 83_000.0)

        sent = server.state.orders[-1]
        assert float(sent["executedQty"]) == pytest.approx(0.00012)

    def test_khoi_luong_duoi_buoc_thi_tu_choi_truoc_khi_goi_san(self, exchange):
        server, broker = exchange

        result = broker.buy("BTCUSDT", 0.000001, 83_000.0)   # dưới stepSize

        assert result.ok is False
        assert server.state.orders == [], "Không được gọi sàn khi đã biết chắc bị từ chối"

    def test_gia_tri_lenh_duoi_min_notional_thi_tu_choi(self, exchange):
        server, broker = exchange

        # 0.0001 BTC × 83.000 = 8,3 USDT… nhưng hạ giá xuống để chắc chắn dưới 5.
        server.state.price = 30_000.0
        result = broker.buy("BTCUSDT", 0.0001, 30_000.0)     # 3 USDT < 5

        assert result.ok is False
        assert "tối thiểu" in result.message
        assert server.state.orders == []

    def test_san_tu_choi_thi_tra_ve_that_bai_chu_khong_nem_loi(self, exchange):
        server, broker = exchange
        server.state.reject_message = "Account has insufficient balance"

        result = broker.buy("BTCUSDT", 0.001, 83_000.0)

        assert result.ok is False
        assert "insufficient balance" in result.message

    def test_ma_khong_co_tren_san(self, exchange):
        _, broker = exchange

        result = broker.buy("KHONGCOUSDT", 1.0, 100.0)

        assert result.ok is False


# ---------------------------------------------------------------------------
# Chốt an toàn tiền thật
# ---------------------------------------------------------------------------

class TestLiveMoneyGuard:
    def test_chan_khi_bat_tien_that_ma_chua_xac_nhan(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "binance_testnet", False, raising=False)
        monkeypatch.setattr(settings, "binance_live_confirm", "", raising=False)
        monkeypatch.setattr(settings, "binance_api_key", "k", raising=False)
        monkeypatch.setattr(settings, "binance_api_secret", "s", raising=False)

        with pytest.raises(RuntimeError, match="chưa xác nhận giao dịch tiền thật"):
            BinanceBroker.from_settings()

    def test_cho_qua_khi_xac_nhan_dung_chu(self, monkeypatch):
        from finagent.config import settings

        monkeypatch.setattr(settings, "binance_testnet", False, raising=False)
        monkeypatch.setattr(settings, "binance_live_confirm", "YES", raising=False)
        monkeypatch.setattr(settings, "binance_api_key", "k", raising=False)
        monkeypatch.setattr(settings, "binance_api_secret", "s", raising=False)

        broker = BinanceBroker.from_settings()

        assert broker.testnet is False
        assert broker.base_url == "https://api.binance.com"

    def test_chap_nhan_xac_nhan_viet_thuong(self, monkeypatch):
        """``yes`` viết thường vẫn tính — người dùng đã chủ động viết ra rồi."""
        from finagent.config import settings

        monkeypatch.setattr(settings, "binance_testnet", False, raising=False)
        monkeypatch.setattr(settings, "binance_live_confirm", "yes", raising=False)
        monkeypatch.setattr(settings, "binance_api_key", "k", raising=False)
        monkeypatch.setattr(settings, "binance_api_secret", "s", raising=False)

        assert BinanceBroker.from_settings().testnet is False

    @pytest.mark.parametrize("value", ["", "y", "true", "1", "ok", "dong y", "YESSS"])
    def test_gia_tri_xac_nhan_khac_deu_bi_chan(self, monkeypatch, value):
        """Chỉ đúng chuỗi xác nhận mới qua được — không nhận giá trị na ná."""
        from finagent.config import settings

        monkeypatch.setattr(settings, "binance_testnet", False, raising=False)
        monkeypatch.setattr(settings, "binance_live_confirm", value, raising=False)
        monkeypatch.setattr(settings, "binance_api_key", "k", raising=False)
        monkeypatch.setattr(settings, "binance_api_secret", "s", raising=False)

        with pytest.raises(RuntimeError, match="chưa xác nhận"):
            BinanceBroker.from_settings()


# ---------------------------------------------------------------------------
# Factory chọn broker
# ---------------------------------------------------------------------------

class TestBrokerFactory:
    def test_che_do_paper_tra_ve_broker_mo_phong(self, temp_db, monkeypatch):
        from finagent.broker import get_broker, reset_broker
        from finagent.config import settings

        monkeypatch.setattr(settings, "trading_mode", "paper", raising=False)
        reset_broker()

        broker = get_broker()

        assert broker.mode == "paper"
        reset_broker()

    def test_che_do_live_tra_ve_broker_binance(self, temp_db, monkeypatch):
        from finagent.broker import get_broker, reset_broker
        from finagent.config import settings

        monkeypatch.setattr(settings, "trading_mode", "live", raising=False)
        monkeypatch.setattr(settings, "binance_testnet", True, raising=False)
        monkeypatch.setattr(settings, "binance_api_key", "k", raising=False)
        monkeypatch.setattr(settings, "binance_api_secret", "s", raising=False)
        reset_broker()

        broker = get_broker()

        assert broker.mode == "live"
        assert broker.testnet is True
        reset_broker()

    def test_che_do_khong_hop_le_thi_bao_loi(self, temp_db, monkeypatch):
        from finagent.broker import get_broker, reset_broker
        from finagent.config import settings

        monkeypatch.setattr(settings, "trading_mode", "lung-tung", raising=False)
        reset_broker()

        with pytest.raises(ValueError, match="không hợp lệ"):
            get_broker()
        reset_broker()

    def test_mo_ta_broker_paper(self, monkeypatch):
        from finagent.broker import describe_broker
        from finagent.config import settings

        monkeypatch.setattr(settings, "trading_mode", "paper", raising=False)
        info = describe_broker()

        assert info["real_money"] is False
        assert "Mô phỏng" in info["venue"]

    def test_mo_ta_broker_testnet_khong_phai_tien_that(self, monkeypatch):
        from finagent.broker import describe_broker
        from finagent.config import settings

        monkeypatch.setattr(settings, "trading_mode", "live", raising=False)
        monkeypatch.setattr(settings, "binance_testnet", True, raising=False)
        info = describe_broker()

        assert info["real_money"] is False
        assert "Testnet" in info["venue"]

    def test_mo_ta_broker_tien_that_canh_bao_ro(self, monkeypatch):
        from finagent.broker import describe_broker
        from finagent.config import settings

        monkeypatch.setattr(settings, "trading_mode", "live", raising=False)
        monkeypatch.setattr(settings, "binance_testnet", False, raising=False)
        info = describe_broker()

        assert info["real_money"] is True
        assert "TIỀN THẬT" in info["venue"]


# ---------------------------------------------------------------------------
# Tích hợp với bộ máy ra quyết định
# ---------------------------------------------------------------------------

class TestEngineIntegration:
    """Bộ máy ra quyết định phải đặt lệnh được qua sàn thật, không chỉ broker mô phỏng.

    Đây là điểm nối dễ hỏng nhất: engine gọi ``get_broker()`` chứ không tự khởi
    tạo, nên nếu factory hoặc chữ ký phương thức lệch nhau thì chỉ phát hiện được
    khi chạy thật.
    """

    @pytest.fixture()
    def live_setup(self, temp_db, monkeypatch):
        from finagent import storage
        from finagent.broker import reset_broker
        from finagent.config import settings

        server = MockBinanceServer(price=83_000.0).start()
        monkeypatch.setattr(settings, "trading_mode", "live", raising=False)
        monkeypatch.setattr(settings, "binance_testnet", True, raising=False)
        monkeypatch.setattr(settings, "binance_api_key", server.state.api_key, raising=False)
        monkeypatch.setattr(settings, "binance_api_secret", server.state.api_secret, raising=False)
        monkeypatch.setattr(settings, "binance_base_url", server.base_url, raising=False)
        monkeypatch.setattr(settings, "usdt_vnd_rate", 25_000.0, raising=False)
        reset_broker()

        # Giá thị trường để engine định giá danh mục.
        storage.save_prices([{
            "symbol": "BTCUSDT", "asset_class": "crypto", "price": 83_000.0,
            "currency": "USDT", "change_pct_24h": 1.0, "volume": 1000.0,
            "source": "test", "captured_at": "2026-09-28T01:00:00+00:00",
        }])

        yield server
        reset_broker()
        server.stop()

    def test_dat_lenh_qua_san_that(self, live_setup):
        from finagent.decision import engine

        result = engine.execute_approved({
            "id": 1, "symbol": "BTCUSDT", "asset_class": "crypto",
            "action": "buy", "quantity": 0.001, "price": 83_000.0,
        })

        assert result["ok"] is True
        assert result["currency"] == "USDT"
        assert live_setup.state.orders, "Lệnh phải được gửi tới sàn"
        assert live_setup.state.balances["BTC"] == pytest.approx(0.001)

    def test_lenh_that_duoc_ghi_vao_so_lenh(self, live_setup):
        from finagent import storage
        from finagent.decision import engine

        engine.execute_approved({
            "id": 1, "symbol": "BTCUSDT", "asset_class": "crypto",
            "action": "buy", "quantity": 0.001, "price": 83_000.0,
        })

        rows = storage.get_conn().execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 1
        assert rows[0]["mode"] == "live"
        assert rows[0]["status"] == "filled"

    def test_lenh_bi_san_tu_choi_thi_bao_that_bai(self, live_setup):
        from finagent.decision import engine

        live_setup.state.reject_message = "Account has insufficient balance"

        result = engine.execute_approved({
            "id": 1, "symbol": "BTCUSDT", "asset_class": "crypto",
            "action": "buy", "quantity": 0.001, "price": 83_000.0,
        })

        assert result["ok"] is False
        assert "insufficient" in result["message"]

    def test_doc_so_du_tu_san_trong_de_xuat(self, live_setup, monkeypatch):
        """Khối lượng lệnh phải tính từ số dư THẬT trên sàn, không phải số cấu hình.

        Bài test này từng phụ thuộc mạng: ``analyze_ml`` lấy lịch sử giá từ API
        Binance thật, nên kết quả đổi theo dữ liệu thị trường tại thời điểm chạy và
        theo việc máy có mạng hay không — chạy cả bộ thì đạt, chạy riêng thì đổ.
        Nay chặn nguồn dữ liệu đó bằng một chuỗi giá tăng đều để kết quả xác định.
        """
        from finagent.decision import engine
        from finagent.decision import vendor as vendor_module

        monkeypatch.setattr(
            vendor_module, "daily_history",
            lambda symbol, days=400: _rising_history(90),
        )

        proposal = engine.build_proposal("BTCUSDT", run_llm=False)

        assert proposal is not None
        assert proposal.action == "buy", "chuỗi giá tăng đều phải cho tín hiệu mua"
        assert proposal.currency == "USDT"
        # Ví USDT trên sàn giả lập có 10.000, giá 83.000.
        # Ngân sách = min(10.000 × 10%, tiền mặt × 98%) = 1.000 USDT
        assert proposal.quantity == pytest.approx(1_000 / 83_000, rel=0.02)

    def test_khoi_luong_bam_theo_so_du_san(self, live_setup, monkeypatch):
        """Đổi số dư trên sàn thì khối lượng phải đổi theo — chứng minh engine đọc
        số dư thật chứ không dùng hằng số nào."""
        from finagent.decision import engine
        from finagent.decision import vendor as vendor_module

        monkeypatch.setattr(
            vendor_module, "daily_history",
            lambda symbol, days=400: _rising_history(90),
        )
        live_setup.state.balances["USDT"] = 50_000.0

        proposal = engine.build_proposal("BTCUSDT", run_llm=False)

        # 10% của 50.000 = 5.000 USDT
        assert proposal.quantity == pytest.approx(5_000 / 83_000, rel=0.02)


def _rising_history(count: int = 90, start: float = 40_000.0, step: float = 1.004) -> list[dict]:
    """Chuỗi nến tăng đều, dùng thay cho dữ liệu mạng trong test.

    Tăng đều để mô hình ML chắc chắn nghiêng về tín hiệu mua — bài test quan tâm
    tới khối lượng lệnh, không quan tâm mô hình đoán đúng hay sai.
    """
    import datetime as _dt

    base = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
    price = start
    rows = []
    for offset in range(count):
        price *= step
        rows.append({
            "date": (base + _dt.timedelta(days=offset)).strftime("%Y-%m-%d"),
            "price": round(price, 2),
            "volume": 1000.0 + offset,
        })
    return rows
