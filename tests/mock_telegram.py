"""Máy chủ Telegram giả lập để kiểm chứng luồng báo cáo và duyệt lệnh.

Đường đi Telegram chỉ chạy được khi có bot token thật, nên nếu không có gì thay thế
thì phần quan trọng nhất — người dùng bấm Duyệt/Từ chối rồi máy chủ đặt lệnh — sẽ
hoàn toàn không được kiểm chứng. Máy chủ giả lập này ghi lại mọi tin nhắn đã gửi,
nhờ đó khẳng định được nội dung báo cáo và nút bấm đúng như thiết kế.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class MockTelegramState:
    """Bản ghi các lời gọi API đã nhận."""

    messages: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    callback_answers: list[dict] = field(default_factory=list)
    _next_id: int = 1

    def next_message_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def last_message(self) -> dict:
        return self.messages[-1] if self.messages else {}

    def all_text(self) -> str:
        return "\n".join(message.get("text", "") for message in self.messages)

    def buttons_of(self, index: int = -1) -> list[list[dict]]:
        """Bàn phím inline của tin nhắn thứ ``index``."""
        if not self.messages:
            return []
        markup = self.messages[index].get("reply_markup") or {}
        return markup.get("inline_keyboard") or []


class _Handler(BaseHTTPRequestHandler):
    """Giả lập các method Bot API mà FinAgent dùng."""

    state: MockTelegramState
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        return

    def _send_json(self, body: dict) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:                  # noqa: N802 - theo chuẩn http.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        # Đường dẫn dạng /bot<token>/<method>
        method = self.path.rstrip("/").rsplit("/", 1)[-1]

        if method == "sendMessage":
            message_id = self.state.next_message_id()
            self.state.messages.append(payload)
            self._send_json({
                "ok": True,
                "result": {
                    "message_id": message_id,
                    "chat": {"id": payload.get("chat_id")},
                    "text": payload.get("text", ""),
                },
            })
            return

        if method == "editMessageText":
            self.state.edits.append(payload)
            self._send_json({"ok": True, "result": {"message_id": 1, "text": payload.get("text", "")}})
            return

        if method == "answerCallbackQuery":
            self.state.callback_answers.append(payload)
            self._send_json({"ok": True, "result": True})
            return

        if method == "getMe":
            self._send_json({"ok": True, "result": {"id": 1, "is_bot": True, "username": "finagent_test_bot"}})
            return

        self._send_json({"ok": True, "result": True})

    def do_GET(self) -> None:                   # noqa: N802
        self._send_json({"ok": True, "result": []})


class MockTelegramServer:
    """Máy chủ Telegram giả lập chạy trong luồng nền, dùng như context manager."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, token: str = "test-token") -> None:
        self.state = MockTelegramState()
        self.token = token
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def api_base(self) -> str:
        return f"http://{self._server.server_address[0]}:{self._server.server_address[1]}"

    def start(self) -> MockTelegramServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> MockTelegramServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


if __name__ == "__main__":
    server = MockTelegramServer(port=8124).start()
    print(f"Telegram giả lập đang chạy tại {server.api_base}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()
