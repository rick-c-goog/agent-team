"""Market data loaders (the `get_market_data` equivalent).

Pure Python, no numpy/pandas, so the whole quant stack runs offline in tests and demos
exactly like the mock runtime does for the loop.

Three loaders ship:
  * ``SyntheticLoader`` — deterministic pseudo-random walk seeded by symbol. Reproducible
    and offline; the default so a new team can run the full research loop immediately.
  * ``CsvLoader``      — real OHLCV from `date,open,high,low,close,volume` CSV files.
  * ``MarketDataLoader`` — the protocol to implement for a live provider (yfinance,
    CCXT, Tushare…). See docs/QUANT_TEAM_TUTORIAL.md §8.

Every loader returns ``Bars``: aligned date and close series. Keeping the interface this
small is what lets an agent's strategy be evaluated identically on synthetic, historical,
and live-provider data.
"""

from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

TRADING_DAYS = 252


@dataclass
class Bars:
    """A single symbol's aligned price history."""

    symbol: str
    dates: list[str] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.closes)

    def slice(self, start: Optional[str] = None, end: Optional[str] = None) -> "Bars":
        idx = [
            i for i, d in enumerate(self.dates)
            if (start is None or d >= start) and (end is None or d <= end)
        ]
        return Bars(
            symbol=self.symbol,
            dates=[self.dates[i] for i in idx],
            closes=[self.closes[i] for i in idx],
        )

    def returns(self) -> list[float]:
        return [
            (self.closes[i] / self.closes[i - 1]) - 1.0 if self.closes[i - 1] else 0.0
            for i in range(1, len(self.closes))
        ]


class MarketDataLoader(Protocol):
    name: str

    def load(self, symbol: str, start: str, end: str) -> Bars:
        ...


# --------------------------------------------------------------------------- #
# Synthetic (offline, deterministic)
# --------------------------------------------------------------------------- #
class SyntheticLoader:
    """Reproducible synthetic prices — same symbol always yields the same series.

    Uses a seeded LCG rather than `random` so results never depend on global state, and
    a mild autocorrelation term so trend-following and mean-reversion strategies both
    have something real to find (and to overfit to, which is the point of the
    out-of-sample gate).
    """

    name = "synthetic"

    def __init__(self, days: int = 1500, start_date: str = "2020-01-01",
                 annual_drift: float = 0.06, annual_vol: float = 0.22,
                 autocorr: float = 0.08):
        self.days = days
        self.start_date = start_date
        self.annual_drift = annual_drift
        self.annual_vol = annual_vol
        self.autocorr = autocorr

    def _seed(self, symbol: str) -> int:
        return int(hashlib.blake2b(symbol.encode(), digest_size=4).hexdigest(), 16)

    def load(self, symbol: str, start: str = "", end: str = "") -> Bars:
        state = self._seed(symbol) or 1
        mu = self.annual_drift / TRADING_DAYS
        sigma = self.annual_vol / math.sqrt(TRADING_DAYS)

        dates = _trading_dates(self.start_date, self.days)
        price = 100.0
        closes: list[float] = []
        prev_shock = 0.0
        for _ in range(self.days):
            # Linear congruential generator → uniform, then Box-Muller-ish normal.
            state = (1103515245 * state + 12345) % (1 << 31)
            u1 = (state or 1) / (1 << 31)
            state = (1103515245 * state + 12345) % (1 << 31)
            u2 = (state or 1) / (1 << 31)
            z = math.sqrt(-2.0 * math.log(max(u1, 1e-12))) * math.cos(2 * math.pi * u2)
            shock = z + self.autocorr * prev_shock      # mild momentum
            prev_shock = z
            price *= math.exp(mu - 0.5 * sigma ** 2 + sigma * shock)
            closes.append(round(price, 4))

        bars = Bars(symbol=symbol, dates=dates, closes=closes)
        if start or end:
            bars = bars.slice(start or None, end or None)
        return bars


# --------------------------------------------------------------------------- #
# CSV (bring your own history)
# --------------------------------------------------------------------------- #
class CsvLoader:
    """Reads `<root>/<SYMBOL>.csv` with a `date` and `close` column (case-insensitive)."""

    name = "csv"

    def __init__(self, root: str = "data"):
        self.root = Path(root)

    def load(self, symbol: str, start: str = "", end: str = "") -> Bars:
        path = self.root / f"{symbol}.csv"
        if not path.exists():
            raise FileNotFoundError(f"no price file for {symbol}: {path}")
        dates: list[str] = []
        closes: list[float] = []
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            fields = {(f or "").strip().lower(): f for f in (reader.fieldnames or [])}
            date_col = fields.get("date") or fields.get("datetime") or fields.get("time")
            close_col = fields.get("close") or fields.get("adj close") or fields.get("price")
            if not date_col or not close_col:
                raise ValueError(f"{path} needs 'date' and 'close' columns; got {reader.fieldnames}")
            for row in reader:
                d = (row[date_col] or "").strip()[:10]
                raw = (row[close_col] or "").strip()
                if not d or not raw:
                    continue
                try:
                    closes.append(float(raw))
                except ValueError:
                    continue
                dates.append(d)
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        bars = Bars(symbol=symbol,
                    dates=[dates[i] for i in order],
                    closes=[closes[i] for i in order])
        return bars.slice(start or None, end or None) if (start or end) else bars


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _trading_dates(start: str, n: int) -> list[str]:
    """Weekday-only date strings, no calendar dependency beyond datetime."""
    from datetime import date, timedelta

    y, m, d = (int(x) for x in start.split("-"))
    cur = date(y, m, d)
    out: list[str] = []
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def split_period(bars: Bars, oos_fraction: float = 0.3) -> tuple[Bars, Bars]:
    """Split into in-sample / out-of-sample halves.

    The Builder only ever sees the in-sample half; the Tester evaluates on the
    out-of-sample half. That separation is what makes the review adversarial in a way
    prose review cannot be (docs/QUANT_TEAM_TUTORIAL.md §5).
    """
    if len(bars) < 20:
        return bars, bars
    cut = int(len(bars) * (1.0 - oos_fraction))
    return (
        Bars(bars.symbol, bars.dates[:cut], bars.closes[:cut]),
        Bars(bars.symbol, bars.dates[cut:], bars.closes[cut:]),
    )
