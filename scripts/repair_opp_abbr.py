"""
Repair script: fix pre-2000 playoff game appearances that are missing opp_abbr.

These rows were inserted by the nba_api code path which never set opp_abbr.
The fix is to:
  1. Find all player/year combos with NULL opp_abbr in playoff appearances (pre-2000)
  2. Delete those rows so _fetch_bbref_playoff_gamelog can re-insert them properly
  3. Delete any stale bbref_playoff_fetch_log entries so the scraper doesn't skip
  4. Re-scrape via _fetch_bbref_playoff_gamelog which correctly reads opp_name_abbr
"""

import time
import logging
from db import get_conn
from scraper import _fetch_bbref_playoff_gamelog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

conn = get_conn()

# Step 1: Find all affected player/year combos
affected = conn.execute(
    """
    SELECT DISTINCT pga.player_id, p.name, p.bbref_url, pga.season_year
    FROM player_game_appearances pga
    JOIN players p ON p.id = pga.player_id
    WHERE pga.opp_abbr IS NULL
      AND pga.season_type = 'playoffs'
      AND pga.season_year < 2000
      AND p.bbref_url IS NOT NULL
    ORDER BY pga.season_year, p.name
    """
).fetchall()

logger.info("Found %d affected player/year combos with NULL opp_abbr", len(affected))
for row in affected:
    logger.info("  player_id=%s name=%s year=%s bbref_url=%s",
                row["player_id"], row["name"], row["season_year"], row["bbref_url"])

if not affected:
    logger.info("Nothing to repair.")
    conn.close()
    exit(0)

# Step 2: Delete affected rows and stale fetch log entries, then re-scrape
for row in affected:
    pid = row["player_id"]
    name = row["name"]
    year = row["season_year"]
    bbref_url = row["bbref_url"]

    logger.info("Repairing player_id=%s (%s) year=%s ...", pid, name, year)

    # Delete existing NULL opp_abbr rows for this player/year
    deleted = conn.execute(
        """
        DELETE FROM player_game_appearances
        WHERE player_id=? AND season_year=? AND season_type='playoffs' AND opp_abbr IS NULL
        """,
        (pid, year),
    ).rowcount
    conn.commit()
    logger.info("  Deleted %d NULL opp_abbr rows", deleted)

    # Delete any stale fetch_log entry so the scraper doesn't skip
    conn.execute(
        "DELETE FROM bbref_playoff_fetch_log WHERE player_id=? AND season_year=?",
        (pid, year),
    )
    conn.commit()

    # Re-scrape — _fetch_bbref_playoff_gamelog correctly reads opp_name_abbr
    dates = _fetch_bbref_playoff_gamelog(pid, bbref_url, year, conn)
    logger.info("  Re-scraped %d game dates", len(dates))

    # Polite delay between requests
    time.sleep(3)

# Verify the fix
remaining = conn.execute(
    """
    SELECT COUNT(*) AS cnt
    FROM player_game_appearances
    WHERE opp_abbr IS NULL AND season_type='playoffs' AND season_year < 2000
    """
).fetchone()["cnt"]

logger.info("Repair complete. Remaining NULL opp_abbr rows (pre-2000 playoffs): %d", remaining)
conn.close()
