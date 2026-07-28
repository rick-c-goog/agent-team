"""Real market-data providers. yfinance first (DESIGN.md §4.1 loader protocol).

Implements the same one-method ``MarketDataLoader`` protocol as the synthetic and CSV
loaders, so every backtest, factor and gate in the system works unchanged — only the
provenance line on the research note changes, from "synthetic … NOT real market data" to
"yfinance (adjusted closes)".

Four correctness details that decide whether real-data results mean anything:

1. **Adjusted closes.** `auto_adjust=True`. A raw close series makes a 2-for-1 split look
   like a −50% day, manufacturing returns that never happened. This is the single most
   common way a backtest on free data lies to you, and it is silent.
2. **Caching.** Downloads are written to disk keyed by symbol and range, so a parameter
   sweep re-reads a file instead of re-hitting Yahoo. Faster, and it keeps a research run
   reproducible even if the upstream series is revised.
3. **Survivorship.** Yahoo serves *currently listed* symbols. A universe assembled from
   today's tickers has already dropped the failures, which inflates any backtest over it.
   The loader cannot fix this; it says so loudly rather than letting it pass unnoticed.
4. **Failing loudly.** A missing package, an unknown ticker, or a series too short to
   conclude from each produce a specific error, never an empty series that a downstream
   gate would happily evaluate.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from .data import Bars

log = logging.getLogger("teleraft.quant.providers")

DEFAULT_CACHE = Path(os.environ.get("TELERAFT_PRICE_CACHE", ".cache/prices"))
MIN_USEFUL_BARS = 60


class ProviderError(RuntimeError):
    """A data provider could not supply a usable series.

    Raised rather than returning an empty series: a downstream gate would happily
    evaluate zero bars and report a confident nothing.
    """


class YFinanceLoader:
    """Daily adjusted closes from Yahoo Finance.

    ``yfinance`` is an unofficial scraper of a free endpoint: it rate-limits, changes
    shape between releases, and occasionally returns partial data. Everything here treats
    it as unreliable — cache aggressively, validate what comes back, and fail with a
    message that names the fix.
    """

    name = "yfinance"

    def __init__(self, start: str = "2015-01-01", end: str = "",
                 cache_dir: Optional[str] = None, use_cache: bool = True,
                 yf: Any = None, warn_survivorship: bool = True):
        self.start = start
        self.end = end
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.use_cache = use_cache
        self.warn_survivorship = warn_survivorship
        self._yf = yf                      # injectable, so tests never hit the network
        self._warned = False

    # ------------------------------------------------------------------ #
    def _module(self):
        if self._yf is not None:
            return self._yf
        try:
            import yfinance                       # noqa: PLC0415 - optional dependency
        except ImportError as e:                  # pragma: no cover
            raise ProviderError(
                "yfinance is not installed. Run:  pip install -e \".[yfinance]\"\n"
                "  (or use the synthetic loader, which needs nothing)"
            ) from e
        self._yf = yfinance
        return self._yf

    def _cache_path(self, symbol: str, start: str, end: str) -> Path:
        safe = symbol.replace("/", "_").replace("\\", "_")
        return self.cache_dir / f"{safe}__{start}__{end or 'latest'}.json"

    def _read_cache(self, path: Path) -> Optional[Bars]:
        if not (self.use_cache and path.exists()):
            return None
        try:
            payload = json.loads(path.read_text())
            return Bars(symbol=payload["symbol"], dates=payload["dates"],
                        closes=payload["closes"])
        except Exception:
            log.warning("ignoring unreadable price cache %s", path)
            return None

    def _write_cache(self, path: Path, bars: Bars) -> None:
        if not self.use_cache:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"symbol": bars.symbol, "dates": bars.dates, "closes": bars.closes}))
        except OSError as e:
            log.warning("could not write price cache %s: %s", path, e)

    # ------------------------------------------------------------------ #
    def load(self, symbol: str, start: str = "", end: str = "") -> Bars:
        start = start or self.start
        end = end or self.end

        cache_path = self._cache_path(symbol, start, end)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        yf = self._module()
        if self.warn_survivorship and not self._warned:
            self._warned = True
            log.warning(
                "survivorship bias: yfinance serves currently-listed symbols only, so a "
                "universe of today's tickers has already dropped the failures, which "
                "inflates any backtest over it. Use a point-in-time universe for "
                "anything you intend to act on."
            )

        try:
            frame = yf.download(symbol, start=start, end=end or None,
                                auto_adjust=True,     # splits/dividends — see module docs
                                progress=False, threads=False)
        except Exception as e:
            raise ProviderError(f"yfinance download of {symbol} failed: "
                                f"{type(e).__name__}: {e}") from e

        bars = _frame_to_bars(symbol, frame)
        if len(bars) == 0:
            raise ProviderError(
                f"yfinance returned no data for {symbol!r} between {start} and "
                f"{end or 'today'}. Check the ticker (Yahoo uses suffixes such as "
                "'0700.HK', 'BTC-USD', 'EURUSD=X'), or the symbol may be delisted."
            )
        if len(bars) < MIN_USEFUL_BARS:
            raise ProviderError(
                f"{symbol} returned only {len(bars)} bars — too short to conclude "
                f"anything. Widen the window or drop the symbol."
            )
        self._write_cache(cache_path, bars)
        return bars

    def load_universe(self, symbols: Sequence[str], start: str = "", end: str = ""
                      ) -> tuple[dict[str, Bars], dict[str, str]]:
        """Load many symbols, returning (loaded, failed → why).

        A universe where one ticker is wrong should not abort the run; the factor
        producers need to know which names they actually have.
        """
        loaded: dict[str, Bars] = {}
        failed: dict[str, str] = {}
        for symbol in symbols:
            try:
                loaded[symbol] = self.load(symbol, start, end)
            except ProviderError as e:
                failed[symbol] = str(e).splitlines()[0]
                log.warning("skipping %s: %s", symbol, failed[symbol])
        return loaded, failed


# --------------------------------------------------------------------------- #
def _frame_to_bars(symbol: str, frame: Any) -> Bars:
    """Convert a yfinance DataFrame to Bars without importing pandas ourselves.

    yfinance returns a MultiIndex column frame for multi-symbol downloads and a flat one
    for a single symbol; both shapes appear in the wild depending on version, so pick the
    close column defensively rather than by position.
    """
    if frame is None or len(frame) == 0:
        return Bars(symbol=symbol, dates=[], closes=[])

    columns = list(frame.columns)
    close_col = None
    for candidate in columns:
        label = candidate[0] if isinstance(candidate, tuple) else candidate
        if str(label).lower() in ("close", "adj close"):
            close_col = candidate
            break
    if close_col is None:
        raise ProviderError(
            f"yfinance frame for {symbol} has no Close column (got {columns}) — "
            "the library's response shape may have changed"
        )

    dates: list[str] = []
    closes: list[float] = []
    for index, value in zip(frame.index, frame[close_col]):
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue                                  # NaN holiday/halted row
        if price != price or price <= 0:              # NaN or non-positive
            continue
        stamp = getattr(index, "strftime", None)
        dates.append(stamp("%Y-%m-%d") if stamp else str(index)[:10])
        closes.append(price)

    return Bars(symbol=symbol, dates=dates, closes=closes)


def build_loader(source: str = "synthetic", **kwargs):
    """Resolve a loader by name — the seam config and the demos use."""
    from .data import CsvLoader, SyntheticLoader

    source = (source or "synthetic").strip().lower()
    if source in ("yfinance", "yahoo"):
        return YFinanceLoader(**kwargs)
    if source == "csv":
        return CsvLoader(kwargs.get("root", "data"))
    if source == "synthetic":
        return SyntheticLoader()
    raise ValueError(
        f"unknown data source {source!r}; choose from: synthetic, csv, yfinance"
    )
