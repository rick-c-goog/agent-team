"""The pipeline executor (DESIGN.md §5.7).

Scheduling semantics, which are the whole reason the node kinds exist:

* **producer** — fans out; emits items and artifacts; never kills.
* **gate** — runs per item. Items are independent, so a gate advances each item as far
  as it can; item A reaching gate 3 does not wait for item B to clear gate 1.
* **join** — a genuine barrier. It cannot start until every item has passed or died,
  because its input *is* the surviving set. Starting early yields a different aggregate,
  not an earlier one.
* **aggregate_gate** — judges the join's output.

Failure semantics:

* A failing gate **kills** the item, with the reason recorded. Killed items do not
  advance, but their **artifacts remain** — a rejected factor's beta estimate is still
  needed by the join (§5.7.2 rule 4).
* `cannot_evaluate` **blocks** the item rather than passing it. A gate whose inputs are
  missing has not been satisfied; synthesizing the input defeats the gate.
* **Zero survivors is a result, not an error.** The join is told the set is empty, and
  the pipeline reports what killed each item.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .spec import Item, Node, NodeKind, NodeOutcome, Pipeline, Verdict

log = logging.getLogger("teleraft.pipeline")


@dataclass
class PipelineResult:
    run_id: str
    pipeline: str
    items: list[Item] = field(default_factory=list)
    aggregate: Optional[dict] = None
    graduated: bool = False
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def survivors(self) -> list[Item]:
        return [i for i in self.items if i.status in ("passed", "graduated")]

    @property
    def killed(self) -> list[Item]:
        return [i for i in self.items if i.status == "killed"]

    @property
    def blocked_items(self) -> list[Item]:
        return [i for i in self.items if i.status == "blocked"]

    def summary(self) -> str:
        parts = [f"{len(self.survivors)}/{len(self.items)} survived"]
        if self.killed:
            parts.append(f"{len(self.killed)} killed")
        if self.blocked_items:
            parts.append(f"{len(self.blocked_items)} blocked")
        if self.aggregate is None and not self.survivors:
            parts.append("no aggregate — nothing survived")
        elif self.graduated:
            parts.append("graduated to human review")
        return "; ".join(parts)

    def kill_report(self) -> list[str]:
        """What killed each item — the negative result, stated as a finding."""
        return [f"{i.subject}: {i.kill_reason}" for i in self.killed + self.blocked_items]


class PipelineEngine:
    def __init__(self, storage=None, notify: Optional[Callable[..., None]] = None,
                 epoch: str = "default"):
        self.storage = storage
        self.notify = notify or (lambda event, **data: None)
        # Trials from a superseded universe/cost model are excluded from the correction
        # (DESIGN.md §12 #11); the epoch label is how they are separated.
        self.epoch = epoch

    # ------------------------------------------------------------------ #
    def run(self, pipeline: Pipeline, context: Optional[dict] = None) -> PipelineResult:
        pipeline.validate()
        context = dict(context or {})
        run_id = "pipe_" + uuid.uuid4().hex[:10]
        result = PipelineResult(run_id=run_id, pipeline=pipeline.name)

        if self.storage:
            self.storage.create_pipeline_run(run_id, pipeline.name)
        self.notify("pipeline_started", run_id=run_id, pipeline=pipeline.name)

        # Artifacts are keyed by name and outlive the items that produced them.
        artifacts: dict[str, object] = {}
        items: list[Item] = []
        aggregate: Optional[dict] = None

        for node in pipeline.nodes:
            if node.kind is NodeKind.PRODUCER:
                items.extend(self._run_producer(node, run_id, context, artifacts, result))
            elif node.kind is NodeKind.GATE:
                self._run_gate(node, run_id, items, context, artifacts, result, pipeline.name)
            elif node.kind is NodeKind.JOIN:
                aggregate = self._run_join(node, run_id, items, context, artifacts, result)
            elif node.kind is NodeKind.AGGREGATE_GATE:
                aggregate = self._run_aggregate_gate(node, run_id, aggregate, context,
                                                     artifacts, result, pipeline.name)

        # Settle item statuses *before* deriving the outcome — `survivors` reads status,
        # so computing `graduated` first would always see an empty set.
        for item in items:
            if item.status == "running":
                item.status = "passed"
            if self.storage:
                self.storage.update_pipeline_item(item.id, status=item.status)

        result.items = items
        result.aggregate = aggregate
        result.graduated = bool(aggregate) and not result.blocked and bool(result.survivors)

        status = "done" if not result.blocked else "failed"
        if self.storage:
            self.storage.finish_pipeline_run(run_id, status, result.summary())
        self.notify("pipeline_finished", run_id=run_id, pipeline=pipeline.name,
                    result=result)
        return result

    # ------------------------------------------------------------------ #
    def _record(self, run_id: str, item: Optional[Item], node: Node,
                outcome: NodeOutcome, elapsed_ms: int) -> None:
        if not self.storage:
            return
        self.storage.add_node_run(
            run_id, item.id if item else None, node.name, node.kind.value,
            node.owner, node.checker, outcome.verdict.value,
            "; ".join(outcome.reasons), elapsed_ms,
        )
        for name, payload in outcome.artifacts.items():
            self.storage.add_pipeline_artifact(
                run_id, item.id if item else None, node.name, name,
                json.dumps(payload, default=str))

    def _record_trial(self, pipeline: str, subject: str, node: Node,
                      outcome: NodeOutcome) -> None:
        """Every evaluated hypothesis counts, whatever the verdict (§5.7.4)."""
        if not self.storage or outcome.statistic is None and outcome.p_value is None:
            return
        self.storage.add_trial(pipeline, subject, node.name,
                               outcome.statistic, outcome.p_value, self.epoch)

    def _invoke(self, node: Node, **kwargs) -> tuple[NodeOutcome, int]:
        started = time.perf_counter()
        try:
            outcome = node.run(**kwargs) or NodeOutcome()
        except Exception as e:
            log.exception("pipeline node %s raised", node.name)
            outcome = NodeOutcome(verdict=Verdict.CANNOT_EVALUATE,
                                  reasons=[f"{type(e).__name__}: {e}"])
        return outcome, int((time.perf_counter() - started) * 1000)

    # ------------------------------------------------------------------ #
    def _run_producer(self, node: Node, run_id: str, context: dict,
                      artifacts: dict, result: PipelineResult) -> list[Item]:
        outcome, ms = self._invoke(node, context=context, artifacts=artifacts)
        artifacts.update(outcome.artifacts)
        self._record(run_id, None, node, outcome, ms)

        produced: list[Item] = []
        for item in outcome.items:
            item.id = item.id or ("item_" + uuid.uuid4().hex[:8])
            if self.storage:
                self.storage.add_pipeline_item(item.id, run_id, item.subject)
            produced.append(item)
        self.notify("pipeline_produced", run_id=run_id, node=node.name,
                    count=len(produced))
        return produced

    def _run_gate(self, node: Node, run_id: str, items: list[Item], context: dict,
                  artifacts: dict, result: PipelineResult, pipeline: str) -> None:
        # Per item and independent: each item is advanced as far as it can go.
        for item in items:
            if not item.alive:
                continue                      # already killed or blocked upstream
            outcome, ms = self._invoke(node, item=item, context=context,
                                       artifacts=artifacts)
            artifacts.update(outcome.artifacts)   # survive even if the item dies
            item.payload.update(outcome.payload)
            self._record(run_id, item, node, outcome, ms)
            self._record_trial(pipeline, item.subject, node, outcome)

            if outcome.verdict is Verdict.FAIL:
                item.status = "killed"
                item.killed_at = node.name
                item.kill_reason = "; ".join(outcome.reasons) or f"failed {node.name}"
                if self.storage:
                    self.storage.update_pipeline_item(
                        item.id, status="killed", killed_at_node=node.name,
                        kill_reason=item.kill_reason, current_node=node.name)
                self.notify("pipeline_killed", run_id=run_id, node=node.name,
                            subject=item.subject, reason=item.kill_reason)
            elif outcome.verdict is Verdict.CANNOT_EVALUATE:
                item.status = "blocked"
                item.killed_at = node.name
                item.kill_reason = ("cannot evaluate: "
                                    + ("; ".join(outcome.reasons) or "inputs unavailable"))
                if self.storage:
                    self.storage.update_pipeline_item(
                        item.id, status="blocked", killed_at_node=node.name,
                        kill_reason=item.kill_reason, current_node=node.name)
                self.notify("pipeline_blocked", run_id=run_id, node=node.name,
                            subject=item.subject, reason=item.kill_reason)
            else:
                if self.storage:
                    self.storage.update_pipeline_item(item.id, current_node=node.name)

    def _run_join(self, node: Node, run_id: str, items: list[Item], context: dict,
                  artifacts: dict, result: PipelineResult) -> Optional[dict]:
        # The barrier: every item has now passed or died, so the survivor set is final.
        survivors = [i for i in items if i.alive]
        outcome, ms = self._invoke(node, survivors=survivors, items=items,
                                   context=context, artifacts=artifacts)
        artifacts.update(outcome.artifacts)
        self._record(run_id, None, node, outcome, ms)

        if not survivors:
            # A well-argued nothing (DESIGN.md §5.7.3): report what killed each.
            result.reasons.append(
                f"{node.name}: nothing survived — " + "; ".join(result.kill_report())
            )
            self.notify("pipeline_empty", run_id=run_id, node=node.name,
                        killed=result.kill_report())
            return None
        if outcome.verdict is Verdict.CANNOT_EVALUATE:
            result.blocked = True
            result.reasons.extend(outcome.reasons)
            return None
        return outcome.payload or {"survivors": [i.subject for i in survivors]}

    def _run_aggregate_gate(self, node: Node, run_id: str, aggregate: Optional[dict],
                            context: dict, artifacts: dict, result: PipelineResult,
                            pipeline: str) -> Optional[dict]:
        if aggregate is None:
            # Nothing to judge; not a failure of this node.
            return None
        outcome, ms = self._invoke(node, aggregate=aggregate, context=context,
                                   artifacts=artifacts)
        artifacts.update(outcome.artifacts)
        self._record(run_id, None, node, outcome, ms)
        self._record_trial(pipeline, "aggregate", node, outcome)

        if outcome.verdict is Verdict.FAIL:
            result.reasons.extend(outcome.reasons or [f"failed {node.name}"])
            result.graduated = False
            self.notify("pipeline_rejected", run_id=run_id, node=node.name,
                        reasons=outcome.reasons)
            return None
        if outcome.verdict is Verdict.CANNOT_EVALUATE:
            result.blocked = True
            result.reasons.extend(outcome.reasons or [f"{node.name} could not evaluate"])
            return None
        return {**aggregate, **outcome.payload}
