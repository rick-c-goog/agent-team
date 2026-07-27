"""SQLite persistence (DESIGN.md §7).

A single-file store that is the *source of truth*; Telegram messages only carry stable
``task_id`` references so state can always be reconciled. Chosen SQLite over Postgres
purely so the reference implementation runs with zero external services — the schema is
a faithful subset of §7 and the access layer would port to Postgres unchanged.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable, Optional

from .models import RunState, TaskStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (
    id INTEGER PRIMARY KEY,
    tg_supergroup_id TEXT,
    tg_channel_id TEXT,
    name TEXT,
    owner_tg_user_id TEXT
);

CREATE TABLE IF NOT EXISTS topic (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    pillar TEXT,
    tg_topic_id TEXT
);

CREATE TABLE IF NOT EXISTS agent (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    soul_version INTEGER NOT NULL DEFAULT 1,
    goals_json TEXT,
    runtime_engine TEXT,
    computer TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS soul_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_md TEXT NOT NULL,
    changed_by TEXT,
    reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    content_md TEXT NOT NULL,
    source TEXT NOT NULL,          -- tester | human_reject | self
    task_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL,
    owner TEXT,                    -- agent name or human handle
    tg_card_message_id TEXT,
    parent_task_id TEXT,
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    state_json TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint_seq INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS run_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    node TEXT NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    gate TEXT NOT NULL,
    tg_user_id TEXT,
    decision TEXT NOT NULL,        -- approve | reject | adjust
    reason TEXT,
    created_at REAL NOT NULL
);

-- §4.1 Knowledge base -------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_source (
    id TEXT PRIMARY KEY,
    agent_name TEXT,               -- NULL when scope='team'
    scope TEXT NOT NULL,           -- agent | team
    type TEXT NOT NULL,            -- web | gdrive | file | upload
    uri TEXT NOT NULL,
    options_json TEXT,
    refresh_cron TEXT,
    sensitive INTEGER NOT NULL DEFAULT 0,  -- 1 = never send text to a hosted provider
    status TEXT NOT NULL,          -- ok | error | syncing | pending
    last_error TEXT,
    last_synced_at REAL,
    created_by TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_doc (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,     -- url, drive fileId, path
    title TEXT,
    mime TEXT,
    content_hash TEXT NOT NULL,
    bytes INTEGER,
    tombstoned_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    locator TEXT,                  -- "p.12" | "# Brand > ## Tone" | "row 4"
    token_count INTEGER,
    embedding TEXT,                -- JSON float list (pgvector in production)
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS citation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    source_id TEXT,
    doc TEXT,
    locator TEXT,
    quote TEXT,
    created_at REAL NOT NULL
);

-- §3.3 Onboarding -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS onboarding_session (
    id TEXT PRIMARY KEY,
    host TEXT NOT NULL,            -- hermes | openclaw | mock
    tg_user_id TEXT,
    answers_json TEXT,
    plan_yaml TEXT,
    status TEXT NOT NULL,          -- interviewing | awaiting_approval | applying | done | failed
    run_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Quant research: hypothesis registry (self-improving loop) --------------------
CREATE TABLE IF NOT EXISTS hypothesis (
    id TEXT PRIMARY KEY,
    agent_name TEXT,
    statement TEXT NOT NULL,
    universe TEXT,
    status TEXT NOT NULL,          -- proposed | testing | supported | invalidated | retired
    rationale TEXT,
    invalidated_reason TEXT,
    parent_id TEXT,                -- research lineage: refinement of an earlier idea
    task_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    run_id TEXT,
    sample TEXT NOT NULL,          -- in_sample | out_of_sample
    spec_json TEXT NOT NULL,
    symbol TEXT,
    period_start TEXT,
    period_end TEXT,
    metrics_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunk_doc ON knowledge_chunk(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_source ON knowledge_doc(source_id);
CREATE INDEX IF NOT EXISTS idx_bt_hypothesis ON backtest_result(hypothesis_id);
"""


def _now() -> float:
    return time.time()


class Storage:
    """Thin, synchronous data-access layer over SQLite."""

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False keeps the demo/tests simple; production would pool.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- agents ------------------------------------------------------------ #
    def upsert_agent(
        self,
        name: str,
        role: str,
        goals_json: str,
        runtime_engine: str,
        computer: str,
        soul_md: str,
    ) -> None:
        cur = self.conn.execute("SELECT soul_version FROM agent WHERE name=?", (name,))
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO agent(name, role, soul_version, goals_json, runtime_engine, computer, status)"
                " VALUES(?,?,?,?,?,?, 'active')",
                (name, role, 1, goals_json, runtime_engine, computer),
            )
            self.conn.execute(
                "INSERT INTO soul_version(agent_name, version, content_md, changed_by, reason, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (name, 1, soul_md, "seed", "initial soul", _now()),
            )
        else:
            self.conn.execute(
                "UPDATE agent SET role=?, goals_json=?, runtime_engine=?, computer=? WHERE name=?",
                (role, goals_json, runtime_engine, computer, name),
            )
        self.conn.commit()

    def get_agent(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agent WHERE name=?", (name,)).fetchone()

    def list_agents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agent ORDER BY name").fetchall()

    def current_soul(self, name: str) -> str:
        row = self.conn.execute(
            "SELECT content_md FROM soul_version WHERE agent_name=? ORDER BY version DESC LIMIT 1",
            (name,),
        ).fetchone()
        return row["content_md"] if row else ""

    def amend_soul(self, name: str, addition_md: str, changed_by: str, reason: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) AS v FROM soul_version WHERE agent_name=?", (name,)
        ).fetchone()
        new_version = (row["v"] or 0) + 1
        base = self.current_soul(name)
        content = base.rstrip() + "\n\n## Amendment v%d\n%s\n" % (new_version, addition_md)
        self.conn.execute(
            "INSERT INTO soul_version(agent_name, version, content_md, changed_by, reason, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (name, new_version, content, changed_by, reason, _now()),
        )
        self.conn.execute("UPDATE agent SET soul_version=? WHERE name=?", (new_version, name))
        self.conn.commit()
        return new_version

    # -- memory ------------------------------------------------------------ #
    def add_memory(self, agent_name: str, content_md: str, source: str, task_id: Optional[str]) -> None:
        self.conn.execute(
            "INSERT INTO memory_note(agent_name, content_md, source, task_id, created_at)"
            " VALUES(?,?,?,?,?)",
            (agent_name, content_md, source, task_id, _now()),
        )
        self.conn.commit()

    def delete_memory(self, memory_id: int) -> None:
        self.conn.execute("DELETE FROM memory_note WHERE id=?", (memory_id,))
        self.conn.commit()

    def memories_for(self, agent_name: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM memory_note WHERE agent_name=? ORDER BY created_at", (agent_name,)
        ).fetchall()

    # -- tasks ------------------------------------------------------------- #
    def create_task(
        self,
        task_id: str,
        topic: str,
        title: str,
        body: str,
        created_by: str,
        parent_task_id: Optional[str] = None,
    ) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO task(id, topic, title, body, status, owner, tg_card_message_id,"
            " parent_task_id, created_by, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, topic, title, body, TaskStatus.TODO.value, None, None,
             parent_task_id, created_by, now, now),
        )
        self.conn.commit()

    def get_task(self, task_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE task SET {cols} WHERE id=?", (*fields.values(), task_id))
        self.conn.commit()

    def list_tasks(self, topic: Optional[str] = None, status: Optional[str] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM task"
        clauses, args = [], []
        if topic:
            clauses.append("topic=?"); args.append(topic)
        if status:
            clauses.append("status=?"); args.append(status)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at"
        return self.conn.execute(q, args).fetchall()

    # -- runs & checkpointing --------------------------------------------- #
    def create_run(self, run_id: str, task_id: str, graph_version: str, state: RunState) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO run(id, task_id, graph_version, state_json, status, checkpoint_seq, started_at)"
            " VALUES(?,?,?,?,?,0,?)",
            (run_id, task_id, graph_version, state.to_json(), state.status.value, now),
        )
        self.conn.commit()

    def checkpoint(self, run_id: str, state: RunState) -> None:
        """Persist RunState after a node — the resume point on crash/restart."""
        self.conn.execute(
            "UPDATE run SET state_json=?, status=?, checkpoint_seq=checkpoint_seq+1 WHERE id=?",
            (state.to_json(), state.status.value, run_id),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, state: RunState) -> None:
        self.conn.execute(
            "UPDATE run SET state_json=?, status=?, finished_at=? WHERE id=?",
            (state.to_json(), state.status.value, _now(), run_id),
        )
        self.conn.commit()

    def load_run(self, run_id: str) -> Optional[tuple[str, RunState]]:
        row = self.conn.execute("SELECT task_id, state_json FROM run WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return row["task_id"], RunState.from_json(row["state_json"])

    def run_for_task(self, task_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT id FROM run WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,)
        ).fetchone()
        return row["id"] if row else None

    def add_run_event(self, run_id: str, seq: int, node: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO run_event(run_id, seq, node, detail, created_at) VALUES(?,?,?,?,?)",
            (run_id, seq, node, detail, _now()),
        )
        self.conn.commit()

    def run_events(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM run_event WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()

    # -- approvals --------------------------------------------------------- #
    def add_approval(self, run_id: str, gate: str, tg_user_id: str, decision: str, reason: str = "") -> None:
        self.conn.execute(
            "INSERT INTO approval(run_id, gate, tg_user_id, decision, reason, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, gate, tg_user_id, decision, reason, _now()),
        )
        self.conn.commit()

    # -- knowledge sources ------------------------------------------------- #
    def add_source(self, source_id: str, agent_name: Optional[str], scope: str, type_: str,
                   uri: str, options_json: str = "{}", refresh_cron: Optional[str] = None,
                   created_by: str = "system", sensitive: bool = False) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO knowledge_source(id, agent_name, scope, type, uri,"
            " options_json, refresh_cron, sensitive, status, last_error, last_synced_at,"
            " created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,?)",
            (source_id, agent_name, scope, type_, uri, options_json, refresh_cron,
             1 if sensitive else 0, created_by, _now()),
        )
        self.conn.commit()

    def get_source(self, source_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM knowledge_source WHERE id=?", (source_id,)
        ).fetchone()

    def find_source(self, agent_name: Optional[str], uri: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM knowledge_source WHERE uri=? AND IFNULL(agent_name,'')=IFNULL(?,'')",
            (uri, agent_name),
        ).fetchone()

    def sources_for(self, agent_name: str, include_team: bool = True) -> list[sqlite3.Row]:
        """Agent-scoped sources first, then shared team sources (DESIGN.md §4.1.3)."""
        if include_team:
            return self.conn.execute(
                "SELECT * FROM knowledge_source WHERE agent_name=? OR scope='team'"
                " ORDER BY (scope='team'), created_at",
                (agent_name,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM knowledge_source WHERE agent_name=? ORDER BY created_at",
            (agent_name,),
        ).fetchall()

    def list_sources(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM knowledge_source ORDER BY created_at"
        ).fetchall()

    def set_source_status(self, source_id: str, status: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE knowledge_source SET status=?, last_error=?, last_synced_at=? WHERE id=?",
            (status, error or None, _now(), source_id),
        )
        self.conn.commit()

    def remove_source(self, source_id: str) -> None:
        docs = [r["id"] for r in self.conn.execute(
            "SELECT id FROM knowledge_doc WHERE source_id=?", (source_id,)).fetchall()]
        for doc_id in docs:
            self.conn.execute("DELETE FROM knowledge_chunk WHERE doc_id=?", (doc_id,))
        self.conn.execute("DELETE FROM knowledge_doc WHERE source_id=?", (source_id,))
        self.conn.execute("DELETE FROM knowledge_source WHERE id=?", (source_id,))
        self.conn.commit()

    # -- knowledge docs & chunks ------------------------------------------- #
    def get_doc(self, doc_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM knowledge_doc WHERE id=?", (doc_id,)).fetchone()

    def upsert_doc(self, doc_id: str, source_id: str, external_id: str, title: str,
                   mime: str, content_hash: str, size: int) -> None:
        self.conn.execute(
            "INSERT INTO knowledge_doc(id, source_id, external_id, title, mime, content_hash,"
            " bytes, tombstoned_at, updated_at) VALUES(?,?,?,?,?,?,?,NULL,?)"
            " ON CONFLICT(id) DO UPDATE SET title=excluded.title, mime=excluded.mime,"
            " content_hash=excluded.content_hash, bytes=excluded.bytes,"
            " tombstoned_at=NULL, updated_at=excluded.updated_at",
            (doc_id, source_id, external_id, title, mime, content_hash, size, _now()),
        )
        self.conn.commit()

    def docs_for_source(self, source_id: str, include_tombstoned: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM knowledge_doc WHERE source_id=?"
        if not include_tombstoned:
            q += " AND tombstoned_at IS NULL"
        return self.conn.execute(q + " ORDER BY updated_at", (source_id,)).fetchall()

    def tombstone_doc(self, doc_id: str) -> None:
        """Source removed the document: hide it from retrieval, keep the audit row."""
        self.conn.execute("UPDATE knowledge_doc SET tombstoned_at=? WHERE id=?", (_now(), doc_id))
        self.conn.execute("DELETE FROM knowledge_chunk WHERE doc_id=?", (doc_id,))
        self.conn.commit()

    def replace_chunks(self, doc_id: str, chunks: list[tuple[str, str, int, str]]) -> None:
        """chunks: (text, locator, token_count, embedding_json) — replaces the doc's chunks."""
        self.conn.execute("DELETE FROM knowledge_chunk WHERE doc_id=?", (doc_id,))
        now = _now()
        self.conn.executemany(
            "INSERT INTO knowledge_chunk(doc_id, seq, text, locator, token_count, embedding,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            [(doc_id, i, t, loc, tc, emb, now) for i, (t, loc, tc, emb) in enumerate(chunks)],
        )
        self.conn.commit()

    def chunks_for_agent(self, agent_name: str) -> list[sqlite3.Row]:
        """All live chunks visible to an agent (its own sources + team sources)."""
        return self.conn.execute(
            "SELECT c.*, d.title AS doc_title, d.source_id AS source_id"
            " FROM knowledge_chunk c"
            " JOIN knowledge_doc d ON d.id = c.doc_id"
            " JOIN knowledge_source s ON s.id = d.source_id"
            " WHERE d.tombstoned_at IS NULL AND (s.agent_name=? OR s.scope='team')",
            (agent_name,),
        ).fetchall()

    def count_chunks(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM knowledge_chunk c JOIN knowledge_doc d ON d.id=c.doc_id"
            " WHERE d.source_id=?", (source_id,)
        ).fetchone()
        return row["n"]

    # -- citations ---------------------------------------------------------- #
    def add_citations(self, run_id: str, step: int, citations) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT INTO citation(run_id, step, source_id, doc, locator, quote, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            [(run_id, step, c.source_id, c.doc, c.locator, c.quote, now) for c in citations],
        )
        self.conn.commit()

    def citations_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM citation WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()

    # -- onboarding --------------------------------------------------------- #
    def create_onboarding(self, session_id: str, host: str, tg_user_id: str) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO onboarding_session(id, host, tg_user_id, answers_json, plan_yaml,"
            " status, run_id, created_at, updated_at) VALUES(?,?,?,'{}',NULL,'interviewing',NULL,?,?)",
            (session_id, host, tg_user_id, now, now),
        )
        self.conn.commit()

    def update_onboarding(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE onboarding_session SET {cols} WHERE id=?", (*fields.values(), session_id)
        )
        self.conn.commit()

    def get_onboarding(self, session_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM onboarding_session WHERE id=?", (session_id,)
        ).fetchone()

    # -- hypotheses & backtests (quant research) --------------------------- #
    def add_hypothesis(self, hid: str, agent_name: str, statement: str, universe: str,
                       rationale: str, parent_id: Optional[str], task_id: Optional[str]) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO hypothesis(id, agent_name, statement, universe, status, rationale,"
            " invalidated_reason, parent_id, task_id, created_at, updated_at)"
            " VALUES(?,?,?,?,'proposed',?,NULL,?,?,?,?)",
            (hid, agent_name, statement, universe, rationale, parent_id, task_id, now, now),
        )
        self.conn.commit()

    def get_hypothesis(self, hid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM hypothesis WHERE id=?", (hid,)).fetchone()

    def list_hypotheses(self, status: Optional[str] = None,
                        agent_name: Optional[str] = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM hypothesis", []
        clauses = []
        if status:
            clauses.append("status=?"); args.append(status)
        if agent_name:
            clauses.append("agent_name=?"); args.append(agent_name)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(q + " ORDER BY created_at", args).fetchall()

    def update_hypothesis(self, hid: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE hypothesis SET {cols} WHERE id=?", (*fields.values(), hid))
        self.conn.commit()

    def add_backtest_result(self, hypothesis_id: str, run_id: Optional[str], sample: str,
                            spec_json: str, symbol: str, period_start: str, period_end: str,
                            metrics_json: str) -> None:
        self.conn.execute(
            "INSERT INTO backtest_result(hypothesis_id, run_id, sample, spec_json, symbol,"
            " period_start, period_end, metrics_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (hypothesis_id, run_id, sample, spec_json, symbol, period_start, period_end,
             metrics_json, _now()),
        )
        self.conn.commit()

    def backtests_for(self, hypothesis_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM backtest_result WHERE hypothesis_id=? ORDER BY id",
            (hypothesis_id,),
        ).fetchall()

    # -- topics ------------------------------------------------------------ #
    def upsert_topic(self, name: str, pillar: str = "") -> None:
        self.conn.execute(
            "INSERT INTO topic(name, pillar) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET pillar=excluded.pillar",
            (name, pillar),
        )
        self.conn.commit()
