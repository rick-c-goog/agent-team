"""Task lifecycle + memory/self-improvement unit tests."""

import pytest

from teleraft.memory.service import MemoryService, SOUL_AMENDMENT_THRESHOLD
from teleraft.models import TaskStatus
from teleraft.storage import Storage
from teleraft.tasks.service import TaskConflict, TaskService


def test_single_owner_claim_conflict():
    st = Storage(":memory:")
    tasks = TaskService(st)
    tid = tasks.create(topic="# content", title="a task", created_by="rick")
    tasks.claim(tid, "Cole")
    with pytest.raises(TaskConflict):
        tasks.claim(tid, "Ray")
    # Unclaim returns it to the pool; another owner may then take it.
    tasks.unclaim(tid)
    tasks.claim(tid, "Ray")
    assert tasks.get(tid)["owner"] == "Ray"


def test_task_board_groups_by_status():
    st = Storage(":memory:")
    tasks = TaskService(st)
    a = tasks.create(topic="# content", title="A", created_by="rick")
    b = tasks.create(topic="# content", title="B", created_by="rick")
    tasks.set_status(b, TaskStatus.DONE)
    board = tasks.board(topic="# content")
    assert [t["id"] for t in board["todo"]] == [a]
    assert [t["id"] for t in board["done"]] == [b]


def test_memory_recall_ranks_relevant_notes():
    st = Storage(":memory:")
    mem = MemoryService(st)
    mem.write("Cole", "Launch posts must include the registration link above the fold", "tester", None)
    mem.write("Cole", "Finance rejects invoices missing PO numbers", "tester", None)
    hits = mem.recall("Cole", "writing a launch post with a registration link", k=1)
    assert hits and "registration link" in hits[0]


def test_recurring_lesson_triggers_soul_amendment():
    st = Storage(":memory:")
    st.upsert_agent("Penn", "admin", "{}", "mock", "vm", "# Penn soul\nInitial rules.")
    mem = MemoryService(st)
    lesson = "Always request the PO number before invoicing"
    amendment = None
    for _ in range(SOUL_AMENDMENT_THRESHOLD):
        mem.write("Penn", lesson, "human_reject", "task-1")
        amendment = mem.propose_soul_amendment("Penn", lesson, "task-1")
    assert amendment == lesson
    assert lesson in st.current_soul("Penn")
    # A soul version was appended.
    row = st.conn.execute("SELECT MAX(version) v FROM soul_version WHERE agent_name='Penn'").fetchone()
    assert row["v"] >= 2
