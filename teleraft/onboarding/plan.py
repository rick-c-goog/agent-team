"""Workspace plan: the reviewable artifact onboarding produces (DESIGN.md §3.3.4).

The interview compiles to a declarative plan that a human approves *before* anything is
created. Apply is diff-based and idempotent, so the same plan can be re-run to resume a
half-finished setup or to add a sixth agent months later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import yaml

from .interview import PILLARS, Answers


@dataclass
class PlannedSource:
    type: str
    uri: str
    scope: str = "agent"


@dataclass
class PlannedAgent:
    name: str
    pillar: str
    role: str = "member"
    soul: str = ""
    owns: list[str] = field(default_factory=list)
    escalate_when: list[str] = field(default_factory=list)
    heartbeat: str = ""
    heartbeat_prompt: str = ""
    knowledge: list[PlannedSource] = field(default_factory=list)


@dataclass
class WorkspacePlan:
    version: int = 1
    business: str = ""
    topics: list[str] = field(default_factory=list)
    agents: list[PlannedAgent] = field(default_factory=list)
    human_ids: list[str] = field(default_factory=list)

    # -- acceptance criteria the Tester role checks after apply (§3.3.2) ---- #
    def criteria(self) -> list[str]:
        return [
            "Every planned topic exists in the workspace",
            "Every planned agent is registered with a soul",
            "At least two agents exist so no agent grades its own work",
            "Every agent has at least one knowledge source, or none were provided",
            "Every planned heartbeat is registered on the host scheduler",
            "At least one human can approve work",
        ]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "business": self.business,
            "topics": list(self.topics),
            "human_ids": list(self.human_ids),
            "agents": [asdict(a) for a in self.agents],
        }

    def summary(self) -> str:
        n_sources = sum(len(a.knowledge) for a in self.agents)
        n_beats = sum(1 for a in self.agents if a.heartbeat)
        return (f"{len(self.agents)} agents, {len(self.topics)} topics, "
                f"{n_sources} knowledge sources, {n_beats} heartbeats")


def _source_type(uri: str) -> str:
    lowered = uri.lower()
    if lowered.startswith("drive://") or "drive.google.com" in lowered:
        return "gdrive"
    if lowered.startswith(("http://", "https://")):
        return "web"
    return "file"


def compile_plan(answers: Answers) -> WorkspacePlan:
    """Deterministically turn interview answers into a workspace plan (the Planner role)."""
    pillars = answers.pillars()
    # A one-agent team cannot satisfy "no agent grades its own work", so the planner
    # always pairs a solo pick with the delivery reviewer (§3.3.2 Tester check).
    if len(pillars) < 2:
        pillars = pillars + [p for p in ("delivery", "content") if p not in pillars][:1]

    escalations = answers.escalations()
    uris = answers.knowledge_uris()

    agents: list[PlannedAgent] = []
    for pillar in pillars:
        spec = PILLARS[pillar]
        cron = answers.cron_for(pillar)
        agents.append(
            PlannedAgent(
                name=spec["agent"],
                pillar=pillar,
                role="admin" if pillar == "finance" else "member",
                soul=spec["soul"],
                owns=list(spec["owns"]),
                escalate_when=list(escalations),
                heartbeat=cron if spec["heartbeat_prompt"] else "",
                heartbeat_prompt=spec["heartbeat_prompt"],
                knowledge=[],
            )
        )

    # Sources named in the interview are shared with the whole team by default — the
    # human can narrow scope later with `/kb`. They attach to the first agent as owner.
    if agents:
        agents[0].knowledge = [
            PlannedSource(type=_source_type(u), uri=u, scope="team") for u in uris
        ]

    topics = [PILLARS[p]["topic"] for p in pillars] + ["# admin"]
    return WorkspacePlan(
        business=answers.get("business", "").strip(),
        topics=topics,
        agents=agents,
        human_ids=answers.human_ids(),
    )


def plan_to_yaml(plan: WorkspacePlan) -> str:
    return yaml.safe_dump(plan.to_dict(), sort_keys=False, allow_unicode=True)


def plan_from_yaml(text: str) -> WorkspacePlan:
    data = yaml.safe_load(text) or {}
    agents = [
        PlannedAgent(
            name=a["name"],
            pillar=a.get("pillar", ""),
            role=a.get("role", "member"),
            soul=a.get("soul", ""),
            owns=list(a.get("owns", [])),
            escalate_when=list(a.get("escalate_when", [])),
            heartbeat=a.get("heartbeat", ""),
            heartbeat_prompt=a.get("heartbeat_prompt", ""),
            knowledge=[PlannedSource(**s) for s in a.get("knowledge", [])],
        )
        for a in data.get("agents", [])
    ]
    return WorkspacePlan(
        version=int(data.get("version", 1)),
        business=data.get("business", ""),
        topics=list(data.get("topics", [])),
        agents=agents,
        human_ids=[str(h) for h in data.get("human_ids", [])],
    )
