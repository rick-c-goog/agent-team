"""Cross-market portfolio backtesting — a shared capital pool across venues.

Single-symbol research is a toy: the unit a desk actually decides on is a *portfolio*.
This runs one strategy (or a per-symbol strategy) across several symbols that may sit in
different markets, sharing one capital pool, and reports both the blended result and
per-symbol attribution.

Three cross-market problems it handles explicitly rather than silently:

  * **Different calendars.** US equities and crypto do not trade on the same days.
    Returns are joined on the *union* of dates; a symbol that did not trade contributes
    a zero return for that date rather than shifting its history forward, which would
    fabricate alignment that never existed.
  * **Different annualisation.** The blended Sharpe uses the exposure-weighted
    periods-per-year of the constituent markets, not a hardcoded 252.
  * **Different currencies.** Mixing HKD and USD without an FX series produces a number
    that means nothing, so it raises unless you supply `fx_rates` or force a base.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from .backtest import BacktestResult, SignalSpec, apply_settlement, generate_weights
from .data import Bars
from .markets import Market, resolve_market


class CurrencyMismatch(ValueError):
    """Raised when a portfolio spans currencies with no conversion supplied."""


@dataclass
class PortfolioResult:
    symbols: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)
    # Present on BacktestResult too, so anything consuming a stored result — the
    # hypothesis registry, /hyp show, the Mini App — can render either shape.
    spec: str = ""
    base_currency: str = "USD"
    start: str = ""
    end: str = ""
    bars: int = 0
    periods_per_year: float = 252.0
    total_return: float = 0.0        # compounded
    # Sum of daily portfolio returns. Per-symbol `contribution` figures are arithmetic,
    # so they reconcile exactly against this, never against the compounded total —
    # attribution that does not add up is worse than no attribution.
    arithmetic_return: float = 0.0
    cagr: float = 0.0
    ann_vol: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    turnover: float = 0.0
    exposure: float = 0.0
    benchmark_return: float = 0.0        # equal-weight buy & hold of the same universe
    per_symbol: dict[str, dict] = field(default_factory=dict)
    equity: list[float] = field(default_factory=list)

    def to_dict(self, with_equity: bool = False) -> dict:
        d = asdict(self)
        if not with_equity:
            d.pop("equity", None)
        return d

    def summary(self) -> str:
        venues = "+".join(sorted(set(self.markets)))
        return (f"portfolio[{len(self.symbols)} symbols across {venues}] "
                f"[{self.start}→{self.end}]: CAGR {self.cagr:+.1%}, "
                f"Sharpe {self.sharpe:.2f}, maxDD {self.max_drawdown:.1%}, "
                f"vs equal-weight buy&hold {self.benchmark_return:+.1%} ({self.base_currency})")

    def attribution_lines(self) -> list[str]:
        out = []
        for sym, stats in sorted(self.per_symbol.items(),
                                 key=lambda kv: kv[1]["contribution"], reverse=True):
            out.append(f"{sym} ({stats['market']}): contributed "
                       f"{stats['contribution']:+.1%}, exposure {stats['exposure']:.0%}")
        return out


def _aligned_dates(bars_by_symbol: dict[str, Bars]) -> list[str]:
    """Union of all trading dates — venues keep their own calendars."""
    dates: set[str] = set()
    for bars in bars_by_symbol.values():
        dates.update(bars.dates)
    return sorted(dates)


def _returns_by_date(bars: Bars) -> dict[str, float]:
    out: dict[str, float] = {}
    for i in range(1, len(bars)):
        prev = bars.closes[i - 1]
        out[bars.dates[i]] = (bars.closes[i] / prev - 1.0) if prev else 0.0
    return out


def _weights_by_date(bars: Bars, spec: SignalSpec, mkt: Market) -> dict[str, float]:
    weights = generate_weights(bars, spec, mkt.periods_per_year)
    if not mkt.allows_short:
        weights = [max(0.0, w) for w in weights]
    weights = apply_settlement(weights, mkt.min_holding_bars)
    return dict(zip(bars.dates, weights))


def backtest_portfolio(
    bars_by_symbol: dict[str, Bars],
    spec: SignalSpec | dict[str, SignalSpec],
    *,
    base_currency: Optional[str] = None,
    fx_rates: Optional[dict[str, float]] = None,
    markets: Optional[dict[str, Market | str]] = None,
    risk_free: float = 0.0,
) -> PortfolioResult:
    """Backtest a universe with one shared capital pool.

    Capital is split equally across symbols; each symbol's signal scales its own sleeve,
    so an unallocated sleeve simply sits in cash. `fx_rates` maps a currency to its rate
    into `base_currency` (a constant rate is a simplification — pass a symbol-level FX
    series if you need path-accurate conversion).
    """
    if not bars_by_symbol:
        raise ValueError("no symbols to backtest")

    specs = spec if isinstance(spec, dict) else {s: spec for s in bars_by_symbol}
    mkts = {
        sym: resolve_market(sym, (markets or {}).get(sym))
        for sym in bars_by_symbol
    }

    # --- currency guard: refuse to add HKD to USD silently ------------------ #
    currencies = {m.currency for m in mkts.values()}
    if base_currency is None:
        if len(currencies) > 1 and not fx_rates:
            raise CurrencyMismatch(
                f"portfolio spans {sorted(currencies)} with no fx_rates supplied — "
                "converting is your decision, not the backtester's. Pass "
                "base_currency= and fx_rates={'HKD': 0.128, ...}, or restrict the "
                "universe to one currency."
            )
        base_currency = next(iter(currencies))
    fx = {base_currency: 1.0, **(fx_rates or {})}
    missing = [c for c in currencies if c not in fx]
    if missing:
        raise CurrencyMismatch(f"no fx_rates entry for {missing} into {base_currency}")

    dates = _aligned_dates(bars_by_symbol)
    if len(dates) < 3:
        raise ValueError("need at least 3 aligned dates to backtest a portfolio")

    rets = {s: _returns_by_date(b) for s, b in bars_by_symbol.items()}
    wts = {s: _weights_by_date(bars_by_symbol[s], specs[s], mkts[s]) for s in bars_by_symbol}

    sleeve = 1.0 / len(bars_by_symbol)
    equity = [1.0]
    port_rets: list[float] = []
    turnover = 0.0
    exposure_sum = 0.0
    prev_w = {s: 0.0 for s in bars_by_symbol}
    contribution = {s: 0.0 for s in bars_by_symbol}
    exposure_by_symbol = {s: 0.0 for s in bars_by_symbol}

    for date in dates[1:]:
        day_ret = 0.0
        day_exposure = 0.0
        for sym in bars_by_symbol:
            # A symbol whose venue was closed contributes nothing that day.
            r = rets[sym].get(date)
            w = wts[sym].get(date, prev_w[sym])
            if r is None:
                # Closed: hold the position, earn nothing, pay nothing.
                day_exposure += abs(w) * sleeve
                exposure_by_symbol[sym] += abs(w)
                prev_w[sym] = w
                continue
            mkt = mkts[sym]
            delta = abs(w - prev_w[sym])
            turnover += delta * sleeve
            cost = delta * mkt.round_trip_cost
            # FX applies to the sleeve's P&L, not the weight.
            local_pnl = (w * r - cost) * sleeve
            converted = local_pnl * (fx[mkt.currency] / fx[base_currency])
            day_ret += converted
            contribution[sym] += converted
            day_exposure += abs(w) * sleeve
            exposure_by_symbol[sym] += abs(w)
            prev_w[sym] = w
        port_rets.append(day_ret)
        equity.append(equity[-1] * (1.0 + day_ret))
        exposure_sum += day_exposure

    # Exposure-weighted annualisation across venues (crypto 365 vs equities 252).
    total_exposure = sum(exposure_by_symbol.values()) or 1.0
    periods = sum(
        mkts[s].periods_per_year * (exposure_by_symbol[s] / total_exposure)
        for s in bars_by_symbol
    ) or 252.0

    years = max(len(port_rets) / periods, 1e-9)
    total_return = equity[-1] - 1.0
    cagr = (equity[-1] ** (1.0 / years) - 1.0) if equity[-1] > 0 else -1.0

    mean_r = sum(port_rets) / len(port_rets)
    var = sum((r - mean_r) ** 2 for r in port_rets) / max(1, len(port_rets) - 1)
    ann_vol = math.sqrt(var * periods)
    sharpe = ((mean_r * periods - risk_free) / ann_vol) if ann_vol > 1e-12 else 0.0

    peak, max_dd = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    # Benchmark: equal-weight buy & hold of the same universe, same FX treatment.
    bench = 0.0
    for sym, bars in bars_by_symbol.items():
        if len(bars) >= 2 and bars.closes[0]:
            local = bars.closes[-1] / bars.closes[0] - 1.0
            bench += local * sleeve * (fx[mkts[sym].currency] / fx[base_currency])

    n_days = max(1, len(port_rets))
    per_symbol = {
        sym: {
            "market": mkts[sym].code,
            "currency": mkts[sym].currency,
            "spec": specs[sym].describe(),
            "contribution": round(contribution[sym], 6),
            "exposure": round(exposure_by_symbol[sym] / n_days, 4),
            "bars": len(bars_by_symbol[sym]),
        }
        for sym in bars_by_symbol
    }

    described = {s.describe() for s in specs.values()}
    return PortfolioResult(
        symbols=sorted(bars_by_symbol),
        markets=[mkts[s].code for s in sorted(bars_by_symbol)],
        spec=described.pop() if len(described) == 1 else "per-symbol specs",
        base_currency=base_currency,
        start=dates[0],
        end=dates[-1],
        bars=len(dates),
        periods_per_year=round(periods, 2),
        total_return=round(total_return, 6),
        arithmetic_return=round(sum(port_rets), 6),
        cagr=round(cagr, 6),
        ann_vol=round(ann_vol, 6),
        sharpe=round(sharpe, 4),
        max_drawdown=round(max_dd, 6),
        turnover=round(turnover, 4),
        exposure=round(exposure_sum / n_days, 4),
        benchmark_return=round(bench, 6),
        per_symbol=per_symbol,
        equity=[round(e, 6) for e in equity],
    )
