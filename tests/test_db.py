"""tests/test_db.py — Unit tests for db.py schema initialisation and migrations.

All tests use an in-memory SQLite database so nba.db is never touched.
"""

import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _NoCloseConn:
    """Proxy for sqlite3.Connection that makes close() a no-op.

    init_db() calls conn.close() at the end, which destroys the in-memory DB.
    This wrapper intercepts that call so the schema remains accessible after
    init_db() returns.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)

    def close(self) -> None:  # no-op
        pass

    def cursor(self):
        return object.__getattribute__(self, "_conn").cursor()

    def execute(self, *args, **kwargs):
        return object.__getattribute__(self, "_conn").execute(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return object.__getattribute__(self, "_conn").executescript(*args, **kwargs)

    def commit(self):
        object.__getattribute__(self, "_conn").commit()


def _make_memory_conn() -> sqlite3.Connection:
    """Return a sqlite3 connection to an in-memory DB (row_factory set)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_names(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _column_names(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}  # column index 1 = name


def _index_names(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


@contextmanager
def _init_memory_db():
    """Run init_db() against a fresh in-memory DB and yield the open connection.

    Uses _NoCloseConn so init_db()'s conn.close() call does not destroy the
    in-memory schema.  The underlying connection is closed in the finally block.
    """
    real_conn = _make_memory_conn()
    proxy = _NoCloseConn(real_conn)

    def fake_get_conn():
        return proxy

    with patch.object(db, "get_conn", fake_get_conn):
        db.init_db()

    try:
        yield real_conn
    finally:
        real_conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInitDbCreatesTables(unittest.TestCase):
    """init_db() creates every expected table."""

    EXPECTED_TABLES = {
        "seasons",
        "players",
        "player_stats",
        "selected_players",
        "team_game_logs",
        "player_game_appearances",
        "watched_playoff_games",
        "watched_game_players",
        "bbref_playoff_fetch_log",
        "league_game_log_fetch_log",
    }

    def test_all_tables_exist(self):
        with _init_memory_db() as conn:
            tables = _table_names(conn)
            for expected in self.EXPECTED_TABLES:
                with self.subTest(table=expected):
                    self.assertIn(expected, tables)


class TestInitDbIdempotent(unittest.TestCase):
    """Calling init_db() twice must not raise or duplicate data."""

    def _run_double_init(self):
        real_conn = _make_memory_conn()
        proxy = _NoCloseConn(real_conn)

        def fake_get_conn():
            return proxy

        with patch.object(db, "get_conn", fake_get_conn):
            db.init_db()
            db.init_db()

        return real_conn

    def test_double_init_does_not_raise(self):
        conn = self._run_double_init()
        conn.close()

    def test_double_init_does_not_duplicate_tables(self):
        conn = self._run_double_init()
        try:
            rows = conn.execute(
                "SELECT name, COUNT(*) AS cnt FROM sqlite_master "
                "WHERE type='table' GROUP BY name HAVING cnt > 1"
            ).fetchall()
            self.assertEqual(rows, [], msg="Duplicate table entries found after double init")
        finally:
            conn.close()


class TestMigrationsAddColumns(unittest.TestCase):
    """Migration v1 columns are present after init_db()."""

    def test_bbref_fetch_log_extra_columns(self):
        with _init_memory_db() as conn:
            cols = _column_names(conn, "bbref_playoff_fetch_log")
            self.assertIn("fetch_status", cols)
            self.assertIn("fetched_at", cols)

    def test_player_game_appearances_opp_abbr(self):
        with _init_memory_db() as conn:
            cols = _column_names(conn, "player_game_appearances")
            self.assertIn("opp_abbr", cols)

    def test_player_stats_migration_columns(self):
        with _init_memory_db() as conn:
            cols = _column_names(conn, "player_stats")
            self.assertIn("playoff_games", cols)
            self.assertIn("on_off_asterisk", cols)

    def test_players_migration_columns(self):
        with _init_memory_db() as conn:
            cols = _column_names(conn, "players")
            self.assertIn("bbref_url", cols)
            self.assertIn("birthdate", cols)
            self.assertIn("nba_id", cols)

    def test_user_version_at_least_one(self):
        """user_version should be >= 1 after migrations are applied."""
        with _init_memory_db() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertGreaterEqual(version, 1)


class TestMigrationsNotReapplied(unittest.TestCase):
    """Migrations are applied exactly once; re-running init_db() keeps version stable."""

    def test_version_stable_after_double_init(self):
        real_conn = _make_memory_conn()
        proxy = _NoCloseConn(real_conn)

        def fake_get_conn():
            return proxy

        with patch.object(db, "get_conn", fake_get_conn):
            db.init_db()
            version_after_first = real_conn.execute("PRAGMA user_version").fetchone()[0]
            db.init_db()
            version_after_second = real_conn.execute("PRAGMA user_version").fetchone()[0]

        real_conn.close()
        self.assertEqual(version_after_first, version_after_second)


class TestIndexesCreated(unittest.TestCase):
    """init_db() creates all six expected secondary indexes."""

    EXPECTED_INDEXES = {
        "idx_player_stats_season",
        "idx_player_stats_player",
        "idx_selected_season",
        "idx_pga_player_year_type",
        "idx_wpg_game_year",
        "idx_wgp_game_player",
    }

    def test_all_indexes_exist(self):
        with _init_memory_db() as conn:
            indexes = _index_names(conn)
            for expected in self.EXPECTED_INDEXES:
                with self.subTest(index=expected):
                    self.assertIn(expected, indexes)


class TestForeignKeysOn(unittest.TestCase):
    """get_conn() enables foreign key enforcement."""

    def test_foreign_keys_pragma_is_on(self):
        conn = _make_memory_conn()
        result = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.close()
        self.assertEqual(result, 1)

    def test_foreign_keys_enforced(self):
        """Inserting a player_stats row with a non-existent player_id must fail."""
        with _init_memory_db() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO seasons (label, season_year, season_type) VALUES (?, ?, ?)",
                ("2025 Playoffs", 2025, "playoffs"),
            )
            conn.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO player_stats (player_id, season_id) VALUES (?, ?)",
                    (9999, 1),  # player_id 9999 does not exist
                )
                conn.commit()


class TestDbConnContextManager(unittest.TestCase):
    """db_conn() yields a connection and closes it on exit."""

    def test_yields_usable_connection(self):
        with patch.object(db, "get_conn", _make_memory_conn):
            with db.db_conn() as conn:
                result = conn.execute("SELECT 1 AS val").fetchone()
                self.assertEqual(result["val"], 1)

    def test_connection_closed_after_exit(self):
        conn = _make_memory_conn()

        with patch.object(db, "get_conn", lambda: conn):
            with db.db_conn():
                pass  # enter and exit

        # The context manager should have closed the connection.
        with self.assertRaises(Exception):
            conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
