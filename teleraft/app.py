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
from .pipeline import PipelineEngine
from .programs import Program, Scheduler
from .quant.hypothesis import HypothesisRegistry
from .runtime.base import Runtime
from .runtime.mock import MockRuntime
from .runtime.quant import QuantRuntime
from .storage import Storage
from .tasks.service import TaskService
from .telegram.client import MockTelegramClient, TelegramClient
from .telegram.gateway import Gateway

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENTS_DIR = str(_REPO_ROOT / "agents")
DEFAULT_KNOWLEDGE_ROOT = str(_REPO_ROOT)

# Sunday 04:00 — weekly and unattended (DESIGN.md §12, decided).
MEMORY_CONSOLIDATION_CRON = "0 4 * * 0"


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
        group_chat_id: str = "",
        default_engine: str = "mock",
        model: str = "",
    ):
        # Engine for agents that declare none of their own.
        self.default_engine = (default_engine or "mock").strip().lower()
        self.model = model
        self._agents_dir = agents_dir      # replay builds a scratch app from this
        self.storage = Storage(db_path)
        self.registry = Registry(self.storage)
        agent_configs = load_agents_from_dir(agents_dir)
        self.registry.register_all(agent_configs)
        self.memory = MemoryService(self.storage)
        self.knowledge = KnowledgeService(self.storage, knowledge_root=knowledge_root)
        self.tasks = TaskService(self.storage)
        self.client = client or MockTelegramClient()
        self.human_ids = human_ids or {"rick"}

        # Quant research services (used when an agent declares `engine: quant`).
        self.hypotheses = HypothesisRegistry(self.storage)

        # One runtime per agent, chosen by the agent's declared engine. Default is the
        # deterministic mock; `engine: quant` gets the backtest-driven QuantRuntime.
        self._runtime_for = runtime_for or self._default_runtime_for(agent_configs)

        self.gateway = Gateway(
            client=self.client,
            storage=self.storage,
            tasks=self.tasks,
            registry=self.registry,
            human_ids=self.human_ids,
            group_chat_id=group_chat_id,
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
        self.gateway.attach_hypotheses(self.hypotheses)

        # Pipelines: DAGs of gated runs (§5.7), sharing the gateway's notify hook so a
        # pipeline is as visible in the workspace as a single task.
        self.pipelines = PipelineEngine(self.storage, notify=self.gateway.notify)
        self.gateway.attach_pipelines(self.pipelines)

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

        # Programs: the scheduler that actually fires heartbeats and maintenance work.
        self.scheduler = Scheduler(self.storage)
        self.register_programs(agent_configs)

    # ------------------------------------------------------------------ #
    def register_programs(self, agent_configs) -> None:
        """Register the platform's own Programs plus each agent's heartbeats (§5.6)."""
        # Weekly, unattended memory consolidation (DESIGN.md §12, decided). Sunday 04:00
        # is chosen to sit outside the weekday heartbeats so a long consolidation cannot
        # collide with real work.
        self.scheduler.register(Program(
            name="memory-consolidation",
            cron=MEMORY_CONSOLIDATION_CRON,
            body=self.consolidate_memories,
            agent="platform",
        ))

        for cfg in agent_configs:
            for i, hb in enumerate(cfg.heartbeats):
                if not hb.cron:
                    continue
                self.scheduler.register(Program(
                    name=f"heartbeat:{cfg.name}:{i}",
                    cron=hb.cron,
                    agent=cfg.name,
                    body=self._heartbeat_body(cfg.name, hb.prompt),
                ))

    def _heartbeat_body(self, agent: str, prompt: str):
        """A heartbeat opens a normal task, so it is claimed, gated and audited like
        any other work — autonomy must not mean a private code path (§5.6)."""
        def run():
            topic = next(
                (o for o in (self.registry.goals(agent) or {}).get("owns", [])
                 if isinstance(o, str) and o.startswith("#")),
                "general",
            )
            task_id = self.tasks.create(topic=topic, title=prompt, body="",
                                        created_by=f"heartbeat:{agent}")
            self.gateway._post_task_card(task_id, claimable=False)
            return self.gateway._run_task(task_id, agent)
        return run

    def metrics(self, since=None):
        """Process metrics from the durable record (§5.9)."""
        from .evaluation import collect
        return collect(self.storage, since)

    def consolidate_memories(self) -> list[dict]:
        reports = self.memory.consolidate_all(self.registry.names())
        touched = [r for r in reports if r["merged"] or r["dropped"]]
        if touched and self.gateway.client is not None:
            summary = " · ".join(
                f"{r['agent']}: {r['before']}→{r['after']}" for r in touched
            )
            self.gateway.client.send_channel(f"🧹 Memory consolidated — {summary}")
        return reports

    def _default_runtime_for(self, agent_configs):
        """Map each agent to a runtime by its declared `runtime.engine`.

        A workspace can mix engines: prose agents on Claude, quant agents on the
        deterministic backtest runtime, all in the same channels and the same loop
        (DESIGN.md §4 runtime model). An agent that declares no engine falls back to
        `default_engine`.

        Engines are built lazily and shared, so a workspace with no Claude agents never
        constructs an Anthropic client and never needs an API key.
        """
        engines = {
            cfg.name: (cfg.runtime_engine or "").strip().lower()
            for cfg in agent_configs
        }
        cache: dict[str, Runtime] = {}

        def build(engine: str) -> Runtime:
            if engine in cache:
                return cache[engine]
            if engine == "quant":
                runtime: Runtime = QuantRuntime(self.hypotheses)
            elif engine in ("claude", "claude-agent-sdk", "anthropic"):
                from .runtime.anthropic_runtime import AnthropicRuntime
                runtime = AnthropicRuntime(model=self.model) if self.model \
                    else AnthropicRuntime()
            else:
                runtime = MockRuntime()
            cache[engine] = runtime
            return runtime

        def pick(agent: str) -> Runtime:
            return build(engines.get(agent) or self.default_engine)

        return pick

    def engines_in_use(self, agent_configs) -> dict[str, str]:
        """agent → resolved engine name, for startup logging and preflight."""
        return {
            cfg.name: (cfg.runtime_engine or "").strip().lower() or self.default_engine
            for cfg in agent_configs
        }

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
