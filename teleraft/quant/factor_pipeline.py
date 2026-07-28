"""The eleven-node factor graph, as a real pipeline (DESIGN.md Appendix A).

| Node(s) | Kind | What it does |
|---|---|---|
| 1–7 | producer | Construct each factor; emit one item per factor |
| 8 | gate | Validator — Newey–West t, bootstrap, IS/OOS degradation |
| 9 | gate | Regime auditor — kills anything that works in one regime only |
| 10 | join | Portfolio constructor — risk parity over the **survivors**, neutralised |
| 11 | aggregate_gate | Risk decomposer — residual alpha against an *independent* benchmark |

Two design decisions carried from DESIGN.md, both of which change the result:

* **Attribution uses an external benchmark, never the constructed factors** (§A.2).
  Regressing the portfolio on the factors it was built from makes the regressors span it
  by construction: R² → 1 and residual alpha → 0 as arithmetic, giving the same answer
  for a brilliant portfolio and a worthless one.
* **A gate that cannot be evaluated blocks.** Four of the seven factors need
  point-in-time fundamentals; with a price-only loader they report `cannot_evaluate`
  rather than being approximated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..pipeline import (
    Item,
    NodeOutcome,
    Pipeline,
    Verdict,
    aggregate_gate,
    gate,
    join,
    producer,
)
from .data import Bars
from .factors import construct
from .stats import (
    bootstrap_pvalue,
    degradation,
    mean,
    naive_tstat,
    newey_west_tstat,
    ols,
    regime_means,
    stdev,
    volatility_regimes,
)


@dataclass
class FactorGateConfig:
    """The bar each gate applies. Strict on purpose — most factors should die here."""

    min_tstat: float = 2.0                 # Newey–West adjusted
    max_p_value: float = 0.05
    max_degradation: float = 0.30          # in-sample → out-of-sample
    bootstrap_iterations: int = 2_000      # 10k in the source; 2k keeps the demo quick
    oos_fraction: float = 0.3
    min_regimes_working: int = 2           # must work in more than one
    min_bars: int = 250
    owner: str = "Quinn"
    validator: str = "Bailey"              # never the maker (§5.1)
    regime_auditor: str = "Mac"
    risk_officer: str = "Robin"


def build_factor_pipeline(universe: dict[str, Bars],
                          benchmark: Optional[dict[str, list[float]]] = None,
                          available_data: Optional[set[str]] = None,
                          config: Optional[FactorGateConfig] = None) -> Pipeline:
    """Assemble the eleven nodes.

    `benchmark` maps an *independently constructed* factor name to its return series —
    published series, not the ones nodes 1–7 build. Without it node 11 reports
    `cannot_evaluate`, because attribution against nothing is not attribution.
    """
    cfg = config or FactorGateConfig()

    # -- nodes 1–7: one parameterised producer (§12 #14) -------------------- #
    def construct_factors(context, artifacts):
        built, blocked = construct(universe, available_data)
        items: list[Item] = []
        produced: dict[str, object] = {}

        for name, series in built.items():
            items.append(Item(subject=name, payload={
                "returns": series.returns, "note": series.note,
            }))
            produced[f"factor:{name}"] = {"bars": len(series), "note": series.note}
            if series.per_stock:
                # Node 1's betas are needed by the join even if MKT is killed at a gate.
                produced["betas"] = series.per_stock

        for name, why in blocked.items():
            items.append(Item(subject=name, payload={"blocked": why}))

        return NodeOutcome(items=items, artifacts=produced)

    # -- node 8: the validator --------------------------------------------- #
    def validate(item, context, artifacts):
        if "blocked" in item.payload:
            return NodeOutcome(verdict=Verdict.CANNOT_EVALUATE,
                               reasons=[item.payload["blocked"]])
        returns = item.payload["returns"]
        if len(returns) < cfg.min_bars:
            return NodeOutcome(verdict=Verdict.CANNOT_EVALUATE,
                               reasons=[f"only {len(returns)} bars, need {cfg.min_bars}"])

        cut = int(len(returns) * (1 - cfg.oos_fraction))
        in_sample, out_sample = returns[:cut], returns[cut:]

        t_hac = newey_west_tstat(returns)
        t_naive = naive_tstat(returns)
        p = bootstrap_pvalue(returns, cfg.bootstrap_iterations)
        drop = degradation(newey_west_tstat(in_sample), newey_west_tstat(out_sample))

        payload = {"tstat": round(t_hac, 3), "p_value": round(p, 4),
                   "degradation": round(drop, 3), "naive_tstat": round(t_naive, 3)}
        reasons = []
        if abs(t_hac) < cfg.min_tstat:
            reasons.append(f"Newey–West t {t_hac:.2f} below {cfg.min_tstat} "
                           f"(naive t was {t_naive:.2f})")
        if p > cfg.max_p_value:
            reasons.append(f"bootstrap p {p:.3f} above {cfg.max_p_value}")
        if drop > cfg.max_degradation:
            reasons.append(f"in-sample→out-of-sample degradation {drop:.0%} "
                           f"above {cfg.max_degradation:.0%}")

        return NodeOutcome(
            verdict=Verdict.FAIL if reasons else Verdict.PASS,
            reasons=reasons, payload=payload,
            statistic=abs(t_hac), p_value=p,          # feeds the selection gate (§5.7.4)
            artifacts={f"validation:{item.subject}": payload},
        )

    # -- node 9: the regime auditor ---------------------------------------- #
    def audit_regimes(item, context, artifacts):
        returns = item.payload.get("returns")
        if not returns:
            return NodeOutcome(verdict=Verdict.CANNOT_EVALUATE,
                               reasons=["no return series to segment"])
        regimes = volatility_regimes(returns)
        if len(regimes) < 2:
            return NodeOutcome(verdict=Verdict.CANNOT_EVALUATE,
                               reasons=[f"{len(returns)} bars is too short to segment"])

        means = regime_means(returns, regimes)
        working = [name for name, m in means.items() if m > 0]
        payload = {"regime_means": {k: round(v, 6) for k, v in means.items()},
                   "regimes_working": working}

        if len(working) < cfg.min_regimes_working:
            return NodeOutcome(
                verdict=Verdict.FAIL,
                reasons=[f"positive in {len(working)} of {len(regimes)} volatility "
                         f"terciles ({', '.join(working) or 'none'}) — regime timing, "
                         "not a factor"],
                payload=payload, artifacts={f"regimes:{item.subject}": payload})
        return NodeOutcome(payload=payload,
                           artifacts={f"regimes:{item.subject}": payload})

    # -- node 10: the portfolio constructor (a barrier) --------------------- #
    def construct_portfolio(survivors, items, context, artifacts):
        if not survivors:
            return NodeOutcome(reasons=["no factor survived the gates"])

        series = {i.subject: i.payload["returns"] for i in survivors}
        length = min(len(s) for s in series.values())

        # Risk parity: inverse-volatility weights, normalised.
        vols = {name: stdev(s[-length:]) or 1e-9 for name, s in series.items()}
        raw = {name: 1.0 / v for name, v in vols.items()}
        total = sum(raw.values())
        weights = {name: w / total for name, w in raw.items()}

        combined = [sum(weights[name] * series[name][-length:][t] for name in series)
                    for t in range(length)]

        # Neutrality: these are long/short spreads, so the book is dollar-neutral by
        # construction. Beta neutrality uses node 1's estimates — available even if MKT
        # itself was killed (rule 4). Sector neutrality needs a classification we do not
        # have, and saying so beats implying it was enforced.
        betas = artifacts.get("betas") or {}
        neutrality = {
            "dollar": "by construction (long/short spreads)",
            "beta": (f"estimates available for {len(betas)} stocks"
                     if betas else "unavailable — node 1 produced no betas"),
            "sector": "NOT enforced — needs a sector classification (see Appendix A.3)",
        }
        return NodeOutcome(payload={
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "returns": combined,
            "neutrality": neutrality,
            "survivors": sorted(series),
        }, artifacts={"portfolio": {"weights": weights, "bars": length}})

    # -- node 11: the risk decomposer -------------------------------------- #
    def decompose_risk(aggregate, context, artifacts):
        if not benchmark:
            return NodeOutcome(
                verdict=Verdict.CANNOT_EVALUATE,
                reasons=["no independent benchmark configured — attribution against the "
                         "factors the portfolio was built from is vacuous (Appendix A.2), "
                         "so this gate blocks rather than reporting a meaningless zero"])

        portfolio = aggregate["returns"]
        names = sorted(benchmark)
        length = min([len(portfolio)] + [len(benchmark[n]) for n in names])
        y = portfolio[-length:]
        xs = [benchmark[n][-length:] for n in names]

        overlap = set(names) & set(aggregate.get("survivors", []))
        reg = ols(y, xs)
        payload = {
            "residual_alpha": round(reg.alpha, 6),
            "alpha_tstat": round(reg.alpha_tstat, 3),
            "r_squared": round(reg.r_squared, 4),
            "benchmark": names,
            "loadings": {n: round(b, 3) for n, b in zip(names, reg.betas)},
        }
        reasons = []
        if overlap:
            # Refuse the self-referential regression rather than reporting its zero.
            return NodeOutcome(
                verdict=Verdict.CANNOT_EVALUATE,
                reasons=[f"benchmark shares {sorted(overlap)} with the constructed "
                         "factors — the regressors would span the portfolio and residual "
                         "alpha would be zero by construction, not by finding"],
                payload=payload)
        if reg.alpha_tstat < 2.0:
            reasons.append(
                f"residual alpha t {reg.alpha_tstat:.2f} below 2.0 — the portfolio is "
                f"explained by known factors (R² {reg.r_squared:.2f}); this is "
                "repackaged style, not new alpha")

        return NodeOutcome(verdict=Verdict.FAIL if reasons else Verdict.PASS,
                           reasons=reasons, payload=payload,
                           statistic=reg.alpha_tstat,
                           artifacts={"attribution": payload})

    return Pipeline(name="eleven-node-factor-graph", nodes=[
        producer("construct-factors (nodes 1–7)", construct_factors, owner=cfg.owner),
        gate("validate (node 8)", validate, owner=cfg.owner, checker=cfg.validator),
        gate("regime-audit (node 9)", audit_regimes, owner=cfg.owner,
             checker=cfg.regime_auditor),
        join("portfolio (node 10)", construct_portfolio, owner=cfg.risk_officer),
        aggregate_gate("risk-decomposition (node 11)", decompose_risk,
                       owner=cfg.risk_officer, checker=cfg.validator),
    ])
