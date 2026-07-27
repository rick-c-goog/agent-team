"""Process metrics (DESIGN.md §5.9).

The numbers worth watching in production are not model metrics. They describe the
*process*: how much a task costs, where runs fail, and how often a human has to step in.

**Human intervention rate is the one to watch.** A rising rate is the earliest signal
that the gates are mis-calibrated — humans are catching what the checkers should have.
It moves before task success does, because a human rejecting work is the system working;
a human rejecting work *more often* is the system degrading.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Metrics:
    tasks: int = 0
    tasks_done: int = 0
    tasks_open: int = 0
    runs: int = 0
    runs_failed: int = 0
    runs_awaiting_human: int = 0
    tokens_total: int = 0
    tokens_per_task: float = 0.0
    nodes_executed: int = 0
    failures_by_node: dict = field(default_factory=dict)
    rejections: int = 0
    approvals: int = 0
    human_intervention_rate: float = 0.0
    tester_rejection_rate: float = 0.0
    verdicts: int = 0
    notes: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"tasks: {self.tasks} ({self.tasks_done} done, {self.tasks_open} open)",
            f"runs: {self.runs} ({self.runs_failed} failed, "
            f"{self.runs_awaiting_human} awaiting a human)",
            f"tokens: {self.tokens_total} total, {self.tokens_per_task:.0f}/task",
            f"tester rejection rate: {self.tester_rejection_rate:.0%} "
            f"of {self.verdicts} verdicts",
            f"human intervention rate: {self.human_intervention_rate:.0%} "
            f"({self.rejections} rejected of {self.approvals + self.rejections} decisions)",
        ]
        if self.failures_by_node:
            worst = sorted(self.failures_by_node.items(), key=lambda kv: -kv[1])[:3]
            lines.append("failures by node: " +
                         ", ".join(f"{n} ×{c}" for n, c in worst))
        lines.extend(self.notes)
        return lines


def collect(storage, since: Optional[float] = None) -> Metrics:
    """Compute process metrics from the durable record — no separate instrumentation."""
    from ..models import RunState

    m = Metrics()

    tasks = storage.list_tasks()
    m.tasks = len(tasks)
    m.tasks_done = sum(1 for t in tasks if t["status"] in ("done", "closed"))
    m.tasks_open = m.tasks - m.tasks_done

    rows = storage.conn.execute(
        "SELECT id, status, state_json FROM run"
        + (" WHERE started_at >= ?" if since else ""),
        (since,) if since else (),
    ).fetchall()
    m.runs = len(rows)

    failures: Counter = Counter()
    verdicts = rejected = 0
    for row in rows:
        if row["status"] == "failed":
            m.runs_failed += 1
        elif row["status"] == "awaiting_human":
            m.runs_awaiting_human += 1
        try:
            state = RunState.from_json(row["state_json"])
        except Exception:
            continue
        m.tokens_total += state.tokens_used
        verdicts += len(state.verdicts)
        rejected += sum(1 for v in state.verdicts if not v.passed)

        for event in storage.run_events(row["id"]):
            m.nodes_executed += 1
            if "failed" in event["node"]:
                failures[event["node"].split("→")[0]] += 1

    m.verdicts = verdicts
    m.tester_rejection_rate = (rejected / verdicts) if verdicts else 0.0
    m.failures_by_node = dict(failures)
    m.tokens_per_task = (m.tokens_total / m.tasks) if m.tasks else 0.0

    decisions = storage.conn.execute(
        "SELECT decision FROM approval" + (" WHERE created_at >= ?" if since else ""),
        (since,) if since else (),
    ).fetchall()
    m.approvals = sum(1 for d in decisions if d["decision"] == "approve")
    m.rejections = sum(1 for d in decisions if d["decision"] in ("reject", "adjust"))
    total_decisions = m.approvals + m.rejections
    m.human_intervention_rate = (m.rejections / total_decisions) if total_decisions else 0.0

    if total_decisions and m.human_intervention_rate > 0.3:
        m.notes.append(
            "⚠️ humans are rejecting >30% of what reaches them — the checkers are "
            "letting through work they should be catching (§5.9)"
        )
    if m.runs and m.runs_failed / m.runs > 0.1:
        m.notes.append("⚠️ >10% of runs failed — check the harness layer first (§5.1)")
    return m
