"""RAG inside the Anthropic loop: grounding enforcement, citations, /kb commands."""

from teleraft.agents.registry import AgentConfig, Registry
from teleraft.app import App
from teleraft.graph.engine import GraphEngine
from teleraft.knowledge.service import KnowledgeService
from teleraft.memory.service import MemoryService
from teleraft.models import RunStatus
from teleraft.runtime.mock import MockRuntime
from teleraft.storage import Storage
from teleraft.tasks.service import TaskService
from teleraft.telegram.gateway import Update


def _grounded_engine(kb_dir, *, cite_knowledge=True):
    st = Storage(":memory:")
    reg = Registry(st)
    reg.register(AgentConfig(name="Cole", soul_md="content", goals={"escalate_when": []}))
    reg.register(AgentConfig(name="Ray", soul_md="reviewer", goals={"escalate_when": []}))
    kb = KnowledgeService(st, knowledge_root=str(kb_dir))
    kb.sync_source(kb.add_source("Cole", "file", "cole"))
    rt = MockRuntime(fail_first_build=False, cite_knowledge=cite_knowledge)
    engine = GraphEngine(st, reg, MemoryService(st), runtime_for=lambda a: rt, knowledge=kb)
    return st, kb, engine, TaskService(st)


def test_builder_cites_retrieved_knowledge_and_citations_are_persisted(kb_dir):
    st, kb, engine, tasks = _grounded_engine(kb_dir)
    tid = tasks.create(topic="# content", title="write the launch post", created_by="rick")
    result = engine.start(tid, "Cole")

    assert result.status is RunStatus.AWAITING_HUMAN         # stopped at review gate
    _tid, state = st.load_run(result.run_id)
    assert state.knowledge, "intake should have retrieved knowledge"
    artifact = state.latest_artifact
    assert artifact.citations, "builder must cite the passages it used"
    assert any("brand.md" in c.doc for c in artifact.citations)

    rows = st.citations_for_run(result.run_id)
    assert rows and rows[0]["locator"].startswith("#")        # markdown heading path


def test_uncited_draft_is_rejected_by_the_tester_as_ungrounded(kb_dir):
    # The builder ignores retrieved passages; the Tester must catch it (§4.1.3).
    st, kb, engine, tasks = _grounded_engine(kb_dir, cite_knowledge=False)
    tid = tasks.create(topic="# content", title="write the launch post", created_by="rick")
    result = engine.start(tid, "Cole")

    _tid, state = st.load_run(result.run_id)
    first = state.verdicts[0]
    assert not first.passed
    assert "ungrounded" in " ".join(first.reasons).lower()
    # …and the retry, which does cite, passes.
    assert any(v.passed for v in state.verdicts)


def test_engine_without_knowledge_service_still_runs(kb_dir):
    """RAG is additive: an ungrounded workspace behaves exactly as before."""
    st = Storage(":memory:")
    reg = Registry(st)
    reg.register(AgentConfig(name="Cole", soul_md="c", goals={}))
    reg.register(AgentConfig(name="Ray", soul_md="r", goals={}))
    engine = GraphEngine(st, reg, MemoryService(st), runtime_for=lambda a: MockRuntime())
    tasks = TaskService(st)
    tid = tasks.create(topic="# content", title="write something", created_by="rick")
    result = engine.start(tid, "Cole")
    assert result.status is RunStatus.AWAITING_HUMAN
    _tid, state = st.load_run(result.run_id)
    assert state.knowledge == []


def test_broken_knowledge_index_degrades_instead_of_failing_the_run(kb_dir):
    st, kb, engine, tasks = _grounded_engine(kb_dir)

    class Exploding:
        def retrieve(self, agent, query, k=5):
            raise RuntimeError("index unavailable")

    engine.knowledge = Exploding()
    tid = tasks.create(topic="# content", title="write the launch post", created_by="rick")
    result = engine.start(tid, "Cole")
    assert result.status is RunStatus.AWAITING_HUMAN         # run survived


# --------------------------------------------------------------------------- #
# /kb commands
# --------------------------------------------------------------------------- #
def _kb_update(text, topic="# content"):
    return Update(text=text, user_id="rick", user_handle="rick", topic=topic)


def test_kb_add_list_sync_remove_round_trip(kb_dir):
    app = App(human_ids={"rick"}, knowledge_root=str(kb_dir), sync_knowledge=False)

    report = app.gateway.handle_kb_command(_kb_update("/kb add cole/brand.md"))
    assert report.ok and report.docs_indexed == 1

    rows = app.gateway.handle_kb_command(_kb_update("/kb list"))
    assert any(r["uri"] == "cole/brand.md" and r["status"] == "ok" for r in rows)
    assert any("Knowledge sources" in t for t in app.client.transcript)

    source_id = next(r["id"] for r in app.knowledge.health() if r["uri"] == "cole/brand.md")

    # `/kb sync` with no args syncs every source the topic's agent can see and reports
    # each one individually — a broken source never hides a healthy one.
    reports = app.gateway.handle_kb_command(_kb_update("/kb sync"))
    by_id = {r.source_id: r for r in reports}
    assert by_id[source_id].ok
    assert by_id[source_id].docs_skipped == 1        # unchanged since the add above

    app.gateway.handle_kb_command(_kb_update(f"/kb remove {source_id}"))
    assert all(r["id"] != source_id for r in app.knowledge.health())
    app.close()


def test_kb_add_scopes_to_the_topic_owning_agent(kb_dir):
    app = App(human_ids={"rick"}, knowledge_root=str(kb_dir), sync_knowledge=False)
    app.gateway.handle_kb_command(_kb_update("/kb add cole/tiers.csv", topic="# content"))
    entry = next(r for r in app.knowledge.health() if r["uri"] == "cole/tiers.csv")
    assert entry["agent"] == "Cole"          # # content is owned by Cole in agents/cole.yaml
    app.close()


def test_kb_add_team_scope_is_visible_to_every_agent(kb_dir):
    app = App(human_ids={"rick"}, knowledge_root=str(kb_dir), sync_knowledge=False)
    app.gateway.handle_kb_command(_kb_update("/kb add cole/tiers.csv --team"))
    assert app.knowledge.retrieve("Penn", "Growth tier price", k=3)
    app.close()


def test_kb_failure_is_surfaced_not_silent(kb_dir):
    app = App(human_ids={"rick"}, knowledge_root=str(kb_dir), sync_knowledge=False)
    report = app.gateway.handle_kb_command(_kb_update("/kb add cole/missing.md"))
    assert not report.ok
    assert any("❌" in t for t in app.client.transcript)
    app.close()
