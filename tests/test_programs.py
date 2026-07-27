"""Programs and the scheduler (DESIGN.md §5.6), plus the §12 decisions they enable."""

from datetime import datetime

import pytest

from teleraft.app import App
from teleraft.programs import Cron, CronError, Program, Scheduler, matches, next_due

HUMAN = "11111111"


# --------------------------------------------------------------------------- #
# Cron parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("expr,when,expected", [
    ("0 9 * * 1-5", datetime(2026, 7, 27, 9, 0), True),     # Monday 09:00
    ("0 9 * * 1-5", datetime(2026, 7, 26, 9, 0), False),    # Sunday
    ("0 9 * * 1-5", datetime(2026, 7, 27, 9, 1), False),    # a minute late
    ("*/15 * * * *", datetime(2026, 7, 27, 13, 30), True),
    ("*/15 * * * *", datetime(2026, 7, 27, 13, 31), False),
    ("0 4 * * 0", datetime(2026, 7, 26, 4, 0), True),       # Sunday 04:00
    ("0 0 1 1 *", datetime(2027, 1, 1, 0, 0), True),
    ("0 8 * * 1", datetime(2026, 7, 27, 8, 0), True),       # Monday
])
def test_cron_matching(expr, when, expected):
    assert matches(expr, when) is expected


def test_day_of_month_and_weekday_are_ored_like_real_cron():
    # Standard cron quirk: both restricted → fire if EITHER matches.
    expr = "0 0 13 * 5"                                     # 13th, or any Friday
    assert matches(expr, datetime(2026, 7, 13, 0, 0))       # a Monday the 13th
    assert matches(expr, datetime(2026, 7, 31, 0, 0))       # a Friday, not the 13th
    assert not matches(expr, datetime(2026, 7, 14, 0, 0))   # neither


@pytest.mark.parametrize("bad", ["", "0 9 * *", "0 9 * * 1-5 7", "99 9 * * *",
                                 "0 9 * * abc", "*/0 * * * *"])
def test_unparseable_schedules_raise_rather_than_never_firing(bad):
    with pytest.raises(CronError):
        Cron.parse(bad)


def test_next_due_finds_the_following_occurrence():
    nxt = next_due("0 9 * * 1-5", datetime(2026, 7, 26, 12, 0))   # Sunday noon
    assert nxt == datetime(2026, 7, 27, 9, 0)                      # Monday 09:00


# --------------------------------------------------------------------------- #
# Scheduler behaviour
# --------------------------------------------------------------------------- #
def test_due_program_fires_once_per_minute_not_once_per_tick():
    fired = []
    s = Scheduler()
    s.register(Program(name="p", cron="0 9 * * *", body=lambda: fired.append(1)))

    when = datetime(2026, 7, 27, 9, 0)
    for _ in range(5):                       # the poll loop ticks many times a minute
        s.tick(when)
    assert len(fired) == 1

    s.tick(datetime(2026, 7, 28, 9, 0))      # next day fires again
    assert len(fired) == 2


def test_not_due_program_does_not_fire():
    fired = []
    s = Scheduler()
    s.register(Program(name="p", cron="0 9 * * *", body=lambda: fired.append(1)))
    s.tick(datetime(2026, 7, 27, 10, 0))
    assert fired == []


def test_a_still_running_program_skips_its_next_trigger():
    """A long run must not have a second copy started underneath it (§5.6)."""
    s = Scheduler()
    seen = []

    def slow():
        # While this run is in flight, the *next* minute's trigger arrives.
        seen.append(s.tick(datetime(2026, 7, 27, 9, 1)))

    s.register(Program(name="p", cron="* * * * *", body=slow))
    s.tick(datetime(2026, 7, 27, 9, 0))

    inner = seen[0]
    assert [n for n, _ in inner.skipped] == ["p"]
    assert "still in flight" in inner.skipped[0][1]
    assert inner.fired == []


def test_a_duplicate_tick_in_the_same_minute_is_a_silent_no_op():
    """The poll loop ticks many times a minute; that is not an overlap."""
    fired = []
    s = Scheduler()
    s.register(Program(name="p", cron="* * * * *", body=lambda: fired.append(1)))
    when = datetime(2026, 7, 27, 9, 0)
    first, second = s.tick(when), s.tick(when)
    assert first.fired == ["p"] and len(fired) == 1
    assert second.fired == [] and second.skipped == []       # not reported as a skip


def test_a_failing_program_does_not_stop_the_others():
    ran = []
    s = Scheduler()
    s.register(Program(name="bad", cron="* * * * *",
                       body=lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
    s.register(Program(name="good", cron="* * * * *", body=lambda: ran.append(1)))
    report = s.tick(datetime(2026, 7, 27, 9, 0))
    assert ran == [1]
    assert [n for n, _ in report.failed] == ["bad"]
    assert "good" in report.fired


def test_disabled_program_never_fires():
    fired = []
    s = Scheduler()
    s.register(Program(name="p", cron="* * * * *", body=lambda: fired.append(1),
                       enabled=False))
    s.tick(datetime(2026, 7, 27, 9, 0))
    assert fired == []


def test_bad_schedule_is_rejected_at_registration():
    s = Scheduler()
    with pytest.raises(CronError):
        s.register(Program(name="p", cron="not a cron", body=lambda: None))


# --------------------------------------------------------------------------- #
# The platform's own Programs
# --------------------------------------------------------------------------- #
def test_agent_heartbeats_are_registered_as_programs():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    names = {p.name for p in app.scheduler.programs()}
    assert "memory-consolidation" in names
    assert any(n.startswith("heartbeat:Quinn") for n in names), names
    # Bailey declares no heartbeat, so none is registered for it.
    assert not any(n.startswith("heartbeat:Bailey") for n in names)
    app.close()


def test_a_fired_heartbeat_opens_a_normal_gated_task():
    """Autonomy must not mean a private code path — the run is claimed and gated."""
    from teleraft.models import RunStatus

    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    heartbeat = next(p for p in app.scheduler.programs()
                     if p.name.startswith("heartbeat:Quinn"))
    result = heartbeat.body()

    assert result.status is RunStatus.AWAITING_HUMAN      # stopped at the human gate
    _tid, state = app.storage.load_run(result.run_id)
    assert state.agent == "Quinn" and state.tester_agent != "Quinn"
    app.close()
