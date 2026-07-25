"""Cross-market data & backtesting: conventions, portfolios, currency, fallback chains."""

import math

import pytest

from teleraft.app import App
from teleraft.models import RunStatus
from teleraft.quant.backtest import SignalSpec, apply_settlement, backtest
from teleraft.quant.data import Bars, LoaderRegistry, SyntheticLoader
from teleraft.quant.markets import MARKETS, market_for, resolve_market
from teleraft.quant.portfolio import CurrencyMismatch, backtest_portfolio
from teleraft.runtime.quant import _symbols_for

from .test_quant import QUANT_AGENTS, HUMAN


# --------------------------------------------------------------------------- #
# Market inference and conventions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol,code,currency,periods", [
    ("AAPL", "US", "USD", 252),
    ("SPY.US", "US", "USD", 252),
    ("600519.SS", "CN", "CNY", 243),
    ("000001.SZ", "CN", "CNY", 243),
    ("0700.HK", "HK", "HKD", 246),
    ("BTC-USD", "CRYPTO", "USD", 365),
    ("ETHUSDT", "CRYPTO", "USD", 365),
    ("EURUSD=X", "FX", "USD", 260),
])
def test_market_is_inferred_from_the_ticker_convention(symbol, code, currency, periods):
    m = market_for(symbol)
    assert (m.code, m.currency, m.periods_per_year) == (code, currency, periods)


def test_explicit_market_overrides_inference():
    assert resolve_market("AAPL", "CRYPTO").code == "CRYPTO"
    assert resolve_market("AAPL", MARKETS["HK"]).code == "HK"
    with pytest.raises(KeyError):
        resolve_market("AAPL", "MARS")


def test_annualisation_follows_the_market_not_a_hardcoded_252():
    """The bug this fixes: a crypto Sharpe annualised with 252 is ~20% too low."""
    bars = SyntheticLoader().load("BTC-USD")
    spec = SignalSpec("momentum", {"lookback": 60})
    # Hold costs identical so only the annualisation factor differs.
    crypto = backtest(bars, spec, market="CRYPTO", commission=0.0, slippage=0.0)
    as_equity = backtest(bars, spec, market="US", commission=0.0, slippage=0.0)

    assert crypto.market == "CRYPTO" and as_equity.market == "US"
    expected_ratio = math.sqrt(365 / 252)          # Sharpe scales with sqrt(periods)
    assert crypto.sharpe / as_equity.sharpe == pytest.approx(expected_ratio, rel=1e-2)
    assert crypto.ann_vol / as_equity.ann_vol == pytest.approx(expected_ratio, rel=1e-3)


def test_result_carries_its_market_and_currency():
    r = backtest(SyntheticLoader().load("0700.HK"), SignalSpec("buy_and_hold"))
    assert r.market == "HK" and r.currency == "HKD"
    assert "(HK)" in r.summary()


def test_a_share_short_ban_is_enforced():
    """CN forbids retail shorting, so no weight may go negative even if the spec says so."""
    from teleraft.quant.backtest import generate_weights

    bars = SyntheticLoader().load("600519.SS")
    spec = SignalSpec("sma_cross", {"fast": 20, "slow": 100}, long_only=False)
    raw = generate_weights(bars, spec)
    assert min(raw) < 0, "the spec itself should want to go short"

    r = backtest(bars, spec)                        # market conventions applied
    assert r.market == "CN"
    assert market_for("600519.SS").allows_short is False
    # A long-only run cannot lose more than the asset itself in a crash.
    assert r.exposure >= 0


def test_costs_differ_by_venue():
    bars = SyntheticLoader().load("SPY")
    spec = SignalSpec("sma_cross", {"fast": 5, "slow": 20})   # high turnover
    us = backtest(bars, spec, market="US")
    hk = backtest(bars, spec, market="HK")            # stamp duty makes HK costlier
    assert MARKETS["HK"].round_trip_cost > MARKETS["US"].round_trip_cost
    assert hk.total_return < us.total_return


def test_min_holding_bars_semantics():
    weights = [0, 1, 0, 1, 1, 0, 0]
    # At daily bars the one-bar lag already satisfies T+1, so 1 is a no-op.
    assert apply_settlement(weights, 1) == weights
    # A stricter 2-bar hold does bind.
    assert apply_settlement(weights, 2) == [0, 1, 1, 1, 1, 0, 0]
    assert apply_settlement(weights, 0) == weights


def test_vol_target_sizes_against_the_right_calendar():
    """vol_target reasons in annualised terms, so the calendar changes position size."""
    from teleraft.quant.backtest import generate_weights

    bars = SyntheticLoader().load("BTC-USD")
    spec = SignalSpec("vol_target", {"lookback": 20, "target_vol": 0.15})
    w252 = generate_weights(bars, spec, 252)
    w365 = generate_weights(bars, spec, 365)
    assert w252 != w365, "annualisation must affect vol-targeted sizing"


# --------------------------------------------------------------------------- #
# Portfolio backtesting
# --------------------------------------------------------------------------- #
def _universe(symbols):
    loader = SyntheticLoader()
    return {s: loader.load(s) for s in symbols}


def test_portfolio_shares_one_capital_pool_and_attributes_per_symbol():
    bars = _universe(["SPY", "QQQ", "BTC-USD"])
    p = backtest_portfolio(bars, SignalSpec("momentum", {"lookback": 60}))

    assert sorted(p.symbols) == ["BTC-USD", "QQQ", "SPY"]
    assert set(p.markets) == {"US", "CRYPTO"}
    assert set(p.per_symbol) == set(bars)
    assert all("contribution" in v and "market" in v for v in p.per_symbol.values())
    # Attribution must reconcile *exactly* against the arithmetic return. It does not
    # reconcile against total_return, which compounds — hence both figures exist.
    total_contrib = sum(v["contribution"] for v in p.per_symbol.values())
    assert total_contrib == pytest.approx(p.arithmetic_return, abs=1e-5)
    assert p.total_return > p.arithmetic_return    # compounding on a positive series
    assert len(p.equity) == p.bars


def test_portfolio_and_single_results_share_a_renderable_shape():
    """Anything consuming a stored result must not care which kind it is."""
    single = backtest(SyntheticLoader().load("SPY"), SignalSpec("momentum", {"lookback": 60}))
    port = backtest_portfolio(_universe(["SPY", "QQQ"]),
                              SignalSpec("momentum", {"lookback": 60}))
    common = {"spec", "start", "end", "sharpe", "max_drawdown", "cagr",
              "total_return", "turnover", "benchmark_return"}
    assert common <= set(single.to_dict())
    assert common <= set(port.to_dict())
    assert port.spec == single.spec == "momentum(lookback=60)"


def test_portfolio_spec_label_reflects_per_symbol_specs():
    bars = _universe(["SPY", "QQQ"])
    mixed = {"SPY": SignalSpec("momentum", {"lookback": 60}),
             "QQQ": SignalSpec("sma_cross", {"fast": 20, "slow": 50})}
    assert backtest_portfolio(bars, mixed).spec == "per-symbol specs"


def test_blended_annualisation_sits_between_the_venues():
    p = backtest_portfolio(_universe(["SPY", "BTC-USD"]),
                           SignalSpec("momentum", {"lookback": 60}))
    assert 252 < p.periods_per_year < 365, p.periods_per_year


def test_venues_keep_their_own_calendars():
    """A symbol that did not trade on a date contributes nothing rather than shifting."""
    us = Bars("SPY", ["2024-01-01", "2024-01-02", "2024-01-03"], [100.0, 101.0, 102.0])
    # Crypto trades a day the equity venue did not.
    crypto = Bars("BTC-USD", ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
                  [100.0, 105.0, 103.0, 108.0])
    p = backtest_portfolio({"SPY": us, "BTC-USD": crypto}, SignalSpec("buy_and_hold"))
    assert p.bars == 4                       # union of dates, not intersection
    assert p.start == "2024-01-01" and p.end == "2024-01-04"


def test_cross_currency_portfolio_is_refused_without_fx():
    bars = _universe(["SPY", "0700.HK"])
    with pytest.raises(CurrencyMismatch, match="fx_rates"):
        backtest_portfolio(bars, SignalSpec("buy_and_hold"))


def test_cross_currency_portfolio_works_with_fx_supplied():
    bars = _universe(["SPY", "0700.HK", "600519.SS"])
    p = backtest_portfolio(bars, SignalSpec("momentum", {"lookback": 60}),
                           base_currency="USD",
                           fx_rates={"USD": 1.0, "HKD": 0.128, "CNY": 0.138})
    assert p.base_currency == "USD"
    assert set(p.markets) == {"US", "HK", "CN"}
    assert "USD" in p.summary()


def test_missing_fx_entry_is_an_explicit_error():
    bars = _universe(["SPY", "0700.HK"])
    with pytest.raises(CurrencyMismatch, match="no fx_rates entry"):
        backtest_portfolio(bars, SignalSpec("buy_and_hold"),
                           base_currency="USD", fx_rates={"USD": 1.0})


def test_single_currency_portfolio_needs_no_fx():
    p = backtest_portfolio(_universe(["SPY", "QQQ"]), SignalSpec("buy_and_hold"))
    assert p.base_currency == "USD"


# --------------------------------------------------------------------------- #
# Loader registry — `source: auto` fallback chains
# --------------------------------------------------------------------------- #
class _Broken:
    name = "broken"

    def load(self, symbol, start="", end=""):
        raise ConnectionError("provider down")


class _EmptyProvider:
    name = "empty"

    def load(self, symbol, start="", end=""):
        return Bars(symbol, [], [])


def test_chain_fails_over_to_the_next_provider():
    reg = LoaderRegistry(chains={"US": [_Broken(), _EmptyProvider(), SyntheticLoader()]},
                         default=[SyntheticLoader()])
    bars = reg.load("SPY")
    assert len(bars) > 0
    assert any(f["loader"] == "broken" for f in reg.health())


def test_exhausted_chain_raises_rather_than_returning_an_empty_series():
    reg = LoaderRegistry(chains={"CRYPTO": [_Broken()]}, default=[SyntheticLoader()])
    with pytest.raises(LookupError, match="no loader could supply"):
        reg.load("BTC-USD")


def test_chains_route_by_market():
    us, crypto = SyntheticLoader(), SyntheticLoader()
    reg = LoaderRegistry(chains={"US": [us], "CRYPTO": [crypto]}, default=[])
    assert reg.chain_for("SPY") == [us]
    assert reg.chain_for("BTC-USD") == [crypto]
    assert reg.chain_for("0700.HK") == []          # falls back to the default chain


# --------------------------------------------------------------------------- #
# The research loop over a cross-market universe
# --------------------------------------------------------------------------- #
def test_multiple_tickers_are_parsed_as_a_universe():
    assert _symbols_for("compare momentum on SPY, BTC-USD and 0700.HK") == \
        ["SPY", "BTC-USD", "0700.HK"]
    assert _symbols_for("just SPY please") == ["SPY"]
    assert _symbols_for("no tickers here") == ["SPY"]


def test_cross_market_task_runs_as_a_portfolio_and_records_venues():
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    from teleraft.telegram.gateway import Update

    result = app.gateway.handle_message(
        Update(text="@Quinn is there a momentum edge across SPY, QQQ and BTC-USD",
               user_id=HUMAN, user_handle="rick", topic="# research",
               as_task=True, mentions=["Quinn"])
    )
    _tid, state = app.storage.load_run(result.run_id)
    plan_text = " ".join(state.plan.criteria)
    assert "conventions" in plan_text            # cross-market criterion was added
    hyp = app.hypotheses.list()[0]
    assert "BTC-USD" in hyp.universe and "SPY" in hyp.universe
    app.close()


def test_cross_currency_task_is_blocked_not_silently_summed():
    """The desk must not invent an FX rate to make a number appear."""
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    from teleraft.telegram.gateway import Update

    app.gateway.handle_message(
        Update(text="@Quinn compare momentum on SPY and 0700.HK", user_id=HUMAN,
               user_handle="rick", topic="# research", as_task=True, mentions=["Quinn"])
    )
    transcript = " ".join(app.client.transcript)
    assert "fx_rates" in transcript or "Blocked" in transcript
    app.close()


def test_run_card_records_the_conventions_used():
    from teleraft.quant.hypothesis import HypothesisRegistry
    from teleraft.runtime.base import RoleRequest
    from teleraft.runtime.quant import QuantRuntime
    from teleraft.storage import Storage

    runtime = QuantRuntime(HypothesisRegistry(Storage(":memory:")))
    title = "momentum across SPY and BTC-USD"
    plan, _ = runtime.plan(RoleRequest(role="planner", agent="Quinn", task_title=title,
                                       goals={"escalate_when": []}))
    runtime.build(RoleRequest(role="builder", agent="Quinn", task_title=title,
                              plan=plan, step=0))
    card = runtime.run_card(title)

    assert set(card["venues"]) == {"US", "CRYPTO"}
    assert card["market_conventions"]["BTC-USD"]["periods_per_year"] == 365
    assert card["market_conventions"]["SPY"]["periods_per_year"] == 252
    assert "Not investment advice" in card["disclaimer"]
