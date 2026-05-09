from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "nba.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_conn():
    """Context manager that opens a DB connection and guarantees it is closed.

    Usage::

        with db_conn() as conn:
            rows = conn.execute("SELECT ...").fetchall()
    """
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS seasons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            label       TEXT NOT NULL,
            season_year INTEGER NOT NULL,
            season_type TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS players (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS player_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id       INTEGER NOT NULL REFERENCES players(id),
            season_id       INTEGER NOT NULL REFERENCES seasons(id),
            minutes         REAL,
            usage_rate      REAL,
            true_shooting_pct REAL,
            assist_rate     REAL,
            turnover_pct    REAL,
            on_court_rating REAL,
            on_off_diff     REAL,
            bpm             REAL,
            defense         REAL,
            position        TEXT,
            playoff_games   INTEGER,
            on_off_asterisk INTEGER DEFAULT 0,
            UNIQUE(player_id, season_id)
        );

        CREATE TABLE IF NOT EXISTS selected_players (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            season_id INTEGER NOT NULL REFERENCES seasons(id),
            UNIQUE(player_id, season_id)
        );

        CREATE TABLE IF NOT EXISTS team_game_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_abbr   TEXT NOT NULL,
            season_year INTEGER NOT NULL,
            season_type TEXT NOT NULL,
            game_date   TEXT NOT NULL,
            margin      REAL NOT NULL,
            UNIQUE(team_abbr, season_year, season_type, game_date)
        );

        CREATE TABLE IF NOT EXISTS player_game_appearances (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id   INTEGER NOT NULL REFERENCES players(id),
            season_year INTEGER NOT NULL,
            season_type TEXT NOT NULL,
            team_abbr   TEXT NOT NULL,
            game_date   TEXT NOT NULL,
            UNIQUE(player_id, season_year, season_type, game_date)
        );

        CREATE TABLE IF NOT EXISTS watched_playoff_games (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            home_team      TEXT NOT NULL,
            away_team      TEXT NOT NULL,
            winner_team    TEXT,
            date_watched   TEXT NOT NULL,
            game_year      INTEGER NOT NULL,
            conference     TEXT NOT NULL,
            round          TEXT NOT NULL,
            game_of_round  INTEGER NOT NULL,
            best_player_id INTEGER REFERENCES players(id),
            notes          TEXT
        );

        CREATE TABLE IF NOT EXISTS watched_game_players (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id   INTEGER NOT NULL REFERENCES watched_playoff_games(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL REFERENCES players(id),
            UNIQUE(game_id, player_id)
        );

        CREATE TABLE IF NOT EXISTS bbref_playoff_fetch_log (
            player_id   INTEGER NOT NULL,
            season_year INTEGER NOT NULL,
            PRIMARY KEY (player_id, season_year)
        );

        CREATE TABLE IF NOT EXISTS league_game_log_fetch_log (
            season_year      INTEGER NOT NULL,
            season_type      TEXT NOT NULL,
            player_or_team   TEXT NOT NULL,
            fetched_at       TEXT NOT NULL,
            PRIMARY KEY (season_year, season_type, player_or_team)
        );
    """)

    # Indexes
    cur.executescript("""
        CREATE INDEX IF NOT EXISTS idx_player_stats_season   ON player_stats(season_id);
        CREATE INDEX IF NOT EXISTS idx_player_stats_player   ON player_stats(player_id);
        CREATE INDEX IF NOT EXISTS idx_selected_season       ON selected_players(season_id);
        CREATE INDEX IF NOT EXISTS idx_pga_player_year_type  ON player_game_appearances(player_id, season_year, season_type);
        CREATE INDEX IF NOT EXISTS idx_wpg_game_year         ON watched_playoff_games(game_year);
        CREATE INDEX IF NOT EXISTS idx_wgp_game_player       ON watched_game_players(game_id, player_id);
    """)

    # ---------------------------------------------------------------------------
    # Schema migration runner — keyed by PRAGMA user_version
    #
    # To add a new migration:
    #   1. Append a (description, [sql, ...]) tuple to _MIGRATIONS below.
    #   2. The runner will apply it exactly once on the next startup and bump
    #      user_version automatically.  Never edit or reorder existing entries.
    # ---------------------------------------------------------------------------
    _MIGRATIONS: list[tuple[str, list[str]]] = [
        # v1 — columns added before versioning was introduced
        (
            "Add fetch_status/fetched_at to bbref_playoff_fetch_log; "
            "opp_abbr to player_game_appearances; "
            "playoff_games/on_off_asterisk to player_stats; "
            "bbref_url/birthdate/nba_id to players",
            [
                "ALTER TABLE bbref_playoff_fetch_log ADD COLUMN fetch_status TEXT NOT NULL DEFAULT 'success'",
                "ALTER TABLE bbref_playoff_fetch_log ADD COLUMN fetched_at TEXT",
                "ALTER TABLE player_game_appearances ADD COLUMN opp_abbr TEXT",
                "ALTER TABLE player_stats ADD COLUMN playoff_games INTEGER",
                "ALTER TABLE player_stats ADD COLUMN on_off_asterisk INTEGER DEFAULT 0",
                "ALTER TABLE players ADD COLUMN bbref_url TEXT",
                "ALTER TABLE players ADD COLUMN birthdate TEXT",
                "ALTER TABLE players ADD COLUMN nba_id INTEGER",
            ],
        ),
        # v2 — add new migrations here, e.g.:
        # ("Short description", ["ALTER TABLE ..."]),
    ]

    current_version: int = cur.execute("PRAGMA user_version").fetchone()[0]
    for version, (description, statements) in enumerate(_MIGRATIONS, start=1):
        if current_version >= version:
            continue  # already applied
        for sql in statements:
            try:
                cur.execute(sql)
            except Exception as exc:  # noqa: BLE001
                # Column may already exist on databases created after the base
                # schema was updated — treat as a no-op.
                if "duplicate column name" in str(exc).lower():
                    pass
                else:
                    raise
        # user_version cannot be set via a bound parameter
        cur.execute(f"PRAGMA user_version = {version}")
        current_version = version

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialised at", DB_PATH)
