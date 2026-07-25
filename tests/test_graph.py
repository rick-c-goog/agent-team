"""Graph engine unit tests: checkpoint/resume, budgets, tester selection."""

import pytest

from teleraft.agents.registry import Registry
from teleraft.graph.engine import GraphEngine, GRAPH_VERSION
from teleraft.memory.service import MemoryService
from teleraft.models import RunStatus
from teleraft.runtime.mock import MockRuntime
from teleraft.storage import Storage
from teleraft.tasks.service import TaskService


def _engine_with_two_agents():
    st = Storage(":memory:")
    reg = Registry(st)
    from teleraft.agents.registry import AgentConfig
    reg.register(AgentConfig(name="Cole", soul_md="content agent", goals={"escalate_when": []}))
    reg.register(AgentConfig(name="Ray", soul_md="reviewer", goals={"escalate_when": []}))
    mem = MemoryService(st)
    rt = MockRuntime()
    eng = GraphEngine(st, reg, mem, runtime_for=lambda a: rt)
    tasks = TaskService(st)
    return st, reg, eng, tasks


def test_resume_from_checkpoint_survives_engine_restart():
    st, reg, eng, tasks = _engine_with_two_agents()
    tid = tasks.create(topic="# content", title="draft a blog post", created_by="rick")
    result = eng.start(tid, "Cole")
    assert result.status is RunStatus.AWAITING_HUMAN

    # Simulate a process restart: brand-new engine, same storage. State is rehydrated.
    eng2 = GraphEngine(st, reg, MemoryService(st), runtime_for=lambda a: MockRuntime())
    r2 = eng2.resume(result.run_id, "approve", user_id="rick")
    assert r2.status is RunStatus.DONE

    _tid, state = st.load_run(result.run_id)
    assert state.status is RunStatus.DONE
    # Graph version recorded for reproducibility / migration.
    row = st.conn.execute("SELECT graph_version FROM run WHERE id=?", (result.run_id,)).fetchone()
    assert row["graph_version"] == GRAPH_VERSION


def test_no_self_grading_tester_differs_from_builder():
    st, reg, eng, tasks = _engine_with_two_agents()
    tid = tasks.create(topic="# content", title="write something", created_by="rick")
    result = eng.start(tid, "Cole")
    _tid, state = st.load_run(result.run_id)
    assert state.agent == "Cole"
    assert state.tester_agent == "Ray"


def test_pick_tester_requires_a_second_agent():
    st = Storage(":memory:")
    reg = Registry(st)
    from teleraft.agents.registry import AgentConfig
    reg.register(AgentConfig(name="Solo", soul_md="x", goals={}))
    with pytest.raises(RuntimeError):
        reg.pick_tester(exclude="Solo")


def test_token_budget_forces_escalation():
    st, reg, eng, tasks = _engine_with_two_agents()

    class GreedyRuntime(MockRuntime):
        def plan(self, req):
            plan, _ = super().plan(req)
            return plan, 10_000_000  # blow the budget immediately

    eng2 = GraphEngine(st, reg, MemoryService(st), runtime_for=lambda a: GreedyRuntime())
    tid = tasks.create(topic="# content", title="expensive task", created_by="rick")
    result = eng2.start(tid, "Cole")
    # Orchestrator sees the budget breach and suspends for a human.
    assert result.status is RunStatus.AWAITING_HUMAN
    assert result.interrupt is not None


def test_checkpoint_written_after_each_node():
    st, reg, eng, tasks = _engine_with_two_agents()
    tid = tasks.create(topic="# content", title="draft a post", created_by="rick")
    result = eng.start(tid, "Cole")
    events = st.run_events(result.run_id)
    nodes = [e["node"] for e in events]
    assert "plan" in nodes
    assert nodes.count("build") >= 2  # reject → retry means build runs at least twice
    assert nodes.count("test") >= 2
