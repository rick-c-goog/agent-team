"""Factor construction — nodes 1–7 of the eleven-node graph (DESIGN.md Appendix A).

**One parameterised producer, not seven near-identical ones** (DESIGN.md §12 #14). Each
factor still gets its own item, its own gate records and its own artifact, so the
per-factor audit trail survives the consolidation.

The honest part: **four of the seven cannot be computed from prices.** Size needs shares
outstanding, value needs book equity, profitability needs revenue and assets, investment
needs asset growth — all **point-in-time**, lagged to public availability. Fundamentals
as currently reported embed restatements the market never had, and that look-ahead is
undetectable downstream because the resulting backtest stays internally consistent and
simply wrong.

So those four report ``cannot_evaluate`` and block, rather than being approximated with
whatever is to hand. A factor built from a proxy nobody agreed to is worse than a factor
that says it is missing its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .data import Bars, MarketDataLoader
from .stats import mean, ols, stdev

# Data a factor needs beyond a price series. Anything listed here that the configured
# loaders cannot supply turns the factor into a blocked item.
FUNDAMENTALS = "point-in-time fundamentals"
SHARES = "shares outstanding"


@dataclass
class FactorSpec:
    """One factor: what it is, what it needs, and how it is built."""

    name: str
    node: int
    description: str
    requires: list[str] = field(default_factory=list)   # empty ⇒ price-only
    build: Optional[Callable] = None

    @property
    def computable_from_prices(self) -> bool:
        return not self.requires


@dataclass
class FactorSeries:
    """A constructed factor: its long/short spread return series."""

    name: str
    returns: list[float] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    per_stock: dict = field(default_factory=dict)   # e.g. betas, kept for the join
    note: str = ""

    def __len__(self) -> int:
        return len(self.returns)


# --------------------------------------------------------------------------- #
# Cross-sectional helpers
# --------------------------------------------------------------------------- #
def _aligned(universe: dict[str, Bars]) -> tuple[list[str], dict[str, list[float]]]:
    """Dates present in every symbol, and each symbol's returns over them."""
    common = set.intersection(*(set(b.dates) for b in universe.values()))
    dates = sorted(common)
    series: dict[str, list[float]] = {}
    for symbol, bars in universe.items():
        by_date = dict(zip(bars.dates, bars.closes))
        closes = [by_date[d] for d in dates]
        series[symbol] = [
            (closes[i] / closes[i - 1] - 1.0) if closes[i - 1] else 0.0
            for i in range(1, len(closes))
        ]
    return dates[1:], series


def _spread(scores_by_date: list[dict[str, float]], returns: dict[str, list[float]],
            fraction: float = 1 / 3) -> list[float]:
    """Long the top fraction, short the bottom, rebalanced each period.

    Deciles are the literature's convention; with a small universe a decile is one or
    two names and the "factor" becomes those names' idiosyncratic noise. Terciles are
    used here and the choice is stated rather than hidden.
    """
    out: list[float] = []
    for t, scores in enumerate(scores_by_date):
        ranked = sorted(scores.items(), key=lambda kv: kv[1])
        k = max(1, int(len(ranked) * fraction))
        shorts = [s for s, _ in ranked[:k]]
        longs = [s for s, _ in ranked[-k:]]
        long_ret = mean([returns[s][t] for s in longs if t < len(returns[s])])
        short_ret = mean([returns[s][t] for s in shorts if t < len(returns[s])])
        out.append(long_ret - short_ret)
    return out


# --------------------------------------------------------------------------- #
# The three factors a price series can actually support
# --------------------------------------------------------------------------- #
def build_market_beta(universe: dict[str, Bars], **_) -> FactorSeries:
    """Node 1. Rolling regression of each stock on the market; MKT is the market return.

    Emits **two** things: the MKT factor (which a gate may kill) and a per-stock beta
    estimate the portfolio join needs for beta-neutralisation regardless — the
    artifact-survives-the-item rule (DESIGN.md §5.7.2 rule 4).
    """
    dates, returns = _aligned(universe)
    symbols = sorted(returns)
    market = [mean([returns[s][t] for s in symbols]) for t in range(len(dates))]

    betas: dict[str, float] = {}
    window = min(len(market), 1260)          # ~60 months of daily bars
    for symbol in symbols:
        y = returns[symbol][-window:]
        x = market[-window:]
        reg = ols(y, [x])
        betas[symbol] = round(reg.betas[0], 4) if reg.betas else 1.0

    return FactorSeries(name="MKT", returns=market, dates=dates, per_stock=betas,
                        note=f"betas from a {window}-bar rolling regression")


def build_momentum(universe: dict[str, Bars], lookback: int = 252,
                   skip: int = 21, **_) -> FactorSeries:
    """Node 4. 12-minus-1-month momentum; spread of the top vs bottom tercile."""
    dates, returns = _aligned(universe)
    symbols = sorted(returns)
    scores: list[dict[str, float]] = []
    for t in range(len(dates)):
        if t < lookback:
            scores.append({s: 0.0 for s in symbols})
            continue
        window = slice(t - lookback, t - skip)
        scores.append({
            s: sum(returns[s][window]) for s in symbols
        })
    return FactorSeries(name="MOM", returns=_spread(scores, returns), dates=dates,
                        note=f"{lookback}-bar return skipping the last {skip}")


def build_low_volatility(universe: dict[str, Bars], window: int = 60, **_) -> FactorSeries:
    """Node 7. Trailing realized volatility; long low-vol, short high-vol."""
    dates, returns = _aligned(universe)
    symbols = sorted(returns)
    scores: list[dict[str, float]] = []
    for t in range(len(dates)):
        if t < window:
            scores.append({s: 0.0 for s in symbols})
            continue
        # Negated so that "high score" means "low volatility" — long the top tercile.
        scores.append({s: -stdev(returns[s][t - window:t]) for s in symbols})
    return FactorSeries(name="LVOL", returns=_spread(scores, returns), dates=dates,
                        note=f"{window}-bar trailing realized volatility")


# --------------------------------------------------------------------------- #
# The seven, as data
# --------------------------------------------------------------------------- #
FACTORS: list[FactorSpec] = [
    FactorSpec("MKT", 1, "Market beta — rolling 60-month regression", [], build_market_beta),
    FactorSpec("SMB", 2, "Size — small-minus-big by market cap", [SHARES]),
    FactorSpec("HML", 3, "Value — high-minus-low book-to-market", [FUNDAMENTALS]),
    FactorSpec("MOM", 4, "Momentum — 12-minus-1-month decile spread", [], build_momentum),
    FactorSpec("RMW", 5, "Profitability — gross profitability", [FUNDAMENTALS]),
    FactorSpec("CMA", 6, "Investment — annual asset growth", [FUNDAMENTALS]),
    FactorSpec("LVOL", 7, "Low volatility — trailing 60-day realized vol", [],
               build_low_volatility),
]


def construct(universe: dict[str, Bars], available: Optional[set[str]] = None
              ) -> tuple[dict[str, FactorSeries], dict[str, str]]:
    """Build every factor whose inputs exist. Returns (built, blocked → why)."""
    available = available or set()
    built: dict[str, FactorSeries] = {}
    blocked: dict[str, str] = {}

    for spec in FACTORS:
        missing = [need for need in spec.requires if need not in available]
        if missing:
            # The data is not there. This is the honest common case.
            blocked[spec.name] = (
                f"node {spec.node} ({spec.description}) needs {', '.join(missing)}"
                " — configure a source that supplies it, or this factor cannot be built"
            )
            continue
        if spec.build is None:
            # The inputs exist but nothing constructs this factor yet. A different
            # problem with a different fix, so it gets a different message — telling an
            # operator to go find data they already have wastes their afternoon.
            blocked[spec.name] = (
                f"node {spec.node} ({spec.description}) has its inputs but no builder is "
                "implemented — see teleraft/quant/factors.py"
            )
            continue
        built[spec.name] = spec.build(universe)
    return built, blocked
