"""Evaluation, metrics, and the selection gate (DESIGN.md §5.9, §5.7.4)."""

import math

import pytest

from teleraft.app import App
from teleraft.evaluation import collect, replay_run, run_fixtures
from teleraft.evaluation.fixtures import (
    Fixture,
    check_grounding,
    default_suites,
    grounding_fixtures,
)
from teleraft.pipeline.selection import (
    assess,
    benjamini_hochberg,
    deflate,
    expected_max_of_n,
)
from teleraft.runtime.mock import MockRuntime
from teleraft.storage import Storage
from teleraft.telegram.gateway import Callback, Update

HUMAN = "11111111"


def _run_a_task(app, text="@Cole write the launch post"):
    return app.gateway.handle_message(
        Update(text=text, user_id=HUMAN, user_handle="rick", topic="# content",
               as_task=True, mentions=["Cole"]))


# --------------------------------------------------------------------------- #
# Process metrics
# --------------------------------------------------------------------------- #
def test_metrics_come_from_the_durable_record_with_no_extra_instrumentation():
    app = App(human_ids={HUMAN})
    result = _run_a_task(app)
    app.gateway.handle_callback(
        Callback(data=f"approve|{result.run_id}|review", user_id=HUMAN))

    m = app.metrics()
    assert m.tasks == 1 and m.tasks_done == 1
    assert m.runs == 1 and m.tokens_total > 0
    assert m.verdicts > 0
    assert m.approvals == 1 and m.rejections == 0
    assert m.human_intervention_rate == 0.0
    assert any("tasks:" in line for line in m.summary_lines())
    app.close()


def test_a_high_human_rejection_rate_is_flagged_as_mis_calibrated_gates():
    app = App(human_ids={HUMAN})
    result = _run_a_task(app)
    app.gateway.handle_callback(
        Callback(data=f"reject|{result.run_id}|review", user_id=HUMAN,
                 reason="not what I asked for"))

    m = app.metrics()
    assert m.rejections >= 1
    assert m.human_intervention_rate > 0.3
    assert any("checkers are" in note for note in m.notes), m.notes
    app.close()


def test_failed_runs_are_counted_and_attributed_to_a_node():
    class Exploding(MockRuntime):
        def plan(self, req):
            raise RuntimeError("boom")

    app = App(human_ids={HUMAN})
    app.engine.runtime_for = lambda agent: Exploding()
    _run_a_task(app)

    m = app.metrics()
    assert m.runs_failed == 1
    assert m.failures_by_node, "a failure should name the node it happened at"
    assert any(">10% of runs failed" in n for n in m.notes)
    app.close()


def test_metrics_command_renders_in_telegram():
    app = App(human_ids={HUMAN})
    _run_a_task(app)
    app.client.transcript.clear()
    app.gateway.handle_message(
        Update(text="/metrics", user_id=HUMAN, user_handle="rick", topic="# content"))
    out = " ".join(app.client.transcript)
    assert "Process metrics" in out and "intervention rate" in out
    app.close()


# --------------------------------------------------------------------------- #
# Trace replay
# --------------------------------------------------------------------------- #
def test_replay_reproduces_a_run_without_touching_the_original():
    app = App(human_ids={HUMAN})
    result = _run_a_task(app)
    memories_before = len(app.storage.memories_for("Cole"))
    messages_before = len(app.client.messages)

    comparison = replay_run(app, result.run_id)

    assert comparison.original_status == comparison.replay_status
    assert not comparison.changed, "an unchanged config must replay identically"
    # Replay is a scratch workspace: no new memories, no new Telegram traffic.
    assert len(app.storage.memories_for("Cole")) == memories_before
    assert len(app.client.messages) == messages_before
    app.close()


def test_replay_detects_that_a_changed_checker_changes_the_outcome():
    """This is the whole point: attribute an improvement to a change, not to intuition."""
    app = App(human_ids={HUMAN})
    result = _run_a_task(app)

    class NeverRejects(MockRuntime):
        def __init__(self):
            super().__init__(fail_first_build=False)

    baseline = replay_run(app, result.run_id)
    changed = replay_run(app, result.run_id, runtime_for=lambda agent: NeverRejects())

    assert baseline.original_rejections > 0
    assert changed.replay_rejections < baseline.replay_rejections
    assert changed.changed and any("attribute this" in n for n in changed.notes)
    app.close()


def test_replaying_an_unknown_run_raises():
    app = App(human_ids={HUMAN})
    with pytest.raises(KeyError):
        replay_run(app, "run_does_not_exist")
    app.close()


# --------------------------------------------------------------------------- #
# Known-answer fixtures
# --------------------------------------------------------------------------- #
def test_the_shipped_grounding_suite_is_green():
    for suite in default_suites():
        assert suite.passed, suite.summary()


def test_a_checker_that_stops_noticing_uncited_claims_is_caught_as_a_miss():
    def lax(given):
        return False, []          # passes everything

    suite = run_fixtures("grounding", lax, grounding_fixtures())
    assert not suite.passed
    assert suite.misses and "MISSED" in suite.summary()


def test_a_checker_that_rejects_everything_is_caught_as_a_false_alarm():
    def paranoid(given):
        return True, ["I don't trust it"]

    suite = run_fixtures("grounding", paranoid, grounding_fixtures())
    assert not suite.passed
    assert suite.false_alarms, "tuning a checker to reject everything is not an improvement"


def test_a_raising_judge_is_recorded_rather_than_crashing_the_suite():
    def broken(given):
        raise ValueError("nope")

    suite = run_fixtures("g", broken, [Fixture("case", given=None, must_reject=False)])
    assert suite.results[0].actual_reject is True
    assert "judge raised" in suite.results[0].reasons[0]


# --------------------------------------------------------------------------- #
# The selection gate — correcting for how hard you searched
# --------------------------------------------------------------------------- #
def test_the_bar_that_pure_search_clears_grows_with_the_number_of_trials():
    assert expected_max_of_n(1) == 0.0
    assert expected_max_of_n(10) < expected_max_of_n(100) < expected_max_of_n(10000)
    # Grows roughly like sqrt(2 ln n) — not linearly.
    assert 3.0 < expected_max_of_n(1000) < 4.0


def test_a_good_looking_result_deflates_to_nothing_after_enough_search():
    assert deflate(2.0, 1) == pytest.approx(2.0)
    assert deflate(2.0, 100) < 0, "a Sharpe of 2 after 100 tries is what luck produces"
    assert deflate(5.0, 100) > 0, "a genuinely large result still survives"


def test_benjamini_hochberg_controls_the_batch():
    # One real signal among noise.
    p_values = [0.001] + [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    threshold, rejected = benjamini_hochberg(p_values, alpha=0.05)
    assert rejected == [0] and threshold > 0

    # Pure noise: nothing should survive.
    _t, rejected_noise = benjamini_hochberg([0.2, 0.4, 0.6, 0.8], alpha=0.05)
    assert rejected_noise == []


def test_pure_noise_produces_naive_discoveries_and_none_after_correction():
    """The property that separates a research pipeline from a random number generator."""
    st = Storage(":memory:")
    n, alpha = 200, 0.05
    # Uniform p-values are exactly what pure noise produces.
    for i in range(n):
        p = (i + 0.5) / n
        st.add_trial("noise", f"h{i}", "validate", statistic=0.0, p_value=p,
                     epoch="e1")

    naive = sum(1 for i in range(n) if (i + 0.5) / n < alpha)
    assert naive == pytest.approx(n * alpha, abs=2), "≈1 in 20 passes by construction"

    report = assess(st, "noise", alpha=alpha)
    assert report.trials == n
    assert report.expected_false_positives == pytest.approx(n * alpha, abs=0.01)
    assert report.survivors_fdr == [], "nothing should survive FDR on pure noise"


def test_a_real_signal_survives_the_selection_gate():
    st = Storage(":memory:")
    for i in range(50):
        st.add_trial("desk", f"noise{i}", "validate", statistic=0.5,
                     p_value=0.2 + i * 0.01, epoch="e1")
    st.add_trial("desk", "real", "validate", statistic=6.0, p_value=1e-6, epoch="e1")

    report = assess(st, "desk")
    assert "real" in report.survivors_fdr
    assert report.best_subject == "real"
    assert report.deflated_statistic > 0
    assert "trials" in report.summary()


def test_the_trial_window_rolls_so_the_correction_stays_honest():
    """Counting since inception makes the correction monotonically punishing (§12 #12)."""
    import time

    st = Storage(":memory:")
    st.add_trial("desk", "old", "validate", 1.0, 0.5, "e1")
    st.conn.execute("UPDATE trial SET created_at=?", (time.time() - 400 * 86400,))
    st.conn.commit()
    st.add_trial("desk", "recent", "validate", 1.0, 0.5, "e1")

    assert assess(st, "desk", window_days=90).trials == 1        # only the recent one
    assert assess(st, "desk", window_days=500).trials == 2


def test_trials_from_a_superseded_epoch_can_be_excluded():
    st = Storage(":memory:")
    st.add_trial("desk", "a", "validate", 1.0, 0.5, "old-universe")
    st.add_trial("desk", "b", "validate", 1.0, 0.5, "current")
    assert assess(st, "desk", epoch="current").trials == 1


def test_no_trials_reports_that_rather_than_implying_significance():
    st = Storage(":memory:")
    report = assess(st, "empty")
    assert report.trials == 0
    assert any("no trials" in n for n in report.notes)
