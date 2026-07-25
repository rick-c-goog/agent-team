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


def generate_weights(bars: Bars, spec: SignalSpec) -> list[float]:
    """Target weight in [-1, 1] for each bar, computed from information up to that bar."""
    spec.validate()
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
            realized = math.sqrt(var * TRADING_DAYS)
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
        return (f"{self.spec} on {self.symbol} [{self.start}→{self.end}]: "
                f"CAGR {self.cagr:+.1%}, Sharpe {self.sharpe:.2f}, "
                f"maxDD {self.max_drawdown:.1%}, turnover {self.turnover:.1f}x, "
                f"vs buy&hold {self.benchmark_return:+.1%}")


def backtest(bars: Bars, spec: SignalSpec, commission: float = 0.001,
             risk_free: float = 0.0) -> BacktestResult:
    """Run `spec` over `bars`, trading the signal with a one-bar lag and costs."""
    spec.validate()
    n = len(bars)
    if n < 3:
        raise ValueError(f"need at least 3 bars to backtest, got {n}")

    weights = generate_weights(bars, spec)
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
        cost = delta * commission
        r = w * rets[i] - cost
        strategy_rets.append(r)
        equity.append(equity[-1] * (1.0 + r))
        exposure_sum += abs(w)
        prev_w = w

    years = max(len(strategy_rets) / TRADING_DAYS, 1e-9)
    total_return = equity[-1] - 1.0
    cagr = (equity[-1] ** (1.0 / years) - 1.0) if equity[-1] > 0 else -1.0

    mean_r = sum(strategy_rets) / len(strategy_rets)
    var = sum((r - mean_r) ** 2 for r in strategy_rets) / max(1, len(strategy_rets) - 1)
    ann_vol = math.sqrt(var * TRADING_DAYS)
    sharpe = ((mean_r * TRADING_DAYS - risk_free) / ann_vol) if ann_vol > 1e-12 else 0.0

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
