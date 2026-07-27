"""The §12 decisions, as executable checks.

Each test names the question it settles, so a future change to one of these behaviours
is a deliberate reversal of a recorded decision rather than an accident.
"""

import time

import pytest

from teleraft.agents.registry import AgentConfig, Registry
from teleraft.app import App
from teleraft.config import Config, load_config
from teleraft.memory.service import MemoryService
from teleraft.storage import Storage
from teleraft.telegram.gateway import Update

HUMAN = "11111111"


# --------------------------------------------------------------------------- #
# Q3 — dedicated QA agent per pillar
# --------------------------------------------------------------------------- #
def _registry_with(*agents):
    st = Storage(":memory:")
    reg = Registry(st)
    for name, role, owns in agents:
        reg.register(AgentConfig(name=name, role=role, soul_md=f"{name} soul",
                                 goals={"owns": owns}))
    return reg


def test_pillar_qa_agent_is_preferred_as_tester():
    reg = _registry_with(
        ("Quinn", "member", ["# research"]),
        ("Bailey", "qa", ["# research"]),          # this pillar's reviewer
        ("Casey", "qa", ["# content"]),            # another pillar's reviewer
        ("Zed", "member", ["# research"]),
    )
    assert reg.pick_tester(exclude="Quinn") == "Bailey"


def test_any_qa_agent_beats_a_non_qa_peer():
    reg = _registry_with(
        ("Quinn", "member", ["# research"]),
        ("Casey", "qa", ["# content"]),            # no overlap, but review is its job
        ("Alice", "member", ["# research"]),       # would win alphabetically
    )
    assert reg.pick_tester(exclude="Quinn") == "Casey"


def test_falls_back_to_any_peer_when_no_qa_agent_exists():
    reg = _registry_with(("Quinn", "member", ["# research"]),
                         ("Alice", "member", ["# research"]))
    assert reg.pick_tester(exclude="Quinn") == "Alice"


def test_a_qa_agent_never_reviews_itself():
    reg = _registry_with(("Bailey", "qa", ["# research"]),
                         ("Quinn", "member", ["# research"]))
    assert reg.pick_tester(exclude="Bailey") == "Quinn"


def test_the_quant_desk_routes_review_to_its_qa_seat():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    assert app.registry.role("Bailey") == "qa"
    assert app.registry.pick_tester(exclude="Quinn") == "Bailey"
    app.close()


# --------------------------------------------------------------------------- #
# Q1 — puppet mode is the default; per_agent needs its usernames
# --------------------------------------------------------------------------- #
def test_puppet_is_the_default_bot_mode():
    assert Config().bot_mode == "puppet"


def test_per_agent_mode_requires_usernames(tmp_path, monkeypatch):
    cfg_file = tmp_path / "teleraft.toml"
    cfg_file.write_text('[telegram]\nbot_mode = "per_agent"\n')
    monkeypatch.delenv("TELERAFT_BOT_MODE", raising=False)
    with pytest.raises(ValueError, match="agent_usernames"):
        load_config(str(cfg_file))


def test_an_unknown_bot_mode_is_rejected(tmp_path):
    cfg_file = tmp_path / "teleraft.toml"
    cfg_file.write_text('[telegram]\nbot_mode = "hologram"\n')
    with pytest.raises(ValueError, match="puppet"):
        load_config(str(cfg_file))


def test_puppet_mode_resolves_mentions_by_display_name():
    """With one bot there are no per-agent @usernames, so display names must route."""
    from teleraft.telegram.runner import LiveRunner

    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    cfg = Config(group_chat_id="-100", human_ids={HUMAN})   # no agent_usernames at all
    runner = LiveRunner(client=None, gateway=app.gateway, config=cfg)

    update = runner.normalize_message({
        "chat": {"id": "-100"},
        "from": {"id": int(HUMAN), "username": "rick", "is_bot": False},
        "text": "@Quinn is there a momentum edge on SPY",
    })
    assert update is not None and update.mentions == ["Quinn"]
    app.close()


# --------------------------------------------------------------------------- #
# Q4 — weekly automated memory consolidation, no human review
# --------------------------------------------------------------------------- #
def test_consolidation_merges_near_duplicate_lessons():
    st = Storage(":memory:")
    mem = MemoryService(st)
    for _ in range(4):
        mem.write("Cole", "Always attach a source to any statistic before review",
                  "tester", None)
    mem.write("Cole", "Always attach a source to any statistic before the review",
              "tester", None)                                  # restatement
    mem.write("Cole", "Launch posts need the registration link above the fold",
              "tester", None)                                  # genuinely different

    report = mem.consolidate("Cole")
    assert report["before"] == 6 and report["after"] == 2
    assert report["merged"] == 4
    kept = [r["content_md"] for r in st.memories_for("Cole")]
    assert any("statistic" in k for k in kept) and any("registration" in k for k in kept)


def test_consolidation_keeps_distinct_lessons_untouched():
    st = Storage(":memory:")
    mem = MemoryService(st)
    for lesson in ("cite every statistic", "registration link above the fold",
                   "never quote a price without approval"):
        mem.write("Cole", lesson, "tester", None)
    report = mem.consolidate("Cole")
    assert report["merged"] == 0 and report["after"] == 3


def test_consolidation_caps_unbounded_growth_oldest_first():
    st = Storage(":memory:")
    mem = MemoryService(st)
    for i in range(30):
        mem.write("Cole", f"distinct lesson number {i} about topic {i}", "tester", None)
    report = mem.consolidate("Cole", keep_recent=10)
    assert report["after"] == 10 and report["dropped"] == 20
    kept = " ".join(r["content_md"] for r in st.memories_for("Cole"))
    assert "number 29" in kept and "number 0 " not in kept   # newest survive


def test_consolidation_is_registered_weekly_and_unattended():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    program = next(p for p in app.scheduler.programs() if p.name == "memory-consolidation")
    assert program.cron == "0 4 * * 0"                        # Sunday 04:00, weekly
    assert program.body() is not None                          # runs with no human input
    app.close()


# --------------------------------------------------------------------------- #
# Q7 — sensitive sources never leave the host
# --------------------------------------------------------------------------- #
class _HostedEmbedding:
    """Stands in for a third-party embedding API; records everything it is shown."""

    dim = 8

    def __init__(self):
        self.seen: list[str] = []

    def embed(self, texts):
        self.seen.extend(texts)
        return [[0.0] * self.dim for _ in texts]


def test_sensitive_source_text_is_never_sent_to_a_hosted_provider(kb_dir):
    from teleraft.knowledge.service import KnowledgeService

    st = Storage(":memory:")
    hosted = _HostedEmbedding()
    kb = KnowledgeService(st, embedding=hosted, knowledge_root=str(kb_dir))

    secret = kb.add_source("Penn", "file", "cole/brand.md", sensitive=True)
    kb.sync_source(secret)
    assert hosted.seen == [], "confidential text reached the hosted embedder"

    public = kb.add_source("Cole", "file", "cole/notes.txt")
    kb.sync_source(public)
    assert hosted.seen, "non-sensitive text should use the configured embedder"


def test_sensitive_sources_are_still_retrievable(kb_dir):
    from teleraft.knowledge.service import KnowledgeService

    st = Storage(":memory:")
    kb = KnowledgeService(st, embedding=_HostedEmbedding(), knowledge_root=str(kb_dir))
    kb.sync_source(kb.add_source("Penn", "file", "cole/brand.md", sensitive=True))
    hits = kb.retrieve("Penn", "what must a launch post include", k=3)
    assert hits and "registration link" in hits[0].text


def test_health_reports_which_sources_are_sensitive(kb_dir):
    from teleraft.knowledge.service import KnowledgeService

    st = Storage(":memory:")
    kb = KnowledgeService(st, knowledge_root=str(kb_dir))
    kb.sync_source(kb.add_source("Penn", "file", "cole/brand.md", sensitive=True))
    assert any(h["sensitive"] for h in kb.health())


# --------------------------------------------------------------------------- #
# Q9 — more retrieval budget after a grounding rejection
# --------------------------------------------------------------------------- #
def test_retrieval_widens_after_an_ungrounded_rejection():
    from teleraft.models import RunState, Verdict

    app = App(human_ids={HUMAN})
    engine = app.engine
    state = RunState(task_id="t", agent="Cole", current_step=0)

    assert engine._last_rejection_was_grounding(state) is False
    state.verdicts.append(Verdict(step=0, passed=False,
                                  reasons=["Ungrounded: the draft cites no source"]))
    assert engine._last_rejection_was_grounding(state) is True

    # A non-grounding rejection must not spend the extra budget.
    state.verdicts.append(Verdict(step=0, passed=False, reasons=["tone is wrong"]))
    assert engine._last_rejection_was_grounding(state) is False
    app.close()


# --------------------------------------------------------------------------- #
# Q10 — stale sources warn on the review card, never block
# --------------------------------------------------------------------------- #
def test_stale_source_warns_on_the_review_card_without_blocking(kb_dir):
    app = App(human_ids={HUMAN}, knowledge_root=str(kb_dir), sync_knowledge=False)
    source_id = app.knowledge.add_source("Cole", "file", "cole/brand.md")
    app.knowledge.sync_source(source_id)
    # Backdate the sync well past the staleness threshold.
    app.storage.conn.execute(
        "UPDATE knowledge_source SET last_synced_at=? WHERE id=?",
        (time.time() - 30 * 86400, source_id))
    app.storage.conn.commit()

    assert app.knowledge.stale_or_failing()

    result = app.gateway.handle_message(
        Update(text="@Cole write the launch post", user_id=HUMAN, user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"]))
    card = next(m.text for m in app.client.messages.values()
                if "In Review" in m.text and "Draft" in m.text)

    assert "⚠️" in card and "Stale evidence" in card
    assert result is not None, "a stale source must warn, not block the run"
    app.close()


def test_a_freshly_synced_source_produces_no_warning(kb_dir):
    app = App(human_ids={HUMAN}, knowledge_root=str(kb_dir), sync_knowledge=False)
    source_id = app.knowledge.add_source("Cole", "file", "cole/brand.md")
    app.knowledge.sync_source(source_id)

    stale_ids = {s["id"] for s in app.knowledge.stale_or_failing()}
    assert source_id not in stale_ids
    app.close()


def test_a_never_synced_source_counts_as_stale(kb_dir):
    """An unsynced source is untrustworthy evidence, and says so."""
    app = App(human_ids={HUMAN}, knowledge_root=str(kb_dir), sync_knowledge=False)
    source_id = app.knowledge.add_source("Cole", "file", "cole/brand.md")

    entry = next(s for s in app.knowledge.stale_or_failing() if s["id"] == source_id)
    assert entry["why"] == "never synced"
    app.close()
