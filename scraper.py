"""
Scrape basketball-reference.com for NBA stats and upsert into the local
SQLite database.

URL patterns:
  Regular season:
    /leagues/NBA_{year}_advanced.html
    /leagues/NBA_{year}_totals.html
    /leagues/NBA_{year}_play-by-play.html
  Playoffs:
    /playoffs/NBA_{year}_advanced.html
    /playoffs/NBA_{year}_totals.html
    /playoffs/NBA_{year}_play-by-play.html
"""

import time
import logging

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
            # Capture the player's basketball-reference page URL
            if key in ("player", "name_display"):
                a = cell.find("a", href=True)
                if a and "/players/" in a.get("href", ""):
                    row["player_href"] = a["href"]
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

def scrape_player_birthdate(bbref_url: str):
    """
    Fetch a player's basketball-reference page and return their birth date as
    'YYYY-MM-DD', or None if not found.
    E.g. bbref_url = '/players/t/tatumja01.html'
    """
    url = BASE_URL + bbref_url
    soup = _get(url)
    birth_span = soup.find("span", {"id": "necro-birth"})
    if birth_span:
        return birth_span.get("data-birth")
    return None

def _base_url(year: int, season_type: str, page: str) -> str:
    """Build a basketball-reference URL for the given year/season_type/page.

    Regular season:  /leagues/NBA_{year}_{page}.html
    Playoffs:        /playoffs/NBA_{year}_{page}.html
    """
    if season_type == "playoffs":
        return f"{BASE_URL}/playoffs/NBA_{year}_{page}.html"
    return f"{BASE_URL}/leagues/NBA_{year}_{page}.html"


def _scrape_advanced(year: int, season_type: str) -> dict[str, dict]:
    """Returns {player_name: {usg_pct, ast_pct, tov_pct, bpm, ts_pct}}"""
    url = _base_url(year, season_type, "advanced")
    soup = _get(url)
    time.sleep(2)
    # Playoffs pages use table id="advanced_stats"; regular season uses "advanced"
    table_id = "advanced_stats" if season_type == "playoffs" else "advanced"
    rows = _parse_table(soup, table_id)
    result = {}
    for r in rows:
        name = (r.get("name_display") or r.get("player") or "").strip()
        # Remove asterisk (HOF marker) from name
        name = name.replace("*", "").strip()
        if not name:
            continue
        # If we already have this player, keep the TOT row (aggregate for traded players)
        team = r.get("team_name_abbr") or r.get("team_id") or ""
        if name in result and team != "TOT":
            continue
        result[name] = {
            "team": team,
            "usg_pct": _safe_float(r.get("usg_pct")),
            "ast_pct": _safe_float(r.get("ast_pct")),
            "tov_pct": _safe_float(r.get("tov_pct")),
            "bpm": _safe_float(r.get("bpm")),
            "ts_pct": _safe_float(r.get("ts_pct")),
            "player_href": r.get("player_href"),
        }
    return result


def _scrape_totals(year: int, season_type: str) -> dict[str, dict]:
    """Returns {player_name: {"mp": total_minutes, "games": games_played}}"""
    url = _base_url(year, season_type, "totals")
    soup = _get(url)
    time.sleep(2)
    rows = _parse_table(soup, "totals_stats")
    result = {}
    for r in rows:
        name = (r.get("name_display") or r.get("player") or "").strip().replace("*", "")
        if not name:
            continue
        team = r.get("team_name_abbr") or r.get("team_id") or ""
        mp = _safe_float(r.get("mp"))
        games_raw = r.get("g")
        games = int(games_raw) if games_raw and games_raw.strip().isdigit() else None
        # Keep TOT row for traded players; otherwise take first occurrence
        if name in result:
            if team == "TOT":
                result[name] = {"mp": mp, "games": games}
        else:
            result[name] = {"mp": mp, "games": games}
    return result


def _scrape_pbp(year: int, season_type: str) -> dict[str, dict]:
    """Returns {player_name: {on_court, on_off}}"""
    url = _base_url(year, season_type, "play-by-play")
    soup = _get(url)
    time.sleep(2)
    rows = _parse_table(soup, "pbp_stats")
    result = {}
    for r in rows:
        name = (r.get("name_display") or r.get("player") or "").strip().replace("*", "")
        if not name:
            continue
        team = r.get("team_name_abbr") or r.get("team_id") or ""
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
    Looks up the season's year and type from the DB to build the correct URLs.
    """
    logger.info("Starting scrape for season_id=%s", season_id)

    conn = get_conn()
    season_row = conn.execute(
        "SELECT season_year, season_type FROM seasons WHERE id = ?", (season_id,)
    ).fetchone()
    conn.close()

    if season_row is None:
        raise ValueError(f"No season found with id={season_id}")

    year = season_row["season_year"]
    season_type = season_row["season_type"]
    logger.info("Scraping %s %s", year, season_type)

    advanced = _scrape_advanced(year, season_type)
    totals = _scrape_totals(year, season_type)
    pbp = _scrape_pbp(year, season_type)

    # Merge by player name
    all_names = set(advanced) | set(totals) | set(pbp)
    logger.info("Total unique player names found: %d", len(all_names))

    conn = get_conn()
    cur = conn.cursor()

    inserted = 0
    for name in sorted(all_names):
        adv = advanced.get(name, {})
        totals_row = totals.get(name, {})
        mp = totals_row.get("mp")
        games = totals_row.get("games")
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

        # Store bbref_url if we captured it and it isn't already saved
        href = adv.get("player_href")
        if href:
            cur.execute(
                "UPDATE players SET bbref_url = ? WHERE id = ? AND (bbref_url IS NULL OR bbref_url = '')",
                (href, player_id),
            )

        # Upsert player_stats — preserve manually entered defense & position
        cur.execute(
            """
            SELECT id, defense, position, playoff_games FROM player_stats
            WHERE player_id = ? AND season_id = ?
            """,
            (player_id, season_id),
        )
        existing = cur.fetchone()
        defense = existing["defense"] if existing else None
        position = existing["position"] if existing else None
        # For playoff_games: use freshly scraped value if available,
        # otherwise preserve any existing value so manual/prior data is not lost
        playoff_games_value = games if (season_type == "playoffs" and games is not None) else (
            existing["playoff_games"] if existing else None
        )

        cur.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm,
                 defense, position, playoff_games)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                position          = excluded.position,
                playoff_games     = excluded.playoff_games
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
                playoff_games_value,
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()
    logger.info("Upserted %d player rows for season_id=%s", inserted, season_id)
    return inserted


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    from db import init_db

    parser = argparse.ArgumentParser(description="Scrape NBA stats from basketball-reference")
    parser.add_argument("--year", type=int, default=2026, help="Season year (e.g. 2026)")
    parser.add_argument("--type", dest="season_type", choices=["regular", "playoffs"],
                        default="regular", help="Season type")
    args = parser.parse_args()

    init_db()
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM seasons WHERE season_year=? AND season_type=?",
        (args.year, args.season_type),
    ).fetchone()
    conn.close()

    if row is None:
        print(f"Season row not found for {args.year} {args.season_type} — run db.py or create via the app first")
    else:
        n = run_scrape(row["id"])
        print(f"Done. {n} players upserted.")
