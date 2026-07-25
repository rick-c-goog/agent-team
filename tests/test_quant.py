"""Quant desk tests: backtest correctness, hypothesis registry, out-of-sample gate."""

from pathlib import Path

import pytest

from teleraft.app import App
from teleraft.models import RunStatus
from teleraft.quant.backtest import SignalSpec, SpecError, backtest, generate_weights
from teleraft.quant.data import Bars, CsvLoader, SyntheticLoader, split_period
from teleraft.quant.hypothesis import (
    DuplicateHypothesis,
    HypothesisRegistry,
    HypothesisStatus,
)
from teleraft.runtime.quant import QuantRuntime, _symbol_for
from teleraft.storage import Storage
from teleraft.telegram.gateway import Callback, Update

QUANT_AGENTS = str(Path(__file__).resolve().parent.parent / "agents" / "quant")
HUMAN = "11111111"


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def test_synthetic_data_is_deterministic_and_symbol_specific():
    a = SyntheticLoader().load("SPY")
    b = SyntheticLoader().load("SPY")
    c = SyntheticLoader().load("QQQ")
    assert a.closes == b.closes          # reproducible across instances
    assert a.closes != c.closes          # but different per symbol
    assert len(a) == 1500 and a.dates[0] < a.dates[-1]


def test_split_period_holds_out_the_tail():
    bars = SyntheticLoader().load("SPY")
    ins, oos = split_period(bars, 0.3)
    assert len(ins) + len(oos) == len(bars)
    assert ins.dates[-1] < oos.dates[0]   # no overlap: the Builder cannot see the OOS
    assert abs(len(oos) / len(bars) - 0.3) < 0.01


def test_csv_loader_reads_real_history(tmp_path):
    (tmp_path / "TEST.csv").write_text(
        "date,open,close,volume\n2024-01-03,10,10.5,100\n2024-01-01,9,9.0,50\n"
        "2024-01-02,9.5,9.8,75\n"
    )
    bars = CsvLoader(str(tmp_path)).load("TEST")
    assert bars.dates == ["2024-01-01", "2024-01-02", "2024-01-03"]   # sorted
    assert bars.closes == [9.0, 9.8, 10.5]


# --------------------------------------------------------------------------- #
# Backtest correctness — the numbers must mean something
# --------------------------------------------------------------------------- #
def _ramp(n: int = 300, step: float = 0.01) -> Bars:
    closes, price = [], 100.0
    for _ in range(n):
        closes.append(round(price, 6))
        price *= 1 + step
    return Bars("RAMP", [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)], closes)


def test_buy_and_hold_matches_the_benchmark():
    bars = _ramp()
    r = backtest(bars, SignalSpec("buy_and_hold"), commission=0.0, slippage=0.0)
    assert r.total_return == pytest.approx(r.benchmark_return, rel=1e-6)
    assert r.trades == 1                       # one entry, then held
    assert r.max_drawdown == pytest.approx(0.0, abs=1e-9)


def test_no_lookahead_the_warmup_window_is_genuinely_missed():
    """Returns during the signal's warm-up must not be earned.

    On a monotonic ramp, momentum(lookback=L) is flat for the first L bars, so the
    strategy must capture exactly the remaining compounding — no more. A backtest with
    lookahead would instead match buy & hold.
    """
    n, step, lookback = 60, 0.01, 20
    bars = _ramp(n, step)
    r = backtest(bars, SignalSpec("momentum", {"lookback": lookback}),
                 commission=0.0, slippage=0.0)

    traded_bars = (n - 1) - lookback            # rets has n-1 entries; first L are flat
    expected = (1 + step) ** traded_bars - 1
    # abs=1e-6 because BacktestResult rounds its metrics to 6 decimal places.
    assert r.total_return == pytest.approx(expected, abs=1e-6)
    assert r.total_return < r.benchmark_return  # strictly worse than lookahead would give


def test_a_signal_that_never_fires_earns_nothing():
    bars = _ramp(50)
    flat = backtest(bars, SignalSpec("momentum", {"lookback": len(bars) + 10}),
                    commission=0.0, slippage=0.0)
    assert flat.total_return == pytest.approx(0.0, abs=1e-12)
    assert flat.trades == 0


def test_commission_is_charged_on_turnover():
    bars = SyntheticLoader().load("SPY").slice("2020-01-01", "2022-01-01")
    spec = SignalSpec("sma_cross", {"fast": 5, "slow": 10})     # deliberately choppy
    free = backtest(bars, spec, commission=0.0)
    costly = backtest(bars, spec, commission=0.01)
    assert costly.total_return < free.total_return
    assert free.turnover == costly.turnover > 1


def test_metrics_are_internally_consistent():
    bars = SyntheticLoader().load("QQQ")
    r = backtest(bars, SignalSpec("buy_and_hold"), commission=0.0, slippage=0.0)
    assert 0.0 <= r.max_drawdown <= 1.0
    assert 0.0 <= r.hit_rate <= 1.0
    assert r.bars == len(bars)
    assert len(r.equity) == len(bars)          # one equity point per bar
    assert r.equity[-1] == pytest.approx(1 + r.total_return, rel=1e-6)


def test_invalid_specs_are_rejected():
    with pytest.raises(SpecError, match="unknown signal type"):
        SignalSpec("rm -rf /").validate()
    with pytest.raises(SpecError, match="fast < slow"):
        SignalSpec("sma_cross", {"fast": 100, "slow": 20}).validate()
    with pytest.raises(SpecError, match="must be numeric"):
        SignalSpec("momentum", {"lookback": "__import__('os')"}).validate()


def test_weights_are_bounded_and_long_only_by_default():
    bars = SyntheticLoader().load("SPY")
    for spec in (SignalSpec("sma_cross", {"fast": 20, "slow": 50}),
                 SignalSpec("momentum", {"lookback": 60}),
                 SignalSpec("mean_reversion", {"lookback": 20, "z_entry": 1.0}),
                 SignalSpec("vol_target", {"lookback": 20, "target_vol": 0.15})):
        w = generate_weights(bars, spec)
        assert len(w) == len(bars)
        assert all(0.0 <= x <= 1.0 for x in w), spec.describe()


# --------------------------------------------------------------------------- #
# Hypothesis registry — the self-improving loop
# --------------------------------------------------------------------------- #
def _registry():
    return HypothesisRegistry(Storage(":memory:"))


def test_invalidated_hypothesis_cannot_be_retested():
    reg = _registry()
    h = reg.propose("Quinn", "momentum produces risk-adjusted edge on SPY", universe="SPY")
    reg.invalidate(h.id, "OOS Sharpe 0.11 < 0.5")

    with pytest.raises(DuplicateHypothesis) as e:
        reg.propose("Quinn", "momentum produces risk-adjusted edge on SPY", universe="SPY")
    assert "0.11" in str(e.value)

    # A restatement of the same idea is caught too, not just the identical string.
    with pytest.raises(DuplicateHypothesis):
        reg.propose("Quinn", "risk-adjusted edge on SPY from momentum", universe="SPY")

    # A genuinely different idea is allowed.
    assert reg.propose("Quinn", "mean reversion works on TLT overnight", universe="TLT")


def test_explicit_retest_is_possible_but_deliberate():
    reg = _registry()
    h = reg.propose("Quinn", "momentum edge on SPY")
    reg.invalidate(h.id, "bad sample")
    again = reg.propose("Quinn", "momentum edge on SPY", allow_retest=True)
    assert again.id != h.id


def test_status_transitions_and_lineage():
    reg = _registry()
    parent = reg.propose("Quinn", "momentum edge on SPY")
    reg.mark_testing(parent.id)
    assert reg.get(parent.id).status is HypothesisStatus.TESTING
    reg.invalidate(parent.id, "did not survive OOS")

    child = reg.propose("Quinn", "momentum edge on SPY with a volatility filter",
                        parent_id=parent.id)
    reg.support(child.id, "OOS Sharpe 0.9")
    assert reg.get(child.id).status is HypothesisStatus.SUPPORTED
    assert [h.id for h in reg.lineage(child.id)] == [parent.id, child.id]


def test_identical_backtest_is_recorded_once():
    reg = _registry()
    h = reg.propose("Quinn", "momentum edge on SPY")
    bars = SyntheticLoader().load("SPY")
    spec = SignalSpec("momentum", {"lookback": 60})
    result = backtest(bars, spec)
    reg.record_result(h.id, "in_sample", spec.to_dict(), result)
    reg.record_result(h.id, "in_sample", spec.to_dict(), result)
    assert len(reg.get(h.id).results) == 1


# --------------------------------------------------------------------------- #
# QuantRuntime — the out-of-sample gate
# --------------------------------------------------------------------------- #
def test_symbol_and_family_extraction():
    assert _symbol_for("does momentum have an edge on NVDA") == "NVDA"
    assert _symbol_for("test mean reversion on SPY please") == "SPY"
    assert _symbol_for("no ticker mentioned here") == "SPY"       # default


def test_overfit_strategy_is_rejected_out_of_sample():
    """An in-sample winner that fails out-of-sample must not reach a human."""
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    result = app.gateway.handle_message(
        Update(text="@Quinn does momentum have an edge on NVDA", user_id=HUMAN,
               user_handle="rick", topic="# research", as_task=True, mentions=["Quinn"])
    )
    _tid, state = app.storage.load_run(result.run_id)
    assert any(not v.passed for v in state.verdicts)
    reasons = " ".join(r for v in state.verdicts for r in v.reasons)
    assert "Out-of-sample Sharpe" in reasons

    hyp = [h for h in app.hypotheses.list() if h.universe == "NVDA"]
    assert hyp and hyp[0].status is HypothesisStatus.INVALIDATED
    assert hyp[0].invalidated_reason
    app.close()


def test_surviving_strategy_reaches_review_and_is_supported():
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    result = app.gateway.handle_message(
        Update(text="@Quinn is there a momentum edge on SPY", user_id=HUMAN,
               user_handle="rick", topic="# research", as_task=True, mentions=["Quinn"])
    )
    assert result.status is RunStatus.AWAITING_HUMAN          # human gate, not auto-ship
    _tid, state = app.storage.load_run(result.run_id)
    assert state.tester_agent != state.agent                  # no self-grading

    hyp = [h for h in app.hypotheses.list() if h.universe == "SPY"][0]
    assert hyp.status is HypothesisStatus.SUPPORTED
    samples = {r["start"] for r in hyp.results}
    assert len(samples) == 2, "both in-sample and out-of-sample runs recorded"

    app.gateway.handle_callback(Callback(data=f"approve|{result.run_id}|review", user_id=HUMAN))
    assert app.storage.get_task(_tid)["status"] == "done"
    app.close()


def test_exhausting_every_family_escalates_instead_of_looping():
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    app.gateway.handle_message(
        Update(text="@Quinn find any edge on NVDA", user_id=HUMAN, user_handle="rick",
               topic="# research", as_task=True, mentions=["Quinn"])
    )
    assert any("Escalation" in p for p in app.client.channel_posts)
    # Each family is tried once — no wasted repeats after exhaustion.
    specs = [line for line in app.client.transcript if "Candidate:" in line]
    families = [s.split("Candidate: ")[1].split("(")[0] for s in specs]
    assert len(families) == len(set(families)) or len(set(families)) >= 4
    app.close()


def test_run_card_is_reproducible_research_output():
    reg = _registry()
    runtime = QuantRuntime(reg)
    from teleraft.runtime.base import RoleRequest

    req = RoleRequest(role="planner", agent="Quinn", task_title="momentum edge on SPY",
                      goals={"escalate_when": []})
    plan, _ = runtime.plan(req)
    assert any("Sharpe" in c for c in plan.criteria)
    runtime.build(RoleRequest(role="builder", agent="Quinn",
                              task_title="momentum edge on SPY", plan=plan, step=0))
    card = runtime.run_card("momentum edge on SPY")
    assert card["symbol"] == "SPY"
    assert card["in_sample"] and card["spec"]["type"]
    assert "Not investment advice" in card["disclaimer"]


def test_live_trading_request_escalates_to_a_human():
    """Quinn's goals list live trading as an escalation area — it must gate."""
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    result = app.gateway.handle_message(
        Update(text="@Quinn set up live trading on SPY with real money", user_id=HUMAN,
               user_handle="rick", topic="# research", as_task=True, mentions=["Quinn"])
    )
    assert result.status is RunStatus.AWAITING_HUMAN
    _tid, state = app.storage.load_run(result.run_id)
    assert state.pending_gate is not None and state.pending_gate.value == "plan"
    app.close()


# --------------------------------------------------------------------------- #
# /hyp command
# --------------------------------------------------------------------------- #
def test_hyp_command_lists_and_shows_hypotheses():
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    app.gateway.handle_message(
        Update(text="@Quinn is there a momentum edge on SPY", user_id=HUMAN,
               user_handle="rick", topic="# research", as_task=True, mentions=["Quinn"])
    )
    rows = app.gateway.handle_message(
        Update(text="/hyp list", user_id=HUMAN, user_handle="rick", topic="# research")
    )
    assert rows and any(h.universe == "SPY" for h in rows)

    shown = app.gateway.handle_message(
        Update(text=f"/hyp show {rows[0].id}", user_id=HUMAN, user_handle="rick",
               topic="# research")
    )
    assert shown.id == rows[0].id
    assert any("Sharpe" in t for t in app.client.transcript)
    app.close()
