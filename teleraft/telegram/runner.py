"""Long-poll runner — inbound Telegram → gateway (DESIGN.md §3.1, inbound).

Pulls updates for the workspace bot via ``getUpdates`` and normalizes each into the
gateway's ``Update`` / ``Callback`` types:

  * group messages  → task creation / claims (mentions, ``/task``)
  * inline buttons  → Claim / Approve / Reject / Adjust
  * Reject          → a two-step forced-reply flow that captures the reason text

The pure normalizers (``normalize_message`` / ``normalize_callback``) are unit-tested;
``run_forever`` is the thin network loop around them.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .gateway import Callback, Gateway, Update

log = logging.getLogger("teleraft.runner")


class LiveRunner:
    def __init__(self, client, gateway: Gateway, config, scheduler=None):
        self.client = client
        self.gateway = gateway
        self.config = config
        self.scheduler = scheduler
        self.group_chat_id = str(config.group_chat_id)
        self.thread_to_topic = config.thread_to_topic()
        self.username_to_agent = config.username_to_agent()
        self._offset = 0
        # user_id → callback data of a pending Reject awaiting a reason reply
        self._pending_reject: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Normalizers (pure, testable)
    # ------------------------------------------------------------------ #
    @property
    def agent_names(self) -> set[str]:
        """Read live from the registry — agents can be added while the runner is up."""
        return set(self.gateway.registry.names())

    def normalize_message(self, msg: dict) -> Optional[Update]:
        frm = msg.get("from", {})
        if frm.get("is_bot"):
            return None
        chat_id = str(msg.get("chat", {}).get("id"))
        if chat_id != self.group_chat_id:
            # Worth saying out loud: a mismatch here means every message is ignored.
            log.warning(
                "ignoring message from chat %s — configured group_chat_id is %s. "
                "If this is your workspace group, fix group_chat_id.",
                chat_id, self.group_chat_id,
            )
            return None
        text = msg.get("text", "") or ""
        user_id = str(frm.get("id", ""))
        handle = frm.get("username") or frm.get("first_name") or "human"
        thread_id = str(msg.get("message_thread_id", "") or "")
        topic = self.thread_to_topic.get(thread_id, "general")
        mentions = self._extract_agent_mentions(text, msg.get("entities", []))
        as_task = text.strip().startswith("/task") or bool(mentions)
        return Update(
            text=text, user_id=user_id, user_handle=handle, topic=topic,
            is_bot=False, as_task=as_task, mentions=mentions,
        )

    def normalize_callback(self, cb: dict) -> Callback:
        return Callback(data=cb.get("data", ""), user_id=str(cb.get("from", {}).get("id", "")))

    def _extract_agent_mentions(self, text: str, entities: list[dict]) -> list[str]:
        found: list[str] = []
        for ent in entities or []:
            if ent.get("type") == "mention":
                off, length = ent["offset"], ent["length"]
                username = text[off:off + length].lstrip("@").lower()
                agent = self.username_to_agent.get(username)
                if agent:
                    found.append(agent)
            elif ent.get("type") == "text_mention":
                # a bot without a public username: fall back to matching its display name
                name = ent.get("user", {}).get("first_name", "")
                if name in self.agent_names:
                    found.append(name)
        # Also accept a plain "@Name" that matches an agent's display name directly.
        # This is what makes **puppet mode** work: with one workspace bot and no
        # per-agent bot accounts, `@Quinn` is not a real Telegram username, so display
        # name matching is the only route from a mention to an agent (§12, decided).
        for name in self.agent_names:
            if f"@{name.lower()}" in text.lower() and name not in found:
                found.append(name)
        return found

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def process_update(self, upd: dict) -> None:
        if "message" in upd:
            self._on_message(upd["message"])
        elif "callback_query" in upd:
            self._on_callback(upd["callback_query"])

    def _on_message(self, msg: dict) -> None:
        frm = msg.get("from", {})
        user_id = str(frm.get("id", ""))
        # If this user owes us a rejection reason, consume this message as that reason.
        if user_id in self._pending_reject:
            data = self._pending_reject.pop(user_id)
            self.gateway.handle_callback(
                Callback(data=data, user_id=user_id, reason=msg.get("text", ""))
            )
            return
        update = self.normalize_message(msg)
        if update is None:
            return
        log.info("message in %s from %s: %r (mentions=%s)",
                 update.topic, update.user_handle, update.text[:80], update.mentions)
        try:
            self.gateway.handle_message(update)
        except Exception:  # keep the loop alive; one bad message shouldn't stop the bot
            log.exception("error handling message")

    def _on_callback(self, cb: dict) -> None:
        self.client.answer_callback(cb.get("id", ""))
        data = cb.get("data", "")
        user_id = str(cb.get("from", {}).get("id", ""))
        action = data.split("|", 1)[0]
        if action == "reject":
            # Two-step: enforce human-only up front, then ask for the reason.
            if user_id not in self.config.human_ids:
                self.client.send_channel(f"⛔ blocked non-human reject by {user_id}")
                return
            self._pending_reject[user_id] = data
            self.client.send_message(
                self.group_chat_id,
                "✏️ Reply with the reason for rejection (it feeds the agent's memory).",
            )
            return
        try:
            self.gateway.handle_callback(self.normalize_callback(cb))
        except Exception:
            log.exception("error handling callback")

    # ------------------------------------------------------------------ #
    # Network loop
    # ------------------------------------------------------------------ #
    def run_forever(self) -> None:  # pragma: no cover - requires live Telegram
        me = self.client.get_me()
        self.gateway.bot_username = me.get("username", "")
        agents = sorted(self.agent_names)
        log.info("TeleRaft runner online as @%s", me.get("username"))
        log.info("agents loaded (%d): %s", len(agents), ", ".join(agents) or "NONE")
        if not agents:
            log.warning("no agents loaded — check `agents_dir` in teleraft.toml; "
                        "@mentions cannot resolve and every task will stay unclaimed")
        log.info("topics mapped: %s", self.thread_to_topic or "none (all → 'general')")
        log.info("polling…")
        while True:
            try:
                updates = self.client.get_updates(self._offset, self.config.poll_timeout)
            except Exception:
                log.exception("getUpdates failed; backing off")
                time.sleep(3)
                continue
            for upd in updates:
                self._offset = max(self._offset, upd["update_id"] + 1)
                self.process_update(upd)

            # Programs tick on the same loop: getUpdates already blocks for the poll
            # timeout, so this costs nothing and needs no second thread.
            if self.scheduler is not None:
                report = self.scheduler.tick()
                if report.fired or report.failed:
                    log.info("scheduler: %s", report.summary())
