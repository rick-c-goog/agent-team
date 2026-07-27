"""Pipeline DAG semantics (DESIGN.md §5.7).

Deliberately domain-neutral: a hiring funnel, because the node kinds are a platform
capability and nothing about them is financial.
"""

import pytest

from teleraft.app import App
from teleraft.pipeline import (
    Item,
    NodeOutcome,
    Pipeline,
    PipelineEngine,
    Verdict,
    aggregate_gate,
    gate,
    join,
    producer,
)
from teleraft.storage import Storage

HUMAN = "11111111"


# --------------------------------------------------------------------------- #
# A hiring funnel: source candidates → screen → interview → shortlist → judge
# --------------------------------------------------------------------------- #
def _funnel(screen_fails=(), interview_fails=(), interview_blocks=(),
            shortlist_verdict=Verdict.PASS):
    def source(context, artifacts):
        names = context.get("candidates", ["ada", "brendan", "chen"])
        return NodeOutcome(
            items=[Item(subject=n) for n in names],
            artifacts={"sourced_from": "referrals"},
        )

    def screen(item, context, artifacts):
        if item.subject in screen_fails:
            return NodeOutcome(verdict=Verdict.FAIL, reasons=["no relevant experience"])
        # An artifact that must survive even if the item is later killed.
        return NodeOutcome(artifacts={f"cv:{item.subject}": {"pages": 2}},
                           payload={"screened": True})

    def interview(item, context, artifacts):
        if item.subject in interview_blocks:
            return NodeOutcome(verdict=Verdict.CANNOT_EVALUATE,
                               reasons=["interviewer unavailable"])
        if item.subject in interview_fails:
            return NodeOutcome(verdict=Verdict.FAIL, reasons=["failed the exercise"])
        return NodeOutcome(payload={"score": 4}, statistic=4.0, p_value=0.01)

    def shortlist(survivors, items, context, artifacts):
        return NodeOutcome(payload={"shortlist": [i.subject for i in survivors]})

    def judge_slate(aggregate, context, artifacts):
        if shortlist_verdict is not Verdict.PASS:
            return NodeOutcome(verdict=shortlist_verdict, reasons=["slate too narrow"])
        return NodeOutcome(payload={"approved": True})

    return Pipeline(name="hiring", nodes=[
        producer("source", source, owner="Recruiter"),
        gate("screen", screen, owner="Recruiter", checker="Reviewer"),
        gate("interview", interview, owner="Engineer", checker="Reviewer"),
        join("shortlist", shortlist, owner="Recruiter"),
        aggregate_gate("slate-review", judge_slate, owner="Recruiter", checker="Head"),
    ])


def _engine():
    return PipelineEngine(Storage(":memory:"))


# --------------------------------------------------------------------------- #
def test_a_clean_run_graduates_every_item():
    result = _engine().run(_funnel())
    assert len(result.items) == 3
    assert len(result.survivors) == 3
    assert result.aggregate["shortlist"] == ["ada", "brendan", "chen"]
    assert result.graduated


def test_a_failing_gate_kills_the_item_with_its_reason():
    result = _engine().run(_funnel(screen_fails=("brendan",)))
    killed = result.killed
    assert [i.subject for i in killed] == ["brendan"]
    assert killed[0].killed_at == "screen"
    assert "no relevant experience" in killed[0].kill_reason
    assert "brendan" not in result.aggregate["shortlist"]


def test_a_killed_item_does_not_reach_later_gates():
    seen = []

    def screen(item, context, artifacts):
        return NodeOutcome(verdict=Verdict.FAIL, reasons=["nope"])

    def interview(item, context, artifacts):
        seen.append(item.subject)
        return NodeOutcome()

    pipe = Pipeline(name="p", nodes=[
        producer("source", lambda context, artifacts: NodeOutcome(items=[Item("a")])),
        gate("screen", screen),
        gate("interview", interview),
    ])
    _engine().run(pipe)
    assert seen == [], "a killed item must not be judged again downstream"


def test_cannot_evaluate_blocks_rather_than_passing():
    """A gate whose inputs are missing has not been satisfied (§5.7.7)."""
    result = _engine().run(_funnel(interview_blocks=("chen",)))
    blocked = result.blocked_items
    assert [i.subject for i in blocked] == ["chen"]
    assert "cannot evaluate" in blocked[0].kill_reason
    assert "chen" not in result.aggregate["shortlist"]


def test_artifacts_outlive_the_item_that_was_killed():
    """Rule 4: survival is a property of the item, not of every artifact it produced."""
    engine = _engine()
    result = engine.run(_funnel(interview_fails=("ada",)))

    stored = {r["name"] for r in engine.storage.pipeline_artifacts(result.run_id)}
    assert "cv:ada" in stored, "the killed candidate's CV should still be on record"
    assert any(i.subject == "ada" and i.status == "killed" for i in result.items)


def test_the_join_sees_only_survivors_and_is_a_barrier():
    seen_at_join = {}

    def shortlist(survivors, items, context, artifacts):
        seen_at_join["survivors"] = [i.subject for i in survivors]
        seen_at_join["all"] = [i.subject for i in items]
        return NodeOutcome(payload={"shortlist": [i.subject for i in survivors]})

    pipe = _funnel(screen_fails=("brendan",))
    pipe.nodes[3] = join("shortlist", shortlist, owner="Recruiter")
    _engine().run(pipe)

    assert seen_at_join["survivors"] == ["ada", "chen"]
    assert seen_at_join["all"] == ["ada", "brendan", "chen"], "the join still sees the dead"


def test_zero_survivors_is_a_reported_result_not_an_error():
    result = _engine().run(_funnel(screen_fails=("ada", "brendan", "chen")))
    assert result.survivors == []
    assert result.aggregate is None
    assert not result.graduated
    assert any("nothing survived" in r for r in result.reasons)
    assert len(result.kill_report()) == 3          # what killed each, as a finding


def test_an_aggregate_gate_can_reject_a_slate_of_good_items():
    """The question is about the whole, so passing parts do not settle it."""
    result = _engine().run(_funnel(shortlist_verdict=Verdict.FAIL))
    assert len(result.survivors) == 3              # every item passed its own gates
    assert result.aggregate is None                # but the slate was rejected
    assert not result.graduated
    assert "slate too narrow" in " ".join(result.reasons)


def test_a_raising_node_becomes_cannot_evaluate_not_a_crash():
    def explode(item, context, artifacts):
        raise RuntimeError("interviewer database is down")

    pipe = Pipeline(name="p", nodes=[
        producer("source", lambda context, artifacts: NodeOutcome(items=[Item("a")])),
        gate("screen", explode),
    ])
    result = _engine().run(pipe)
    assert result.blocked_items and "database is down" in result.blocked_items[0].kill_reason


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #
def test_a_pipeline_must_start_with_a_producer():
    with pytest.raises(ValueError, match="must start with a producer"):
        Pipeline(name="p", nodes=[gate("g", lambda **kw: NodeOutcome())]).validate()


def test_an_aggregate_gate_needs_a_preceding_join():
    pipe = Pipeline(name="p", nodes=[
        producer("source", lambda **kw: NodeOutcome()),
        aggregate_gate("judge", lambda **kw: NodeOutcome()),
    ])
    with pytest.raises(ValueError, match="no preceding join"):
        pipe.validate()


def test_a_gate_cannot_have_the_same_owner_and_checker():
    with pytest.raises(ValueError, match="no agent grades its own work"):
        gate("g", lambda **kw: NodeOutcome(), owner="Quinn", checker="Quinn")


def test_duplicate_node_names_are_rejected():
    pipe = Pipeline(name="p", nodes=[
        producer("dup", lambda **kw: NodeOutcome()),
        gate("dup", lambda **kw: NodeOutcome()),
    ])
    with pytest.raises(ValueError, match="duplicate node name"):
        pipe.validate()


# --------------------------------------------------------------------------- #
# Persistence and the Telegram surface
# --------------------------------------------------------------------------- #
def test_every_node_execution_is_recorded_for_audit():
    engine = _engine()
    result = engine.run(_funnel(screen_fails=("brendan",)))
    runs = engine.storage.node_runs(result.run_id)
    by_node = {}
    for r in runs:
        by_node.setdefault(r["node"], []).append(r["verdict"])

    assert by_node["screen"].count("fail") == 1
    assert by_node["screen"].count("pass") == 2
    assert "shortlist" in by_node and "slate-review" in by_node


def test_pipeline_command_reports_runs_and_what_was_killed():
    from teleraft.telegram.gateway import Update

    app = App(human_ids={HUMAN})
    app.pipelines.run(_funnel(screen_fails=("brendan",)))
    app.client.transcript.clear()

    app.gateway.handle_message(
        Update(text="/pipeline", user_id=HUMAN, user_handle="rick", topic="# content"))
    report = " ".join(app.client.transcript)
    assert "Pipeline runs" in report and "hiring" in report
    assert "brendan" in report and "no relevant experience" in report
    app.close()
