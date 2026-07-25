"""Market conventions — the part of cross-market research that decides correctness.

A backtest that applies US equity conventions to a crypto series is not "roughly right";
it is wrong in a specific, quantifiable way. Crypto trades ~365 days a year, equities
~252, so annualising a crypto Sharpe with 252 understates it by ~24%. Chinese A-shares
settle T+1, so a strategy that round-trips intraday cannot be executed at all. Costs and
currencies differ by an order of magnitude across venues.

This module makes those conventions explicit and attaches them to every result, so a
number always carries the assumptions that produced it.

Adding a market is adding a row here — the conventions are data, not scattered
constants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Market:
    code: str                  # US | HK | CN | CRYPTO | FX
    name: str
    currency: str
    periods_per_year: int      # annualisation factor for vol and Sharpe
    commission: float          # per unit of turnover, one way
    slippage: float            # per unit of turnover, one way
    min_holding_bars: int      # bars a position must be held after opening (see note)
    allows_short: bool
    note: str = ""

    @property
    def round_trip_cost(self) -> float:
        return self.commission + self.slippage


# Conventions are approximate but defensible defaults; override per venue if you have
# better numbers for your broker (see docs/QUANT_TEAM_TUTORIAL.md §9).
MARKETS: dict[str, Market] = {
    "US": Market(
        code="US", name="US equities", currency="USD", periods_per_year=252,
        commission=0.0005, slippage=0.0005, min_holding_bars=0, allows_short=True,
    ),
    "HK": Market(
        code="HK", name="Hong Kong equities", currency="HKD", periods_per_year=246,
        commission=0.0011, slippage=0.0008, min_holding_bars=0, allows_short=True,
        note="stamp duty + trading fees make HK costlier than US",
    ),
    "CN": Market(
        code="CN", name="China A-shares", currency="CNY", periods_per_year=243,
        commission=0.0008, slippage=0.0010, min_holding_bars=1, allows_short=False,
        note="T+1 settlement and no retail shorting. At daily bars T+1 is already "
             "satisfied by the one-bar signal lag; the binding constraints here are "
             "the short ban and the higher costs. min_holding_bars matters if you "
             "feed intraday bars.",
    ),
    "CRYPTO": Market(
        code="CRYPTO", name="Crypto", currency="USD", periods_per_year=365,
        commission=0.0010, slippage=0.0015, min_holding_bars=0, allows_short=True,
        note="trades every day of the year — annualising with 252 is simply wrong",
    ),
    "FX": Market(
        code="FX", name="Foreign exchange", currency="USD", periods_per_year=260,
        commission=0.0001, slippage=0.0002, min_holding_bars=0, allows_short=True,
    ),
}

DEFAULT_MARKET = "US"

# Symbol conventions per venue. Order matters: the first match wins.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("CN", re.compile(r"\.(SS|SZ|SH)$", re.I)),               # 600519.SS, 000001.SZ
    ("HK", re.compile(r"\.HK$", re.I)),                        # 0700.HK
    ("HK", re.compile(r"^\d{4,5}$")),                          # bare HK board lot codes
    ("CRYPTO", re.compile(r"(-USD|-USDT|/USDT|/USD|USDT)$", re.I)),   # BTC-USD, ETHUSDT
    ("FX", re.compile(r"^[A-Z]{6}(=X)?$")),                    # EURUSD, EURUSD=X
    ("US", re.compile(r"\.US$", re.I)),
]

_CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "LTC", "AVAX", "DOT"}


def market_for(symbol: str) -> Market:
    """Infer the market from the ticker convention (Vibe-Trading's `source: auto` idea).

    Explicit beats inferred: pass a Market directly when your symbols do not follow a
    recognisable convention.
    """
    s = (symbol or "").strip()
    if not s:
        return MARKETS[DEFAULT_MARKET]
    for code, pattern in _PATTERNS:
        if pattern.search(s):
            return MARKETS[code]
    if s.upper() in _CRYPTO_BASES:
        return MARKETS["CRYPTO"]
    return MARKETS[DEFAULT_MARKET]


def resolve_market(symbol: str, override: Optional[Market | str] = None) -> Market:
    if isinstance(override, Market):
        return override
    if isinstance(override, str) and override:
        key = override.upper()
        if key not in MARKETS:
            raise KeyError(f"unknown market {override!r}; known: {', '.join(MARKETS)}")
        return MARKETS[key]
    return market_for(symbol)


def currencies_of(symbols: list[str]) -> set[str]:
    return {market_for(s).currency for s in symbols}
