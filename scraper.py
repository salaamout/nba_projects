"""
Scrape basketball-reference.com for 2026 NBA regular season stats and
upsert into the local SQLite database.

Tables used:
  1. /leagues/NBA_2026_advanced.html   — USG%, AST%, TOV%, BPM, TS%
  2. /leagues/NBA_2026_totals.html     — MP (total minutes)
  3. /leagues/NBA_2026_play-by-play.html — OnCourt, On-Off
"""

import re
import time
import logging
from io import StringIO

import requests
from bs4 import BeautifulSoup, Comment

from db import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.basketball-reference.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}

SEASON_YEAR = 2026


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str) -> BeautifulSoup:
    """Fetch a page and return a BeautifulSoup object."""
    logger.info("Fetching %s", url)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "html.parser")


def _uncomment_tables(soup: BeautifulSoup) -> BeautifulSoup:
    """
    basketball-reference wraps some tables in HTML comments.
    Replace every comment node that contains a <table> with its parsed content.
    """
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if "<table" in comment:
            fragment = BeautifulSoup(comment, "html.parser")
            comment.replace_with(fragment)
    return soup


def _safe_float(val: str):
    if val is None:
        return None
    val = val.strip()
    if val in ("", "—", "-"):
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _parse_table(soup: BeautifulSoup, table_id: str) -> list[dict]:
    """
    Return list-of-dicts for a bbref stats table (handles commented-out tables).
    Removes header rows that appear mid-table (where Rk == 'Rk').
    """
    soup = _uncomment_tables(soup)
    table = soup.find("table", {"id": table_id})
    if table is None:
        raise ValueError(f"Table #{table_id} not found on page")

    headers = [th.get("data-stat", th.get_text(strip=True))
               for th in table.select("thead tr th")]

    rows = []
    for tr in table.select("tbody tr"):
        if "thead" in tr.get("class", []):
            continue
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = {}
        for i, cell in enumerate(cells):
            key = cell.get("data-stat") or (headers[i] if i < len(headers) else str(i))
            row[key] = cell.get_text(strip=True)
        # Skip repeated header rows
        if row.get("ranker") == "Rk" or row.get("player") == "Player":
            continue
        # Skip totally empty rows
        if not any(v for v in row.values()):
            continue
        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Per-table scrapers
# ---------------------------------------------------------------------------

def _scrape_advanced() -> dict[str, dict]:
    """Returns {player_name: {usg_pct, ast_pct, tov_pct, bpm, ts_pct, player_id}}"""
    url = f"{BASE_URL}/leagues/NBA_{SEASON_YEAR}_advanced.html"
    soup = _get(url)
    time.sleep(2)
    rows = _parse_table(soup, "advanced")
    result = {}
    for r in rows:
        name = r.get("name_display", "").strip()
        # Remove asterisk (HOF marker) from name
        name = name.replace("*", "").strip()
        if not name:
            continue
        # If we already have this player, keep the TOT row (aggregate for traded players)
        team = r.get("team_name_abbr", "")
        if name in result and team != "TOT":
            continue
        result[name] = {
            "team": team,
            "usg_pct": _safe_float(r.get("usg_pct")),
            "ast_pct": _safe_float(r.get("ast_pct")),
            "tov_pct": _safe_float(r.get("tov_pct")),
            "bpm": _safe_float(r.get("bpm")),
            "ts_pct": _safe_float(r.get("ts_pct")),
        }
    return result


def _scrape_totals() -> dict[str, float]:
    """Returns {player_name: total_minutes}"""
    url = f"{BASE_URL}/leagues/NBA_{SEASON_YEAR}_totals.html"
    soup = _get(url)
    time.sleep(2)
    rows = _parse_table(soup, "totals_stats")
    result = {}
    for r in rows:
        name = r.get("name_display", "").strip().replace("*", "")
        if not name:
            continue
        team = r.get("team_name_abbr", "")
        mp = _safe_float(r.get("mp"))
        # Keep TOT row for traded players; otherwise take first occurrence
        if name in result:
            if team == "TOT":
                result[name] = mp
        else:
            result[name] = mp
    return result


def _scrape_pbp() -> dict[str, dict]:
    """Returns {player_name: {on_court, on_off}}"""
    url = f"{BASE_URL}/leagues/NBA_{SEASON_YEAR}_play-by-play.html"
    soup = _get(url)
    time.sleep(2)
    rows = _parse_table(soup, "pbp_stats")
    result = {}
    for r in rows:
        name = r.get("name_display", "").strip().replace("*", "")
        if not name:
            continue
        team = r.get("team_name_abbr", "")
        if name in result and team != "TOT":
            continue
        result[name] = {
            "on_court": _safe_float(r.get("plus_minus_on")),
            "on_off": _safe_float(r.get("plus_minus_net")),
        }
    return result


# ---------------------------------------------------------------------------
# Main upsert
# ---------------------------------------------------------------------------

def run_scrape(season_id: int):
    """
    Scrape all three tables and upsert into player_stats for the given season.
    """
    logger.info("Starting scrape for season_id=%s", season_id)

    advanced = _scrape_advanced()
    totals = _scrape_totals()
    pbp = _scrape_pbp()

    # Merge by player name
    all_names = set(advanced) | set(totals) | set(pbp)
    logger.info("Total unique player names found: %d", len(all_names))

    conn = get_conn()
    cur = conn.cursor()

    inserted = 0
    for name in sorted(all_names):
        adv = advanced.get(name, {})
        mp = totals.get(name)
        pbp_row = pbp.get(name, {})

        # Skip players with no meaningful data
        if mp is None and not adv and not pbp_row:
            continue

        # Upsert player
        cur.execute(
            "INSERT OR IGNORE INTO players (name) VALUES (?)", (name,)
        )
        cur.execute("SELECT id FROM players WHERE name = ?", (name,))
        player_id = cur.fetchone()["id"]

        # Upsert player_stats — preserve manually entered defense & position
        cur.execute(
            """
            SELECT id, defense, position FROM player_stats
            WHERE player_id = ? AND season_id = ?
            """,
            (player_id, season_id),
        )
        existing = cur.fetchone()
        defense = existing["defense"] if existing else None
        position = existing["position"] if existing else None

        cur.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm,
                 defense, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id, season_id) DO UPDATE SET
                minutes           = excluded.minutes,
                usage_rate        = excluded.usage_rate,
                true_shooting_pct = excluded.true_shooting_pct,
                assist_rate       = excluded.assist_rate,
                turnover_pct      = excluded.turnover_pct,
                on_court_rating   = excluded.on_court_rating,
                on_off_diff       = excluded.on_off_diff,
                bpm               = excluded.bpm,
                defense           = excluded.defense,
                position          = excluded.position
            """,
            (
                player_id,
                season_id,
                mp,
                adv.get("usg_pct"),
                adv.get("ts_pct"),
                adv.get("ast_pct"),
                adv.get("tov_pct"),
                pbp_row.get("on_court"),
                pbp_row.get("on_off"),
                adv.get("bpm"),
                defense,
                position,
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()
    logger.info("Upserted %d player rows for season_id=%s", inserted, season_id)
    return inserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from db import init_db, get_conn as _gc

    init_db()
    conn = _gc()
    row = conn.execute(
        "SELECT id FROM seasons WHERE season_year=2026 AND season_type='regular'"
    ).fetchone()
    conn.close()

    if row is None:
        print("Season row not found — run db.py first")
    else:
        n = run_scrape(row["id"])
        print(f"Done. {n} players upserted.")
