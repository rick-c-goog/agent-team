"""The eleven-node factor graph (DESIGN.md Appendix A) and its estimators."""

import math
import random

import pytest

from teleraft.pipeline import PipelineEngine
from teleraft.quant.data import SyntheticLoader
from teleraft.quant.factor_pipeline import FactorGateConfig, build_factor_pipeline
from teleraft.quant.factors import FACTORS, FUNDAMENTALS, SHARES, construct
from teleraft.quant.stats import (
    bootstrap_pvalue,
    degradation,
    naive_tstat,
    newey_west_tstat,
    ols,
    regime_means,
    volatility_regimes,
)
from teleraft.storage import Storage

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ",
            "KKK", "LLL"]


def _universe(symbols=None):
    loader = SyntheticLoader()
    return {s: loader.load(s) for s in (symbols or UNIVERSE)}


def _engine():
    return PipelineEngine(Storage(":memory:"))


# --------------------------------------------------------------------------- #
# Estimators — known answers
# --------------------------------------------------------------------------- #
def test_newey_west_discounts_autocorrelation():
    """The whole reason for HAC: a naive t-stat overstates autocorrelated data."""
    rnd = random.Random(7)
    series, prev = [], 0.0
    for _ in range(500):
        shock = rnd.gauss(0, 1)
        series.append((0.02 + 0.7 * prev + shock) / 100)
        prev = shock
    assert abs(newey_west_tstat(series)) < abs(naive_tstat(series))


def test_newey_west_matches_naive_on_independent_data():
    rnd = random.Random(11)
    series = [rnd.gauss(0.05, 1) for _ in range(600)]
    assert newey_west_tstat(series) == pytest.approx(naive_tstat(series), rel=0.35)


def test_bootstrap_separates_signal_from_noise():
    rnd = random.Random(3)
    noise = [rnd.gauss(0, 1) for _ in range(300)]
    signal = [rnd.gauss(0.5, 1) for _ in range(300)]
    assert bootstrap_pvalue(noise, 1000) > 0.05
    assert bootstrap_pvalue(signal, 1000) < 0.01


def test_bootstrap_is_reproducible():
    rnd = random.Random(5)
    series = [rnd.gauss(0.1, 1) for _ in range(200)]
    assert bootstrap_pvalue(series, 500) == bootstrap_pvalue(series, 500)


@pytest.mark.parametrize("in_s,out_s,expected", [
    (1.0, 0.9, 0.10), (1.0, 0.5, 0.50), (1.0, 1.2, 0.0), (-0.1, 0.5, 1.0),
])
def test_degradation(in_s, out_s, expected):
    assert degradation(in_s, out_s) == pytest.approx(expected, abs=1e-9)


def test_volatility_regimes_partition_the_series():
    rnd = random.Random(13)
    series = [rnd.gauss(0, 1 if i < 600 else 4) for i in range(1200)]
    regimes = volatility_regimes(series)
    assert len(regimes) == 3
    assert sum(len(r) for r in regimes) <= len(series)
    assert all(len(r) > 0 for r in regimes)
    assert set(regime_means(series, regimes)) == {"calm", "normal", "stressed"}


def test_ols_recovers_known_coefficients_and_alpha_significance():
    rnd = random.Random(17)
    n = 400
    f1 = [rnd.gauss(0, 1) for _ in range(n)]
    f2 = [rnd.gauss(0, 1) for _ in range(n)]
    y = [0.5 + 2.0 * f1[i] - 1.0 * f2[i] + rnd.gauss(0, 0.1) for i in range(n)]

    reg = ols(y, [f1, f2])
    assert reg.alpha == pytest.approx(0.5, abs=0.05)
    assert reg.betas[0] == pytest.approx(2.0, abs=0.05)
    assert reg.betas[1] == pytest.approx(-1.0, abs=0.05)
    assert reg.alpha_tstat > 10, "a real intercept must be significant"
    assert reg.r_squared > 0.99


def test_regressing_a_series_on_itself_gives_zero_alpha_by_construction():
    """Appendix A.2's warning, as arithmetic — this is why node 11 needs an
    *independent* benchmark."""
    rnd = random.Random(19)
    y = [rnd.gauss(0.3, 1) for _ in range(200)]
    reg = ols(y, [y])
    assert reg.alpha == pytest.approx(0.0, abs=1e-9)
    assert reg.r_squared == pytest.approx(1.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# Factor construction — nodes 1–7
# --------------------------------------------------------------------------- #
def test_the_seven_factors_are_declared_with_their_data_requirements():
    assert [f.node for f in FACTORS] == [1, 2, 3, 4, 5, 6, 7]
    by_name = {f.name: f for f in FACTORS}
    assert by_name["MKT"].computable_from_prices
    assert by_name["MOM"].computable_from_prices
    assert by_name["LVOL"].computable_from_prices
    assert SHARES in by_name["SMB"].requires
    for name in ("HML", "RMW", "CMA"):
        assert FUNDAMENTALS in by_name[name].requires, name


def test_price_only_data_builds_three_factors_and_blocks_four():
    built, blocked = construct(_universe())
    assert set(built) == {"MKT", "MOM", "LVOL"}
    assert set(blocked) == {"SMB", "HML", "RMW", "CMA"}
    assert all("needs" in why for why in blocked.values())


def test_supplying_fundamentals_is_what_unblocks_them():
    """The block is about data, not about the code being unwritten."""
    _built, blocked = construct(_universe(), available={FUNDAMENTALS})
    assert "SMB" in blocked, "SMB needs shares outstanding, a different input"
    # HML/RMW/CMA are no longer blocked *for want of fundamentals* — they now report
    # that no builder is wired, which is an honest and different message.
    assert all("point-in-time fundamentals" not in blocked.get(n, "")
               for n in ("HML", "RMW", "CMA"))


def test_node_one_emits_per_stock_betas_as_well_as_the_factor():
    built, _ = construct(_universe())
    assert built["MKT"].per_stock, "node 10 needs these even if MKT is killed"
    assert set(built["MKT"].per_stock) == set(UNIVERSE)


# --------------------------------------------------------------------------- #
# The graph end to end
# --------------------------------------------------------------------------- #
def test_the_graph_blocks_the_four_fundamentals_factors():
    result = _engine().run(build_factor_pipeline(_universe()))
    blocked = {i.subject for i in result.blocked_items}
    assert {"SMB", "HML", "RMW", "CMA"} <= blocked
    assert all("needs" in i.kill_reason for i in result.blocked_items)


def test_the_validator_kills_with_a_number_not_an_opinion():
    result = _engine().run(build_factor_pipeline(_universe()))
    killed = result.killed
    assert killed, "on synthetic data most factors should fail the bar"
    for item in killed:
        assert any(marker in item.kill_reason
                   for marker in ("Newey–West t", "bootstrap p", "degradation")), \
            item.kill_reason


def test_node_eleven_blocks_without_an_independent_benchmark():
    """Attribution against nothing is not attribution."""
    result = _engine().run(build_factor_pipeline(_universe()))
    assert not result.graduated
    assert any("no independent benchmark" in r for r in result.reasons)


def test_a_benchmark_overlapping_the_constructed_factors_is_refused():
    """Appendix A.2: the regressors would span the portfolio, so alpha is zero by
    construction. Refuse rather than report a meaningless number."""
    universe = _universe()
    built, _ = construct(universe)
    lenient = FactorGateConfig(min_tstat=0.0, max_p_value=1.0, max_degradation=99.0,
                               min_regimes_working=1, bootstrap_iterations=200)
    result = _engine().run(build_factor_pipeline(
        universe, benchmark={k: v.returns for k, v in built.items()}, config=lenient))
    assert any("would span the portfolio" in r for r in result.reasons)
    assert not result.graduated


def test_an_independent_benchmark_lets_attribution_run():
    universe = _universe()
    bench_built, _ = construct(_universe(["ZZ1", "ZZ2", "ZZ3", "ZZ4", "ZZ5", "ZZ6",
                                          "ZZ7", "ZZ8", "ZZ9"]))
    benchmark = {f"BM_{k}": v.returns for k, v in bench_built.items()}
    lenient = FactorGateConfig(min_tstat=0.0, max_p_value=1.0, max_degradation=99.0,
                               min_regimes_working=1, bootstrap_iterations=200)

    result = _engine().run(build_factor_pipeline(universe, benchmark=benchmark,
                                                 config=lenient))
    assert result.aggregate is not None
    assert "residual_alpha" in result.aggregate and "alpha_tstat" in result.aggregate
    assert result.aggregate["r_squared"] < 0.9, "an independent benchmark must not span it"


def test_the_portfolio_uses_risk_parity_weights_over_survivors_only():
    universe = _universe()
    bench_built, _ = construct(_universe(["ZZ1", "ZZ2", "ZZ3"]))
    lenient = FactorGateConfig(min_tstat=0.0, max_p_value=1.0, max_degradation=99.0,
                               min_regimes_working=1, bootstrap_iterations=200)
    result = _engine().run(build_factor_pipeline(
        universe, benchmark={f"BM_{k}": v.returns for k, v in bench_built.items()},
        config=lenient))

    weights = result.aggregate["weights"]
    assert set(weights) == {i.subject for i in result.survivors}
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_neutrality_is_reported_honestly_including_what_is_not_enforced():
    universe = _universe()
    bench_built, _ = construct(_universe(["ZZ1", "ZZ2", "ZZ3"]))
    lenient = FactorGateConfig(min_tstat=0.0, max_p_value=1.0, max_degradation=99.0,
                               min_regimes_working=1, bootstrap_iterations=200)
    result = _engine().run(build_factor_pipeline(
        universe, benchmark={f"BM_{k}": v.returns for k, v in bench_built.items()},
        config=lenient))

    neutrality = result.aggregate["neutrality"]
    assert "by construction" in neutrality["dollar"]
    assert "estimates available" in neutrality["beta"]
    assert "NOT enforced" in neutrality["sector"], \
        "claiming sector neutrality we do not enforce would be the dishonest option"


def test_every_factor_evaluated_is_recorded_as_a_trial():
    """The selection gate needs the count of everything tested (§5.7.4)."""
    engine = _engine()
    engine.run(build_factor_pipeline(_universe()))
    trials = engine.storage.trials("eleven-node-factor-graph")
    assert trials, "validated factors must land in the trial ledger"
    assert all(t["p_value"] is not None for t in trials)


def test_a_strict_desk_can_kill_everything_and_says_what_killed_each():
    strict = FactorGateConfig(min_tstat=99.0, bootstrap_iterations=200)
    result = _engine().run(build_factor_pipeline(_universe(), config=strict))
    assert result.survivors == []
    assert result.aggregate is None
    assert len(result.kill_report()) == 7, "all seven accounted for"
