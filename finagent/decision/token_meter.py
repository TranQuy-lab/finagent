"""Đo lượng token LLM tiêu thụ cho mỗi lượt phân tích.

Khung TradingAgents không đếm token, và sàn opencode cũng không có endpoint thống
kê. Nhưng ``TradingAgentsGraph`` có sẵn tham số ``callbacks`` và truyền thẳng vào
LLM, nên chỉ cần cắm một callback của LangChain vào là đo được.

Cần đo vì hai lý do thực tế:

- **Ước lượng chi phí.** Một lượt quét gọi hàng trăm lời gọi LLM; không biết tốn bao
  nhiêu thì không biết có nên chạy thường xuyên không.
- **Phát hiện bất thường.** Nếu prompt phình to bất thường (ví dụ dữ liệu tin tức
  lọt nguyên trang vào ngữ cảnh), số token vào sẽ tăng vọt — nhìn thấy được ngay.

Cách dùng::

    from finagent.decision.token_meter import measure

    with measure() as meter:
        graph.propagate(...)
    print(meter.summary())
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class TokenMeter:
    """Bộ đếm luỹ kế lượng token và số lời gọi LLM.

    An toàn khi dùng từ nhiều luồng: lượt quét chạy song song nên callback sẽ được
    gọi đồng thời từ nhiều luồng.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Token dùng cho bước suy luận bên trong của model (nếu nhà cung cấp báo).
    reasoning_tokens: int = 0
    #: Số lời gọi mà nhà cung cấp không trả về số liệu token.
    unmeasured_calls: int = 0
    #: Token lớn nhất trong một lời gọi — dùng để phát hiện prompt phình bất thường.
    largest_prompt: int = 0
    per_model: dict[str, int] = field(default_factory=dict)

    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(self, usage: dict, model: str = "không rõ") -> None:
        """Ghi nhận số liệu của một lời gọi."""
        if not usage:
            with self._lock:
                self.unmeasured_calls += 1
            return

        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        reasoning = int(details.get("reasoning_tokens") or 0)

        with self._lock:
            self.calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.reasoning_tokens += reasoning
            self.largest_prompt = max(self.largest_prompt, prompt)
            self.per_model[model] = self.per_model.get(model, 0) + prompt + completion

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "largest_prompt": self.largest_prompt,
            "unmeasured_calls": self.unmeasured_calls,
            "per_model": dict(self.per_model),
        }

    def summary(self, symbols: int = 1) -> str:
        """Tóm tắt dạng văn bản cho người đọc."""
        if not self.calls and not self.unmeasured_calls:
            return "Chưa ghi nhận lời gọi LLM nào."

        lines = [
            f"  Số lời gọi LLM      : {self.calls}"
            + (f" (+{self.unmeasured_calls} không đo được)" if self.unmeasured_calls else ""),
            f"  Token vào           : {self.prompt_tokens:,}",
            f"  Token ra            : {self.completion_tokens:,}",
        ]
        if self.reasoning_tokens:
            lines.append(f"  (trong đó suy luận) : {self.reasoning_tokens:,}")
        lines.append(f"  TỔNG                : {self.total_tokens:,}")

        if self.calls:
            lines.append(f"  Trung bình mỗi gọi  : {self.total_tokens // self.calls:,} token")
        if symbols > 1:
            lines.append(f"  Trung bình mỗi mã   : {self.total_tokens // symbols:,} token")
        if self.largest_prompt:
            lines.append(f"  Prompt lớn nhất     : {self.largest_prompt:,} token")

        return "\n".join(lines)


def _base_callback_class():
    """Lớp cơ sở của LangChain, hoặc một lớp rỗng nếu không có.

    Phải kế thừa ``BaseCallbackHandler`` thật: LangChain dùng pydantic để kiểm tra
    kiểu và sẽ từ chối mọi callback không phải là lớp con, kèm lỗi
    ``Input should be an instance of BaseCallbackHandler``. Một lớp thường có đúng
    phương thức ``on_llm_end`` vẫn không qua được bước kiểm tra này.

    Trả về lớp rỗng khi thiếu ``langchain_core`` để module vẫn import được ở nơi
    không cài khung đa tác nhân (ví dụ máy con).
    """
    try:
        from langchain_core.callbacks import BaseCallbackHandler

        return BaseCallbackHandler
    except ImportError:  # pragma: no cover - chỉ xảy ra khi thiếu langchain
        return object


class _MeterCallback(_base_callback_class()):  # type: ignore[misc]
    """Callback LangChain: đọc số liệu token mỗi khi một lời gọi LLM kết thúc."""

    def __init__(self, meter: TokenMeter) -> None:
        try:
            super().__init__()
        except TypeError:  # lớp rỗng không nhận tham số
            pass
        self.meter = meter

    # LangChain gọi hàm này sau mỗi lời gọi LLM.
    def on_llm_end(self, response, **kwargs) -> None:  # noqa: ANN001
        model = "không rõ"
        usage: dict = {}

        try:
            # Đường đi chuẩn: response.llm_output["token_usage"].
            llm_output = getattr(response, "llm_output", None) or {}
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            model = llm_output.get("model_name") or llm_output.get("model") or model

            # Một số nhà cung cấp chỉ đặt số liệu trên từng thế hệ (generation).
            if not usage:
                generations = getattr(response, "generations", None) or []
                if generations and generations[0]:
                    message = getattr(generations[0][0], "message", None)
                    usage = getattr(message, "usage_metadata", None) or {}
                    model = getattr(message, "response_metadata", {}).get("model_name", model)
        except Exception as exc:  # noqa: BLE001 - đo lường không được làm hỏng lượt chạy
            logger.debug("Không đọc được số liệu token: %s", exc)
            return

        self.meter.record(usage, str(model))


#: Bộ đếm dùng chung cho lượt chạy hiện tại. Callback không nhận được tham số, nên
#: cần một chỗ để ghi vào.
_CURRENT: TokenMeter | None = None
_CURRENT_LOCK = Lock()


def current_meter() -> TokenMeter | None:
    return _CURRENT


@contextmanager
def measure():
    """Bật đo token trong phạm vi khối lệnh.

    Lồng nhau thì bộ đếm ngoài cùng được giữ nguyên (không hỗ trợ đo lồng).
    """
    global _CURRENT
    meter = TokenMeter()
    with _CURRENT_LOCK:
        previous, _CURRENT = _CURRENT, meter

    try:
        yield meter
    finally:
        with _CURRENT_LOCK:
            _CURRENT = previous


def build_callbacks() -> list:
    """Danh sách callback để truyền vào ``TradingAgentsGraph(callbacks=...)``.

    Trả về rỗng nếu chưa bật đo, để không tốn gì khi không cần.
    """
    meter = current_meter()
    if meter is None:
        return []
    return [_MeterCallback(meter)]


def install_global_callback() -> TokenMeter:
    """Bộ đếm toàn cục, dùng cho tiến trình dài (máy chủ chạy nền).

    Đặt một lần rồi mọi lượt phân tích đều cộng dồn vào đó, kể cả khi chạy song
    song. Dùng để xem tổng mức tiêu thụ qua nhiều lượt quét.
    """
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is None:
            _CURRENT = TokenMeter()
        return _CURRENT
