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
    """)

    # Migrations: player_game_appearances
    existing_pga_cols = [row[1] for row in cur.execute("PRAGMA table_info(player_game_appearances)").fetchall()]
    if "opp_abbr" not in existing_pga_cols:
        cur.execute("ALTER TABLE player_game_appearances ADD COLUMN opp_abbr TEXT")

    # Migrations: player_stats
    existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(player_stats)").fetchall()]
    if "playoff_games" not in existing_cols:
        cur.execute("ALTER TABLE player_stats ADD COLUMN playoff_games INTEGER")
    if "on_off_asterisk" not in existing_cols:
        cur.execute("ALTER TABLE player_stats ADD COLUMN on_off_asterisk INTEGER DEFAULT 0")

    # Migrations: players
    existing_player_cols = [row[1] for row in cur.execute("PRAGMA table_info(players)").fetchall()]
    if "bbref_url" not in existing_player_cols:
        cur.execute("ALTER TABLE players ADD COLUMN bbref_url TEXT")
    if "birthdate" not in existing_player_cols:
        cur.execute("ALTER TABLE players ADD COLUMN birthdate TEXT")
    if "nba_id" not in existing_player_cols:
        cur.execute("ALTER TABLE players ADD COLUMN nba_id INTEGER")

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
