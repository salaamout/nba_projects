"""One-time import script for Playoff Game Watch Log CSV.

Usage:
    python import_watch_log.py
"""

import csv
import re
import sys
from db import get_conn, init_db

CSV_PATH = "data_for_import/Playoff Game Watch Log - Sheet1.csv"

ROUND_MAP = {
    "1": "First Round",
    "2": "Second Round",
    "3": "Conference Finals",
    "4": "NBA Finals",
}


def strip_annotations(raw: str) -> list[str]:
    """Return bare last names from a semicolon-separated player cell."""
    names = []
    for token in raw.split(";"):
        token = token.strip()
        if not token:
            continue
        # Remove everything from the first ' (' onwards
        bare = re.sub(r"\s*\(.*", "", token).strip()
        if bare:
            names.append(bare)
    return names


def parse_date(raw: str) -> str:
    """Convert M/D/YYYY → YYYY-MM-DD."""
    parts = raw.strip().split("/")
    if len(parts) != 3:
        return raw.strip()
    m, d, y = parts
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def find_player(conn, last_name: str, game_year: int):
    """
    Try to match a last name to a players row.
    Priority:
      1. Unique match by last name restricted to players with stats in game_year.
      2. Unique match by last name across all players.
    Returns (player_id, flagged) where flagged=True means review is needed.
    Returns (None, True) if no match at all.
    """
    # Attempt 1: last name match + has stats in game_year
    rows = conn.execute(
        """
        SELECT DISTINCT p.id, p.name
        FROM players p
        JOIN player_stats ps ON ps.player_id = p.id
        JOIN seasons s ON s.id = ps.season_id
        WHERE LOWER(p.name) LIKE ? AND s.season_year = ?
        """,
        (f"% {last_name.lower()}", game_year),
    ).fetchall()

    # Also try exact last name (single-word names like "Magic")
    if not rows:
        rows = conn.execute(
            """
            SELECT DISTINCT p.id, p.name
            FROM players p
            JOIN player_stats ps ON ps.player_id = p.id
            JOIN seasons s ON s.id = ps.season_id
            WHERE LOWER(p.name) = ? AND s.season_year = ?
            """,
            (last_name.lower(), game_year),
        ).fetchall()

    if len(rows) == 1:
        return rows[0]["id"], False
    if len(rows) > 1:
        return None, True  # ambiguous in this year

    # Attempt 2: all players, by last name
    rows = conn.execute(
        "SELECT id, name FROM players WHERE LOWER(name) LIKE ?",
        (f"% {last_name.lower()}",),
    ).fetchall()
    if not rows:
        # exact single-name match
        rows = conn.execute(
            "SELECT id, name FROM players WHERE LOWER(name) = ?",
            (last_name.lower(),),
        ).fetchall()

    if len(rows) == 1:
        return rows[0]["id"], False
    if len(rows) > 1:
        return None, True

    return None, True  # not found at all


def get_or_create_stub(conn, last_name: str) -> int:
    """Return existing player id (by last name) or create a stub row."""
    # Try last-name-only match
    row = conn.execute(
        "SELECT id FROM players WHERE LOWER(name) LIKE ?",
        (f"% {last_name.lower()}",),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT id FROM players WHERE LOWER(name) = ?",
            (last_name.lower(),),
        ).fetchone()
    if row:
        return row["id"]

    # Create stub
    cur = conn.execute(
        "INSERT INTO players (name) VALUES (?)", (last_name,)
    )
    return cur.lastrowid


def main():
    init_db()
    conn = get_conn()

    unmatched = []
    ambiguous = []
    games_inserted = 0
    links_inserted = 0

    with open(CSV_PATH, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)  # skip header

        for line_no, row in enumerate(reader, start=2):
            if len(row) < 10:
                print(f"  [line {line_no}] Skipping short row: {row}")
                continue

            date_watched_raw = row[0].strip()
            game_year_raw    = row[1].strip()
            round_raw        = row[2].strip()
            conference       = row[3].strip()
            game_of_round    = row[4].strip()
            best_last        = row[5].strip()
            home_team        = row[6].strip()
            home_players_raw = row[7].strip()
            away_team        = row[8].strip()
            away_players_raw = row[9].strip()
            notes            = row[10].strip() if len(row) > 10 else ""

            date_watched  = parse_date(date_watched_raw)
            game_year     = int(game_year_raw)
            round_str     = ROUND_MAP.get(round_raw, round_raw)
            game_of_round = int(game_of_round) if game_of_round.isdigit() else None

            # --- Best player ---
            best_player_id = None
            if best_last:
                pid, flagged = find_player(conn, best_last, game_year)
                if pid:
                    best_player_id = pid
                elif flagged:
                    # Try to create/find stub only if truly not found
                    existing_rows = conn.execute(
                        "SELECT id FROM players WHERE LOWER(name) LIKE ? OR LOWER(name) = ?",
                        (f"% {best_last.lower()}", best_last.lower()),
                    ).fetchall()
                    if len(existing_rows) > 1:
                        ambiguous.append(
                            f"  Line {line_no}: best player '{best_last}' ({game_year}) — multiple matches: "
                            + ", ".join(str(r['id']) for r in existing_rows)
                        )
                    else:
                        stub_id = get_or_create_stub(conn, best_last)
                        best_player_id = stub_id
                        unmatched.append(
                            f"  Line {line_no}: best player '{best_last}' ({game_year}) → stub id={stub_id}"
                        )

            # --- Insert game ---
            cur = conn.execute(
                """
                INSERT INTO watched_playoff_games
                    (home_team, away_team, winner_team, date_watched, game_year,
                     conference, round, game_of_round, best_player_id, notes)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (home_team, away_team, date_watched, game_year,
                 conference, round_str, game_of_round, best_player_id, notes),
            )
            game_id = cur.lastrowid
            games_inserted += 1

            # --- Important players (home + away merged) ---
            all_player_names = (
                strip_annotations(home_players_raw) +
                strip_annotations(away_players_raw)
            )
            seen_ids = set()
            for last in all_player_names:
                pid, flagged = find_player(conn, last, game_year)
                if pid and pid not in seen_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO watched_game_players (game_id, player_id) VALUES (?, ?)",
                        (game_id, pid),
                    )
                    seen_ids.add(pid)
                    links_inserted += 1
                elif not pid:
                    existing_rows = conn.execute(
                        "SELECT id FROM players WHERE LOWER(name) LIKE ? OR LOWER(name) = ?",
                        (f"% {last.lower()}", last.lower()),
                    ).fetchall()
                    if len(existing_rows) > 1:
                        ambiguous.append(
                            f"  Line {line_no}: important player '{last}' ({game_year}) — multiple matches"
                        )
                    else:
                        stub_id = get_or_create_stub(conn, last)
                        if stub_id not in seen_ids:
                            conn.execute(
                                "INSERT OR IGNORE INTO watched_game_players (game_id, player_id) VALUES (?, ?)",
                                (game_id, stub_id),
                            )
                            seen_ids.add(stub_id)
                            links_inserted += 1
                        unmatched.append(
                            f"  Line {line_no}: important player '{last}' ({game_year}) → stub id={stub_id}"
                        )
                elif flagged:
                    ambiguous.append(
                        f"  Line {line_no}: important player '{last}' ({game_year}) — ambiguous, skipped"
                    )

    conn.commit()
    conn.close()

    print(f"\n✅ Import complete: {games_inserted} games, {links_inserted} player links")

    if unmatched:
        print(f"\n⚠️  Stub players created ({len(unmatched)}) — review manually:")
        for m in unmatched:
            print(m)

    if ambiguous:
        print(f"\n❌ Ambiguous matches skipped ({len(ambiguous)}) — resolve manually:")
        for m in ambiguous:
            print(m)


if __name__ == "__main__":
    main()
