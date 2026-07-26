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

import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("teleraft.telegram")

from .client import Button


# Telegram usernames: 5–32 chars, must start with a letter, then letters/digits/
# underscores only. Hyphens and dots are NOT allowed — a handle containing them can
# never resolve, so we can say so without asking Telegram.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def chat_ref_problem(ref: str) -> Optional[str]:
    """Structural problem with a chat reference, or None if it *could* resolve.

    Catches the misconfiguration class that no permission change can fix.
    """
    ref = (ref or "").strip()
    if not ref:
        return "is empty"
    if ref.lstrip("-").isdigit():
        return None                                  # numeric id: plausible
    if not ref.startswith("@"):
        return (f"{ref!r} is neither a numeric chat id (-100…) nor an @handle, "
                "so it can never resolve")
    handle = ref[1:]
    if not _USERNAME_RE.match(handle):
        bad = sorted({c for c in handle if not (c.isalnum() or c == "_")})
        detail = (f"contains {', '.join(repr(c) for c in bad)}" if bad
                  else f"is {len(handle)} characters")
        return (
            f"{ref!r} is not a valid Telegram username — it {detail}. "
            "Usernames are 5–32 characters, start with a letter, and may contain only "
            "letters, digits and underscores (no hyphens or dots). "
            "Private channels have no @handle at all: use the numeric -100… id."
        )
    return None


def _explain(method: str, params: dict, data: dict, hint_key: str = "") -> str:
    """Turn a terse Bot API error into one that names the likely misconfiguration.

    `chat not found` in particular is almost never a Telegram problem — it means the
    chat id being sent is not the one the bot belongs to. `hint_key` names the config
    key that supplied the id, so the advice points at the right setting.
    """
    description = str(data.get("description", ""))
    base = f"Telegram API {method} failed: {data}"
    lowered = description.lower()

    if "chat not found" in lowered:
        chat_id = params.get("chat_id")
        structural = chat_ref_problem(str(chat_id or ""))
        hint = f"\n  → chat_id={chat_id!r} is not a chat this bot can post to."
        if structural:
            hint += f"\n    {structural}"
            return base + hint
        if hint_key == "channel_id":
            hint += (
                "\n    It came from `channel_id`. Check:\n"
                "    1. For a public channel, the @handle matches its t.me/<handle>.\n"
                "    2. For a private channel, use the numeric -100… id instead.\n"
                "    3. The bot is an administrator of the channel with Post Messages."
            )
        else:
            hint += (
                "\n    It came from `group_chat_id`. Check:\n"
                "    1. It is the numeric supergroup id (-100…), read from getUpdates\n"
                "       (TELEGRAM_SETUP.md §6.2).\n"
                "    2. The bot has been added to that group."
            )
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


@dataclass
class PreflightReport:
    """Startup validation split by severity: fatal blocks, warnings degrade."""

    fatal: list[str] = None          # type: ignore[assignment]
    warnings: list[str] = None       # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.fatal = [] if self.fatal is None else self.fatal
        self.warnings = [] if self.warnings is None else self.warnings

    @property
    def ok(self) -> bool:
        return not self.fatal


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
    def _call(self, method: str, _hint_key: str = "", **params) -> dict:
        url = f"{self.API}/bot{self.token}/{method}"
        resp = self._http.post(url, json={k: v for k, v in params.items() if v is not None})
        data = resp.json()

        # Formatting must never cost us the message. Agent output legitimately contains
        # characters that look like markup ("[Quinn]", "hyp_a3b4"), so if the parser
        # rejects the text, resend it verbatim as plain text and log the miss.
        if not data.get("ok", False) and "can't parse entities" in \
                str(data.get("description", "")).lower() and params.get("parse_mode"):
            log.warning("%s: %s — resending as plain text",
                        method, data.get("description"))
            retry = {k: v for k, v in params.items() if k != "parse_mode"}
            resp = self._http.post(url, json={k: v for k, v in retry.items() if v is not None})
            data = resp.json()

        if not data.get("ok", False):
            hint_key = _hint_key or (
                "channel_id" if str(params.get("chat_id")) == str(self.channel_id)
                and self.channel_id else "group_chat_id"
            )
            raise RuntimeError(_explain(method, params, data, hint_key))
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
            parse_mode="HTML",
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
                parse_mode="HTML",
                reply_markup=self._keyboard(buttons),
            )
        except RuntimeError as e:
            # "message is not modified" is benign — ignore it.
            if "not modified" not in str(e):
                raise

    def send_channel(self, text: str) -> str:
        # Empty when unconfigured *or* disabled by preflight — the activity feed is
        # optional, so a bad channel must never break the work happening in the group.
        if not self.channel_id:
            return ""
        try:
            result = self._call("sendMessage", _hint_key="channel_id",
                                chat_id=self.channel_id, text=text,
                                parse_mode="HTML")
        except RuntimeError as e:
            # Degrade, don't crash: work in the group must not fail because the
            # optional activity feed is misconfigured. Logged once, then disabled.
            log.warning("activity feed disabled — %s", e)
            self.disable_channel()
            return ""
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

    def preflight(self) -> "PreflightReport":
        """Validate the live configuration before polling starts.

        Severity matters: the group is load-bearing, so a problem there is **fatal**.
        The broadcast channel is an optional convenience (TELEGRAM_SETUP.md §5), so a
        problem there is a **warning** — it disables the activity feed and everything
        else keeps working. Blocking startup over an optional feature would be the
        wrong trade.
        """
        report = PreflightReport()
        try:
            me = self.get_me()
        except RuntimeError as e:
            report.fatal.append(f"bot token rejected: {e}")
            return report
        self._me = me

        # --- group: fatal ---------------------------------------------------- #
        structural = chat_ref_problem(self.group_chat_id)
        if not self.group_chat_id:
            report.fatal.append(
                "group_chat_id is not set — the bot has nowhere to post "
                "(TELERAFT_GROUP_CHAT_ID or [telegram].group_chat_id)"
            )
        elif structural:
            report.fatal.append(f"group_chat_id {structural}")
        else:
            try:
                chat = self.get_chat(self.group_chat_id)
                if chat.get("type") not in ("group", "supergroup"):
                    report.fatal.append(
                        f"group_chat_id {self.group_chat_id!r} is a "
                        f"{chat.get('type')!r}, expected a group/supergroup"
                    )
                elif not chat.get("is_forum") and self.topic_threads:
                    report.warnings.append(
                        "topic_threads is configured but the group does not have Topics "
                        "enabled (Group → Edit → Topics) — messages will ignore threads"
                    )
            except RuntimeError as e:
                report.fatal.append(f"group chat unreachable: {e}")

        # --- broadcast channel: optional, so warn and degrade ----------------- #
        if self.channel_id:
            structural = chat_ref_problem(self.channel_id)
            if structural:
                report.warnings.append(f"channel_id {structural}")
                self.disable_channel()
            else:
                try:
                    self.get_chat(self.channel_id)
                except RuntimeError as e:
                    report.warnings.append(f"broadcast channel unreachable: {e}")
                    self.disable_channel()
        return report

    def disable_channel(self) -> None:
        """Stop attempting the activity feed after preflight found it unusable."""
        self.channel_id = ""

    def close(self) -> None:
        self._http.close()
