"""Telegram client interface + an offline mock (DESIGN.md §3.1 Gateway).

The gateway only ever calls this small surface, so the live Bot API adapter and the
mock are interchangeable. The mock records everything sent and lets tests/the demo
locate a button's callback data to simulate a human tap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class Button:
    label: str
    callback_data: str


class TelegramClient(Protocol):
    def send_message(self, chat_id: str, text: str,
                     buttons: Optional[list[Button]] = None,
                     thread: str = "") -> str: ...
    def edit_message_text(self, chat_id: str, message_id: str, text: str,
                          buttons: Optional[list[Button]] = None) -> None: ...
    def send_channel(self, text: str) -> str: ...


@dataclass
class _Message:
    message_id: str
    chat_id: str
    text: str
    thread: str = ""
    buttons: list[Button] = field(default_factory=list)


class MockTelegramClient:
    """In-memory Telegram used for offline runs and tests."""

    def __init__(self):
        self.messages: dict[str, _Message] = {}
        self.channel_posts: list[str] = []
        self.transcript: list[str] = []      # human-readable log for the demo
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"m{self._counter}"

    def send_message(self, chat_id: str, text: str,
                     buttons: Optional[list[Button]] = None, thread: str = "") -> str:
        mid = self._next_id()
        self.messages[mid] = _Message(mid, chat_id, text, thread, list(buttons or []))
        prefix = f"[{thread}] " if thread else ""
        self.transcript.append(f"{prefix}{text}")
        if buttons:
            self.transcript.append("    buttons: " + " | ".join(b.label for b in buttons))
        return mid

    def edit_message_text(self, chat_id: str, message_id: str, text: str,
                          buttons: Optional[list[Button]] = None) -> None:
        msg = self.messages.get(message_id)
        if msg is None:
            return
        msg.text = text
        msg.buttons = list(buttons or [])
        self.transcript.append(f"(edit {message_id}) {text}")

    def send_channel(self, text: str) -> str:
        self.channel_posts.append(text)
        self.transcript.append(f"[broadcast] {text}")
        return f"c{len(self.channel_posts)}"

    # -- test helpers ------------------------------------------------------ #
    def button_data(self, message_id: str, label_contains: str) -> str:
        msg = self.messages[message_id]
        for b in msg.buttons:
            if label_contains.lower() in b.label.lower():
                return b.callback_data
        raise KeyError(f"no button matching {label_contains!r} on {message_id}")

    def has_button(self, message_id: str, label_contains: str) -> bool:
        msg = self.messages.get(message_id)
        if not msg:
            return False
        return any(label_contains.lower() in b.label.lower() for b in msg.buttons)
