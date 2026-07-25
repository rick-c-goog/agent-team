"""End-to-end loop tests: the full Planner→Orchestrator→Builder→Tester flow."""

from teleraft.app import App
from teleraft.models import RunStatus, TaskStatus
from teleraft.telegram.gateway import Callback, Update


def _handoff(app, text="@Cole write the launch post for the June webinar", mentions=("Cole",)):
    return app.gateway.handle_message(
        Update(text=text, user_id="rick", user_handle="rick", topic="# content",
               as_task=True, mentions=list(mentions))
    )


def test_task_runs_to_review_then_approves_and_learns():
    app = App(human_ids={"rick"})
    result = _handoff(app)

    # The run pauses at the human review gate — nothing external ships without approval.
    assert result.status is RunStatus.AWAITING_HUMAN
    run_id = result.run_id
    task_id = app.storage.load_run(run_id)[1].task_id
    assert app.storage.get_task(task_id)["status"] == TaskStatus.IN_REVIEW.value

    # A different agent tested it — no agent grades its own work.
    state = app.storage.load_run(run_id)[1]
    assert state.tester_agent and state.tester_agent != state.agent
    assert any(not v.passed for v in state.verdicts), "the reject→retry path should be exercised"

    # Human approves → Learn writeback → Done.
    app.gateway.handle_callback(Callback(data=f"approve|{run_id}|review", user_id="rick"))
    assert app.storage.get_task(task_id)["status"] == TaskStatus.DONE.value

    mems = app.storage.memories_for("Cole")
    assert any("source" in m["content_md"].lower() for m in mems)
    app.close()


def test_reject_at_review_sends_back_through_replan():
    app = App(human_ids={"rick"})
    result = _handoff(app)
    run_id = result.run_id
    task_id = app.storage.load_run(run_id)[1].task_id

    # Human rejects with a reason → replan → rebuild → back to review (suspended again).
    r2 = app.gateway.handle_callback(
        Callback(data=f"reject|{run_id}|review", user_id="rick", reason="headline is too generic")
    )
    assert r2.status is RunStatus.AWAITING_HUMAN
    state = app.storage.load_run(run_id)[1]
    assert state.replans >= 1
    assert any(v.tester == "human" and not v.passed for v in state.verdicts)

    # Now approve → Done.
    app.gateway.handle_callback(Callback(data=f"approve|{run_id}|review", user_id="rick"))
    assert app.storage.get_task(task_id)["status"] == TaskStatus.DONE.value
    app.close()


def test_human_only_gate_enforcement():
    app = App(human_ids={"rick"})
    result = _handoff(app)
    run_id = result.run_id

    # A non-allow-listed (bot) user cannot approve.
    app.gateway.handle_callback(Callback(data=f"approve|{run_id}|review", user_id="Cole"))
    assert app.storage.load_run(run_id)[1].status is RunStatus.AWAITING_HUMAN
    assert any("blocked" in p for p in app.client.channel_posts)
    app.close()


def test_plan_gate_when_task_touches_escalation_area():
    app = App(human_ids={"rick"})
    # "pricing" is in Cole's escalate_when → plan must gate for a human first.
    result = _handoff(app, text="@Cole write the pricing page for the new plan")
    assert result.status is RunStatus.AWAITING_HUMAN
    run_id = result.run_id
    state = app.storage.load_run(run_id)[1]
    assert state.pending_gate is not None and state.pending_gate.value == "plan"

    # Approve the plan → run proceeds and ends at the review gate.
    r2 = app.gateway.handle_callback(Callback(data=f"approve|{run_id}|plan", user_id="rick"))
    assert r2.status is RunStatus.AWAITING_HUMAN
    assert app.storage.load_run(run_id)[1].pending_gate.value == "review"
    app.close()


def test_broadcast_feed_and_review_card_buttons():
    app = App(human_ids={"rick"})
    _handoff(app)
    # The review card exposes Approve/Reject; the activity channel got a review ping.
    assert any("Review needed" in p for p in app.client.channel_posts)
    # Some message in the transcript carries the Approve button.
    assert any(
        app.client.has_button(mid, "Approve") for mid in app.client.messages
    )
    app.close()
