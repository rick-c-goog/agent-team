"""Hypothesis registry — what makes the trading agent *self-improving*.

Vibe-Trading's self-improving agent tracks research lineage and invalidation so the loop
closes: hypothesis → signal engine → backtest → metrics → refine. This is that registry.

The single most valuable thing it does is **stop the team re-testing a dead end**. An
LLM with no memory of yesterday will happily propose "20/50 SMA crossover on SPY" every
morning. Here, ``propose()`` first checks whether a near-identical statement was already
invalidated and, if so, refuses and hands back the reason — which the Planner then has
to design around.

Statuses:  proposed → testing → supported | invalidated → (retired)
"""

from __future__ import annotations

import enum
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

_WORD = re.compile(r"[a-z0-9]+")

# Jaccard similarity above this counts as "the same idea, restated".
DUPLICATE_THRESHOLD = 0.6


class HypothesisStatus(str, enum.Enum):
    PROPOSED = "proposed"
    TESTING = "testing"
    SUPPORTED = "supported"
    INVALIDATED = "invalidated"
    RETIRED = "retired"


@dataclass
class Hypothesis:
    id: str
    statement: str
    agent: str = ""
    universe: str = ""
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    rationale: str = ""
    invalidated_reason: str = ""
    parent_id: Optional[str] = None
    results: list[dict] = field(default_factory=list)

    def short(self) -> str:
        mark = {"supported": "✅", "invalidated": "❌", "testing": "🔬",
                "proposed": "💡", "retired": "🗄"}.get(self.status.value, "•")
        return f"{mark} {self.id} [{self.status.value}] {self.statement}"


class DuplicateHypothesis(Exception):
    """Raised when a hypothesis restates one that was already invalidated."""

    def __init__(self, existing: Hypothesis):
        super().__init__(
            f"already invalidated as {existing.id}: {existing.invalidated_reason}"
        )
        self.existing = existing


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "on", "in", "of", "for", "and", "is", "are", "to", "with",
            "that", "this", "will", "over", "than", "be"}
    return {t for t in _WORD.findall(text.lower()) if t not in stop}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class HypothesisRegistry:
    def __init__(self, storage):
        self.storage = storage

    # ------------------------------------------------------------------ #
    def propose(self, agent: str, statement: str, *, universe: str = "",
                rationale: str = "", parent_id: Optional[str] = None,
                task_id: Optional[str] = None, allow_retest: bool = False) -> Hypothesis:
        """Register a new hypothesis, refusing to blindly re-test an invalidated one.

        Two escape hatches, both deliberate:

        * ``parent_id`` — this is a **refinement**: the proposer has read the earlier
          result and is responding to it ("…with a volatility filter"). Refining is the
          whole point of a self-improving loop, so lineage bypasses the duplicate check.
        * ``allow_retest`` — an explicit "test it again anyway", e.g. because the
          original test itself was flawed. Deliberate, never accidental.
        """
        if not allow_retest and parent_id is None:
            clash = self.find_invalidated_like(statement)
            if clash is not None:
                raise DuplicateHypothesis(clash)

        hid = "hyp_" + uuid.uuid4().hex[:8]
        self.storage.add_hypothesis(hid, agent, statement.strip(), universe,
                                    rationale, parent_id, task_id)
        return self.get(hid)

    def find_invalidated_like(self, statement: str) -> Optional[Hypothesis]:
        for row in self.storage.list_hypotheses(status=HypothesisStatus.INVALIDATED.value):
            if similarity(statement, row["statement"]) >= DUPLICATE_THRESHOLD:
                return self._row_to_hypothesis(row)
        return None

    def find_similar(self, statement: str, threshold: float = DUPLICATE_THRESHOLD
                     ) -> list[tuple[float, Hypothesis]]:
        out = []
        for row in self.storage.list_hypotheses():
            score = similarity(statement, row["statement"])
            if score >= threshold:
                out.append((round(score, 3), self._row_to_hypothesis(row)))
        return sorted(out, key=lambda x: x[0], reverse=True)

    # ------------------------------------------------------------------ #
    def mark_testing(self, hid: str) -> None:
        self.storage.update_hypothesis(hid, status=HypothesisStatus.TESTING.value)

    def record_result(self, hid: str, sample: str, spec: dict, result: Any,
                      run_id: Optional[str] = None) -> None:
        """Attach a backtest to the hypothesis. `sample` is in_sample|out_of_sample.

        Re-running an identical (sample, spec, period) is a no-op: a deterministic
        backtest repeated across steps of one run is the same evidence, not new evidence.
        """
        metrics = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        spec_json = json.dumps(spec)
        for existing in self.storage.backtests_for(hid):
            if (existing["sample"] == sample and existing["spec_json"] == spec_json
                    and existing["period_start"] == metrics.get("start", "")
                    and existing["period_end"] == metrics.get("end", "")):
                return
        self.storage.add_backtest_result(
            hypothesis_id=hid, run_id=run_id, sample=sample,
            spec_json=json.dumps(spec), symbol=metrics.get("symbol", ""),
            period_start=metrics.get("start", ""), period_end=metrics.get("end", ""),
            metrics_json=json.dumps(metrics),
        )

    def support(self, hid: str, note: str = "") -> Hypothesis:
        self.storage.update_hypothesis(hid, status=HypothesisStatus.SUPPORTED.value,
                                       rationale=note or None)
        return self.get(hid)

    def invalidate(self, hid: str, reason: str) -> Hypothesis:
        """Record *why* it died — that reason is what prevents the team retrying it."""
        self.storage.update_hypothesis(hid, status=HypothesisStatus.INVALIDATED.value,
                                       invalidated_reason=reason)
        return self.get(hid)

    def retire(self, hid: str) -> Hypothesis:
        self.storage.update_hypothesis(hid, status=HypothesisStatus.RETIRED.value)
        return self.get(hid)

    # ------------------------------------------------------------------ #
    def get(self, hid: str) -> Hypothesis:
        row = self.storage.get_hypothesis(hid)
        if row is None:
            raise KeyError(f"unknown hypothesis {hid}")
        return self._row_to_hypothesis(row)

    def list(self, status: Optional[str] = None, agent: Optional[str] = None) -> list[Hypothesis]:
        return [self._row_to_hypothesis(r)
                for r in self.storage.list_hypotheses(status=status, agent_name=agent)]

    def lineage(self, hid: str) -> list[Hypothesis]:
        """Walk parent links back to the original idea (research provenance)."""
        chain: list[Hypothesis] = []
        current: Optional[str] = hid
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            h = self.get(current)
            chain.append(h)
            current = h.parent_id
        return list(reversed(chain))

    def board(self) -> str:
        """Human-readable registry, rendered by `/hyp` in Telegram."""
        rows = self.list()
        if not rows:
            return "🔬 No hypotheses yet."
        lines = ["🔬 *Hypothesis registry*"]
        for h in rows:
            lines.append("  " + h.short())
            if h.status is HypothesisStatus.INVALIDATED and h.invalidated_reason:
                lines.append(f"      ↳ {h.invalidated_reason}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _row_to_hypothesis(self, row) -> Hypothesis:
        results = [json.loads(r["metrics_json"]) for r in self.storage.backtests_for(row["id"])]
        return Hypothesis(
            id=row["id"],
            statement=row["statement"],
            agent=row["agent_name"] or "",
            universe=row["universe"] or "",
            status=HypothesisStatus(row["status"]),
            rationale=row["rationale"] or "",
            invalidated_reason=row["invalidated_reason"] or "",
            parent_id=row["parent_id"],
            results=results,
        )
