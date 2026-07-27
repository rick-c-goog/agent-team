"""Schema migration against a persisted database.

Live regression: `CREATE TABLE IF NOT EXISTS` creates missing tables but never adds a
column to a table that already exists. A deployment whose database predated the
`sensitive` column kept working until something read it, then failed with a bare
`IndexError: No item with that key` far from the cause.

Every other test in this suite uses `:memory:`, which always gets the current schema —
so this whole class of bug was invisible to them. These tests open a **file** database
built from an older DDL, which is the only way to see it.
"""

import sqlite3

import pytest

from teleraft.storage import SCHEMA, Storage, _declared_columns, migrate

# The knowledge_source table exactly as it was before `sensitive` was introduced.
_OLD_KNOWLEDGE_SOURCE = """
CREATE TABLE knowledge_source (
    id TEXT PRIMARY KEY,
    agent_name TEXT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    uri TEXT NOT NULL,
    options_json TEXT,
    refresh_cron TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    last_synced_at REAL,
    created_by TEXT,
    created_at REAL NOT NULL
);
"""


def _old_database(path, extra_sql: str = "") -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_KNOWLEDGE_SOURCE + extra_sql)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
def test_an_old_database_gains_the_missing_column(tmp_path):
    db = tmp_path / "old.db"
    _old_database(db, """
        INSERT INTO knowledge_source VALUES
          ('src_old', NULL, 'team', 'file', 'kb/handbook.md', '{}', NULL,
           'ok', NULL, NULL, 'seed', 0);
    """)

    storage = Storage(str(db))
    assert "knowledge_source.sensitive" in storage.migrations

    rows = storage.list_sources()
    assert [r["uri"] for r in rows] == ["kb/handbook.md"], "existing data must survive"
    assert rows[0]["sensitive"] == 0, "the new column defaults, it does not vanish"
    storage.close()


def test_the_service_layer_works_after_migration(tmp_path):
    """The reported traceback was in KnowledgeService.health(), not in Storage."""
    from teleraft.knowledge.service import KnowledgeService

    db = tmp_path / "old.db"
    _old_database(db, """
        INSERT INTO knowledge_source VALUES
          ('src_old', 'Cole', 'agent', 'file', 'kb/x.md', '{}', NULL,
           'ok', NULL, NULL, 'seed', 0);
    """)

    storage = Storage(str(db))
    health = KnowledgeService(storage).health()      # used to raise IndexError
    assert health and health[0]["sensitive"] is False
    storage.close()


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "old.db"
    _old_database(db)

    first = Storage(str(db))
    assert first.migrations
    first.close()

    second = Storage(str(db))
    assert second.migrations == [], "a migrated database must not migrate again"
    second.close()


def test_a_current_database_needs_no_migration(tmp_path):
    db = tmp_path / "fresh.db"
    fresh = Storage(str(db))
    assert fresh.migrations == []
    fresh.close()

    reopened = Storage(str(db))
    assert reopened.migrations == []
    reopened.close()


def test_missing_tables_are_created_not_migrated(tmp_path):
    """An old database predates whole tables too — those come from the DDL."""
    db = tmp_path / "old.db"
    _old_database(db)

    storage = Storage(str(db))
    # Tables added long after knowledge_source existed.
    for table in ("pipeline_run", "trial", "node_run", "hypothesis"):
        assert storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone(), f"{table} should have been created"
    storage.close()


def test_a_real_app_opens_an_old_database(tmp_path):
    """End to end: this is the path that crashed on startup."""
    from teleraft.app import App

    db = tmp_path / "old.db"
    _old_database(db)

    app = App(db_path=str(db), human_ids={"1"}, sync_knowledge=False)
    assert app.knowledge.health() is not None       # the reported crash site
    assert app.metrics().tasks == 0
    app.close()


# --------------------------------------------------------------------------- #
# The parser the migration depends on
# --------------------------------------------------------------------------- #
def test_declared_columns_parses_every_table_in_the_schema():
    declared = _declared_columns(SCHEMA)
    assert "knowledge_source" in declared
    names = [n for n, _ in declared["knowledge_source"]]
    assert "sensitive" in names and "id" in names
    # Comments and trailing commas must not leak into a column name.
    assert all(" " not in n and "-" not in n for n in names), names


def test_the_parser_skips_table_level_constraints():
    for table, columns in _declared_columns(SCHEMA).items():
        for name, _decl in columns:
            assert name.upper() not in ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK"), table


def test_columns_that_cannot_be_added_are_warned_about_not_crashed(tmp_path, caplog):
    """A PRIMARY KEY or UNIQUE column needs a hand-written migration; say so."""
    db = tmp_path / "quirk.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("CREATE TABLE topic (id INTEGER PRIMARY KEY AUTOINCREMENT);")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    with caplog.at_level("WARNING"):
        applied = migrate(conn)          # `topic.name` is UNIQUE — not addable
    assert "topic.name" not in applied
    assert "hand-written migration" in caplog.text
    conn.close()


def test_not_null_columns_get_a_default_so_the_alter_succeeds(tmp_path):
    """SQLite refuses NOT NULL without a default on a populated table."""
    db = tmp_path / "nn.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE node_run (id INTEGER PRIMARY KEY AUTOINCREMENT);"
        "INSERT INTO node_run DEFAULT VALUES;")
    conn.commit()
    conn.close()

    storage = Storage(str(db))           # must not raise
    assert any(m.startswith("node_run.") for m in storage.migrations)
    storage.close()
