"""Signal specs + backtest engine + metrics.

**Declarative signals, not generated code.** Vibe-Trading has its agent write Python and
then sandboxes it with AST inspection. TeleRaft takes the safer route: an agent emits a
*declarative* ``SignalSpec`` validated against a schema, and this module — ordinary,
reviewed code — turns it into positions. An agent therefore cannot execute arbitrary
code as a side effect of doing research, and every strategy is diffable and replayable.

Two correctness details that decide whether a backtest means anything:

  * **No lookahead.** A signal computed from the bar at *t* is traded at *t+1*.
  * **Costs on turnover.** Commission is charged on the weight change, so a strategy
    that flips daily cannot look free.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .data import TRADING_DAYS, Bars
from .markets import Market, resolve_market

# Every spec type the agents are allowed to propose. Anything else is rejected at
# validation time — the allow-list *is* the safety boundary.
SPEC_TYPES = ("buy_and_hold", "sma_cross", "momentum", "mean_reversion", "vol_target")


class SpecError(ValueError):
    """An invalid or unsupported signal spec."""


@dataclass
class SignalSpec:
    """A strategy an agent may propose, as data."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    long_only: bool = True

    def validate(self) -> "SignalSpec":
        if self.type not in SPEC_TYPES:
            raise SpecError(f"unknown signal type {self.type!r}; allowed: {', '.join(SPEC_TYPES)}")
        for key, value in self.params.items():
            if not isinstance(value, (int, float)):
                raise SpecError(f"param {key!r} must be numeric, got {type(value).__name__}")
            if isinstance(value, (int, float)) and value < 0:
                raise SpecError(f"param {key!r} must be non-negative")
        if self.type == "sma_cross":
            fast, slow = int(self.params.get("fast", 20)), int(self.params.get("slow", 50))
            if fast >= slow:
                raise SpecError(f"sma_cross needs fast < slow (got {fast} >= {slow})")
        return self

    def describe(self) -> str:
        if not self.params:
            return self.type
        inner = ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.type}({inner})"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SignalSpec":
        return SignalSpec(
            type=d.get("type", ""),
            params={k: v for k, v in (d.get("params") or {}).items()},
            long_only=bool(d.get("long_only", True)),
        )


# --------------------------------------------------------------------------- #
# Signal generation → target weights
# --------------------------------------------------------------------------- #
def _sma(values: list[float], window: int, i: int) -> Optional[float]:
    if window <= 0 or i + 1 < window:
        return None
    return sum(values[i + 1 - window: i + 1]) / window


def generate_weights(bars: Bars, spec: SignalSpec,
                     periods_per_year: Optional[int] = None) -> list[float]:
    """Target weight in [-1, 1] for each bar, computed from information up to that bar.

    `periods_per_year` matters for any signal that reasons in *annualised* terms —
    `vol_target` sizes against an annual vol target, so using 252 on a crypto series
    would mis-size every position. Defaults to the symbol's market convention.
    """
    spec.validate()
    if periods_per_year is None:
        periods_per_year = resolve_market(bars.symbol).periods_per_year
    n = len(bars)
    closes = bars.closes
    weights = [0.0] * n

    if spec.type == "buy_and_hold":
        return [1.0] * n

    if spec.type == "sma_cross":
        fast = int(spec.params.get("fast", 20))
        slow = int(spec.params.get("slow", 50))
        for i in range(n):
            f, s = _sma(closes, fast, i), _sma(closes, slow, i)
            if f is None or s is None:
                continue
            weights[i] = 1.0 if f > s else (0.0 if spec.long_only else -1.0)
        return weights

    if spec.type == "momentum":
        lookback = int(spec.params.get("lookback", 60))
        for i in range(n):
            if i < lookback or closes[i - lookback] == 0:
                continue
            trailing = closes[i] / closes[i - lookback] - 1.0
            weights[i] = 1.0 if trailing > 0 else (0.0 if spec.long_only else -1.0)
        return weights

    if spec.type == "mean_reversion":
        lookback = int(spec.params.get("lookback", 20))
        z_entry = float(spec.params.get("z_entry", 1.0))
        for i in range(n):
            mean = _sma(closes, lookback, i)
            if mean is None:
                continue
            window = closes[i + 1 - lookback: i + 1]
            var = sum((c - mean) ** 2 for c in window) / max(1, len(window) - 1)
            sd = math.sqrt(var)
            if sd <= 0:
                continue
            z = (closes[i] - mean) / sd
            if z <= -z_entry:
                weights[i] = 1.0                      # cheap vs its own mean → buy
            elif z >= z_entry:
                weights[i] = 0.0 if spec.long_only else -1.0
            else:
                weights[i] = weights[i - 1] if i else 0.0   # hold through the band
        return weights

    if spec.type == "vol_target":
        lookback = int(spec.params.get("lookback", 20))
        target = float(spec.params.get("target_vol", 0.15))
        rets = [0.0] + bars.returns()
        for i in range(n):
            if i < lookback:
                continue
            window = rets[i + 1 - lookback: i + 1]
            mean = sum(window) / len(window)
            var = sum((r - mean) ** 2 for r in window) / max(1, len(window) - 1)
            realized = math.sqrt(var * periods_per_year)
            weights[i] = min(1.0, target / realized) if realized > 0 else 0.0
        return weights

    raise SpecError(f"unhandled signal type {spec.type!r}")   # pragma: no cover


# --------------------------------------------------------------------------- #
# Backtest + metrics
# --------------------------------------------------------------------------- #
@dataclass
class BacktestResult:
    symbol: str
    spec: str
    market: str = "US"          # conventions used (annualisation, costs, settlement)
    currency: str = "USD"
    start: str = ""
    end: str = ""
    bars: int = 0
    total_return: float = 0.0
    cagr: float = 0.0
    ann_vol: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    hit_rate: float = 0.0
    turnover: float = 0.0
    trades: int = 0
    exposure: float = 0.0
    benchmark_return: float = 0.0
    equity: list[float] = field(default_factory=list)

    def to_dict(self, with_equity: bool = False) -> dict:
        d = asdict(self)
        if not with_equity:
            d.pop("equity", None)
        return d

    def summary(self) -> str:
        return (f"{self.spec} on {self.symbol} ({self.market}) [{self.start}→{self.end}]: "
                f"CAGR {self.cagr:+.1%}, Sharpe {self.sharpe:.2f}, "
                f"maxDD {self.max_drawdown:.1%}, turnover {self.turnover:.1f}x, "
                f"vs buy&hold {self.benchmark_return:+.1%}")


def apply_settlement(weights: list[float], min_holding_bars: int) -> list[float]:
    """Enforce a minimum holding period: a position opened at bar *i* cannot be reduced
    before bar *i + min_holding_bars*.

    Note on T+1 markets (China A-shares): at **daily** bars, T+1 is already satisfied by
    the one-bar signal lag — a position established at bar *i* is exited at bar *i+1* at
    the earliest, which is exactly T+1. So `min_holding_bars=1` is a no-op on daily data
    and is correct rather than redundant: it binds when you feed intraday bars. The
    constraints that actually bite on A-shares at daily frequency are the short ban and
    the higher costs, both applied in `backtest()`.
    """
    if min_holding_bars <= 0:
        return list(weights)

    out = list(weights)
    held_until = -1
    prev = 0.0
    for i, w in enumerate(out):
        if i < held_until and abs(w) < abs(prev):
            out[i] = prev                       # locked in: cannot sell yet
        elif abs(w) > abs(prev):
            held_until = i + min_holding_bars    # opened/increased → start the clock
        prev = out[i]
    return out


def backtest(bars: Bars, spec: SignalSpec, commission: Optional[float] = None,
             risk_free: float = 0.0, market: Optional[Market | str] = None,
             slippage: Optional[float] = None) -> BacktestResult:
    """Run `spec` over `bars` with the conventions of its market.

    Market conventions decide annualisation, costs, and settlement — pass `market` to
    override, otherwise it is inferred from the ticker (`600519.SS` → China A-shares,
    `BTC-USD` → crypto, …). See markets.py for why this is a correctness issue.
    """
    spec.validate()
    n = len(bars)
    if n < 3:
        raise ValueError(f"need at least 3 bars to backtest, got {n}")

    mkt = resolve_market(bars.symbol, market)
    commission = mkt.commission if commission is None else commission
    slippage = mkt.slippage if slippage is None else slippage
    cost_per_turn = commission + slippage

    weights = generate_weights(bars, spec, mkt.periods_per_year)
    if not mkt.allows_short:
        weights = [max(0.0, w) for w in weights]
    weights = apply_settlement(weights, mkt.min_holding_bars)
    rets = bars.returns()                       # length n-1; rets[i] is bar i→i+1

    equity = [1.0]
    strategy_rets: list[float] = []
    turnover = 0.0
    trades = 0
    exposure_sum = 0.0
    prev_w = 0.0

    for i in range(len(rets)):
        # Trade on yesterday's signal: weights[i] is known at bar i, applied to rets[i].
        w = weights[i]
        delta = abs(w - prev_w)
        turnover += delta
        if delta > 1e-9:
            trades += 1
        cost = delta * cost_per_turn
        r = w * rets[i] - cost
        strategy_rets.append(r)
        equity.append(equity[-1] * (1.0 + r))
        exposure_sum += abs(w)
        prev_w = w

    periods = mkt.periods_per_year
    years = max(len(strategy_rets) / periods, 1e-9)
    total_return = equity[-1] - 1.0
    cagr = (equity[-1] ** (1.0 / years) - 1.0) if equity[-1] > 0 else -1.0

    mean_r = sum(strategy_rets) / len(strategy_rets)
    var = sum((r - mean_r) ** 2 for r in strategy_rets) / max(1, len(strategy_rets) - 1)
    ann_vol = math.sqrt(var * periods)
    sharpe = ((mean_r * periods - risk_free) / ann_vol) if ann_vol > 1e-12 else 0.0

    peak, max_dd = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    active = [r for r, w in zip(strategy_rets, weights) if abs(w) > 1e-9]
    hit_rate = (sum(1 for r in active if r > 0) / len(active)) if active else 0.0

    bench = (bars.closes[-1] / bars.closes[0] - 1.0) if bars.closes[0] else 0.0

    return BacktestResult(
        symbol=bars.symbol,
        spec=spec.describe(),
        market=mkt.code,
        currency=mkt.currency,
        start=bars.dates[0] if bars.dates else "",
        end=bars.dates[-1] if bars.dates else "",
        bars=n,
        total_return=round(total_return, 6),
        cagr=round(cagr, 6),
        ann_vol=round(ann_vol, 6),
        sharpe=round(sharpe, 4),
        max_drawdown=round(max_dd, 6),
        hit_rate=round(hit_rate, 4),
        turnover=round(turnover, 4),
        trades=trades,
        exposure=round(exposure_sum / max(1, len(strategy_rets)), 4),
        benchmark_return=round(bench, 6),
        equity=[round(e, 6) for e in equity],
    )
