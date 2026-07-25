"""Agent registry — the five elements of every agent (DESIGN.md §4).

Name, Soul, Memory, Goals, Heartbeat + a runtime binding and a role. Agents are loaded
from YAML files and persisted into storage so identity (name, soul version, goals,
memberships) survives restarts and runtime swaps.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class Heartbeat:
    cron: str
    prompt: str


@dataclass
class KnowledgeSpec:
    """One entry of an agent's `knowledge:` block (DESIGN.md §4.1)."""

    type: str                            # web | gdrive | file | upload
    uri: str
    scope: str = "agent"                 # agent | team
    refresh: str = ""                    # cron expression, run by the host scheduler
    options: dict = field(default_factory=dict)   # crawl, recursive, …


@dataclass
class AgentConfig:
    name: str
    role: str = "member"                 # member | admin
    soul_md: str = ""
    goals: dict = field(default_factory=dict)
    heartbeats: list[Heartbeat] = field(default_factory=list)
    knowledge: list[KnowledgeSpec] = field(default_factory=list)
    runtime_engine: str = "mock"
    computer: str = "local"
    model: str = ""


def load_agents_from_dir(directory: str) -> list[AgentConfig]:
    """Load every ``*.yaml`` agent spec from a directory; souls are sibling files."""
    base = Path(directory)
    configs: list[AgentConfig] = []
    for path in sorted(base.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        soul_ref = data.get("soul", "")
        soul_md = ""
        if soul_ref:
            soul_path = base / soul_ref
            if soul_path.exists():
                soul_md = soul_path.read_text()
        hbs = [Heartbeat(**hb) for hb in data.get("heartbeat", [])]
        kb: list[KnowledgeSpec] = []
        for entry in data.get("knowledge", []) or []:
            known = {"type", "uri", "scope", "refresh"}
            kb.append(
                KnowledgeSpec(
                    type=entry["type"],
                    uri=entry["uri"],
                    scope=entry.get("scope", "agent"),
                    refresh=entry.get("refresh", ""),
                    # everything else (crawl, recursive, max_pages…) is fetcher options
                    options={k: v for k, v in entry.items() if k not in known},
                )
            )
        rt = data.get("runtime", {}) or {}
        configs.append(
            AgentConfig(
                name=data["name"],
                role=data.get("role", "member"),
                soul_md=soul_md,
                goals=data.get("goals", {}) or {},
                heartbeats=hbs,
                knowledge=kb,
                runtime_engine=rt.get("engine", "mock"),
                computer=rt.get("computer", "local"),
                model=rt.get("model", ""),
            )
        )
    return configs


class Registry:
    """Live view over agents, backed by storage. Implements the engine's Registry port."""

    def __init__(self, storage):
        self.storage = storage
        self._configs: dict[str, AgentConfig] = {}
        self._heartbeats: dict[str, list[Heartbeat]] = {}

    # -- registration ------------------------------------------------------ #
    def register(self, cfg: AgentConfig) -> None:
        self.storage.upsert_agent(
            name=cfg.name,
            role=cfg.role,
            goals_json=json.dumps(cfg.goals),
            runtime_engine=cfg.runtime_engine,
            computer=cfg.computer,
            soul_md=cfg.soul_md,
        )
        self._configs[cfg.name] = cfg
        self._heartbeats[cfg.name] = cfg.heartbeats

    def register_all(self, cfgs: list[AgentConfig]) -> None:
        for c in cfgs:
            self.register(c)

    # -- engine Registry port --------------------------------------------- #
    def soul(self, agent: str) -> str:
        return self.storage.current_soul(agent)

    def goals(self, agent: str) -> dict:
        row = self.storage.get_agent(agent)
        return json.loads(row["goals_json"]) if row and row["goals_json"] else {}

    def pick_tester(self, exclude: str) -> str:
        """Choose a reviewer that is NOT the builder (no self-grading).

        Policy v1 (DESIGN.md §12 open question): any other active agent, preferring one
        whose goals overlap the excluded agent's ownership; deterministic tie-break by
        name so runs are reproducible.
        """
        candidates = [
            r["name"] for r in self.storage.list_agents()
            if r["name"] != exclude and r["status"] == "active"
        ]
        if not candidates:
            raise RuntimeError(
                f"no eligible tester for {exclude!r}: a team needs at least two agents "
                "so no agent grades its own work"
            )
        return sorted(candidates)[0]

    # -- misc -------------------------------------------------------------- #
    def config(self, agent: str) -> Optional[AgentConfig]:
        return self._configs.get(agent)

    def heartbeats(self) -> dict[str, list[Heartbeat]]:
        return dict(self._heartbeats)

    def names(self) -> list[str]:
        return [r["name"] for r in self.storage.list_agents()]

    def role(self, agent: str) -> str:
        row = self.storage.get_agent(agent)
        return row["role"] if row else "member"
