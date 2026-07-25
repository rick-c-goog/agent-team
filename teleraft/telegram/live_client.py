"""Live Telegram Bot API client (DESIGN.md §3.1 Gateway, outbound).

Implements the same tiny ``TelegramClient`` surface as the mock, backed by real Bot API
HTTP calls via httpx. Outbound messages go through the **workspace bot** (the MVP
topology in DESIGN.md §3.2): the bot always present for cards, the board, and the
broadcast channel, with agent attribution carried in the message text. To run the full
one-bot-per-agent topology, give each agent its own client keyed by its token and route
notify() per agent — the interface is unchanged.

Forum topics: our internal ``thread`` label is mapped to a Telegram
``message_thread_id`` via ``topic_threads`` so each channel's messages land in the right
forum topic.

Requires ``pip install teleraft[telegram]`` (httpx).
"""

from __future__ import annotations

from typing import Optional

from .client import Button


class LiveTelegramClient:
    API = "https://api.telegram.org"

    def __init__(
        self,
        token: str,
        group_chat_id: str,
        channel_id: str = "",
        topic_threads: Optional[dict[str, str]] = None,
        *,
        http=None,
    ):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "LiveTelegramClient needs httpx: pip install teleraft[telegram]"
            ) from e
        self._httpx = httpx
        self.token = token
        self.group_chat_id = group_chat_id
        self.channel_id = channel_id
        self.topic_threads = topic_threads or {}
        # Injectable client makes this unit-testable with httpx.MockTransport.
        self._http = http or httpx.Client(timeout=60)

    # -- low-level --------------------------------------------------------- #
    def _call(self, method: str, **params) -> dict:
        url = f"{self.API}/bot{self.token}/{method}"
        resp = self._http.post(url, json={k: v for k, v in params.items() if v is not None})
        data = resp.json()
        if not data.get("ok", False):
            raise RuntimeError(f"Telegram API {method} failed: {data}")
        return data["result"]

    @staticmethod
    def _keyboard(buttons: Optional[list[Button]]):
        if not buttons:
            return None
        return {"inline_keyboard": [[{"text": b.label, "callback_data": b.callback_data}]
                                    for b in buttons]}

    def _thread_id(self, thread_label: str) -> Optional[int]:
        # thread_label looks like "#<id> · <topic>"; we key topic_threads by the topic.
        for topic, tid in self.topic_threads.items():
            if topic and topic in thread_label:
                return int(tid)
        return None

    # -- TelegramClient interface ----------------------------------------- #
    def send_message(self, chat_id: str, text: str,
                     buttons: Optional[list[Button]] = None, thread: str = "") -> str:
        result = self._call(
            "sendMessage",
            chat_id=chat_id or self.group_chat_id,
            text=text,
            parse_mode="Markdown",
            message_thread_id=self._thread_id(thread) if thread else None,
            reply_markup=self._keyboard(buttons),
        )
        return str(result["message_id"])

    def edit_message_text(self, chat_id: str, message_id: str, text: str,
                          buttons: Optional[list[Button]] = None) -> None:
        try:
            self._call(
                "editMessageText",
                chat_id=chat_id or self.group_chat_id,
                message_id=int(message_id),
                text=text,
                parse_mode="Markdown",
                reply_markup=self._keyboard(buttons),
            )
        except RuntimeError as e:
            # "message is not modified" is benign — ignore it.
            if "not modified" not in str(e):
                raise

    def send_channel(self, text: str) -> str:
        if not self.channel_id:
            return ""
        result = self._call("sendMessage", chat_id=self.channel_id, text=text,
                            parse_mode="Markdown")
        return str(result["message_id"])

    # -- helpers used by the runner --------------------------------------- #
    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout,
                          allowed_updates=["message", "callback_query"])

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)
        except RuntimeError:
            pass

    def get_me(self) -> dict:
        return self._call("getMe")

    def close(self) -> None:
        self._http.close()
