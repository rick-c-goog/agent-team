"""Configuration for a live TeleRaft deployment (see docs/TELEGRAM_SETUP.md).

Config is read from a TOML file (default ``teleraft.toml``) and/or environment
variables, with env vars taking precedence so secrets can stay out of the file.

Nothing here is needed for the offline demo/tests — it only powers the live Bot API
runner.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    # --- workspace bot (always present: commands, cards, board, broadcast) ------
    workspace_bot_token: str = ""
    # --- Telegram ids ----------------------------------------------------------
    group_chat_id: str = ""          # the supergroup, e.g. "-1001234567890"
    channel_id: str = ""             # broadcast channel id or "@handle"
    human_ids: set[str] = field(default_factory=set)   # numeric Telegram user ids
    # --- forum topics ↔ thread ids --------------------------------------------
    # label shown in the app  →  message_thread_id of that forum topic
    topic_threads: dict[str, str] = field(default_factory=dict)
    # --- optional full multi-bot topology -------------------------------------
    # agent name → its bot username ("@Cole_TR_Bot") for @mention routing
    agent_usernames: dict[str, str] = field(default_factory=dict)
    # agent name → its own bot token (only if you run one bot per agent)
    agent_bot_tokens: dict[str, str] = field(default_factory=dict)
    # --- runtime / storage -----------------------------------------------------
    agents_dir: str = "agents"
    db_path: str = "teleraft_data/teleraft.db"
    poll_timeout: int = 30
    # --- LLM runtime -----------------------------------------------------------
    runtime_engine: str = "mock"     # "mock" | "claude"
    model: str = "claude-fable-5"
    # --- knowledge base (§4.1) -------------------------------------------------
    knowledge_root: str = "."        # allow-listed root for `file` sources
    sync_knowledge_on_start: bool = True
    # --- onboarding host (§3.3) ------------------------------------------------
    onboarding_host: str = "hermes"  # "hermes" | "openclaw" | "mock"
    host_gateway_url: str = ""       # defaults per host adapter
    host_api_key: str = ""

    # ------------------------------------------------------------------ #
    def validate_for_live(self) -> None:
        missing = [
            n for n, v in [
                ("workspace_bot_token", self.workspace_bot_token),
                ("group_chat_id", self.group_chat_id),
            ] if not v
        ]
        if missing:
            raise ValueError(
                "missing required config for a live run: " + ", ".join(missing)
                + " (see docs/TELEGRAM_SETUP.md)"
            )
        if not self.human_ids:
            raise ValueError(
                "human_ids is empty: no one could approve gates. Add your numeric "
                "Telegram user id (see docs/TELEGRAM_SETUP.md §5)."
            )

    # thread_id → label, for routing inbound forum-topic messages back to a topic.
    def thread_to_topic(self) -> dict[str, str]:
        return {v: k for k, v in self.topic_threads.items()}

    # "@username" → agent name, for @mention routing.
    def username_to_agent(self) -> dict[str, str]:
        return {v.lstrip("@").lower(): k for k, v in self.agent_usernames.items()}


def _split_ids(raw: str) -> set[str]:
    return {p.strip() for p in raw.replace(",", " ").split() if p.strip()}


def load_config(path: str | os.PathLike = "teleraft.toml") -> Config:
    """Load config from a TOML file (if present) then overlay environment variables."""
    cfg = Config()

    p = Path(path)
    if p.exists():
        data = tomllib.loads(p.read_text())
        tg = data.get("telegram", {})
        cfg.workspace_bot_token = tg.get("workspace_bot_token", cfg.workspace_bot_token)
        cfg.group_chat_id = str(tg.get("group_chat_id", cfg.group_chat_id))
        cfg.channel_id = str(tg.get("channel_id", cfg.channel_id))
        cfg.human_ids = {str(x) for x in tg.get("human_ids", [])} or cfg.human_ids
        cfg.topic_threads = {k: str(v) for k, v in tg.get("topic_threads", {}).items()}
        cfg.agent_usernames = dict(tg.get("agent_usernames", {}))
        cfg.agent_bot_tokens = dict(tg.get("agent_bot_tokens", {}))
        cfg.poll_timeout = int(tg.get("poll_timeout", cfg.poll_timeout))

        app = data.get("app", {})
        cfg.agents_dir = app.get("agents_dir", cfg.agents_dir)
        cfg.db_path = app.get("db_path", cfg.db_path)
        cfg.runtime_engine = app.get("runtime_engine", cfg.runtime_engine)
        cfg.model = app.get("model", cfg.model)

        kb = data.get("knowledge", {})
        cfg.knowledge_root = kb.get("root", cfg.knowledge_root)
        cfg.sync_knowledge_on_start = bool(kb.get("sync_on_start", cfg.sync_knowledge_on_start))

        onb = data.get("onboarding", {})
        cfg.onboarding_host = onb.get("host", cfg.onboarding_host)
        cfg.host_gateway_url = onb.get("gateway_url", cfg.host_gateway_url)
        cfg.host_api_key = onb.get("api_key", cfg.host_api_key)

    # Environment overrides (never commit secrets to the TOML file).
    env = os.environ
    cfg.workspace_bot_token = env.get("TELERAFT_BOT_TOKEN", cfg.workspace_bot_token)
    cfg.group_chat_id = env.get("TELERAFT_GROUP_CHAT_ID", cfg.group_chat_id)
    cfg.channel_id = env.get("TELERAFT_CHANNEL_ID", cfg.channel_id)
    if env.get("TELERAFT_HUMAN_IDS"):
        cfg.human_ids = _split_ids(env["TELERAFT_HUMAN_IDS"])
    cfg.agents_dir = env.get("TELERAFT_AGENTS_DIR", cfg.agents_dir)
    cfg.db_path = env.get("TELERAFT_DB_PATH", cfg.db_path)
    cfg.runtime_engine = env.get("TELERAFT_RUNTIME", cfg.runtime_engine)
    cfg.model = env.get("TELERAFT_MODEL", cfg.model)
    cfg.knowledge_root = env.get("TELERAFT_KNOWLEDGE_ROOT", cfg.knowledge_root)
    cfg.onboarding_host = env.get("TELERAFT_ONBOARDING_HOST", cfg.onboarding_host)
    cfg.host_gateway_url = env.get("TELERAFT_HOST_GATEWAY_URL", cfg.host_gateway_url)
    cfg.host_api_key = env.get("TELERAFT_HOST_API_KEY", cfg.host_api_key)

    return cfg
