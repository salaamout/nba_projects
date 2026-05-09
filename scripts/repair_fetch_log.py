"""
repair_fetch_log.py — One-time (re-runnable) repair script for corrupt
bbref_playoff_fetch_log entries caused by rate-limiting during bulk backfills.

Usage:
    python repair_fetch_log.py                  # reset definitive corrupt rows
    python repair_fetch_log.py --include-unknown # also reset ambiguous rows
    python repair_fetch_log.py --check           # report post-fix anomalies
    python repair_fetch_log.py --min-year 1980 --max-year 1997
"""

import sqlite3
import argparse

from db import DB_PATH


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Apply fetch_status / fetched_at migrations if they haven't been run yet."""
    existing = [row[1] for row in conn.execute("PRAGMA table_info(bbref_playoff_fetch_log)").fetchall()]
    if "fetch_status" not in existing:
        conn.execute("ALTER TABLE bbref_playoff_fetch_log ADD COLUMN fetch_status TEXT NOT NULL DEFAULT 'success'")
    if "fetched_at" not in existing:
        conn.execute("ALTER TABLE bbref_playoff_fetch_log ADD COLUMN fetched_at TEXT")
    conn.commit()


def repair(min_year: int = 1947, max_year: int = 9999, include_unknown: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)
    _ensure_columns(conn)

    # Query A: definitive — playoff_games > 0 but no appearances in DB
    definitive = conn.execute("""
        SELECT f.player_id, f.season_year
        FROM bbref_playoff_fetch_log f
        JOIN seasons s ON s.season_year = f.season_year AND s.season_type = 'playoffs'
        JOIN player_stats ps ON ps.player_id = f.player_id AND ps.season_id = s.id
        LEFT JOIN player_game_appearances pga
               ON pga.player_id  = f.player_id
              AND pga.season_year = f.season_year
              AND pga.season_type = 'playoffs'
        WHERE ps.playoff_games > 0
          AND pga.id IS NULL
          AND f.season_year BETWEEN ? AND ?
    """, (min_year, max_year)).fetchall()
    print(f"[Definitive] {len(definitive)} rows with playoff_games > 0 but no appearances — marking for retry")

    rows_to_fix = list(definitive)

    if include_unknown:
        # Query B: ambiguous — no appearances, playoff_games unknown
        unknown = conn.execute("""
            SELECT f.player_id, f.season_year
            FROM bbref_playoff_fetch_log f
            LEFT JOIN player_game_appearances pga
                   ON pga.player_id  = f.player_id
                  AND pga.season_year = f.season_year
                  AND pga.season_type = 'playoffs'
            LEFT JOIN (
                SELECT ps.player_id, s.season_year
                FROM player_stats ps
                JOIN seasons s ON s.id = ps.season_id AND s.season_type = 'playoffs'
                WHERE ps.playoff_games > 0
            ) has_games ON has_games.player_id = f.player_id AND has_games.season_year = f.season_year
            WHERE pga.id IS NULL
              AND has_games.player_id IS NULL
              AND f.season_year BETWEEN ? AND ?
        """, (min_year, max_year)).fetchall()
        print(f"[Unknown]    {len(unknown)} rows with no appearances and unknown playoff_games — marking for retry")
        rows_to_fix += list(unknown)

    # Deduplicate
    rows_to_fix = list({(r[0], r[1]) for r in rows_to_fix})
    conn.executemany(
        "UPDATE bbref_playoff_fetch_log SET fetch_status='no_table' WHERE player_id=? AND season_year=?",
        rows_to_fix,
    )
    conn.commit()
    conn.close()
    print(f"Done. {len(rows_to_fix)} total rows reset to fetch_status='no_table'.")


def check(min_year: int = 1947, max_year: int = 9999) -> None:
    """Report post-fix anomalies: fetch_status='success' but no appearances despite playoff_games > 0."""
    conn = sqlite3.connect(DB_PATH)
    _ensure_columns(conn)
    rows = conn.execute("""
        SELECT p.name, ps.playoff_games, COUNT(pga.id) AS appearances, f.season_year
        FROM bbref_playoff_fetch_log f
        JOIN players p ON p.id = f.player_id
        JOIN seasons s ON s.season_year = f.season_year AND s.season_type = 'playoffs'
        JOIN player_stats ps ON ps.player_id = f.player_id AND ps.season_id = s.id
        LEFT JOIN player_game_appearances pga
               ON pga.player_id = f.player_id
              AND pga.season_year = f.season_year
              AND pga.season_type = 'playoffs'
        WHERE f.fetch_status = 'success'
          AND f.season_year BETWEEN ? AND ?
        GROUP BY f.player_id, f.season_year
        HAVING ps.playoff_games > 0 AND COUNT(pga.id) = 0
    """, (min_year, max_year)).fetchall()
    conn.close()

    if not rows:
        print("No anomalies found. All success-marked rows have corresponding appearances.")
        return

    print(f"[ANOMALY] {len(rows)} row(s) marked success but have no appearances despite playoff_games > 0:")
    for r in rows:
        print(f"  {r[0]!r:30s}  year={r[3]}  playoff_games={r[1]}  appearances={r[2]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Repair corrupt bbref_playoff_fetch_log entries.")
    p.add_argument("--min-year", type=int, default=1947)
    p.add_argument("--max-year", type=int, default=9999)
    p.add_argument("--include-unknown", action="store_true",
                   help="Also reset rows where playoff_games is unknown (ambiguous)")
    p.add_argument("--check", action="store_true",
                   help="Report post-fix anomalies instead of repairing")
    args = p.parse_args()

    if args.check:
        check(args.min_year, args.max_year)
    else:
        repair(args.min_year, args.max_year, args.include_unknown)
