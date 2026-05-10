"""
Backfill playoff round data for existing player_game_appearances rows.

Usage:
    # Backfill nba_api rows (post-2000, fast, no rate-limit concerns):
    .venv/bin/python scripts/backfill_rounds.py --nba-api

    # Backfill BBRef rows (pre-2000, slow due to rate limits):
    .venv/bin/python scripts/backfill_rounds.py --bbref

    # Both:
    .venv/bin/python scripts/backfill_rounds.py --nba-api --bbref
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

# Allow running from project root
sys.path.insert(0, ".")

from db import get_conn
from scraper import (
    _apply_series_rounds_to_appearances,
    _fetch_bbref_playoff_gamelog,
    _fetch_series_round_map,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def backfill_nba_api(conn) -> None:
    """Backfill round for all post-2000 playoff rows using SeriesStandings."""
    years = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT season_year
            FROM player_game_appearances
            WHERE season_type = 'playoffs'
              AND round IS NULL
              AND opp_abbr IS NOT NULL
              AND season_year >= 2000
            ORDER BY season_year
            """
        ).fetchall()
    ]
    if not years:
        logger.info("No post-2000 playoff rows with missing rounds — nothing to do.")
        return

    logger.info("Backfilling rounds for %d seasons via SeriesStandings: %s", len(years), years)
    for year in years:
        _fetch_series_round_map(year, conn)
        _apply_series_rounds_to_appearances(year, conn)
        logger.info("Done: season=%s", year)


def backfill_bbref(conn) -> None:
    """
    Backfill round for pre-2000 BBRef rows by resetting bbref_playoff_fetch_log
    entries for players who have NULL round values, then re-fetching.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT pga.player_id, pga.season_year, p.bbref_url, p.name
        FROM player_game_appearances pga
        JOIN players p ON p.id = pga.player_id
        WHERE pga.season_type = 'playoffs'
          AND pga.round IS NULL
          AND pga.season_year < 2000
          AND p.bbref_url IS NOT NULL
        ORDER BY pga.season_year, p.name
        """
    ).fetchall()

    if not rows:
        logger.info("No pre-2000 BBRef playoff rows with missing rounds — nothing to do.")
        return

    logger.info("Backfilling rounds for %d (player, year) pairs via BBRef", len(rows))
    for row in rows:
        player_id   = row["player_id"]
        season_year = row["season_year"]
        bbref_url   = row["bbref_url"]
        name        = row["name"]

        # Reset fetch log to force re-fetch
        conn.execute(
            "UPDATE bbref_playoff_fetch_log SET fetch_status = 'needs_round_backfill' "
            "WHERE player_id = ? AND season_year = ? AND fetch_status = 'success'",
            (player_id, season_year),
        )
        conn.commit()

        logger.info("Re-fetching BBRef gamelog: %s year=%s", name, season_year)
        try:
            _fetch_bbref_playoff_gamelog(player_id, bbref_url, season_year, conn)
        except Exception as exc:
            logger.warning("Failed for %s year=%s: %s", name, season_year, exc)

        time.sleep(3)  # Be polite to BBRef


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill playoff round data")
    parser.add_argument("--nba-api", action="store_true", help="Backfill post-2000 rows via SeriesStandings")
    parser.add_argument("--bbref",   action="store_true", help="Backfill pre-2000 rows via BBRef re-fetch")
    args = parser.parse_args()

    if not args.nba_api and not args.bbref:
        parser.print_help()
        sys.exit(1)

    conn = get_conn()
    try:
        if args.nba_api:
            backfill_nba_api(conn)
        if args.bbref:
            backfill_bbref(conn)
    finally:
        conn.close()

    logger.info("Backfill complete.")


if __name__ == "__main__":
    main()
