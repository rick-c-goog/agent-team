"""QuantRuntime — the Anthropic loop applied to quant research.

This is what turns TeleRaft's four roles into a research cycle rather than a writing
cycle. It is *deterministic* (no model calls), so the whole team runs offline and the
tests assert on real numbers:

  Planner  — registers a **hypothesis** (refusing dead ends the registry already killed)
             and sets numeric acceptance criteria: min Sharpe, max drawdown, min bars.
  Builder  — searches a small parameter grid **in-sample only** and emits a declarative
             ``SignalSpec`` plus its in-sample metrics.
  Tester   — re-runs the winning spec **out-of-sample**, on data the Builder never saw,
             and rejects when the edge does not survive. On rejection it *invalidates*
             the hypothesis with the reason, which the registry then uses to block a
             re-test.
  Learner  — distils the durable lesson ("mean-reversion params overfit at z<1.0").

That out-of-sample split is the quant analogue of "no agent grades its own work": prose
review cannot catch overfitting, but a held-out period can.

For an LLM-driven variant, use ``AnthropicRuntime`` for prose roles and keep this class
for the numeric Tester — see docs/QUANT_TEAM_TUTORIAL.md §9.
"""

from __future__ import annotations

from typing import Optional

from ..models import Artifact, Citation, Plan, Verdict
from ..quant.backtest import BacktestResult, SignalSpec, backtest
from ..quant.data import Bars, MarketDataLoader, SyntheticLoader, split_period
from ..quant.hypothesis import DuplicateHypothesis, HypothesisRegistry
from .base import RoleRequest

# Default research bar. Deliberately strict: most ideas should die here.
DEFAULT_CRITERIA = {
    "min_sharpe": 0.5,
    "max_drawdown": 0.35,
    "min_bars": 200,
    "beat_benchmark": False,
}

# The grid a Builder may search. Small on purpose — a huge grid mostly buys overfitting.
PARAM_GRID: dict[str, list[dict]] = {
    "sma_cross": [
        {"fast": 10, "slow": 50},
        {"fast": 20, "slow": 100},
        {"fast": 50, "slow": 200},
    ],
    "momentum": [{"lookback": 20}, {"lookback": 60}, {"lookback": 120}],
    "mean_reversion": [
        {"lookback": 10, "z_entry": 1.0},
        {"lookback": 20, "z_entry": 1.5},
        {"lookback": 40, "z_entry": 2.0},
    ],
    "vol_target": [{"lookback": 20, "target_vol": 0.15}, {"lookback": 60, "target_vol": 0.10}],
}


def _family_for(text: str) -> str:
    """Pick a strategy family from the task wording — the agent's 'idea'."""
    lowered = text.lower()
    if any(k in lowered for k in ("mean revers", "reversion", "oversold", "z-score")):
        return "mean_reversion"
    if any(k in lowered for k in ("vol target", "volatility target", "risk parity")):
        return "vol_target"
    if any(k in lowered for k in ("momentum", "trend", "breakout")):
        return "momentum"
    return "sma_cross"


def _symbol_for(text: str, default: str = "SPY") -> str:
    """Extract a ticker from the task wording.

    Tickers are written in caps by convention (`NVDA`, `BTC-USD`), so an already-uppercase
    token is a far better signal than a stop-word list — which silently mis-fires on
    ordinary words like "HAVE".
    """
    for token in text.replace(",", " ").split():
        cleaned = token.strip("().:?!@#'\"")
        if not cleaned:
            continue
        core = cleaned.replace("-", "").replace(".", "")
        if 1 <= len(core) <= 6 and core.isalnum() and cleaned.upper() == cleaned \
                and any(c.isalpha() for c in core):
            return cleaned.upper()
    return default


class QuantRuntime:
    """Plays the four loop roles using the backtest engine instead of a model."""

    name = "quant"

    def __init__(
        self,
        registry: HypothesisRegistry,
        loader: Optional[MarketDataLoader] = None,
        *,
        criteria: Optional[dict] = None,
        oos_fraction: float = 0.3,
        commission: float = 0.001,
    ):
        self.hypotheses = registry
        self.loader = loader or SyntheticLoader()
        self.criteria = {**DEFAULT_CRITERIA, **(criteria or {})}
        self.oos_fraction = oos_fraction
        self.commission = commission
        # Per-task working state: task title → (hypothesis_id, symbol, family)
        self._context: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    def _bars(self, symbol: str) -> Bars:
        return self.loader.load(symbol, "", "")

    def _samples(self, symbol: str) -> tuple[Bars, Bars]:
        return split_period(self._bars(symbol), self.oos_fraction)

    # ------------------------------------------------------------------ #
    # Planner
    # ------------------------------------------------------------------ #
    def plan(self, req: RoleRequest) -> tuple[Plan, int]:
        text = f"{req.task_title} {req.task_body}".strip()
        # A replan is the *same* research question, so keep the task's context: which
        # families have already been ruled out, and which hypothesis we are testing.
        ctx = dict(self._context.get(req.task_title, {}))
        is_replan = bool(ctx)

        symbol = ctx.get("symbol") or _symbol_for(text)
        family = ctx.get("family") or _family_for(text)
        statement = f"{_family_for(text)} produces risk-adjusted edge on {symbol}"

        needs_human = False
        risks: list[str] = []
        rationale = ""
        hypothesis_id = ctx.get("hypothesis_id", "")

        if not is_replan:
            try:
                hypothesis = self.hypotheses.propose(
                    agent=req.agent, statement=statement, universe=symbol,
                    rationale=f"proposed from task: {req.task_title}",
                )
                hypothesis_id = hypothesis.id
                self.hypotheses.mark_testing(hypothesis_id)
            except DuplicateHypothesis as e:
                # Self-improvement in action: the registry blocks a known dead end and
                # the plan escalates instead of burning a cycle on it.
                risks.append(f"previously invalidated: {e.existing.invalidated_reason}")
                rationale = str(e)
                needs_human = True

        # Escalation areas from the agent's goals still apply (live trading, capital…).
        for trigger in req.goals.get("escalate_when", []):
            if trigger.lower() in text.lower():
                needs_human = True
                risks.append(f"touches escalation area: {trigger}")

        ctx.update({
            "hypothesis_id": hypothesis_id,
            "symbol": symbol,
            "family": family,
        })
        self._context[req.task_title] = ctx

        criteria = [
            f"Hypothesis registered and testable: {statement}",
            f"Out-of-sample Sharpe ≥ {self.criteria['min_sharpe']}",
            f"Out-of-sample max drawdown ≤ {self.criteria['max_drawdown']:.0%}",
            f"At least {self.criteria['min_bars']} bars of out-of-sample data",
            "Every reported number comes from the backtest artifact, not prose",
        ]
        if self.criteria.get("beat_benchmark"):
            criteria.append("Out-of-sample return beats buy & hold")

        steps = [
            f"Search {family} parameters in-sample on {symbol}",
            "Draft the research note with in-sample metrics and the signal spec",
        ]
        plan = Plan(
            criteria=criteria,
            steps=steps,
            risks=risks + ["In-sample fit may not survive out-of-sample"],
            needs_human=needs_human,
        )
        if rationale:
            plan.risks.append(rationale)
        return plan, 0

    # ------------------------------------------------------------------ #
    # Builder — in-sample only
    # ------------------------------------------------------------------ #
    def build(self, req: RoleRequest) -> tuple[Artifact, int]:
        ctx = self._context.get(req.task_title) or {
            "symbol": _symbol_for(f"{req.task_title} {req.task_body}"),
            "family": _family_for(f"{req.task_title} {req.task_body}"),
            "hypothesis_id": "",
        }
        symbol, family = ctx["symbol"], ctx["family"]
        in_sample, _oos = self._samples(symbol)

        attempt = 1 + sum(1 for v in req.prior_verdicts if v.step == req.step and not v.passed)

        # Step 2 is the write-up: summarise, do not re-fit.
        if req.step > 0 and ctx.get("best_spec"):
            spec = SignalSpec.from_dict(ctx["best_spec"])
            result: BacktestResult = ctx["best_result"]
            content = (
                f"[{req.agent}] Research note — {spec.describe()} on {symbol}\n"
                f"In-sample: {result.summary()}\n"
                f"Hypothesis: {ctx.get('hypothesis_id') or '(unregistered)'}"
            )
            return Artifact(step=req.step, content=content,
                            files=[f"research/{symbol}-{family}-note.md"],
                            notes=f"attempt {attempt}",
                            citations=_citations(ctx)), 0

        # A rejection means "this family did not survive". Move to a family this task has
        # not tried yet, rather than re-fitting the same one harder — that is how
        # overfitting happens, and re-testing an exhausted family wastes the budget.
        tried: list[str] = list(ctx.get("tried_families", []))
        if attempt > 1 or family in tried:
            remaining = [f for f in PARAM_GRID if f not in tried]
            if remaining:
                family = remaining[0]
            else:
                ctx["families_exhausted"] = True     # nothing left → honest "no edge"
        ctx["family"] = family
        if family not in tried:
            tried.append(family)
        ctx["tried_families"] = tried

        # Parameter search, strictly on the in-sample window.
        candidates = PARAM_GRID.get(family, PARAM_GRID["sma_cross"])

        scored: list[tuple[float, SignalSpec, BacktestResult]] = []
        for params in candidates:
            spec = SignalSpec(type=family, params=dict(params))
            res = backtest(in_sample, spec, commission=self.commission)
            scored.append((res.sharpe, spec, res))
        scored.sort(key=lambda x: x[0], reverse=True)
        best_sharpe, best_spec, best_result = scored[0]

        ctx["best_spec"] = best_spec.to_dict()
        ctx["best_result"] = best_result
        self._context[req.task_title] = ctx

        if ctx.get("hypothesis_id"):
            self.hypotheses.record_result(ctx["hypothesis_id"], "in_sample",
                                          best_spec.to_dict(), best_result)

        content = (
            f"[{req.agent}] Candidate: {best_spec.describe()} on {symbol}\n"
            f"In-sample ({best_result.start}→{best_result.end}): "
            f"CAGR {best_result.cagr:+.1%}, Sharpe {best_sharpe:.2f}, "
            f"maxDD {best_result.max_drawdown:.1%}, trades {best_result.trades}\n"
            f"Searched {len(candidates)} parameter sets in the {family} family."
        )
        return Artifact(
            step=req.step,
            content=content,
            files=[f"research/{symbol}-{family}-insample.json"],
            notes=f"attempt {attempt}",
            citations=_citations(ctx),
        ), 0

    # ------------------------------------------------------------------ #
    # Tester — out-of-sample, adversarial
    # ------------------------------------------------------------------ #
    def test(self, req: RoleRequest) -> tuple[Verdict, int]:
        ctx = self._context.get(req.task_title, {})
        spec_dict = ctx.get("best_spec")
        if not spec_dict:
            return Verdict(step=req.step, passed=False, tester=req.agent,
                           reasons=["No signal spec to verify — the draft reports no "
                                    "backtest artifact"],
                           lessons=["Always attach the signal spec and its metrics"]), 0

        symbol = ctx["symbol"]
        spec = SignalSpec.from_dict(spec_dict)
        _in_sample, oos = self._samples(symbol)

        reasons: list[str] = []
        lessons: list[str] = []

        if len(oos) < self.criteria["min_bars"]:
            reasons.append(
                f"Only {len(oos)} out-of-sample bars, need {self.criteria['min_bars']}"
            )

        oos_result = backtest(oos, spec, commission=self.commission)
        if ctx.get("hypothesis_id"):
            self.hypotheses.record_result(ctx["hypothesis_id"], "out_of_sample",
                                          spec_dict, oos_result)

        in_sharpe = ctx["best_result"].sharpe if ctx.get("best_result") else 0.0
        if oos_result.sharpe < self.criteria["min_sharpe"]:
            reasons.append(
                f"Out-of-sample Sharpe {oos_result.sharpe:.2f} < "
                f"{self.criteria['min_sharpe']} (in-sample was {in_sharpe:.2f})"
            )
            lessons.append(
                f"{spec.describe()} did not survive out-of-sample on {symbol} — "
                "in-sample parameter search overfits this family"
            )
        if oos_result.max_drawdown > self.criteria["max_drawdown"]:
            reasons.append(
                f"Out-of-sample max drawdown {oos_result.max_drawdown:.1%} > "
                f"{self.criteria['max_drawdown']:.0%}"
            )
            lessons.append(f"Cap drawdown before proposing {spec.type} on {symbol}")
        if self.criteria.get("beat_benchmark") and \
                oos_result.total_return <= oos_result.benchmark_return:
            reasons.append(
                f"Out-of-sample return {oos_result.total_return:+.1%} does not beat "
                f"buy & hold {oos_result.benchmark_return:+.1%}"
            )

        ctx["oos_result"] = oos_result
        self._context[req.task_title] = ctx

        exhausted = bool(reasons) and bool(ctx.get("families_exhausted"))
        if exhausted:
            reasons.append(
                f"All {len(PARAM_GRID)} strategy families tried on {symbol} — "
                "no out-of-sample edge found; this is a negative result, not a failure"
            )
            lessons.append(
                f"No strategy family in the current grid shows out-of-sample edge on "
                f"{symbol}; a new family or a different universe is needed"
            )

        if reasons:
            # Invalidate with the reason, so the registry blocks a re-test (§ self-improving).
            if ctx.get("hypothesis_id"):
                self.hypotheses.invalidate(ctx["hypothesis_id"], reasons[0])
            # Nothing left to try → terminal, so the run escalates instead of looping.
            return Verdict(step=req.step, passed=False, reasons=reasons,
                           lessons=lessons, tester=req.agent, terminal=exhausted), 0

        if ctx.get("hypothesis_id"):
            self.hypotheses.support(
                ctx["hypothesis_id"],
                f"OOS Sharpe {oos_result.sharpe:.2f}, maxDD {oos_result.max_drawdown:.1%}",
            )
        return Verdict(step=req.step, passed=True, reasons=[],
                       lessons=[f"{spec.describe()} held up out-of-sample on {symbol} "
                                f"(Sharpe {oos_result.sharpe:.2f})"],
                       tester=req.agent), 0

    # ------------------------------------------------------------------ #
    # Learner
    # ------------------------------------------------------------------ #
    def learn(self, req: RoleRequest) -> tuple[list[str], int]:
        # Dedupe: the same verdict lesson repeats across steps of one run, and a memory
        # full of identical notes crowds out real ones at recall time.
        seen: set[str] = set()
        lessons: list[str] = []
        for v in req.prior_verdicts:
            for lesson in v.lessons:
                key = lesson.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    lessons.append(lesson)
        return lessons, 0

    # ------------------------------------------------------------------ #
    def run_card(self, task_title: str) -> dict:
        """Reproducible research summary — the `run_card.json` equivalent."""
        ctx = self._context.get(task_title, {})
        in_res = ctx.get("best_result")
        oos_res = ctx.get("oos_result")
        return {
            "task": task_title,
            "hypothesis_id": ctx.get("hypothesis_id", ""),
            "symbol": ctx.get("symbol", ""),
            "family": ctx.get("family", ""),
            "spec": ctx.get("best_spec", {}),
            "data_source": self.loader.name,
            "criteria": dict(self.criteria),
            "in_sample": in_res.to_dict() if in_res else None,
            "out_of_sample": oos_res.to_dict() if oos_res else None,
            "disclaimer": "Research output only. Not investment advice. No orders are "
                          "placed by this system.",
        }


def _citations(ctx: dict) -> list[Citation]:
    """Cite the artifact a number came from, so the Tester can check it."""
    result = ctx.get("best_result")
    if result is None:
        return []
    return [Citation(
        source_id="backtest",
        doc=f"{ctx.get('symbol','')}-in-sample",
        locator=f"{result.start}→{result.end}",
        quote=result.summary(),
    )]
