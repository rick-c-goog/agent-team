"""Onboarding agent tests (DESIGN.md §3.3): interview → plan → approve → apply → verify."""

import pytest

from teleraft.app import App
from teleraft.onboarding import MockHost, OnboardingAgent, compile_plan, plan_to_yaml
from teleraft.onboarding.interview import INTERVIEW, Answers
from teleraft.onboarding.plan import WorkspacePlan, PlannedAgent, plan_from_yaml

# Local-only sources: these tests must never touch the network.
ANSWERS = [
    "We run a dev-tools consultancy for platform teams",
    "content, delivery, finance",
    "pricing, legal, refunds",
    "kb/cole/brand-voice.md, kb/shared/personas.csv",
    "weekday-morning",
    "11111111",
]

# Used only for plan compilation (never applied), to cover URI type inference.
ANSWERS_MIXED = ANSWERS[:3] + [
    "kb/cole/brand-voice.md, https://docs.acme.com/handbook, drive://folders/1A2B"
] + ANSWERS[4:]


@pytest.fixture
def empty_app(tmp_path):
    """A workspace with no pre-seeded agents, so provisioning is actually observable."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    app = App(human_ids=set(), agents_dir=str(agents_dir), sync_knowledge=False)
    yield app
    app.close()


def _run_interview(app, host=None, answers=ANSWERS):
    onb = OnboardingAgent(app, host or MockHost(), user_ref="rick")
    onb.start()
    for a in answers:
        onb.answer(a)
    return onb


# --------------------------------------------------------------------------- #
# Interview → plan (the Planner role)
# --------------------------------------------------------------------------- #
def test_interview_asks_every_question_then_proposes_a_plan(empty_app):
    host = MockHost()
    onb = _run_interview(empty_app, host)
    asked = " ".join(m for _, m, _ in host.sent)
    for q in INTERVIEW:
        assert q.text in asked
    assert onb.plan is not None
    assert "Here's the plan" in host.sent[-1][1]
    assert "Approve" in host.sent[-1][2]          # human gate offered, not auto-applied


def test_plan_compilation_is_deterministic(empty_app):
    a = Answers(raw=dict(zip([q.key for q in INTERVIEW], ANSWERS)))
    assert plan_to_yaml(compile_plan(a)) == plan_to_yaml(compile_plan(a))


def test_plan_round_trips_through_yaml(empty_app):
    plan = compile_plan(Answers(raw=dict(zip([q.key for q in INTERVIEW], ANSWERS))))
    again = plan_from_yaml(plan_to_yaml(plan))
    assert [x.name for x in again.agents] == [x.name for x in plan.agents]
    assert again.topics == plan.topics
    assert [s.type for s in again.agents[0].knowledge] == [
        s.type for s in plan.agents[0].knowledge
    ]


def test_source_types_are_inferred_from_the_uri(empty_app):
    plan = compile_plan(Answers(raw=dict(zip([q.key for q in INTERVIEW], ANSWERS_MIXED))))
    types = {s.uri: s.type for s in plan.agents[0].knowledge}
    assert types["kb/cole/brand-voice.md"] == "file"
    assert types["https://docs.acme.com/handbook"] == "web"
    assert types["drive://folders/1A2B"] == "gdrive"


def test_a_solo_pillar_is_paired_so_nobody_grades_their_own_work(empty_app):
    answers = list(ANSWERS)
    answers[1] = "content"                        # only one pillar requested
    plan = compile_plan(Answers(raw=dict(zip([q.key for q in INTERVIEW], answers))))
    assert len(plan.agents) >= 2


def test_escalations_reach_every_agents_goals_and_soul(empty_app):
    onb = _run_interview(empty_app)
    onb.approve(by_user_id="11111111")
    goals = empty_app.registry.goals("Cole")
    assert "pricing" in goals["escalate_when"]
    assert "pricing" in empty_app.registry.soul("Cole").lower()


# --------------------------------------------------------------------------- #
# Apply (Builder) + verify (Tester)
# --------------------------------------------------------------------------- #
def test_apply_provisions_the_whole_workspace(empty_app):
    host = MockHost()
    onb = _run_interview(empty_app, host)
    report = onb.approve(by_user_id="11111111")

    assert report.verdict_passed, report.verdict_reasons
    assert sorted(report.agents_created) == ["Cole", "Penn", "Ray"]
    assert "# content" in report.topics_created and "# admin" in report.topics_created
    assert report.sources_added                      # knowledge registered
    assert len(host.jobs()) == len(report.heartbeats_scheduled) >= 1
    assert "11111111" in empty_app.gateway.human_ids  # a human can now approve work
    # BotFather is human-only: the agent reports it instead of pretending (§3.3.5).
    assert any("BotFather" in step for step in report.manual_steps)


def test_apply_is_idempotent(empty_app):
    onb = _run_interview(empty_app)
    onb.approve(by_user_id="11111111")
    second = onb.apply(onb.plan)
    assert second.agents_created == [] and second.topics_created == []
    assert second.sources_added == []
    assert second.skipped and second.verdict_passed


def test_provisioned_team_can_immediately_run_a_task(empty_app):
    """The smoke test from §3.3.3: the workspace is usable the moment onboarding ends."""
    from teleraft.models import RunStatus
    from teleraft.telegram.gateway import Update

    onb = _run_interview(empty_app)
    onb.approve(by_user_id="11111111")

    result = empty_app.gateway.handle_message(
        Update(text="@Cole draft the launch post", user_id="11111111", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    assert result.status is RunStatus.AWAITING_HUMAN
    _tid, state = empty_app.storage.load_run(result.run_id)
    assert state.tester_agent != state.agent


def test_unreachable_source_is_reported_not_crashed(empty_app, monkeypatch):
    """A dead URL must degrade to source health + a manual step (§4.1.5), never raise."""
    import httpx

    from teleraft.knowledge.fetchers import WebFetcher

    def boom(*a, **kw):
        raise httpx.ConnectError("nodename nor servname provided")

    transport = httpx.MockTransport(boom)
    empty_app.knowledge.fetchers.web_factory = lambda: WebFetcher(
        http=httpx.Client(transport=transport)
    )

    answers = list(ANSWERS)
    answers[3] = "https://does-not-exist.invalid/handbook"
    onb = _run_interview(empty_app, answers=answers)
    report = onb.approve(by_user_id="11111111")

    assert any("unreachable" in step for step in report.manual_steps)
    assert any(h["status"] == "error" for h in empty_app.knowledge.health())


def test_verify_fails_loudly_on_an_unusable_workspace(empty_app):
    """A plan yielding a single agent must be caught by the Tester role."""
    onb = OnboardingAgent(empty_app, MockHost(), user_ref="rick")
    solo = WorkspacePlan(
        topics=["# content"],
        agents=[PlannedAgent(name="Solo", pillar="content", soul="only one")],
        human_ids=["11111111"],
    )
    report = onb.apply(solo)
    assert not report.verdict_passed
    assert any("two agents" in r for r in report.verdict_reasons)


# --------------------------------------------------------------------------- #
# Guardrails (§3.3.5) and resume (§3.3.4)
# --------------------------------------------------------------------------- #
def test_an_agent_cannot_approve_the_plan(empty_app):
    onb = _run_interview(empty_app)
    empty_app.registry.register(
        __import__("teleraft.agents.registry", fromlist=["AgentConfig"]).AgentConfig(
            name="Cole", soul_md="x", goals={})
    )
    with pytest.raises(PermissionError):
        onb.approve(by_user_id="Cole")
    with pytest.raises(PermissionError):
        onb.approve(by_user_id="Onboarding")


def test_session_is_persisted_and_resumable(empty_app):
    host = MockHost()
    onb = OnboardingAgent(empty_app, host, user_ref="rick")
    onb.start()
    onb.answer(ANSWERS[0])
    onb.answer(ANSWERS[1])

    row = empty_app.storage.get_onboarding(onb.session_id)
    assert row["status"] == "interviewing"

    # A restart mid-interview picks up where it left off, not from question one.
    resumed = OnboardingAgent.resume(empty_app, onb.session_id, host)
    assert resumed.answers.next_question().key == INTERVIEW[2].key
    for a in ANSWERS[2:]:
        resumed.answer(a)
    assert resumed.plan is not None
    assert empty_app.storage.get_onboarding(onb.session_id)["status"] == "awaiting_approval"

    report = resumed.approve(by_user_id="11111111")
    assert report.verdict_passed
    assert empty_app.storage.get_onboarding(onb.session_id)["status"] == "done"
