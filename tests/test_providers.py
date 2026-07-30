"""The yfinance loader, exercised without ever touching the network.

A fake `yfinance` module returns frame-shaped objects, so the real conversion, caching,
validation and error paths all run — the parts that actually break in production.
"""

import math
import os
from datetime import date, timedelta

import pytest

from teleraft.quant.data import Bars
from teleraft.quant.providers import (
    MIN_USEFUL_BARS,
    ProviderError,
    YFinanceLoader,
    build_loader,
)
from teleraft.runtime.quant import QuantRuntime
from teleraft.quant.hypothesis import HypothesisRegistry
from teleraft.storage import Storage


# --------------------------------------------------------------------------- #
# A minimal stand-in for a pandas frame: indexable columns and an index.
# --------------------------------------------------------------------------- #
class _Stamp:
    def __init__(self, text):
        self.text = text

    def strftime(self, _fmt):
        return self.text


class _Frame:
    def __init__(self, dates, closes, close_label="Close"):
        self.index = [_Stamp(d) for d in dates]
        self.columns = [close_label, "Volume"]
        self._data = {close_label: closes, "Volume": [1] * len(closes)}

    def __len__(self):
        return len(self.index)

    def __getitem__(self, key):
        return self._data[key]


class _FakeYF:
    """Records what it was asked for, so the request itself can be asserted."""

    def __init__(self, frame=None, raises=None):
        self.frame = frame
        self.raises = raises
        self.calls = []

    def download(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        if self.raises:
            raise self.raises
        return self.frame


def _frame(n=300, start=100.0):
    """A plausible daily series: real weekday dates and a deterministic random walk.

    Both details matter. Naive month/day arithmetic overflows into impossible dates like
    `2024-23-14` past ~336 bars, and a fixture that emits those could hide a genuine
    date-handling bug. A pure exponential ramp has no volatility, so anything asserting
    on a Sharpe ratio would see ~85 and look fine while proving nothing.
    """
    day = date(2020, 1, 1)
    dates, closes, price, seed = [], [], start, 12345
    while len(dates) < n:
        if day.weekday() < 5:                      # skip weekends, like a real feed
            seed = (1103515245 * seed + 12345) % (2 ** 31)     # deterministic LCG
            price *= 1.0 + ((seed / 2 ** 31) - 0.49) * 0.02
            dates.append(day.isoformat())
            closes.append(price)
        day += timedelta(days=1)
    return _Frame(dates, closes)


# --------------------------------------------------------------------------- #
# The correctness detail that matters most
# --------------------------------------------------------------------------- #
def test_adjusted_closes_are_always_requested(tmp_path):
    """Raw closes make a 2-for-1 split look like a -50% day. This must never be off."""
    yf = _FakeYF(_frame())
    YFinanceLoader(yf=yf, cache_dir=str(tmp_path)).load("SPY")
    _symbol, kwargs = yf.calls[0]
    assert kwargs["auto_adjust"] is True


def test_a_frame_becomes_bars_with_iso_dates(tmp_path):
    bars = YFinanceLoader(yf=_FakeYF(_frame()), cache_dir=str(tmp_path)).load("SPY")
    assert isinstance(bars, Bars) and bars.symbol == "SPY"
    assert len(bars) == 300
    assert bars.dates[0] == "2020-01-01"
    assert all(isinstance(c, float) and c > 0 for c in bars.closes)


def test_adj_close_column_is_also_accepted(tmp_path):
    """yfinance's column shape varies by version; pick the close defensively."""
    frame = _Frame(["2024-01-01", "2024-01-02"] * 40,
                   [100.0 + i for i in range(80)], close_label="Adj Close")
    bars = YFinanceLoader(yf=_FakeYF(frame), cache_dir=str(tmp_path)).load("SPY")
    assert len(bars) == 80


def test_multiindex_columns_are_handled(tmp_path):
    frame = _frame(120)
    frame.columns = [("Close", "SPY"), ("Volume", "SPY")]
    frame._data = {("Close", "SPY"): [100.0 + i for i in range(120)],
                   ("Volume", "SPY"): [1] * 120}
    bars = YFinanceLoader(yf=_FakeYF(frame), cache_dir=str(tmp_path)).load("SPY")
    assert len(bars) == 120


def test_nan_and_non_positive_rows_are_dropped(tmp_path):
    frame = _Frame([f"2024-01-{i+1:02d}" for i in range(70)],
                   [100.0] * 68 + [float("nan"), 0.0])
    bars = YFinanceLoader(yf=_FakeYF(frame), cache_dir=str(tmp_path)).load("SPY")
    assert len(bars) == 68
    assert all(c > 0 and not math.isnan(c) for c in bars.closes)


# --------------------------------------------------------------------------- #
# Failing loudly rather than returning an empty series
# --------------------------------------------------------------------------- #
def test_an_unknown_ticker_raises_with_the_ticker_conventions(tmp_path):
    loader = YFinanceLoader(yf=_FakeYF(_Frame([], [])), cache_dir=str(tmp_path))
    with pytest.raises(ProviderError) as e:
        loader.load("NOT_A_TICKER")
    message = str(e.value)
    assert "no data" in message and "0700.HK" in message   # names the convention


def test_a_series_too_short_to_conclude_from_is_rejected(tmp_path):
    frame = _frame(MIN_USEFUL_BARS - 1)
    loader = YFinanceLoader(yf=_FakeYF(frame), cache_dir=str(tmp_path))
    with pytest.raises(ProviderError, match="too short to conclude"):
        loader.load("SPY")


def test_a_download_error_is_wrapped_not_leaked(tmp_path):
    loader = YFinanceLoader(yf=_FakeYF(raises=ConnectionError("yahoo is down")),
                            cache_dir=str(tmp_path))
    with pytest.raises(ProviderError, match="yahoo is down"):
        loader.load("SPY")


def test_a_frame_without_a_close_column_is_reported(tmp_path):
    frame = _Frame([f"2024-01-{i+1:02d}" for i in range(70)], [100.0] * 70,
                   close_label="Open")
    with pytest.raises(ProviderError, match="no Close column"):
        YFinanceLoader(yf=_FakeYF(frame), cache_dir=str(tmp_path)).load("SPY")


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
def test_a_second_load_is_served_from_cache(tmp_path):
    yf = _FakeYF(_frame())
    loader = YFinanceLoader(yf=yf, cache_dir=str(tmp_path))
    first = loader.load("SPY")
    second = loader.load("SPY")
    assert len(yf.calls) == 1, "a parameter sweep must not re-hit Yahoo per run"
    assert first.closes == second.closes


def test_the_cache_survives_a_new_loader_instance(tmp_path):
    YFinanceLoader(yf=_FakeYF(_frame()), cache_dir=str(tmp_path)).load("SPY")
    cold = _FakeYF(_frame())
    YFinanceLoader(yf=cold, cache_dir=str(tmp_path)).load("SPY")
    assert cold.calls == [], "the on-disk cache should serve a fresh process"


def test_caching_can_be_turned_off(tmp_path):
    yf = _FakeYF(_frame())
    loader = YFinanceLoader(yf=yf, cache_dir=str(tmp_path), use_cache=False)
    loader.load("SPY")
    loader.load("SPY")
    assert len(yf.calls) == 2


def test_a_corrupt_cache_file_falls_back_to_downloading(tmp_path):
    yf = _FakeYF(_frame())
    loader = YFinanceLoader(yf=yf, cache_dir=str(tmp_path))
    loader.load("SPY")
    next(tmp_path.glob("*.json")).write_text("{ not json")
    loader.load("SPY")                     # must not raise
    assert len(yf.calls) == 2


def test_different_date_ranges_are_cached_separately(tmp_path):
    yf = _FakeYF(_frame())
    loader = YFinanceLoader(yf=yf, cache_dir=str(tmp_path))
    loader.load("SPY", start="2020-01-01")
    loader.load("SPY", start="2021-01-01")
    assert len(yf.calls) == 2


# --------------------------------------------------------------------------- #
# Universes
# --------------------------------------------------------------------------- #
def test_a_bad_ticker_does_not_abort_the_whole_universe(tmp_path):
    class _Selective(_FakeYF):
        def download(self, symbol, **kwargs):
            self.calls.append((symbol, kwargs))
            return _frame() if symbol != "BAD" else _Frame([], [])

    loader = YFinanceLoader(yf=_Selective(), cache_dir=str(tmp_path))
    loaded, failed = loader.load_universe(["AAPL", "BAD", "MSFT"])
    assert set(loaded) == {"AAPL", "MSFT"}
    assert "BAD" in failed and "no data" in failed["BAD"]


def test_survivorship_is_warned_about_once(tmp_path, caplog):
    loader = YFinanceLoader(yf=_FakeYF(_frame()), cache_dir=str(tmp_path))
    with caplog.at_level("WARNING"):
        loader.load("AAPL")
        loader.load("MSFT")
    assert caplog.text.count("survivorship") == 1, "warn, but do not nag"


# --------------------------------------------------------------------------- #
# Provenance and selection
# --------------------------------------------------------------------------- #
def test_provenance_names_yfinance_and_its_caveats(tmp_path):
    runtime = QuantRuntime(HypothesisRegistry(Storage(":memory:")),
                           loader=YFinanceLoader(yf=_FakeYF(_frame()),
                                                 cache_dir=str(tmp_path)))
    provenance = runtime.data_provenance()
    assert "yfinance" in provenance
    assert "adjusted closes" in provenance
    assert "survivorship" in provenance
    assert "NOT real market data" not in provenance


def test_build_loader_resolves_by_name():
    assert build_loader("synthetic").name == "synthetic"
    assert build_loader("yfinance").name == "yfinance"
    assert build_loader("yahoo").name == "yfinance"
    assert build_loader("csv", root="data").name == "csv"
    with pytest.raises(ValueError, match="unknown data source"):
        build_loader("bloomberg")


def test_the_missing_package_message_names_the_install(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def no_yfinance(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("No module named 'yfinance'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yfinance)
    with pytest.raises(ProviderError, match=r"\[yfinance\]"):
        YFinanceLoader(cache_dir=str(tmp_path)).load("SPY")


# --------------------------------------------------------------------------- #
# It is a drop-in: the rest of the stack does not know the difference
# --------------------------------------------------------------------------- #
def test_a_backtest_runs_unchanged_on_provider_data(tmp_path):
    from teleraft.quant.backtest import SignalSpec, backtest

    bars = YFinanceLoader(yf=_FakeYF(_frame(400)), cache_dir=str(tmp_path)).load("SPY")
    result = backtest(bars, SignalSpec("momentum", {"lookback": 60}))
    assert result.bars == 400 and result.market == "US"


def test_the_factor_graph_runs_on_provider_data(tmp_path):
    from teleraft.pipeline import PipelineEngine
    from teleraft.quant.factor_pipeline import build_factor_pipeline

    loader = YFinanceLoader(yf=_FakeYF(_frame(400)), cache_dir=str(tmp_path))
    universe = {s: loader.load(s) for s in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]}
    result = PipelineEngine(Storage(":memory:")).run(build_factor_pipeline(universe))
    assert len(result.items) == 7          # all seven factors accounted for
    assert {i.subject for i in result.blocked_items} >= {"HML", "RMW", "CMA", "SMB"}


# --------------------------------------------------------------------------- #
# Opt-in: the only test that proves the fake frame above matches the real library.
#
#   TELERAFT_LIVE_DATA=1 pytest tests/test_providers.py -k live
#
# Kept out of the default suite deliberately — the rest of this repo's tests are
# offline and deterministic, and a network test would make CI fail for reasons that
# have nothing to do with the code.
# --------------------------------------------------------------------------- #
live = pytest.mark.skipif(
    not os.environ.get("TELERAFT_LIVE_DATA"),
    reason="set TELERAFT_LIVE_DATA=1 to run tests that hit Yahoo Finance",
)


@live
def test_live_a_real_split_is_adjusted_away(tmp_path):
    """AAPL split 4-for-1 on 2020-08-31. Unadjusted that is a −75% day.

    This is the assertion the fake-frame tests cannot make: it proves `auto_adjust`
    reaches the real library and does what we claim.
    """
    bars = YFinanceLoader(start="2020-01-01", end="2021-01-01",
                          cache_dir=str(tmp_path)).load("AAPL")
    i = bars.dates.index("2020-08-31")
    split_day_return = bars.closes[i] / bars.closes[i - 1] - 1
    assert -0.10 < split_day_return < 0.10, (
        f"split day returned {split_day_return:.1%} — adjustment is not being applied"
    )
    assert len(bars) > 200


@live
def test_live_a_universe_loads_and_feeds_the_factor_graph(tmp_path):
    from teleraft.pipeline import PipelineEngine
    from teleraft.quant.factor_pipeline import build_factor_pipeline

    loader = YFinanceLoader(start="2018-01-01", cache_dir=str(tmp_path))
    universe, failed = loader.load_universe(
        ["AAPL", "MSFT", "JNJ", "XOM", "JPM", "PG", "KO", "WMT"])
    assert len(universe) >= 6, f"too few symbols loaded (failed: {failed})"
    result = PipelineEngine(Storage(":memory:")).run(build_factor_pipeline(universe))
    assert len(result.items) == 7


# --------------------------------------------------------------------------- #
# Config wiring: the source has to survive the trip from TOML to the runtime.
# --------------------------------------------------------------------------- #
def test_market_data_config_is_read_from_toml(tmp_path):
    from teleraft.config import load_config

    cfg_file = tmp_path / "teleraft.toml"
    cfg_file.write_text(
        '[market_data]\nsource = "yfinance"\nstart = "2010-01-01"\ncache = "/tmp/px"\n')
    cfg = load_config(str(cfg_file))
    assert cfg.data_source == "yfinance"
    assert cfg.data_start == "2010-01-01"
    assert cfg.price_cache == "/tmp/px"


def test_an_unknown_source_is_rejected_at_startup(tmp_path):
    from teleraft.config import load_config

    cfg_file = tmp_path / "teleraft.toml"
    cfg_file.write_text('[market_data]\nsource = "bloomberg"\n')
    with pytest.raises(ValueError, match="synthetic, csv or yfinance"):
        load_config(str(cfg_file))


def test_the_shipped_example_config_parses():
    """The file users copy must stay loadable as the schema grows."""
    from teleraft.config import load_config

    cfg = load_config("teleraft.toml.example")
    assert cfg.data_source == "synthetic"      # safe default: no network, no surprises


def test_the_app_hands_its_loader_to_every_quant_agent(tmp_path):
    """A configured source must actually reach the runtime, not just the config object."""
    from pathlib import Path

    from teleraft.app import App

    loader = YFinanceLoader(yf=_FakeYF(_frame(400)), cache_dir=str(tmp_path))
    agents = str(Path(__file__).resolve().parent.parent / "agents" / "quant")
    app = App(agents_dir=agents, human_ids={"1"}, market_loader=loader,
              sync_knowledge=False)
    runtime = app._runtime_for("Quinn")
    assert runtime.name == "quant"
    assert runtime.loader is loader
    assert "yfinance" in runtime.data_provenance()


def test_no_loader_falls_back_to_synthetic(tmp_path):
    from pathlib import Path

    from teleraft.app import App

    agents = str(Path(__file__).resolve().parent.parent / "agents" / "quant")
    app = App(agents_dir=agents, human_ids={"1"}, sync_knowledge=False)
    assert "NOT real market data" in app._runtime_for("Quinn").data_provenance()


def _desk_run(loader, tmp_path):
    """Drive one real research task and return every message the desk emitted."""
    from pathlib import Path

    from teleraft.app import App
    from teleraft.telegram.gateway import Update

    agents = str(Path(__file__).resolve().parent.parent / "agents" / "quant")
    app = App(human_ids={"11111111"}, agents_dir=agents, market_loader=loader,
              sync_knowledge=False)
    app.gateway.handle_message(
        Update(text="@Quinn is there a momentum edge on SPY", user_id="11111111",
               user_handle="rick", topic="# research", as_task=True,
               mentions=["Quinn"]))
    messages = [m.text for m in app.client.messages.values()]
    app.close()
    return messages


def test_the_survivorship_caveat_reaches_what_the_human_reads(tmp_path):
    """DESIGN.md §11.1 claims the bias is stamped on the artifact, not just logged.

    A warning in a server log is invisible to the person deciding what to do. The claim
    has to hold on whichever message the desk ends on — the review card when an edge
    survives, the escalation when nothing does. On a realistic random walk it is usually
    the escalation, which is exactly the case a provenance line is easiest to forget.
    """
    loader = YFinanceLoader(yf=_FakeYF(_frame(900)), cache_dir=str(tmp_path))
    messages = _desk_run(loader, tmp_path)

    verdicts = [m for m in messages if "In Review" in m or "Escalation" in m]
    assert verdicts, "the desk ended without a review card or an escalation"
    assert any("yfinance" in m and "survivorship" in m for m in verdicts), (
        "no verdict message named the data source and its bias:\n"
        + "\n--\n".join(verdicts))
    assert not any("NOT real market data" in m for m in verdicts), \
        "real data must not be labelled synthetic"


def test_a_negative_result_on_synthetic_data_says_so(tmp_path):
    """The inverse case: 'no edge found' on pseudo-prices must not read as a market fact."""
    messages = _desk_run(None, tmp_path)
    verdicts = [m for m in messages if "In Review" in m or "Escalation" in m]
    assert verdicts
    assert any("NOT real market data" in m for m in verdicts), (
        "a synthetic-data verdict did not disclose that it is not real market data:\n"
        + "\n--\n".join(verdicts))
