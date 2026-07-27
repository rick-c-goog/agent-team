"""Trace replay (DESIGN.md §5.9).

Re-run a recorded run against a changed configuration — a different soul version, role
prompt, runtime, or gate threshold — and compare the outcomes. Until this exists, every
change to a prompt or a threshold is an unfalsifiable opinion.

Replay is deliberately *not* a re-execution of the original side effects: it creates a
throwaway task in a scratch workspace, so replaying a hundred historical runs neither
posts to Telegram nor mutates the memory the original run learned from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models import RunStatus


@dataclass
class ReplayComparison:
    run_id: str
    original_status: str = ""
    replay_status: str = ""
    original_verdicts: int = 0
    replay_verdicts: int = 0
    original_rejections: int = 0
    replay_rejections: int = 0
    original_tokens: int = 0
    replay_tokens: int = 0
    changed: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verdict = "CHANGED" if self.changed else "same"
        return (f"{self.run_id}: {verdict} — status {self.original_status} → "
                f"{self.replay_status}, rejections {self.original_rejections} → "
                f"{self.replay_rejections}, tokens {self.original_tokens} → "
                f"{self.replay_tokens}")


def replay_run(app, run_id: str, *, runtime_for: Optional[Callable] = None,
               criteria_override: Optional[dict] = None) -> ReplayComparison:
    """Replay one recorded run in a scratch workspace and compare.

    `runtime_for` swaps the runtime (a changed prompt, a different model); anything else
    the caller wants to vary — a gate threshold, a soul — is configured on the scratch
    app before calling.
    """
    from ..app import App

    loaded = app.storage.load_run(run_id)
    if loaded is None:
        raise KeyError(f"unknown run {run_id}")
    task_id, original = loaded
    task = app.storage.get_task(task_id)
    if task is None:
        raise KeyError(f"run {run_id} has no task to replay")

    comparison = ReplayComparison(
        run_id=run_id,
        original_status=original.status.value,
        original_verdicts=len(original.verdicts),
        original_rejections=sum(1 for v in original.verdicts if not v.passed),
        original_tokens=original.tokens_used,
    )

    # A scratch workspace: in-memory storage, mock surface, no knowledge sync. Replay
    # must not post to Telegram or write to the memory the original run produced.
    scratch = App(db_path=":memory:", agents_dir=app._agents_dir,
                  human_ids=set(app.human_ids), sync_knowledge=False)
    try:
        if runtime_for is not None:
            scratch.engine.runtime_for = runtime_for
        if criteria_override:
            for agent in scratch.registry.names():
                runtime = scratch.engine.runtime_for(agent)
                if hasattr(runtime, "criteria"):
                    runtime.criteria = {**runtime.criteria, **criteria_override}

        replay_task = scratch.tasks.create(
            topic=task["topic"], title=task["title"], body=task["body"] or "",
            created_by="replay")
        result = scratch.engine.start(replay_task, original.agent)

        _tid, replayed = scratch.storage.load_run(result.run_id)
        comparison.replay_status = replayed.status.value
        comparison.replay_verdicts = len(replayed.verdicts)
        comparison.replay_rejections = sum(1 for v in replayed.verdicts if not v.passed)
        comparison.replay_tokens = replayed.tokens_used
    except Exception as e:
        comparison.replay_status = "error"
        comparison.notes.append(f"{type(e).__name__}: {e}")
    finally:
        scratch.close()

    comparison.changed = (
        comparison.original_status != comparison.replay_status
        or comparison.original_rejections != comparison.replay_rejections
    )
    if comparison.changed:
        comparison.notes.append(
            "outcome differs — attribute this to the change under test, not to intuition"
        )
    return comparison


def replay_all(app, runtime_for: Optional[Callable] = None,
               limit: int = 50) -> list[ReplayComparison]:
    """Replay recent runs; the comparison set is what makes a change reviewable."""
    rows = app.storage.conn.execute(
        "SELECT id FROM run ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        try:
            out.append(replay_run(app, row["id"], runtime_for=runtime_for))
        except KeyError:
            continue
    return out
