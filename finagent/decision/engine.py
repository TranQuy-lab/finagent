"""Bộ máy ra quyết định: từ dữ liệu thô tới một đề xuất giao dịch cụ thể.

Luồng xử lý cho mỗi mã:

1. Lấy giá mới nhất cùng lịch sử từ kho dữ liệu (do máy con thu thập).
2. Chạy **mô hình ML nhỏ** trên chuỗi giá → xác suất tăng giá.
3. Nếu có API key, chạy thêm **TradingAgents** (đa tác nhân LLM) trên dữ liệu
   Việt Nam đã cắm vào qua vendor ``finagent``.
4. Hợp nhất hai nguồn tín hiệu, tính khối lượng theo giới hạn rủi ro, rồi tạo
   một ``Proposal`` để máy chủ gửi Telegram xin người dùng duyệt.

Việc tách bạch như vậy giúp hệ thống vẫn ra được quyết định khi mất mạng hoặc
hết hạn mức API — chỉ chuyển sang chế độ chỉ dùng ML.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from finagent.collectors.base import utcnow_iso
from finagent.config import settings
from finagent.decision import ml_model, vendor
from finagent.storage import get_conn, price_history, transaction

logger = logging.getLogger(__name__)

#: Điểm tin cậy quy cho từng mức tín hiệu của TradingAgents.
SIGNAL_CONFIDENCE = {
    "Buy": 0.90,
    "Overweight": 0.72,
    "Hold": 0.50,
    "Underweight": 0.35,
    "Sell": 0.20,
    "REVIEW": 0.00,
}

#: Tín hiệu dẫn tới hành động mua / bán.
BUY_SIGNALS = {"Buy", "Overweight"}
SELL_SIGNALS = {"Sell", "Underweight"}


@dataclass
class Proposal:
    """Một đề xuất giao dịch chờ người dùng duyệt qua Telegram."""

    symbol: str
    asset_class: str
    action: str                 # "buy" | "sell" | "hold"
    signal: str                 # Buy / Overweight / Hold / Underweight / Sell
    confidence: float
    price: float
    quantity: float
    amount: float
    rationale: str = ""
    currency: str = "VND"
    source: str = "ml"          # "llm" | "ml" | "llm+ml"
    created_at: str = field(default_factory=utcnow_iso)
    id: int | None = None

    @property
    def actionable(self) -> bool:
        """Đề xuất có đáng để xin người dùng duyệt hay không."""
        return (
            self.action in ("buy", "sell")
            and self.quantity > 0
            and self.confidence >= settings.min_signal_confidence
        )

    @property
    def needs_approval(self) -> bool:
        """Lệnh lớn phải chờ người dùng bấm duyệt; lệnh nhỏ vẫn nên hỏi."""
        return self.amount >= settings.require_approval_above

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Phân tích tín hiệu
# ---------------------------------------------------------------------------

def analyze_ml(symbol: str) -> dict:
    """Chạy mô hình ML nhỏ trên lịch sử nến ngày của mã."""
    try:
        history = vendor.daily_history(symbol, days=400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Không lấy được lịch sử cho ML %s: %s", symbol, exc)
        return {"available": False, "probability": 0.5, "reason": f"Không có lịch sử: {exc}"}

    result = ml_model.predict_from_history(history)
    result["symbol"] = symbol
    result["history_length"] = len(history)
    return result


def _sync_api_key_env() -> None:
    """Đồng bộ khoá API từ cấu hình của FinAgent sang biến môi trường.

    TradingAgents đọc khoá trực tiếp từ biến môi trường (``DEEPSEEK_API_KEY``…),
    còn FinAgent quản lý cấu hình tập trung. Nếu không đồng bộ, người dùng điền
    khoá đúng vào ``.env`` mà vẫn nhận lỗi "API key is not set" rất khó hiểu.
    """
    import os

    try:
        from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    except ImportError:  # pragma: no cover - phòng khi TradingAgents đổi API
        PROVIDER_API_KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY"}

    env_var = PROVIDER_API_KEY_ENV.get(settings.llm_provider)
    if env_var and settings.llm_api_key:
        os.environ[env_var] = settings.llm_api_key


def analyze_llm(symbol: str, trade_date: str | None = None, backend_url: str | None = None) -> dict:
    """Chạy TradingAgents (đa tác nhân LLM) trên dữ liệu đã cắm vào.

    ``backend_url`` cho phép trỏ tới một endpoint OpenAI-compatible khác (endpoint
    tự lưu trữ, hoặc LLM giả lập trong test). Bỏ trống thì dùng cấu hình chung,
    và nếu cấu hình cũng trống thì dùng mặc định của nhà cung cấp.
    """
    if not settings.llm_enabled:
        return {
            "available": False,
            "signal": "Hold",
            "reason": f"Thiếu {settings.llm_key_env_var or 'khoá API'} cho nhà cung cấp {settings.llm_provider!r}.",
        }

    _sync_api_key_env()

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        vendor.register_vendor()

        config = DEFAULT_CONFIG.copy()
        config.update({
            "llm_provider": settings.llm_provider,
            "deep_think_llm": settings.deep_think_llm,
            "quick_think_llm": settings.quick_think_llm,
            "output_language": settings.output_language,
            "max_debate_rounds": settings.max_debate_rounds,
            "max_risk_discuss_rounds": settings.max_risk_rounds,
            "backend_url": backend_url or settings.llm_backend_url or None,
        })
        config["data_vendors"] = vendor.vendor_config()

        graph = TradingAgentsGraph(debug=False, config=config)
        asset_type = "crypto" if vendor.detect_asset_class(symbol) == "crypto" else "stock"
        date = trade_date or utcnow_iso()[:10]

        state, signal = graph.propagate(symbol, date, asset_type=asset_type)

        final_decision = ""
        if isinstance(state, dict):
            final_decision = state.get("final_trade_decision") or state.get("trader_investment_plan") or ""

        return {
            "available": True,
            "signal": signal or "Hold",
            "decision": str(final_decision),
            "reason": f"TradingAgents trả về tín hiệu {signal}.",
        }
    except Exception as exc:  # noqa: BLE001 - lỗi LLM không được làm sập cả chu kỳ
        logger.exception("TradingAgents thất bại cho %s", symbol)
        return {"available": False, "signal": "Hold", "reason": f"Lỗi LLM: {exc}"}


def combine_signals(ml_result: dict, llm_result: dict) -> tuple[str, float, str, str]:
    """Hợp nhất tín hiệu ML và LLM.

    Trả về ``(signal, confidence, source, rationale)``. Khi cả hai cùng chiều,
    độ tin cậy được cộng thêm; khi mâu thuẫn, hệ thống thiên về thận trọng (Hold).
    """
    ml_available = ml_result.get("available")
    llm_available = llm_result.get("available")

    llm_signal = llm_result.get("signal", "Hold")
    llm_confidence = SIGNAL_CONFIDENCE.get(llm_signal, 0.5)

    # ML: xác suất tăng giá → quy về thang tín hiệu 5 mức.
    ml_probability = float(ml_result.get("probability", 0.5))
    # Biên lệch khỏi 50/50, trải vào dải [0.5, 0.95] để dùng được làm điểm tin cậy.
    # p=0.5 → 0.50 (không tin), p=1.0 → 0.95 (rất tin).
    ml_margin = abs(ml_probability - 0.5) * 2
    ml_confidence = 0.5 + ml_margin * 0.45
    ml_signal = "Buy" if ml_probability >= 0.55 else "Sell" if ml_probability <= 0.45 else "Hold"

    if llm_available and ml_available:
        # Đồng thuận thì tăng độ tin cậy, mâu thuẫn thì hạ về Hold.
        if llm_signal in BUY_SIGNALS and ml_signal == "Buy":
            confidence = min(0.95, (llm_confidence + ml_confidence) / 2 + 0.1)
            signal = llm_signal
        elif llm_signal in SELL_SIGNALS and ml_signal == "Sell":
            confidence = min(0.95, (llm_confidence + ml_confidence) / 2 + 0.1)
            signal = llm_signal
        elif llm_signal == "Hold":
            signal, confidence = "Hold", 0.5
        else:
            signal, confidence = "Hold", 0.4   # mâu thuẫn → đứng ngoài

        rationale = (
            f"LLM: {llm_signal} ({llm_confidence:.0%}). "
            f"ML: P(tăng)={ml_probability:.1%} → {ml_signal}. "
            f"{ml_result.get('reason', '')} {llm_result.get('reason', '')}"
        ).strip()
        return signal, round(confidence, 4), "llm+ml", rationale

    if llm_available:
        return (
            llm_signal,
            round(llm_confidence, 4),
            "llm",
            f"{llm_result.get('reason', '')} {llm_result.get('decision', '')[:600]}".strip(),
        )

    if ml_available:
        return (
            ml_signal,
            round(ml_confidence, 4),
            "ml",
            f"Chỉ dùng ML: P(tăng)={ml_probability:.1%}. {ml_result.get('reason', '')}",
        )

    return "Hold", 0.0, "none", "Không có nguồn tín hiệu nào khả dụng."


# ---------------------------------------------------------------------------
# Tính khối lượng lệnh theo giới hạn rủi ro
# ---------------------------------------------------------------------------

def size_order(action: str, price: float, cash: float, portfolio_value: float,
               holding: float, asset_class: str) -> float:
    """Tính khối lượng lệnh theo tỷ lệ vốn tối đa cho phép.

    ``cash`` và ``portfolio_value`` phải **cùng loại tiền tệ với giá** của tài sản
    (USDT với crypto, VND với chứng khoán và vàng). Nhờ vậy không xảy ra việc lấy
    tiền đồng đi mua tài sản niêm yết bằng USDT.

    Mua: dùng tối đa ``max_position_pct`` giá trị danh mục, không vượt tiền mặt.
    Bán: bán hết vị thế đang có.
    Đứng ngoài: khối lượng bằng 0.
    """
    if action == "hold" or price <= 0:
        return 0.0

    if action == "sell":
        return holding

    budget = min(portfolio_value * settings.max_position_pct, cash * 0.98)
    if budget <= 0:
        return 0.0

    quantity = budget / price
    if asset_class == "crypto":
        return float(int(quantity * 1e6) / 1e6)
    return float(int(quantity / 100) * 100)     # lô 100 cho chứng khoán VN


# ---------------------------------------------------------------------------
# Tạo đề xuất
# ---------------------------------------------------------------------------

def build_proposal(symbol: str, run_llm: bool = True) -> Proposal | None:
    """Phân tích một mã và tạo đề xuất giao dịch tương ứng."""
    from finagent.broker import get_broker
    from finagent.broker.base import currency_for

    latest = get_conn().execute(
        "SELECT * FROM prices WHERE symbol = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    if not latest:
        logger.warning("Chưa có giá cho %s — bỏ qua.", symbol)
        return None

    latest = dict(latest)
    price = float(latest["price"])
    asset_class = latest["asset_class"]

    broker = get_broker()
    position = broker.get_position(symbol)
    holding = float(position["quantity"]) if position else 0.0

    # Mọi phép tính khối lượng phải cùng loại tiền tệ với giá của tài sản.
    # Lấy theo nhóm tài sản (không tin cột currency trong kho) để tránh lệch đơn vị.
    currency = currency_for(asset_class)
    cash = broker.get_cash(currency)
    portfolio_vnd = broker.portfolio_value(
        lambda s: (get_conn().execute(
            "SELECT price FROM prices WHERE symbol = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
            (s,),
        ).fetchone() or {"price": None})["price"]
    )
    # Danh mục quy về đúng loại tiền đang giao dịch trước khi tính cỡ lệnh.
    if currency == "USDT":
        portfolio_in_currency = portfolio_vnd / settings.usdt_vnd_rate
    else:
        portfolio_in_currency = portfolio_vnd

    ml_result = analyze_ml(symbol)
    llm_result = analyze_llm(symbol) if run_llm else {"available": False, "reason": "Bỏ qua LLM theo yêu cầu."}

    signal, confidence, source, rationale = combine_signals(ml_result, llm_result)

    if signal in BUY_SIGNALS:
        action = "buy"
    elif signal in SELL_SIGNALS and holding > 0:
        action = "sell"
    else:
        action = "hold"

    quantity = size_order(action, price, cash, portfolio_in_currency, holding, asset_class)

    return Proposal(
        symbol=symbol.upper(),
        asset_class=asset_class,
        action=action,
        signal=signal,
        confidence=confidence,
        price=price,
        quantity=quantity,
        amount=quantity * price,
        rationale=rationale,
        currency=currency,
        source=source,
    )


def save_proposal(proposal: Proposal) -> int:
    """Lưu đề xuất vào cơ sở dữ liệu, trả về id vừa tạo.

    Đề xuất không đủ điều kiện hành động được lưu với trạng thái ``observed``
    (chỉ để đối chiếu, phục vụ báo cáo) nên không làm phiền người dùng trên
    Telegram; chỉ đề xuất ``pending`` mới chờ duyệt.
    """
    status = "pending" if proposal.actionable else "observed"
    with transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO proposals
               (symbol, asset_class, currency, action, signal, confidence, source,
                price, quantity, amount, rationale, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.symbol, proposal.asset_class, proposal.currency, proposal.action,
                proposal.signal, proposal.confidence, proposal.source, proposal.price,
                proposal.quantity, proposal.amount, proposal.rationale, status,
                proposal.created_at,
            ),
        )
        proposal.id = int(cursor.lastrowid)
    return proposal.id


def pending_proposals() -> list[dict]:
    """Danh sách đề xuất đang chờ người dùng duyệt."""
    rows = get_conn().execute(
        "SELECT * FROM proposals WHERE status = 'pending' ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_proposal(proposal_id: int, approved: bool, decided_by: str = "telegram") -> dict | None:
    """Ghi nhận quyết định của người dùng cho một đề xuất.

    Chỉ chuyển được từ ``pending`` sang ``approved``/``rejected`` **một lần duy
    nhất**. Đây là bảo vệ bắt buộc: người dùng có thể bấm nút hai lần, hoặc
    Telegram gửi lại cùng một callback, và nếu không chặn thì lệnh sẽ được đặt
    hai lần — sai lệch vị thế và mất tiền thật.

    Trả về bản ghi kèm khoá ``already_decided`` cho biết đề xuất đã được xử lý
    từ trước hay chưa; ``None`` nếu không tìm thấy đề xuất.
    """
    with transaction() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if not row:
            return None

        result = dict(row)
        if row["status"] != "pending":
            result["already_decided"] = True
            return result

        status = "approved" if approved else "rejected"
        # Điều kiện ``status = 'pending'`` trong câu UPDATE khiến thao tác có tính
        # nguyên tử: hai lần bấm song song chỉ một lần thắng.
        cursor = conn.execute(
            """UPDATE proposals SET status = ?, decided_at = ?, decided_by = ?
               WHERE id = ? AND status = 'pending'""",
            (status, utcnow_iso(), decided_by, proposal_id),
        )
        result["already_decided"] = cursor.rowcount == 0
        result["status"] = status if cursor.rowcount else row["status"]
        return result


def _linkable_proposal_id(conn, proposal: dict) -> int | None:
    """Mã đề xuất để gắn vào sổ lệnh, hoặc ``None`` nếu đề xuất không còn tồn tại.

    Bảng ``orders`` có khoá ngoại trỏ tới ``proposals``. Lệnh vẫn có thể được đặt
    khi đề xuất đã bị xoá (ví dụ người dùng dọn dữ liệu cũ) — khi đó ghi ``None``
    thay vì để SQLite báo ``FOREIGN KEY constraint failed`` và làm hỏng cả giao
    dịch đã thực hiện thành công trên sàn.
    """
    proposal_id = proposal.get("id")
    if not proposal_id:
        return None

    row = conn.execute("SELECT id FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        logger.warning(
            "Đề xuất #%s không còn trong sổ — lệnh vẫn được ghi nhưng không gắn đề xuất.",
            proposal_id,
        )
        return None
    return int(proposal_id)


def execute_approved(proposal: dict) -> dict:
    """Đặt lệnh cho một đề xuất đã được người dùng duyệt.

    Lệnh được gửi tới broker **trước**, rồi mới ghi sổ. Thứ tự này là chủ đích:
    nếu ghi sổ trước mà sàn từ chối thì sổ sai; còn nếu sàn khớp mà ghi sổ lỗi thì
    ít nhất tiền đã ra/vào đúng như người dùng duyệt, và lỗi được ghi log rõ ràng.
    """
    from finagent.broker import get_broker

    broker = get_broker()
    if proposal["action"] == "buy":
        order = broker.buy(proposal["symbol"], proposal["quantity"], proposal["price"], proposal["asset_class"])
    else:
        order = broker.sell(proposal["symbol"], proposal["quantity"], proposal["price"], proposal["asset_class"])

    try:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO orders
                   (proposal_id, symbol, side, quantity, price, status, mode, broker_ref, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _linkable_proposal_id(conn, proposal),
                    order.symbol, order.side, order.quantity, order.price,
                    order.status, order.mode, order.broker_ref, utcnow_iso(),
                ),
            )
    except Exception:  # noqa: BLE001 - lệnh đã gửi rồi, không được nuốt lỗi ghi sổ
        logger.exception(
            "Lệnh %s %s đã gửi tới sàn nhưng ghi sổ thất bại (broker_ref=%s)",
            order.side, order.symbol, order.broker_ref,
        )
        raise
    return {
        "ok": order.ok,
        "status": order.status,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "price": order.price,
        "amount": order.amount,
        "message": order.message,
        "broker_ref": order.broker_ref,
        "currency": order.currency,
    }
