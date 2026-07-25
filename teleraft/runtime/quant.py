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
from ..quant.markets import market_for
from ..quant.portfolio import CurrencyMismatch, backtest_portfolio
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


def _symbols_for(text: str, default: str = "SPY") -> list[str]:
    """Extract every ticker mentioned, preserving order.

    Tickers are written in caps by convention (`NVDA`, `BTC-USD`, `0700.HK`), so an
    already-uppercase token is a far better signal than a stop-word list — which
    silently mis-fires on ordinary words like "HAVE". More than one ticker means a
    **cross-market universe**, which the loop backtests as a portfolio.
    """
    found: list[str] = []
    for token in text.replace(",", " ").split():
        cleaned = token.strip("().:?!@#'\"")
        if not cleaned:
            continue
        core = cleaned.replace("-", "").replace(".", "").replace("=", "")
        if 1 <= len(core) <= 8 and core.isalnum() and cleaned.upper() == cleaned \
                and any(c.isalpha() for c in core):
            symbol = cleaned.upper()
            if symbol not in found:
                found.append(symbol)
    return found or [default]


def _symbol_for(text: str, default: str = "SPY") -> str:
    """First ticker mentioned — kept for single-symbol callers."""
    return _symbols_for(text, default)[0]


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
        commission: Optional[float] = None,
        base_currency: Optional[str] = None,
        fx_rates: Optional[dict[str, float]] = None,
    ):
        self.hypotheses = registry
        self.loader = loader or SyntheticLoader()
        self.criteria = {**DEFAULT_CRITERIA, **(criteria or {})}
        self.oos_fraction = oos_fraction
        # None = use each market's own cost convention (see quant/markets.py).
        self.commission = commission
        self.base_currency = base_currency
        self.fx_rates = fx_rates
        # Per-task working state: task title → {hypothesis_id, symbols, family, …}
        self._context: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    def _bars(self, symbol: str) -> Bars:
        return self.loader.load(symbol, "", "")

    def _samples(self, symbol: str) -> tuple[Bars, Bars]:
        return split_period(self._bars(symbol), self.oos_fraction)

    def _universe_samples(self, symbols: list[str]) -> tuple[dict[str, Bars], dict[str, Bars]]:
        """In-sample / out-of-sample split for every symbol in the universe."""
        ins: dict[str, Bars] = {}
        oos: dict[str, Bars] = {}
        for symbol in symbols:
            a, b = self._samples(symbol)
            ins[symbol], oos[symbol] = a, b
        return ins, oos

    def _evaluate(self, bars_or_universe, spec: SignalSpec):
        """Backtest a single symbol or a portfolio, returning a result with `.summary()`."""
        if isinstance(bars_or_universe, dict):
            if len(bars_or_universe) == 1:
                only = next(iter(bars_or_universe.values()))
                return backtest(only, spec, commission=self.commission)
            return backtest_portfolio(
                bars_or_universe, spec,
                base_currency=self.base_currency, fx_rates=self.fx_rates,
            )
        return backtest(bars_or_universe, spec, commission=self.commission)

    # ------------------------------------------------------------------ #
    # Planner
    # ------------------------------------------------------------------ #
    def plan(self, req: RoleRequest) -> tuple[Plan, int]:
        text = f"{req.task_title} {req.task_body}".strip()
        # A replan is the *same* research question, so keep the task's context: which
        # families have already been ruled out, and which hypothesis we are testing.
        ctx = dict(self._context.get(req.task_title, {}))
        is_replan = bool(ctx)

        symbols = ctx.get("symbols") or _symbols_for(text)
        symbol = symbols[0]
        universe = ", ".join(symbols)
        family = ctx.get("family") or _family_for(text)
        venues = sorted({market_for(s).code for s in symbols})
        scope = f"the {universe} portfolio" if len(symbols) > 1 else symbol
        statement = f"{_family_for(text)} produces risk-adjusted edge on {scope}"

        needs_human = False
        risks: list[str] = []
        rationale = ""
        hypothesis_id = ctx.get("hypothesis_id", "")

        if not is_replan:
            try:
                hypothesis = self.hypotheses.propose(
                    agent=req.agent, statement=statement, universe=universe,
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
            "symbols": symbols,
            "universe": universe,
            "venues": venues,
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
        if len(venues) > 1:
            # Cross-market work carries assumptions a single-venue backtest does not.
            criteria.append(
                f"Each venue's own conventions are applied ({'+'.join(venues)}: "
                "annualisation, costs, settlement, shorting)"
            )
            currencies = sorted({market_for(s).currency for s in symbols})
            if len(currencies) > 1:
                criteria.append(
                    f"Currencies {'+'.join(currencies)} are converted explicitly, not summed"
                )
                risks.append(f"cross-currency portfolio ({'+'.join(currencies)}) needs FX rates")
            risks.append("venues keep different calendars — alignment is by date, not by row")

        steps = [
            f"Search {family} parameters in-sample on {universe}",
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
        if not self._context.get(req.task_title):
            syms = _symbols_for(f"{req.task_title} {req.task_body}")
            self._context[req.task_title] = {
                "symbol": syms[0], "symbols": syms, "universe": ", ".join(syms),
                "family": _family_for(f"{req.task_title} {req.task_body}"),
                "hypothesis_id": "",
            }
        ctx = self._context[req.task_title]
        symbols = ctx.get("symbols") or [ctx["symbol"]]
        universe = ctx.get("universe") or ctx["symbol"]
        family = ctx["family"]
        in_sample, _oos = self._universe_samples(symbols)

        attempt = 1 + sum(1 for v in req.prior_verdicts if v.step == req.step and not v.passed)

        # Step 2 is the write-up: summarise, do not re-fit.
        if req.step > 0 and ctx.get("best_spec"):
            spec = SignalSpec.from_dict(ctx["best_spec"])
            result = ctx["best_result"]
            lines = [
                f"[{req.agent}] Research note — {spec.describe()} on {universe}",
                f"In-sample: {result.summary()}",
            ]
            if hasattr(result, "attribution_lines"):
                lines.append("Attribution: " + " | ".join(result.attribution_lines()))
            lines.append(f"Hypothesis: {ctx.get('hypothesis_id') or '(unregistered)'}")
            return Artifact(step=req.step, content="\n".join(lines),
                            files=[f"research/{_slug(universe)}-{family}-note.md"],
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

        scored: list[tuple[float, SignalSpec, object]] = []
        for params in candidates:
            spec = SignalSpec(type=family, params=dict(params))
            try:
                res = self._evaluate(in_sample, spec)
            except CurrencyMismatch as e:
                # Refuse to invent an FX rate: report it as work a human must resolve.
                return Artifact(
                    step=req.step,
                    content=f"[{req.agent}] Blocked: {e}",
                    notes="cross-currency universe needs fx_rates",
                ), 0
            scored.append((res.sharpe, spec, res))
        scored.sort(key=lambda x: x[0], reverse=True)
        best_sharpe, best_spec, best_result = scored[0]

        ctx["best_spec"] = best_spec.to_dict()
        ctx["best_result"] = best_result
        self._context[req.task_title] = ctx

        if ctx.get("hypothesis_id"):
            self.hypotheses.record_result(ctx["hypothesis_id"], "in_sample",
                                          best_spec.to_dict(), best_result)

        venue_note = ""
        if len(ctx.get("venues", [])) > 1:
            venue_note = (f"\nVenues: {'+'.join(ctx['venues'])} — each with its own "
                          f"annualisation, costs and settlement "
                          f"({getattr(best_result, 'periods_per_year', '')} blended periods/yr).")
        content = (
            f"[{req.agent}] Candidate: {best_spec.describe()} on {universe}\n"
            f"In-sample ({best_result.start}→{best_result.end}): "
            f"CAGR {best_result.cagr:+.1%}, Sharpe {best_sharpe:.2f}, "
            f"maxDD {best_result.max_drawdown:.1%}\n"
            f"Searched {len(candidates)} parameter sets in the {family} family."
            f"{venue_note}"
        )
        return Artifact(
            step=req.step,
            content=content,
            files=[f"research/{_slug(universe)}-{family}-insample.json"],
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
        symbols = ctx.get("symbols") or [symbol]
        universe = ctx.get("universe") or symbol
        spec = SignalSpec.from_dict(spec_dict)
        _in_sample, oos = self._universe_samples(symbols)

        reasons: list[str] = []
        lessons: list[str] = []

        shortest = min(len(b) for b in oos.values())
        if shortest < self.criteria["min_bars"]:
            thin = [s for s, b in oos.items() if len(b) < self.criteria["min_bars"]]
            reasons.append(
                f"Only {shortest} out-of-sample bars for {', '.join(thin)}, "
                f"need {self.criteria['min_bars']}"
            )

        oos_result = self._evaluate(oos, spec)
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
                f"{spec.describe()} did not survive out-of-sample on {universe} — "
                "in-sample parameter search overfits this family"
            )
        if oos_result.max_drawdown > self.criteria["max_drawdown"]:
            reasons.append(
                f"Out-of-sample max drawdown {oos_result.max_drawdown:.1%} > "
                f"{self.criteria['max_drawdown']:.0%}"
            )
            lessons.append(f"Cap drawdown before proposing {spec.type} on {universe}")
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
                f"All {len(PARAM_GRID)} strategy families tried on {universe} — "
                "no out-of-sample edge found; this is a negative result, not a failure"
            )
            lessons.append(
                f"No strategy family in the current grid shows out-of-sample edge on "
                f"{universe}; a new family or a different universe is needed"
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
                       lessons=[f"{spec.describe()} held up out-of-sample on {universe} "
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
        symbols = ctx.get("symbols") or ([ctx["symbol"]] if ctx.get("symbol") else [])
        return {
            "task": task_title,
            "hypothesis_id": ctx.get("hypothesis_id", ""),
            "symbol": ctx.get("symbol", ""),
            "symbols": symbols,
            "universe": ctx.get("universe", ctx.get("symbol", "")),
            # Cross-market provenance: which venue conventions produced these numbers.
            "venues": ctx.get("venues") or [market_for(s).code for s in symbols],
            "market_conventions": {
                s: {
                    "market": market_for(s).code,
                    "currency": market_for(s).currency,
                    "periods_per_year": market_for(s).periods_per_year,
                    "round_trip_cost": market_for(s).round_trip_cost,
                    "allows_short": market_for(s).allows_short,
                }
                for s in symbols
            },
            "base_currency": self.base_currency,
            "fx_rates": self.fx_rates,
            "family": ctx.get("family", ""),
            "spec": ctx.get("best_spec", {}),
            "data_source": self.loader.name,
            "criteria": dict(self.criteria),
            "in_sample": in_res.to_dict() if in_res else None,
            "out_of_sample": oos_res.to_dict() if oos_res else None,
            "disclaimer": "Research output only. Not investment advice. No orders are "
                          "placed by this system.",
        }


def _slug(universe: str) -> str:
    """Filename-safe label for a universe ('SPY, BTC-USD' → 'SPY_BTC-USD')."""
    return "_".join(s.strip().replace("/", "-") for s in universe.split(",") if s.strip())


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
