"""Deterministic mock runtime — lets the whole loop run offline with no API keys.

It is intentionally *scripted* so tests and the demo are reproducible and so the
interesting control-flow paths in the graph are always exercised:

  * the Tester rejects the first artifact once  → reject → retry → pass
  * the Tester rejects an artifact that used retrieved knowledge without citing it
    → the grounding check from DESIGN.md §4.1.3

It mimics the shape of real Planner/Builder/Tester output, not its intelligence.
"""

from __future__ import annotations

from ..models import Artifact, Citation, Plan, Verdict
from .base import RoleRequest


class MockRuntime:
    name = "mock"

    def __init__(self, *, fail_first_build: bool = True, cite_knowledge: bool = True):
        # When True, the Tester rejects the very first artifact once, so the demo/tests
        # cover the reject→retry path. Real runs get their verdict from a model.
        self.fail_first_build = fail_first_build
        # When False, the Builder ignores retrieved passages and cites nothing — which
        # the Tester must catch as an ungrounded claim.
        self.cite_knowledge = cite_knowledge

    # -- Planner ----------------------------------------------------------- #
    def plan(self, req: RoleRequest) -> tuple[Plan, int]:
        title = req.task_title or "the task"
        # Escalate to a human if the task touches one of the agent's escalation areas.
        escalate = req.goals.get("escalate_when", [])
        needs_human = any(
            trigger.lower() in (req.task_title + " " + req.task_body).lower()
            for trigger in escalate
        )
        criteria = [
            f"Deliverable directly addresses: {title}",
            "Every factual claim is supported (no unsourced stats)",
            "Tone matches the agent's soul and audience",
        ]
        if req.knowledge:
            # Grounded runs get an explicit, checkable criterion (§4.1.3).
            criteria.append("Claims drawn from the knowledge base are cited")
        plan = Plan(
            criteria=criteria,
            steps=[
                f"Draft the deliverable for: {title}",
                "Self-check against the acceptance criteria",
            ],
            risks=["May require facts the agent does not have"],
            needs_human=needs_human,
        )
        return plan, 350

    # -- Builder ----------------------------------------------------------- #
    def build(self, req: RoleRequest) -> tuple[Artifact, int]:
        step = req.step
        attempt = 1 + sum(1 for v in req.prior_verdicts if v.step == step and not v.passed)
        # On a retry, incorporate the tester's reasons so the next draft passes.
        addressed = ""
        if attempt > 1:
            last = next((v for v in reversed(req.prior_verdicts) if v.step == step), None)
            if last and last.reasons:
                addressed = " (revised: " + "; ".join(last.reasons) + ")"

        grounding = ""
        citations: list[Citation] = []
        if req.knowledge and (self.cite_knowledge or attempt > 1):
            # Cite the top passages actually retrieved for this step.
            for p in req.knowledge[:2]:
                citations.append(
                    Citation(source_id=p.source_id, doc=p.doc, locator=p.locator,
                             quote=p.text[:120])
                )
            grounding = " Grounded in: " + "; ".join(c.render() for c in citations) + "."

        content = (
            f"[{req.agent} · draft v{attempt}] Deliverable for step {step + 1}: "
            f"{req.plan.steps[step] if req.plan and step < len(req.plan.steps) else req.task_title}"
            f"{addressed}{grounding}"
        )
        artifact = Artifact(
            step=step,
            content=content,
            files=[f"drafts/task-step{step + 1}-v{attempt}.md"],
            notes=f"attempt {attempt}",
            citations=citations,
        )
        return artifact, 600

    # -- Tester ------------------------------------------------------------ #
    def test(self, req: RoleRequest) -> tuple[Verdict, int]:
        step = req.step
        attempt = 1 + sum(1 for v in req.prior_verdicts if v.step == step and not v.passed)
        artifact = req.artifact

        # Grounding check: passages were available but the draft cited none (§4.1.3).
        if req.knowledge and artifact is not None and not artifact.citations:
            return Verdict(
                step=step,
                passed=False,
                reasons=["Ungrounded: knowledge was available for this step but the "
                         "draft cites no source"],
                lessons=["Cite the knowledge-base passage behind every factual claim"],
                tester=req.agent,
            ), 400

        # Scripted first-attempt rejection, so the retry path is always exercised.
        if self.fail_first_build and step == 0 and attempt == 1:
            return Verdict(
                step=step,
                passed=False,
                reasons=["Fails criterion 2: a statistic is cited with no source"],
                lessons=["Always attach a source to any statistic before review"],
                tester=req.agent,
            ), 400

        return Verdict(step=step, passed=True, reasons=[], lessons=[], tester=req.agent), 400

    # -- Learner ----------------------------------------------------------- #
    def learn(self, req: RoleRequest) -> tuple[list[str], int]:
        lessons: list[str] = []
        for v in req.prior_verdicts:
            lessons.extend(v.lessons)
        return lessons, 150
