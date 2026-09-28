"""Cấu hình trung tâm của hệ thống FinAgent.

Toàn bộ tham số đọc từ biến môi trường (hoặc file .env) để máy chủ và máy con
dùng chung một nguồn sự thật, không hard-code rải rác trong code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Nạp .env ở thư mục gốc dự án (nếu có) trước khi đọc biến môi trường.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_str(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} phải là số nguyên, nhận được {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} phải là số thực, nhận được {raw!r}") from exc


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw in (None, ""):
        return default
    normalized = raw.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{key} phải là true/false, nhận được {raw!r}")


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if raw in (None, ""):
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve_llm_api_key() -> str:
    """Tìm khoá API của nhà cung cấp LLM đang được chọn.

    Đọc biến môi trường ứng với ``TRADINGAGENTS_LLM_PROVIDER``. Nếu biến của nhà
    cung cấp đó trống, quét các biến khoá đã biết còn lại — người dùng thường chỉ
    dán một khoá vào ``.env`` mà quên đổi tên nhà cung cấp, và trong trường hợp đó
    chạy được vẫn hơn là báo lỗi khó hiểu.
    """
    provider = _env_str("TRADINGAGENTS_LLM_PROVIDER", "deepseek").lower()

    if provider in KEYLESS_PROVIDERS:
        return ""

    direct = os.getenv(PROVIDER_KEY_ENV.get(provider, ""), "")
    if direct:
        return direct

    # Dự phòng: nhà cung cấp đang chọn không có khoá, nhưng có khoá của nhà cung
    # cấp khác thì dùng tạm để hệ thống vẫn chạy được.
    for env_var in PROVIDER_KEY_ENV.values():
        value = os.getenv(env_var, "")
        if value:
            return value
    return ""


#: Tài sản theo dõi mặc định: crypto + chứng khoán Việt Nam.
DEFAULT_CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
DEFAULT_VN_SYMBOLS = ["VNM", "FPT", "HPG", "VCB", "TCB"]

#: Thương hiệu vàng theo dõi: SJC, DOJI, PNJ, Bảo Tín và vàng thế giới.
DEFAULT_GOLD_SYMBOLS = ["SJL1L10", "DOJINHTV", "PQHNVM", "BT9999NTT", "XAUUSD"]

#: Nhà cung cấp LLM → tên biến môi trường chứa khoá API.
#: Giữ khớp với TradingAgents để không phải cấu hình khoá ở hai nơi.
PROVIDER_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    # Endpoint OpenAI-compatible bất kỳ (opencode zen, vLLM, LM Studio, gateway nội bộ).
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "qwen-cn": "DASHSCOPE_CN_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
}

#: Nhà cung cấp chạy cục bộ, không cần khoá.
KEYLESS_PROVIDERS = {"ollama"}


@dataclass
class Settings:
    """Cấu hình của tiến trình.

    Không đánh dấu ``frozen``: cho phép tinh chỉnh ngưỡng rủi ro lúc đang chạy
    (ví dụ qua bot Telegram) và cho phép test ghi đè từng trường mà không phải
    khởi động lại tiến trình.
    """

    # --- Hạ tầng phân tán -------------------------------------------------
    redis_url: str = field(default_factory=lambda: _env_str("REDIS_URL", "redis://127.0.0.1:6379/0"))
    #: Tổng số slot thu thập chạy song song trên TOÀN BỘ máy con.
    max_concurrency: int = field(default_factory=lambda: _env_int("FINAGENT_MAX_CONCURRENCY", 10))
    #: Số tiến trình con mỗi máy con tự chạy.
    worker_concurrency: int = field(default_factory=lambda: _env_int("FINAGENT_WORKER_CONCURRENCY", 4))
    #: Tên hàng đợi Celery mà máy con lắng nghe.
    crawl_queue: str = field(default_factory=lambda: _env_str("FINAGENT_CRAWL_QUEUE", "crawl"))
    #: Khoá Redis dùng làm semaphore giới hạn slot toàn cục.
    slot_lock_key: str = field(default_factory=lambda: _env_str("FINAGENT_SLOT_KEY", "finagent:slots"))
    #: Thời gian chờ tối đa (giây) để giành được một slot trước khi bỏ qua lượt.
    slot_wait_seconds: int = field(default_factory=lambda: _env_int("FINAGENT_SLOT_WAIT", 30))
    #: Slot tự hết hạn sau bao lâu nếu máy con chết giữa chừng (giây).
    slot_ttl_seconds: int = field(default_factory=lambda: _env_int("FINAGENT_SLOT_TTL", 300))

    # --- Mô hình ngôn ngữ --------------------------------------------------
    #: Khoá API của nhà cung cấp đang chọn, đọc từ biến môi trường tương ứng
    #: (``GOOGLE_API_KEY`` với google, ``DEEPSEEK_API_KEY`` với deepseek…).
    #: Nhờ vậy đổi nhà cung cấp chỉ cần sửa ``TRADINGAGENTS_LLM_PROVIDER``.
    llm_api_key: str = field(default_factory=lambda: _resolve_llm_api_key())
    llm_provider: str = field(default_factory=lambda: _env_str("TRADINGAGENTS_LLM_PROVIDER", "deepseek"))
    #: V4 Pro / Gemini Pro cho suy luận sâu (tranh luận, ra quyết định cuối).
    deep_think_llm: str = field(default_factory=lambda: _env_str("TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-pro"))
    #: V4 Flash / Gemini Flash cho việc nhẹ (phân tích nhanh), rẻ và nhanh hơn.
    quick_think_llm: str = field(default_factory=lambda: _env_str("TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-flash"))
    output_language: str = field(default_factory=lambda: _env_str("TRADINGAGENTS_OUTPUT_LANGUAGE", "Vietnamese"))
    #: Ghi đè địa chỉ endpoint LLM. Để trống thì dùng mặc định của nhà cung cấp.
    #: Hữu ích khi trỏ vào endpoint tự lưu trữ (vLLM, LM Studio, Ollama) hoặc khi test.
    llm_backend_url: str = field(default_factory=lambda: _env_str("TRADINGAGENTS_LLM_BACKEND_URL", ""))
    max_debate_rounds: int = field(default_factory=lambda: _env_int("TRADINGAGENTS_MAX_DEBATE_ROUNDS", 1))
    max_risk_rounds: int = field(default_factory=lambda: _env_int("TRADINGAGENTS_MAX_RISK_ROUNDS", 1))

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: str = field(default_factory=lambda: _env_str("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env_str("TELEGRAM_CHAT_ID", ""))
    #: Địa chỉ API Telegram. Đổi được để dùng Bot API tự lưu trữ, hoặc để test.
    telegram_api_base: str = field(
        default_factory=lambda: _env_str("TELEGRAM_API_BASE", "https://api.telegram.org")
    )
    #: Thời gian (giây) chờ người dùng bấm duyệt trước khi đề xuất hết hạn.
    approval_timeout: int = field(default_factory=lambda: _env_int("FINAGENT_APPROVAL_TIMEOUT", 900))

    # --- Giao dịch ---------------------------------------------------------
    #: "paper" = mô phỏng. Đổi sang "live" khi đã cắm adapter broker thật.
    trading_mode: str = field(default_factory=lambda: _env_str("FINAGENT_TRADING_MODE", "paper"))
    #: Số dư ban đầu của ví tiền đồng (dùng cho chứng khoán và vàng).
    paper_starting_cash: float = field(default_factory=lambda: _env_float("FINAGENT_PAPER_CASH", 1_000_000_000.0))
    #: Số dư ban đầu của ví USDT (dùng cho crypto, vì crypto niêm yết bằng USDT).
    paper_starting_usdt: float = field(default_factory=lambda: _env_float("FINAGENT_PAPER_USDT", 5_000.0))
    #: Tỷ giá quy đổi khi hiển thị tổng giá trị danh mục bằng VND.
    usdt_vnd_rate: float = field(default_factory=lambda: _env_float("FINAGENT_USDT_VND", 26_300.0))
    #: Chỉ đặt lệnh khi tín hiệu đạt mức này trở lên.
    min_signal_confidence: float = field(default_factory=lambda: _env_float("FINAGENT_MIN_CONFIDENCE", 0.55))
    #: Tỷ lệ vốn tối đa cho một lệnh.
    max_position_pct: float = field(default_factory=lambda: _env_float("FINAGENT_MAX_POSITION_PCT", 0.10))
    #: Lệnh vượt ngưỡng này (VND) bắt buộc người dùng duyệt qua Telegram.
    require_approval_above: float = field(default_factory=lambda: _env_float("FINAGENT_APPROVAL_THRESHOLD", 5_000_000.0))

    # --- Trí nhớ và tự phản tỉnh (TradingAgents) --------------------------
    #: Bật nhật ký quyết định. Mỗi lần chạy ghi lại quyết định; lần sau với cùng
    #: mã, hệ thống lấy lợi nhuận thực tế, sinh đoạn tự rút kinh nghiệm rồi bơm
    #: vào prompt của Portfolio Manager. Đây là cơ chế "học từ sai lầm".
    memory_enabled: bool = field(default_factory=lambda: _env_bool("FINAGENT_MEMORY_ENABLED", True))
    #: Chặn trên số mục trong nhật ký (None = không giới hạn).
    memory_max_entries: int = field(default_factory=lambda: _env_int("FINAGENT_MEMORY_MAX_ENTRIES", 500))
    #: Nơi lưu nhật ký quyết định. Đặt trong thư mục dự án để dễ sao lưu và
    #: không lẫn với nhật ký của TradingAgents gốc.
    memory_log_path: Path = field(default_factory=lambda: Path(
        _env_str("FINAGENT_MEMORY_LOG", str(PROJECT_ROOT / "data" / "decision_memory.md"))
    ))
    #: Nơi lưu checkpoint để chạy tiếp khi hỏng.
    checkpoint_dir: Path = field(default_factory=lambda: Path(
        _env_str("FINAGENT_CHECKPOINT_DIR", str(PROJECT_ROOT / "data" / "checkpoints"))
    ))
    #: Nơi lưu kết quả phân tích và backtest.
    results_dir: Path = field(default_factory=lambda: Path(
        _env_str("FINAGENT_RESULTS_DIR", str(PROJECT_ROOT / "data" / "results"))
    ))
    #: Số ngày nắm giữ trước khi chấm điểm một quyết định.
    holding_period_days: int = field(default_factory=lambda: _env_int("FINAGENT_HOLDING_DAYS", 5))

    # --- Lưu trạng thái để chạy tiếp khi hỏng -----------------------------
    #: LangGraph lưu state sau mỗi bước; chạy hỏng thì chạy lại tiếp từ bước cuối
    #: thay vì làm lại từ đầu. Rất đáng bật vì mỗi lượt phân tích tốn vài phút.
    checkpoint_enabled: bool = field(default_factory=lambda: _env_bool("FINAGENT_CHECKPOINT", True))

    # --- Tốc độ quét thị trường --------------------------------------------
    #: Lọc trước bằng mô hình ML rẻ tiền trước khi gọi LLM đắt tiền.
    #:
    #: Một lượt phân tích đầy đủ 12 tác nhân tốn khoảng 13 phút cho MỘT mã. Quét
    #: cả 7 mã tuần tự mất hơn một tiếng rưỡi — quá chậm để dùng thật, và tốn hạn
    #: mức API vô ích cho những mã đang ở vùng trung tính.
    #:
    #: Bật cờ này thì hệ thống chạy mô hình ML trước (vài mili giây). Chỉ gọi LLM
    #: khi tín hiệu ML ra khỏi vùng trung tính, HOẶC khi đang giữ vị thế mã đó —
    #: vì lúc đang có tiền trong mã thì luôn cần phân tích kỹ để biết khi nào thoát.
    llm_prefilter: bool = field(default_factory=lambda: _env_bool("FINAGENT_LLM_PREFILTER", True))
    #: Số mã phân tích LLM chạy song song. Các lời gọi này chờ mạng là chính nên
    #: chạy song song cho tốc độ gần như nhân lên theo số luồng.
    #: Để 1 nếu muốn chạy tuần tự như cũ.
    scan_parallelism: int = field(default_factory=lambda: _env_int("FINAGENT_SCAN_PARALLELISM", 3))
    #: Vùng trung tính của ML: xác suất trong khoảng này coi như không có tín hiệu.
    #: Khớp với ngưỡng trong ``combine_signals``.
    ml_neutral_band: float = field(default_factory=lambda: _env_float("FINAGENT_ML_NEUTRAL_BAND", 0.05))

    # --- Giờ giao dịch -----------------------------------------------------
    #: Chỉ phân tích chứng khoán Việt Nam trong giờ giao dịch.
    #:
    #: Sàn Việt Nam chỉ mở 9:00–15:00 các ngày trong tuần. Phân tích chứng khoán lúc
    #: 3 giờ sáng tốn khoảng 217.000 token cho mỗi mã mà giá và thanh khoản đều là
    #: số cũ — kết luận không dùng được vào việc gì. Crypto không bị giới hạn này vì
    #: thị trường crypto chạy 24/7.
    market_hours_only: bool = field(default_factory=lambda: _env_bool("FINAGENT_MARKET_HOURS_ONLY", True))
    #: Giờ mở cửa và đóng cửa theo giờ Việt Nam, dạng "HH:MM".
    market_open: str = field(default_factory=lambda: _env_str("FINAGENT_MARKET_OPEN", "09:00"))
    market_close: str = field(default_factory=lambda: _env_str("FINAGENT_MARKET_CLOSE", "15:00"))

    #: Các mốc giờ quét trong ngày, cách nhau dấu phẩy, dạng "HH:MM" giờ Việt Nam.
    #:
    #: Quét theo mốc giờ thay vì theo chu kỳ là điều khác biệt lớn về chi phí. Một
    #: lượt quét 4 mã tốn khoảng 867.000 token; quét mỗi 30 phút suốt ngày là hơn
    #: **1,2 tỷ token mỗi tháng** — vừa tốn kém vừa vô nghĩa, vì ngoài giờ giao dịch
    #: giá và thanh khoản đều không đổi.
    #:
    #: Hai mốc là đủ và hợp lý với nhịp thị trường Việt Nam:
    #:   09:45 — sau khi phiên sáng đã ổn định, biến động mở cửa đã qua
    #:   14:00 — trước khi đóng cửa, kịp hành động trong ngày
    #:
    #: Đặt rỗng để quay lại quét theo chu kỳ ``monitor_interval``.
    scan_times: list[str] = field(default_factory=lambda: _env_list("FINAGENT_SCAN_TIMES", ["09:45", "14:00"]))

    # --- Độ sâu suy luận ---------------------------------------------------
    #: Số vòng tranh luận giữa bò và gấu. Nhiều vòng hơn = soi kỹ hơn, nhưng tốn
    #: thời gian và hạn mức API gấp bội.
    extra_debate_rounds: int = field(default_factory=lambda: _env_int("FINAGENT_EXTRA_DEBATE_ROUNDS", 0))
    #: Mức suy luận của Gemini: low / high. Để trống thì dùng mặc định của model.
    google_thinking_level: str = field(default_factory=lambda: _env_str("FINAGENT_GOOGLE_THINKING", ""))
    #: Nhiệt độ mẫu. Thấp hơn = ổn định hơn giữa các lần chạy.
    temperature: float = field(default_factory=lambda: _env_float("TRADINGAGENTS_TEMPERATURE", 0.0))

    # --- Độ giàu của ngữ cảnh ---------------------------------------------
    #: Số bài tin tối đa cho mỗi mã.
    news_article_limit: int = field(default_factory=lambda: _env_int("FINAGENT_NEWS_ARTICLE_LIMIT", 30))
    #: Số bài tin vĩ mô tối đa.
    global_news_article_limit: int = field(default_factory=lambda: _env_int("FINAGENT_GLOBAL_NEWS_LIMIT", 25))
    #: Cửa sổ nhìn lại của tin vĩ mô (ngày).
    global_news_lookback_days: int = field(default_factory=lambda: _env_int("FINAGENT_GLOBAL_NEWS_LOOKBACK", 14))

    # --- Nguồn dữ liệu bổ trợ (không bắt buộc) ----------------------------
    #: FRED cho chỉ số vĩ mô Mỹ (lãi suất Fed, CPI, lợi suất trái phiếu). Miễn phí
    #: tại https://fred.stlouisfed.org/docs/api/api_key.html
    #: Vĩ mô Mỹ tác động trực tiếp tới tỷ giá, vàng và crypto của Việt Nam.
    fred_api_key: str = field(default_factory=lambda: _env_str("FRED_API_KEY", ""))
    #: Polymarket cho xác suất các sự kiện tương lai (Fed giảm lãi suất, suy thoái).
    #: Miễn phí, không cần khoá. Để tắt nếu không cần.
    polymarket_enabled: bool = field(default_factory=lambda: _env_bool("FINAGENT_POLYMARKET", True))

    # --- Chỉ số chuẩn để chấm điểm alpha ----------------------------------
    #: Chỉ số so sánh cho từng nhóm tài sản. Backtest chấm điểm theo alpha —
    #: lợi nhuận vượt chỉ số — nên phải có chỉ số đúng thị trường.
    benchmark_vn: str = field(default_factory=lambda: _env_str("FINAGENT_BENCHMARK_VN", "VNINDEX"))
    benchmark_crypto: str = field(default_factory=lambda: _env_str("FINAGENT_BENCHMARK_CRYPTO", "BTCUSDT"))

    # --- Sàn giao dịch thật (Binance) --------------------------------------
    binance_api_key: str = field(default_factory=lambda: _env_str("BINANCE_API_KEY", ""))
    binance_api_secret: str = field(default_factory=lambda: _env_str("BINANCE_API_SECRET", ""))
    #: Mặc định dùng Testnet — tiền giả, an toàn để thử.
    binance_testnet: bool = field(default_factory=lambda: _env_bool("BINANCE_TESTNET", True))
    #: Cửa chặn an toàn: muốn giao dịch tiền thật phải đặt chính xác ``YES``.
    #: Cố ý khó, để không ai vô tình bật tiền thật chỉ vì sửa nhầm một dòng.
    binance_live_confirm: str = field(default_factory=lambda: _env_str("BINANCE_LIVE_CONFIRM", ""))
    #: Phí sàn Binance spot (0,1% mặc định; 0,075% nếu trả bằng BNB).
    binance_fee_rate: float = field(default_factory=lambda: _env_float("BINANCE_FEE_RATE", 0.001))
    #: Ghi đè địa chỉ API. Để trống thì tự chọn theo ``binance_testnet``.
    #: Hữu ích khi dùng Binance US, một proxy, hoặc sàn giả lập trong test.
    binance_base_url: str = field(default_factory=lambda: _env_str("BINANCE_BASE_URL", ""))

    # --- Thu thập dữ liệu --------------------------------------------------
    crypto_symbols: list[str] = field(default_factory=lambda: _env_list("FINAGENT_CRYPTO_SYMBOLS", DEFAULT_CRYPTO_SYMBOLS))
    vn_symbols: list[str] = field(default_factory=lambda: _env_list("FINAGENT_VN_SYMBOLS", DEFAULT_VN_SYMBOLS))
    gold_symbols: list[str] = field(default_factory=lambda: _env_list("FINAGENT_GOLD_SYMBOLS", DEFAULT_GOLD_SYMBOLS))
    #: Chu kỳ quét thị trường (giây) của bộ giám sát.
    #: Nhịp quét thị trường. Phải dài hơn thời gian một lượt quét, nếu không bộ lập
    #: lịch sẽ thử khởi động lượt mới khi lượt cũ chưa xong và ghi cảnh báo liên tục.
    #: Sau khi lọc trước bằng ML và chạy song song, một lượt quét mất khoảng 25–30
    #: phút cho 4–7 mã, nên để 1800 giây.
    monitor_interval: int = field(default_factory=lambda: _env_int("FINAGENT_MONITOR_INTERVAL", 1800))
    #: Chu kỳ thu thập tin tức (giây).
    news_interval: int = field(default_factory=lambda: _env_int("FINAGENT_NEWS_INTERVAL", 900))
    #: Chặn trên số bài viết lấy về mỗi nguồn mỗi lượt, tránh ngốn tài nguyên.
    news_max_items: int = field(default_factory=lambda: _env_int("FINAGENT_NEWS_MAX_ITEMS", 40))

    # --- Lưu trữ -----------------------------------------------------------
    db_path: Path = field(default_factory=lambda: Path(_env_str("FINAGENT_DB", str(PROJECT_ROOT / "data" / "finagent.db"))))
    log_level: str = field(default_factory=lambda: _env_str("FINAGENT_LOG_LEVEL", "INFO"))

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def llm_enabled(self) -> bool:
        """Có đủ điều kiện chạy suy luận LLM hay không."""
        return bool(self.llm_api_key) or self.llm_provider in KEYLESS_PROVIDERS

    @property
    def llm_key_env_var(self) -> str:
        """Tên biến môi trường chứa khoá của nhà cung cấp đang chọn."""
        return PROVIDER_KEY_ENV.get(self.llm_provider.lower(), "")

    def validate(self) -> list[str]:
        """Trả về danh sách cảnh báo cấu hình (rỗng nghĩa là hợp lệ)."""
        warnings: list[str] = []
        if not self.llm_enabled:
            env_var = self.llm_key_env_var or "khoá API"
            warnings.append(
                f"Thiếu {env_var} cho nhà cung cấp {self.llm_provider!r} — "
                "phần suy luận LLM sẽ không chạy được."
            )
        if not self.telegram_enabled:
            warnings.append("Thiếu TELEGRAM_BOT_TOKEN/CHAT_ID — thông báo Telegram sẽ bị tắt.")
        if self.max_concurrency < 1:
            warnings.append("FINAGENT_MAX_CONCURRENCY phải >= 1.")
        if self.trading_mode not in ("paper", "live"):
            warnings.append(f"FINAGENT_TRADING_MODE không hợp lệ: {self.trading_mode!r} (chỉ nhận 'paper' hoặc 'live').")
        return warnings


settings = Settings()
