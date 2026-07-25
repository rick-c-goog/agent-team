"""Per-agent runtime routing and run-failure surfacing.

Both regressions came from live testing: `main.py` overrode every agent with one
runtime (routing a deterministic quant desk through Claude), and a crashed run died in
the server log while Telegram showed nothing.
"""

from pathlib import Path

import pytest

from teleraft.app import App
from teleraft.main import check_engine_prerequisites
from teleraft.models import RunStatus, TaskStatus
from teleraft.runtime.anthropic_runtime import _explain_api_error
from teleraft.runtime.mock import MockRuntime
from teleraft.runtime.quant import QuantRuntime
from teleraft.telegram.gateway import Update

QUANT_AGENTS = str(Path(__file__).resolve().parent.parent / "agents" / "quant")
HUMAN = "11111111"


# --------------------------------------------------------------------------- #
# Per-agent engine routing
# --------------------------------------------------------------------------- #
def test_quant_agents_get_the_quant_runtime_not_the_default():
    """Regression: quant agents were routed through Claude, needing an API key for a
    workload that is fully deterministic."""
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS, default_engine="claude")
    for name in ("Quinn", "Bailey", "Robin", "Mac"):
        assert isinstance(app._runtime_for(name), QuantRuntime), name
    app.close()


def test_default_engine_applies_only_to_agents_declaring_none(tmp_path):
    agents = tmp_path / "agents"
    (agents / "souls").mkdir(parents=True)
    (agents / "souls" / "s.md").write_text("soul")
    (agents / "declared.yaml").write_text(
        "name: Declared\nsoul: souls/s.md\nruntime:\n  engine: quant\n")
    (agents / "undeclared.yaml").write_text("name: Undeclared\nsoul: souls/s.md\n")

    app = App(human_ids={HUMAN}, agents_dir=str(agents), default_engine="mock")
    assert isinstance(app._runtime_for("Declared"), QuantRuntime)
    assert isinstance(app._runtime_for("Undeclared"), MockRuntime)
    app.close()


def test_engines_in_use_reports_the_resolved_engine_per_agent():
    from teleraft.agents.registry import load_agents_from_dir

    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS, default_engine="claude")
    engines = app.engines_in_use(load_agents_from_dir(QUANT_AGENTS))
    assert set(engines.values()) == {"quant"}, engines
    app.close()


def test_a_pure_quant_desk_needs_no_api_key():
    """The whole point: a deterministic desk must run with no credentials at all."""
    assert check_engine_prerequisites({"Quinn": "quant", "Bailey": "quant"}) == []


def test_missing_api_key_is_caught_before_any_task_runs(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    problems = check_engine_prerequisites({"Cole": "claude", "Ray": "quant"})
    assert problems and "ANTHROPIC_API_KEY" in problems[0]
    assert "Cole" in problems[0] and "Ray" not in problems[0]
    assert "engine: quant" in problems[0]          # points at the no-key alternative


def test_api_key_present_passes(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert check_engine_prerequisites({"Cole": "claude"}) == []


def test_unknown_engine_is_rejected():
    problems = check_engine_prerequisites({"Cole": "gpt5"})
    assert problems and "unknown runtime engine" in problems[0]


def test_claude_runtime_is_never_constructed_for_a_quant_only_desk(monkeypatch):
    """Constructing AnthropicRuntime raises without the SDK/key — a quant desk must
    never touch that path."""
    def explode(*a, **kw):
        raise AssertionError("AnthropicRuntime must not be constructed")

    monkeypatch.setattr("teleraft.runtime.anthropic_runtime.AnthropicRuntime", explode)
    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    app.gateway.handle_message(
        Update(text="@Quinn momentum edge on SPY", user_id=HUMAN, user_handle="rick",
               topic="# research", as_task=True, mentions=["Quinn"])
    )
    app.close()


# --------------------------------------------------------------------------- #
# Error message quality
# --------------------------------------------------------------------------- #
def test_auth_error_explains_the_fix():
    class AuthenticationError(Exception):
        pass

    message = _explain_api_error(AuthenticationError("Error code: 401 - invalid x-api-key"))
    assert "ANTHROPIC_API_KEY" in message
    assert "engine: quant" in message           # the no-key escape hatch
    assert "same" in message                    # the same-environment gotcha


def test_other_api_errors_are_labelled():
    class RateLimitError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    assert "rate limit" in _explain_api_error(RateLimitError("429")).lower()
    assert "network" in _explain_api_error(APIConnectionError("boom")).lower()


# --------------------------------------------------------------------------- #
# A crashed run must be visible in Telegram
# --------------------------------------------------------------------------- #
class _ExplodingRuntime(MockRuntime):
    def plan(self, req):
        raise RuntimeError("Anthropic rejected the API key (401 invalid x-api-key).\n"
                           "  → Check ANTHROPIC_API_KEY is set")


def test_run_failure_is_reported_in_the_thread_and_task_returns_to_todo():
    app = App(human_ids={HUMAN})
    exploding = _ExplodingRuntime()
    app.engine.runtime_for = lambda agent: exploding

    result = app.gateway.handle_message(
        Update(text="@Cole write the launch post", user_id=HUMAN, user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )

    assert result.status is RunStatus.FAILED
    assert "401" in result.error

    transcript = " ".join(app.client.transcript)
    assert "Run failed" in transcript and "plan" in transcript
    assert "ANTHROPIC_API_KEY" in transcript          # the fix reaches the user
    assert any("Run failed" in p for p in app.client.channel_posts)

    # The task is claimable again rather than stuck owned-but-dead.
    task_id = app.storage.load_run(result.run_id)[1].task_id
    task = app.storage.get_task(task_id)
    assert task["status"] == TaskStatus.TODO.value and task["owner"] is None
    app.close()


def test_failed_run_is_recorded_for_audit():
    app = App(human_ids={HUMAN})
    app.engine.runtime_for = lambda agent: _ExplodingRuntime()
    result = app.gateway.handle_message(
        Update(text="@Cole draft something", user_id=HUMAN, user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    events = app.storage.run_events(result.run_id)
    assert any("failed" in e["node"] for e in events)
    _tid, state = app.storage.load_run(result.run_id)
    assert state.status is RunStatus.FAILED
    app.close()
