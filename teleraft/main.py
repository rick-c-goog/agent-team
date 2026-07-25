"""Live entrypoint:  python -m teleraft.main  (see docs/TELEGRAM_SETUP.md).

Loads config, assembles the App against the live Bot API client and the chosen LLM
runtime, then long-polls Telegram. Ctrl-C to stop; SQLite state persists so a restart
resumes in-flight runs from their last checkpoint.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .app import App
from .config import load_config
from .telegram.live_client import LiveTelegramClient
from .telegram.runner import LiveRunner


def build_runtime_factory(cfg):
    if cfg.runtime_engine == "claude":
        from .runtime.anthropic_runtime import AnthropicRuntime
        rt = AnthropicRuntime(model=cfg.model)
        return lambda agent: rt
    from .runtime.mock import MockRuntime
    rt = MockRuntime()
    return lambda agent: rt


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config()
    cfg.validate_for_live()

    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    client = LiveTelegramClient(
        token=cfg.workspace_bot_token,
        group_chat_id=cfg.group_chat_id,
        channel_id=cfg.channel_id,
        topic_threads=cfg.topic_threads,
    )

    # Fail loudly at startup rather than on the first inbound message.
    problems = client.preflight()
    if problems:
        for problem in problems:
            logging.error("preflight: %s", problem)
        raise SystemExit(
            "Telegram configuration is not usable — fix the problems above "
            "(see docs/TELEGRAM_SETUP.md §6 and §16) and restart."
        )
    app = App(
        db_path=cfg.db_path,
        agents_dir=cfg.agents_dir,
        human_ids=cfg.human_ids,
        client=client,
        runtime_for=build_runtime_factory(cfg),
        knowledge_root=cfg.knowledge_root,
        sync_knowledge=cfg.sync_knowledge_on_start,
        group_chat_id=cfg.group_chat_id,
    )
    for report in app.knowledge.health():
        if report["status"] == "error":
            logging.warning("knowledge source %s (%s) is unhealthy: %s",
                            report["id"], report["uri"], report["error"])
    runner = LiveRunner(client, app.gateway, cfg)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        client.close()


if __name__ == "__main__":
    main()
