"""LLM giả lập tương thích OpenAI, dùng để kiểm chứng đường đi LLM mà không cần API key.

Đây là công cụ kiểm thử quan trọng: đường đi LLM (TradingAgents + DeepSeek) chỉ
chạy được khi có khoá API, nên nếu không có gì thay thế thì nó hoàn toàn không
được kiểm chứng. Máy chủ giả lập này cho phép chạy trọn đồ thị đa tác nhân và
— quan trọng nhất — **ghi lại toàn bộ prompt đã nhận**, nhờ đó khẳng định được
rằng dữ liệu Việt Nam thật sự chảy vào LLM chứ không phải chỉ chạy cho có.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Câu trả lời mặc định: đủ để bộ tách tín hiệu nhận ra mức "Buy".
DEFAULT_REPLY = (
    "BÁO CÁO PHÂN TÍCH\n"
    "\n"
    "Dựa trên dữ liệu giá và tin tức được cung cấp, thanh khoản ở mức trung bình "
    "và động lượng ngắn hạn nghiêng về phía tăng. Các chỉ báo kỹ thuật cho thấy "
    "vùng hỗ trợ đang giữ vững.\n"
    "\n"
    "Khuyến nghị: nắm giữ tỷ trọng vừa phải và theo dõi thêm.\n"
    "Rating: Buy\n"
)


@dataclass
class MockLLMState:
    """Trạng thái dùng chung giữa luồng máy chủ và luồng kiểm thử."""

    reply: str = DEFAULT_REPLY
    requests: list[dict] = field(default_factory=list)
    #: Toàn bộ nội dung prompt đã nhận, gộp lại để tiện tìm kiếm.
    prompts: list[str] = field(default_factory=list)
    #: Tên các tool mà LLM giả lập đã yêu cầu gọi.
    tool_calls_made: list[str] = field(default_factory=list)
    #: Có mô phỏng hành vi gọi tool hay không.
    emit_tool_calls: bool = True
    #: Số lượt gọi tool tối đa cho mỗi cuộc hội thoại trước khi trả lời kết luận.
    max_tool_calls: int = 4

    def all_prompt_text(self) -> str:
        return "\n".join(self.prompts)

    def call_count(self) -> int:
        return len(self.requests)


#: Tham số hợp lệ cho từng tool mà tác nhân có thể gọi.
#: Thứ tự này cũng là thứ tự LLM giả lập lần lượt yêu cầu gọi, để trải đều
#: các nhánh lấy dữ liệu: giá → chỉ báo → tin theo mã → tin vĩ mô.
TOOL_ARGUMENTS: dict[str, dict] = {
    "get_stock_data": {"symbol": "FPT", "start_date": "2026-08-01", "end_date": "2026-09-28"},
    "get_verified_market_snapshot": {"symbol": "FPT", "curr_date": "2026-09-28", "look_back_days": 30},
    "get_indicators": {"symbol": "FPT", "indicator": "rsi", "curr_date": "2026-09-28", "look_back_days": 30},
    "get_news": {"ticker": "FPT", "start_date": "2026-09-01", "end_date": "2026-09-28"},
    "get_global_news": {"curr_date": "2026-09-28", "look_back_days": 7, "limit": 20},
    "get_fundamentals": {"ticker": "FPT", "curr_date": "2026-09-28"},
}


def _has_tool_result(messages: list[dict]) -> int:
    """Đếm số kết quả tool đã có trong lịch sử hội thoại."""
    return sum(1 for message in messages if message.get("role") == "tool")


def _pick_tool(payload: dict, already_called: int) -> str | None:
    """Chọn tool tiếp theo cần gọi, theo thứ tự ưu tiên đã định."""
    tools = payload.get("tools") or []
    if not tools:
        return None

    available = {
        tool.get("function", {}).get("name")
        for tool in tools
        if isinstance(tool, dict)
    }
    ordered = [name for name in TOOL_ARGUMENTS if name in available]
    if not ordered:
        return None
    if already_called >= len(ordered):
        return None
    return ordered[already_called]


#: Tên trường, nếu gặp, sẽ được điền giá trị tín hiệu để bộ tách rating hoạt động.
_RATING_FIELD_HINTS = ("rating", "signal", "action", "recommendation", "decision")


def json_from_schema(schema: dict, field_name: str = "") -> object:
    """Sinh một giá trị JSON tối thiểu nhưng **hợp lệ** theo JSON Schema.

    TradingAgents dùng structured output cho những bước then chốt (Research Manager,
    Trader, Portfolio Manager). Nếu LLM giả lập chỉ trả văn bản thường, các bước đó
    phải chạy lại lần hai và bài test không còn phản ánh đúng luồng thật. Hàm này
    dựng đủ dữ liệu để đi đúng nhánh structured output.
    """
    if not isinstance(schema, dict):
        return "Buy"

    # Hợp nhất các nhánh oneOf/anyOf/allOf: dùng nhánh đầu tiên.
    for key in ("oneOf", "anyOf", "allOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            return json_from_schema(schema[key][0], field_name)

    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((t for t in schema_type if t != "null"), "string")

    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        required = schema.get("required") or list(properties)
        return {
            name: json_from_schema(properties.get(name, {}), name)
            for name in required
            if name in properties
        }

    if schema_type == "array":
        return []
    if schema_type in ("number", "integer"):
        return 0
    if schema_type == "boolean":
        return True

    # Chuỗi: trường tín hiệu phải trả về một mức hợp lệ để tách được rating.
    if any(hint in field_name.lower() for hint in _RATING_FIELD_HINTS):
        return "Buy"
    return "Phân tích tự động từ FinAgent."


def _schema_tool_name(payload: dict) -> str | None:
    """Tìm hàm schema của structured output trong yêu cầu, nếu có.

    Có hai cách LangChain gửi structured output, và cả hai đều gặp trong thực tế:

    * **Ép buộc** — ``tool_choice`` chỉ đích danh hàm (SentimentReport,
      TraderProposal).
    * **Để model tự chọn** — ``tool_choice`` là ``None`` nhưng danh sách tool chỉ
      gồm đúng một hàm schema (ResearchPlan, PortfolioDecision).

    Điểm chung để nhận diện: hàm schema **không** nằm trong danh sách tool lấy dữ
    liệu. Nếu tất cả tool đều là hàm schema thì đây chắc chắn là structured output.
    """
    tools = payload.get("tools") or []
    names = [
        (tool.get("function") or {}).get("name")
        for tool in tools
        if isinstance(tool, dict)
    ]
    names = [name for name in names if name]
    if not names:
        return None

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        forced = (tool_choice.get("function") or {}).get("name")
        if forced and forced not in TOOL_ARGUMENTS:
            return forced
        if forced:
            return None       # ép gọi tool lấy dữ liệu — không phải structured output

    # tool_choice là None / "required" / "any": chỉ là structured output khi
    # toàn bộ tool đều là hàm schema.
    if all(name not in TOOL_ARGUMENTS for name in names):
        return names[0]
    return None


def build_structured_reply(payload: dict) -> dict | None:
    """Mô phỏng structured output bằng phản hồi gọi hàm schema.

    LangChain dùng chế độ function-calling cho structured output: nó gửi một tool
    có ``parameters`` là JSON Schema rồi chờ phản hồi chứa ``tool_calls`` với tham
    số hợp lệ. Không mô phỏng đúng thì mọi bước structured đều phải chạy lại bằng
    văn bản thường, và bài test sẽ không còn phản ánh luồng thật.
    """
    forced = _schema_tool_name(payload)
    if not forced:
        return None

    for tool in payload.get("tools") or []:
        function = tool.get("function") or {}
        if function.get("name") != forced:
            continue
        arguments = json_from_schema(function.get("parameters") or {}, forced)
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_structured",
                "type": "function",
                "function": {
                    "name": forced,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        }
    return None


def _build_response(payload: dict, state: MockLLMState) -> dict:
    """Dựng phản hồi: hoặc yêu cầu gọi tool, hoặc trả lời kết luận.

    Mô phỏng đúng hành vi của một LLM thật: gọi vài tool để lấy dữ liệu, rồi mới
    viết báo cáo. Nhờ vậy toàn bộ đường đi tool → vendor → dữ liệu Việt Nam được
    thực sự chạy qua, chứ không chỉ chạy phần vỏ.
    """
    messages = payload.get("messages") or []
    content = state.reply

    # Structured output được gửi dưới dạng ép gọi hàm schema — phải trả về
    # tool_call có tham số hợp lệ, không phải văn bản thường.
    structured = build_structured_reply(payload)
    if structured is not None:
        return structured

    if state.emit_tool_calls:
        already_called = _has_tool_result(messages)
        if already_called < state.max_tool_calls:
            tool_name = _pick_tool(payload, already_called)
            if tool_name:
                state.tool_calls_made.append(tool_name)
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": f"call_{len(state.tool_calls_made)}",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(TOOL_ARGUMENTS[tool_name]),
                        },
                    }],
                }

    return {"role": "assistant", "content": content}


def _extract_prompt_text(payload: dict) -> str:
    """Gộp mọi trường văn bản trong yêu cầu thành một chuỗi để kiểm tra.

    Bao gồm cả nội dung các thông điệp ``tool`` — đây chính là nơi dữ liệu do
    vendor trả về xuất hiện, nên phải thu thập để khẳng định dữ liệu Việt Nam
    thật sự tới được LLM.
    """
    chunks: list[str] = []
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks)


class _Handler(BaseHTTPRequestHandler):
    """Bắt mọi yêu cầu chat completions và trả về câu trả lời cố định."""

    state: MockLLMState            # gán khi tạo server
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:      # tắt log ồn ào ra stderr
        return

    def _send_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:                  # noqa: N802 - theo chuẩn http.server
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": "mock-model", "object": "model"}]})
        else:
            self._send_json(200, {"status": "ok"})

    def do_POST(self) -> None:                 # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        self.state.requests.append(payload)
        self.state.prompts.append(_extract_prompt_text(payload))

        # Trả về đúng định dạng OpenAI chat completions mà LangChain mong đợi.
        self._send_json(200, {
            "id": f"chatcmpl-mock-{len(self.state.requests)}",
            "object": "chat.completion",
            "created": 1_790_000_000,
            "model": payload.get("model") or "mock-model",
            "choices": [{
                "index": 0,
                "message": _build_response(payload, self.state),
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })


class MockLLMServer:
    """Máy chủ LLM giả lập chạy trong luồng nền, dùng được như context manager.

    Ví dụ::

        with MockLLMServer() as llm:
            config["backend_url"] = llm.base_url
            ...
            assert "FPT" in llm.state.all_prompt_text()
    """

    def __init__(self, reply: str = DEFAULT_REPLY, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = MockLLMState(reply=reply)
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return self._server.server_address[0]

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        """Địa chỉ gốc để đặt vào ``backend_url`` của TradingAgents."""
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> MockLLMServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> MockLLMServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


if __name__ == "__main__":
    # Chạy độc lập để thử tay: python tests/mock_llm.py
    server = MockLLMServer(port=8123).start()
    print(f"LLM giả lập đang chạy tại {server.base_url}")
    print("Đặt TRADINGAGENTS_LLM_BACKEND_URL trỏ về địa chỉ này để thử.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
