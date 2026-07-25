"""Application wiring — assemble the whole system (DESIGN.md §3).

Builds Storage → Registry → Memory → Gateway → GraphEngine and connects the engine's
notify hook to the gateway (resolving the construction cycle: the gateway needs the
engine to run tasks, the engine needs the gateway's notify to post to Telegram).

The default build is fully offline: a MockTelegramClient and a MockRuntime per agent.
Swap ``runtime_for`` and the client to go live without touching the graph or gateway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .agents.registry import Registry, load_agents_from_dir
from .graph.engine import GraphEngine
from .knowledge.service import KnowledgeService
from .memory.service import MemoryService
from .runtime.base import Runtime
from .runtime.mock import MockRuntime
from .storage import Storage
from .tasks.service import TaskService
from .telegram.client import MockTelegramClient, TelegramClient
from .telegram.gateway import Gateway

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENTS_DIR = str(_REPO_ROOT / "agents")
DEFAULT_KNOWLEDGE_ROOT = str(_REPO_ROOT)


class App:
    def __init__(
        self,
        db_path: str = ":memory:",
        agents_dir: str = DEFAULT_AGENTS_DIR,
        human_ids: Optional[set[str]] = None,
        client: Optional[TelegramClient] = None,
        runtime_for: Optional[Callable[[str], Runtime]] = None,
        knowledge_root: str = DEFAULT_KNOWLEDGE_ROOT,
        sync_knowledge: bool = True,
    ):
        self.storage = Storage(db_path)
        self.registry = Registry(self.storage)
        agent_configs = load_agents_from_dir(agents_dir)
        self.registry.register_all(agent_configs)
        self.memory = MemoryService(self.storage)
        self.knowledge = KnowledgeService(self.storage, knowledge_root=knowledge_root)
        self.tasks = TaskService(self.storage)
        self.client = client or MockTelegramClient()
        self.human_ids = human_ids or {"rick"}

        # One runtime per agent. Default: a shared deterministic mock.
        shared_mock = MockRuntime()
        self._runtime_for = runtime_for or (lambda agent: shared_mock)

        self.gateway = Gateway(
            client=self.client,
            storage=self.storage,
            tasks=self.tasks,
            registry=self.registry,
            human_ids=self.human_ids,
        )
        self.engine = GraphEngine(
            storage=self.storage,
            registry=self.registry,
            memory=self.memory,
            runtime_for=self._runtime_for,
            notify=self.gateway.notify,
            knowledge=self.knowledge,
        )
        self.gateway.attach_engine(self.engine)
        self.gateway.attach_knowledge(self.knowledge)

        # Topics come from the agents that own them (plus the always-present catch-all).
        # A workspace with no agents yet has nothing but `general` — which is exactly
        # what lets the onboarding agent provision the real topics (§3.3.3).
        self.storage.upsert_topic("general")
        for cfg in agent_configs:
            for owned in (cfg.goals or {}).get("owns", []):
                if isinstance(owned, str) and owned.startswith("#"):
                    self.storage.upsert_topic(owned)

        # Register each agent's declared knowledge sources and do the first ingest.
        self.register_agent_knowledge(agent_configs, sync=sync_knowledge)

    def register_agent_knowledge(self, agent_configs, sync: bool = True) -> list:
        """Register `knowledge:` entries from agent YAML; returns sync reports (§4.1)."""
        reports = []
        for cfg in agent_configs:
            for spec in cfg.knowledge:
                source_id = self.knowledge.add_source(
                    agent=cfg.name,
                    type_=spec.type,
                    uri=spec.uri,
                    scope=spec.scope,
                    options=spec.options,
                    refresh_cron=spec.refresh or None,
                    created_by="agent-config",
                )
                if sync:
                    reports.append(self.knowledge.sync_source(source_id))
        return reports

    def close(self) -> None:
        self.storage.close()
