"""The Onboarding Agent (DESIGN.md §3.3).

Runs the interview, compiles a plan, gets a **human** to approve it, applies it
idempotently, then verifies the result — i.e. it plays the same
Planner → Builder → Tester roles as any other unit of work, with the human gate on the
plan. Setup therefore gets the same audit trail and resumability as real work, and a
half-finished setup resumes instead of corrupting.

Guardrails (§3.3.5): admin role, never owner; it cannot approve its own plan; it never
creates bots (BotFather is human-only) — it validates what the human supplies and
reports what it could not do itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..agents.registry import AgentConfig, Heartbeat, KnowledgeSpec
from .host import OnboardingHost, MockHost
from .interview import INTERVIEW, Answers, Question
from .plan import WorkspacePlan, compile_plan, plan_from_yaml, plan_to_yaml


@dataclass
class ApplyReport:
    topics_created: list[str] = field(default_factory=list)
    agents_created: list[str] = field(default_factory=list)
    sources_added: list[str] = field(default_factory=list)
    heartbeats_scheduled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)      # idempotency: already present
    manual_steps: list[str] = field(default_factory=list)  # what a human must do
    verdict_passed: bool = False
    verdict_reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.agents_created)} agents, {len(self.topics_created)} topics, "
                f"{len(self.sources_added)} sources, "
                f"{len(self.heartbeats_scheduled)} heartbeats "
                f"({len(self.skipped)} already existed)")


class OnboardingAgent:
    """Stateful interview + provisioning driver for one workspace."""

    NAME = "Onboarding"

    def __init__(self, app, host: Optional[OnboardingHost] = None, user_ref: str = "human"):
        self.app = app
        self.host = host or MockHost()
        self.user_ref = user_ref
        self.answers = Answers()
        self.plan: Optional[WorkspacePlan] = None
        self.session_id = "onb_" + uuid.uuid4().hex[:10]
        host_name = getattr(self.host, "__class__").__name__.replace("Host", "").lower()
        self.app.storage.create_onboarding(self.session_id, host_name, user_ref)

    # ------------------------------------------------------------------ #
    # 1. Interview
    # ------------------------------------------------------------------ #
    def start(self) -> Question:
        self.host.send(self.user_ref,
                       "👋 I'm your onboarding agent. Six questions and your team is live.")
        question = INTERVIEW[0]
        self._ask(question)
        return question

    def _ask(self, question: Question) -> None:
        text = question.text + (f"\n_{question.help}_" if question.help else "")
        self.host.send(self.user_ref, text, buttons=question.options or None)

    def answer(self, value: str) -> Optional[Question]:
        """Record the answer to the current question; returns the next one, or None."""
        current = self.answers.next_question()
        if current is None:
            return None
        self.answers.set(current.key, value)
        self.app.storage.update_onboarding(
            self.session_id, answers_json=_json(self.answers.raw)
        )
        nxt = self.answers.next_question()
        if nxt is not None:
            self._ask(nxt)
            return nxt
        self._propose_plan()
        return None

    # ------------------------------------------------------------------ #
    # 2. Plan (Planner role) + human gate
    # ------------------------------------------------------------------ #
    def _propose_plan(self) -> WorkspacePlan:
        self.plan = compile_plan(self.answers)
        yaml_text = plan_to_yaml(self.plan)
        self.app.storage.update_onboarding(
            self.session_id, plan_yaml=yaml_text, status="awaiting_approval"
        )
        self.host.send(
            self.user_ref,
            f"Here's the plan: {self.plan.summary()}.\n\n```yaml\n{yaml_text}```",
            buttons=["Approve", "Adjust"],
        )
        return self.plan

    def approve(self, by_user_id: str) -> ApplyReport:
        """Apply the plan. Only a human may approve — the agent cannot approve itself."""
        if self.plan is None:
            raise ValueError("no plan to approve; finish the interview first")
        if by_user_id in set(self.app.registry.names()) or by_user_id == self.NAME:
            # §3.3.5: an agent — including this one — can never approve a gate.
            self.host.send(self.user_ref, "⛔ Only a human can approve the workspace plan.")
            raise PermissionError("agents cannot approve the onboarding plan")

        self.app.storage.update_onboarding(self.session_id, status="applying")
        report = self.apply(self.plan)
        status = "done" if report.verdict_passed else "failed"
        self.app.storage.update_onboarding(self.session_id, status=status)
        self.host.send(
            self.user_ref,
            ("✅ Team is live — " + report.summary()) if report.verdict_passed
            else ("⚠️ Applied with problems: " + "; ".join(report.verdict_reasons)),
        )
        return report

    # ------------------------------------------------------------------ #
    # 3. Apply (Builder role) — diff-based and idempotent
    # ------------------------------------------------------------------ #
    def apply(self, plan: WorkspacePlan) -> ApplyReport:
        report = ApplyReport()
        storage = self.app.storage

        existing_topics = {r["name"] for r in storage.conn.execute(
            "SELECT name FROM topic").fetchall()}
        for topic in plan.topics:
            if topic in existing_topics:
                report.skipped.append(f"topic {topic}")
            else:
                storage.upsert_topic(topic)
                report.topics_created.append(topic)

        existing_agents = set(self.app.registry.names())
        for planned in plan.agents:
            if planned.name in existing_agents:
                report.skipped.append(f"agent {planned.name}")
            else:
                self.app.registry.register(
                    AgentConfig(
                        name=planned.name,
                        role=planned.role,
                        soul_md=_soul_md(planned),
                        goals={"owns": planned.owns, "escalate_when": planned.escalate_when},
                        heartbeats=(
                            [Heartbeat(cron=planned.heartbeat, prompt=planned.heartbeat_prompt)]
                            if planned.heartbeat else []
                        ),
                        knowledge=[
                            KnowledgeSpec(type=s.type, uri=s.uri, scope=s.scope)
                            for s in planned.knowledge
                        ],
                    )
                )
                report.agents_created.append(planned.name)

            # Knowledge sources are registered idempotently by (agent, uri).
            for src in planned.knowledge:
                before = {r["id"] for r in storage.list_sources()}
                source_id = self.app.knowledge.add_source(
                    agent=planned.name, type_=src.type, uri=src.uri, scope=src.scope,
                    created_by=self.NAME,
                )
                if source_id in before:
                    report.skipped.append(f"source {src.uri}")
                else:
                    report.sources_added.append(source_id)
                    sync = self.app.knowledge.sync_source(source_id)
                    if not sync.ok:
                        report.manual_steps.append(f"knowledge source {src.uri}: {sync.error}")

            if planned.heartbeat and planned.heartbeat_prompt:
                job_id = self.host.schedule(planned.heartbeat, planned.heartbeat_prompt,
                                            planned.name)
                report.heartbeats_scheduled.append(job_id)

        for human in plan.human_ids:
            self.app.human_ids.add(human)
            self.app.gateway.human_ids.add(human)

        # Bots are human-only to create (§3.3.5): say so rather than pretending.
        for planned in plan.agents:
            report.manual_steps.append(
                f"create @{planned.name}_TR_Bot in BotFather and send me its token in DM"
            )

        self._verify(plan, report)
        return report

    # ------------------------------------------------------------------ #
    # 4. Verify (Tester role) — check the workspace against the plan's criteria
    # ------------------------------------------------------------------ #
    def _verify(self, plan: WorkspacePlan, report: ApplyReport) -> None:
        reasons: list[str] = []
        storage = self.app.storage

        topics = {r["name"] for r in storage.conn.execute("SELECT name FROM topic").fetchall()}
        for topic in plan.topics:
            if topic not in topics:
                reasons.append(f"topic {topic} was not created")

        registered = set(self.app.registry.names())
        for planned in plan.agents:
            if planned.name not in registered:
                reasons.append(f"agent {planned.name} was not registered")
            elif not self.app.registry.soul(planned.name).strip():
                reasons.append(f"agent {planned.name} has an empty soul")

        if len(registered) < 2:
            reasons.append("fewer than two agents: nobody could review another's work")

        planned_sources = sum(len(a.knowledge) for a in plan.agents)
        if planned_sources:
            healthy = [h for h in self.app.knowledge.health() if h["status"] == "ok"]
            if not healthy:
                reasons.append("no knowledge source synced successfully")

        expected_beats = sum(1 for a in plan.agents if a.heartbeat and a.heartbeat_prompt)
        if expected_beats and len(self.host.jobs()) < expected_beats:
            reasons.append("not every heartbeat was registered on the host scheduler")

        if not self.app.gateway.human_ids:
            reasons.append("no human can approve work")

        report.verdict_reasons = reasons
        report.verdict_passed = not reasons

    # ------------------------------------------------------------------ #
    # Resume / re-run
    # ------------------------------------------------------------------ #
    @classmethod
    def resume(cls, app, session_id: str, host: Optional[OnboardingHost] = None) -> "OnboardingAgent":
        """Re-hydrate a session so a half-finished setup continues instead of restarting."""
        row = app.storage.get_onboarding(session_id)
        if row is None:
            raise KeyError(f"unknown onboarding session {session_id}")
        agent = cls.__new__(cls)
        agent.app = app
        agent.host = host or MockHost()
        agent.user_ref = row["tg_user_id"] or "human"
        agent.session_id = session_id
        agent.answers = Answers(raw=_unjson(row["answers_json"]))
        agent.plan = plan_from_yaml(row["plan_yaml"]) if row["plan_yaml"] else None
        return agent


def _soul_md(planned) -> str:
    lines = [f"# {planned.name} — {planned.pillar} agent", "", planned.soul, "",
             "## Hard rules",
             "- Everything you produce is a **draft for human review**; you never send "
             "anything externally yourself.",
             "- Cite the knowledge-base passage behind every factual claim."]
    for area in planned.escalate_when:
        lines.append(f"- Never act on **{area}** without asking a human first.")
    return "\n".join(lines) + "\n"


def _json(obj) -> str:
    import json

    return json.dumps(obj)


def _unjson(text) -> dict:
    import json

    try:
        return json.loads(text or "{}")
    except ValueError:
        return {}
