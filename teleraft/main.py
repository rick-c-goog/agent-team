"""Live entrypoint:  python -m teleraft.main  (see docs/TELEGRAM_SETUP.md).

Loads config, assembles the App against the live Bot API client and the chosen LLM
runtime, then long-polls Telegram. Ctrl-C to stop; SQLite state persists so a restart
resumes in-flight runs from their last checkpoint.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .app import App
from .config import load_config
from .telegram.live_client import LiveTelegramClient
from .telegram.runner import LiveRunner


CLAUDE_ENGINES = {"claude", "claude-agent-sdk", "anthropic"}


def check_engine_prerequisites(engines: dict[str, str]) -> list[str]:
    """Fail fast on engines that cannot possibly run, before any task is claimed.

    Only checks what is actually used: a desk whose agents all declare `engine: quant`
    is fully deterministic and needs no API key at all.
    """
    problems: list[str] = []
    claude_agents = sorted(a for a, e in engines.items() if e in CLAUDE_ENGINES)
    if claude_agents and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append(
            f"agents {', '.join(claude_agents)} use the Claude engine but "
            "ANTHROPIC_API_KEY is not set. Export a valid key, or set "
            "`engine: quant` (deterministic, no key) / `engine: mock` in their "
            "agent YAML — see docs/QUANT_TEAM_TUTORIAL.md §10."
        )
    unknown = {e for e in engines.values()} - CLAUDE_ENGINES - {"quant", "mock"}
    if unknown:
        problems.append(f"unknown runtime engine(s) {sorted(unknown)} — "
                        "valid: quant, claude, mock")
    return problems


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

    # Validate at startup rather than on the first inbound message. Warnings degrade
    # an optional feature; only fatal problems stop the run.
    report = client.preflight()
    for warning in report.warnings:
        logging.warning("preflight: %s", warning)
    if not report.ok:
        for problem in report.fatal:
            logging.error("preflight: %s", problem)
        raise SystemExit(
            "Telegram configuration is not usable — fix the problems above "
            "(see docs/TELEGRAM_SETUP.md §6 and §16) and restart."
        )
    if report.warnings:
        logging.warning("starting with reduced functionality (see warnings above)")
    # No `runtime_for` override: each agent's own `runtime.engine` decides, with
    # cfg.runtime_engine as the fallback for agents that declare none. Overriding it
    # here would route a deterministic quant desk through Claude.
    app = App(
        db_path=cfg.db_path,
        agents_dir=cfg.agents_dir,
        human_ids=cfg.human_ids,
        client=client,
        knowledge_root=cfg.knowledge_root,
        sync_knowledge=cfg.sync_knowledge_on_start,
        group_chat_id=cfg.group_chat_id,
        default_engine=cfg.runtime_engine,
        model=cfg.model,
    )

    from .agents.registry import load_agents_from_dir
    engines = app.engines_in_use(load_agents_from_dir(cfg.agents_dir))
    for agent, engine in sorted(engines.items()):
        logging.info("agent %s → %s runtime", agent, engine)
    engine_problems = check_engine_prerequisites(engines)
    if engine_problems:
        for problem in engine_problems:
            logging.error("preflight: %s", problem)
        raise SystemExit("Runtime configuration is not usable — fix the above and restart.")
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
