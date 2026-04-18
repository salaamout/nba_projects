import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "nba.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
            UNIQUE(player_id, season_id)
        );

        CREATE TABLE IF NOT EXISTS selected_players (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            season_id INTEGER NOT NULL REFERENCES seasons(id),
            UNIQUE(player_id, season_id)
        );
    """)

    # Migrations: player_stats
    existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(player_stats)").fetchall()]
    if "playoff_games" not in existing_cols:
        cur.execute("ALTER TABLE player_stats ADD COLUMN playoff_games INTEGER")

    # Migrations: players
    existing_player_cols = [row[1] for row in cur.execute("PRAGMA table_info(players)").fetchall()]
    if "bbref_url" not in existing_player_cols:
        cur.execute("ALTER TABLE players ADD COLUMN bbref_url TEXT")
    if "birthdate" not in existing_player_cols:
        cur.execute("ALTER TABLE players ADD COLUMN birthdate TEXT")

    # Seed the 2026 Regular Season row if it doesn't exist
    cur.execute(
        "SELECT id FROM seasons WHERE season_year = 2026 AND season_type = 'regular'"
    )
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO seasons (label, season_year, season_type) VALUES (?, ?, ?)",
            ("2026 Regular Season", 2026, "regular"),
        )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialised at", DB_PATH)
