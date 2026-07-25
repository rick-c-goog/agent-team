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


def _explain(method: str, params: dict, data: dict) -> str:
    """Turn a terse Bot API error into one that names the likely misconfiguration.

    `chat not found` in particular is almost never a Telegram problem — it means the
    chat id being sent is not the one the bot belongs to.
    """
    description = str(data.get("description", ""))
    base = f"Telegram API {method} failed: {data}"
    lowered = description.lower()

    if "chat not found" in lowered:
        chat_id = params.get("chat_id")
        hint = (
            f"\n  → chat_id={chat_id!r} is not a chat this bot can post to. Check:\n"
            "    1. `group_chat_id` in teleraft.toml / TELERAFT_GROUP_CHAT_ID is the\n"
            "       supergroup id (a large negative number like -1001234567890),\n"
            "       not a name — read it from getUpdates (TELEGRAM_SETUP.md §6.2).\n"
            "    2. The bot has been added to that group.\n"
            "    3. For a channel, the bot is an admin with Post Messages rights."
        )
        if isinstance(chat_id, str) and not chat_id.lstrip("-").isdigit() \
                and not chat_id.startswith("@"):
            hint += (f"\n    Note: {chat_id!r} is neither a numeric id nor an @handle, "
                     "so it can never resolve.")
        return base + hint

    if "not enough rights" in lowered or "have no rights" in lowered:
        return base + ("\n  → the bot lacks permission in this chat. Promote it to admin "
                       "(Manage Topics / Post Messages) — TELEGRAM_SETUP.md §4.")
    if "message thread not found" in lowered:
        return base + ("\n  → a `topic_threads` id is stale. Re-read each topic's "
                       "message_thread_id from getUpdates (TELEGRAM_SETUP.md §6.2).")
    if "unauthorized" in lowered:
        return base + ("\n  → the bot token is wrong or has been revoked. "
                       "Reissue it in BotFather and update TELERAFT_BOT_TOKEN.")
    return base


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
            raise RuntimeError(_explain(method, params, data))
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

    def get_chat(self, chat_id: str) -> dict:
        return self._call("getChat", chat_id=chat_id)

    def preflight(self) -> list[str]:
        """Validate the live configuration before polling starts.

        Returns a list of human-readable problems (empty when healthy). Checking at
        startup turns a confusing per-message traceback into one clear message while
        the operator is still looking at the terminal.
        """
        problems: list[str] = []
        try:
            me = self.get_me()
        except RuntimeError as e:
            return [f"bot token rejected: {e}"]

        if not self.group_chat_id:
            problems.append("group_chat_id is not set — the bot has nowhere to post")
        else:
            try:
                chat = self.get_chat(self.group_chat_id)
                if chat.get("type") not in ("group", "supergroup"):
                    problems.append(
                        f"group_chat_id {self.group_chat_id!r} is a "
                        f"{chat.get('type')!r}, expected a group/supergroup"
                    )
                elif not chat.get("is_forum") and self.topic_threads:
                    problems.append(
                        "topic_threads is configured but the group does not have Topics "
                        "enabled (Group → Edit → Topics) — messages will ignore threads"
                    )
            except RuntimeError as e:
                problems.append(f"group chat unreachable: {e}")

        if self.channel_id:
            try:
                self.get_chat(self.channel_id)
            except RuntimeError as e:
                problems.append(f"broadcast channel unreachable: {e}")

        self._me = me
        return problems

    def close(self) -> None:
        self._http.close()
