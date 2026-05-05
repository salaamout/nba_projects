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

from __future__ import annotations

import time
import logging
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Comment

from db import get_conn

logger = logging.getLogger(__name__)

BASE_URL = "https://www.basketball-reference.com"

# In-memory cache for team game log fetches that returned no data (e.g. no
# playoff games for a team).  Without this, every player on the same team
# would trigger a redundant HTTP request.
_empty_team_log_cache: set[tuple[str, int, str]] = set()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# nba_api imports (used for pre-1997 on/off approximation)
# ---------------------------------------------------------------------------
try:
    from nba_api.stats.endpoints import leaguegamelog as _leaguegamelog
    from nba_api.stats.static import players as _nba_players_static
    _NBA_API_AVAILABLE = True
except ImportError:
    _NBA_API_AVAILABLE = False
    logger.warning("nba_api not installed — pre-1997 on/off will fall back to bbref scraping")

# Tracks which (season_year, season_type) pairs have already been fetched from
# stats.nba.com in the current process so we only call the API once per season.
_fetched_seasons: set[tuple[int, str, str]] = set()  # (year, season_type, "T"|"P")

# In-memory cache for P-mode results: (season_year, season_type) → dict[nba_player_id → list[dict]]
_p_mode_cache: dict[tuple[int, str], dict[int, list[dict]]] = {}

# Mapping from bbref team abbreviations → nba_api/stats.nba.com abbreviations.
# Only entries where the two systems DIFFER are needed; the fallback in
# _to_nba_abbr returns the original string for anything not listed here.
# These were verified by inspecting team_game_logs rows inserted from the API.
_BBREF_TO_NBA_ABBR: dict[str, str] = {
    "PHO": "PHX",  # Phoenix Suns
    "GSW": "GOS",  # Golden State Warriors (pre-1997 era)
    "PHI": "PHL",  # Philadelphia 76ers (pre-1997 era)
    "SAS": "SAN",  # San Antonio Spurs (pre-1997 era)
    "UTA": "UTH",  # Utah Jazz (pre-1997 era)
    "WSB": "WAS",  # Washington Bullets
}


def _to_nba_abbr(bbref_abbr: str) -> str:
    """Convert a bbref team abbreviation to its nba_api equivalent."""
    return _BBREF_TO_NBA_ABBR.get(bbref_abbr, bbref_abbr)


# ---------------------------------------------------------------------------
# Abbreviation → display team name mapping (for suggest-game cross-reference)
# ---------------------------------------------------------------------------
# Maps team abbreviations (as stored in player_game_appearances) to the full
# team name used in watched_playoff_games.  Historical franchises use the name
# that was correct at the time; callers should pass season_year to pick the
# right name where a franchise relocated.
_ABBR_TO_TEAM_NAME_BY_YEAR: dict[str, list[tuple[int, int, str]]] = {
    # abbr: [(start_year, end_year, name), ...]  — end_year is inclusive last season
    "ATL": [(1969, 2099, "Atlanta Hawks")],
    "BOS": [(1947, 2099, "Boston Celtics")],
    "BRK": [(2013, 2099, "Brooklyn Nets")],
    "NJN": [(1977, 2012, "New Jersey Nets")],
    "NYK": [(1947, 2099, "New York Knicks")],
    "PHI": [(1964, 2099, "Philadelphia 76ers")],
    "PHL": [(1964, 2099, "Philadelphia 76ers")],
    "TOR": [(1996, 2099, "Toronto Raptors")],
    "CHI": [(1967, 2099, "Chicago Bulls")],
    "CLE": [(1971, 2099, "Cleveland Cavaliers")],
    "DET": [(1958, 2099, "Detroit Pistons")],
    "IND": [(1977, 2099, "Indiana Pacers")],
    "MIL": [(1969, 2099, "Milwaukee Bucks")],
    "DAL": [(1981, 2099, "Dallas Mavericks")],
    "HOU": [(1972, 2099, "Houston Rockets")],
    "MEM": [(2002, 2099, "Memphis Grizzlies")],
    "VAN": [(1996, 2001, "Vancouver Grizzlies")],
    "NOP": [(2014, 2099, "New Orleans Pelicans")],
    "NOH": [(2003, 2013, "New Orleans Hornets")],
    "NOK": [(2006, 2007, "New Orleans/Oklahoma City Hornets")],
    "SAS": [(1977, 2099, "San Antonio Spurs")],
    "SAN": [(1977, 2099, "San Antonio Spurs")],
    "OKC": [(2009, 2099, "Oklahoma City Thunder")],
    "SEA": [(1968, 2008, "Seattle SuperSonics")],
    "DEN": [(1977, 2099, "Denver Nuggets")],
    "MIN": [(1990, 2099, "Minnesota Timberwolves")],
    "UTA": [(1980, 2099, "Utah Jazz")],
    "UTH": [(1980, 2099, "Utah Jazz")],
    "NOJ": [(1975, 1979, "New Orleans Jazz")],
    "POR": [(1971, 2099, "Portland Trail Blazers")],
    "GOS": [(1972, 2099, "Golden State Warriors")],
    "GSW": [(1972, 2099, "Golden State Warriors")],
    "LAC": [(1985, 2099, "Los Angeles Clippers")],
    "SDC": [(1979, 1984, "San Diego Clippers")],
    "LAL": [(1961, 2099, "Los Angeles Lakers")],
    "MNL": [(1949, 1960, "Minneapolis Lakers")],
    "PHX": [(1969, 2099, "Phoenix Suns")],
    "PHO": [(1969, 2099, "Phoenix Suns")],
    "SAC": [(1986, 2099, "Sacramento Kings")],
    "KCK": [(1976, 1985, "Kansas City Kings")],
    "CIN": [(1958, 1972, "Cincinnati Royals")],
    "WSB": [(1974, 1997, "Washington Bullets")],
    "WAS": [(1998, 2099, "Washington Wizards")],
    "MIA": [(1989, 2099, "Miami Heat")],
    "ORL": [(1990, 2099, "Orlando Magic")],
    "CHA": [(1989, 2002, "Charlotte Hornets"), (2015, 2099, "Charlotte Hornets")],
    "CHH": [(1989, 2002, "Charlotte Hornets")],
    "CHO": [(2015, 2099, "Charlotte Hornets")],
    "BUF": [(1971, 1978, "Buffalo Braves")],
    "CHP": [(1950, 1952, "Chicago Stags")],
    "BAL": [(1964, 1973, "Baltimore Bullets")],
    "CAP": [(1974, 1974, "Capital Bullets")],
}

# Flat fallback dict for abbrs with only one name (most common case)
_ABBR_TO_TEAM_NAME: dict[str, str] = {
    abbr: entries[0][2]
    for abbr, entries in _ABBR_TO_TEAM_NAME_BY_YEAR.items()
    if len(entries) == 1
}


def abbr_to_team_name(abbr: str, season_year: int | None = None) -> str | None:
    """Return the full team name for a given abbreviation, optionally checking season_year."""
    entries = _ABBR_TO_TEAM_NAME_BY_YEAR.get(abbr.upper())
    if not entries:
        return None
    if season_year is None:
        return entries[0][2]
    for start, end, name in entries:
        if start <= season_year <= end:
            return name
    return entries[-1][2]


# Nickname used when logging games manually in watched_playoff_games
_ABBR_TO_NICKNAME: dict[str, str] = {
    "ATL": "Hawks",
    "BOS": "Celtics",
    "BRK": "Nets",
    "NJN": "Nets",
    "NYK": "Knicks",
    "PHI": "76ers",
    "PHL": "76ers",
    "TOR": "Raptors",
    "CHI": "Bulls",
    "CLE": "Cavs",
    "DET": "Pistons",
    "IND": "Pacers",
    "MIL": "Bucks",
    "DAL": "Mavs",
    "HOU": "Rockets",
    "MEM": "Grizzlies",
    "VAN": "Grizzlies",
    "NOP": "Pels",
    "NOH": "Pels",
    "NOK": "Pels",
    "SAS": "Spurs",
    "SAN": "Spurs",
    "OKC": "Thunder",
    "SEA": "Sonics",
    "DEN": "Nuggets",
    "MIN": "Wolves",
    "UTA": "Jazz",
    "UTH": "Jazz",
    "NOJ": "Jazz",
    "POR": "Blazers",
    "GOS": "Warriors",
    "GSW": "Warriors",
    "LAC": "Clippers",
    "SDC": "Clippers",
    "LAL": "Lakers",
    "MNL": "Lakers",
    "PHX": "Suns",
    "PHO": "Suns",
    "SAC": "Kings",
    "KCK": "Kings",
    "CIN": "Kings",
    "WSB": "Wizards",
    "WAS": "Wizards",
    "MIA": "Heat",
    "ORL": "Magic",
    "CHA": "Hornets",
    "CHH": "Hornets",
    "CHO": "Hornets",
    "BUF": "Braves",
}


def abbr_to_team_name_variants(abbr: str, season_year: int | None = None) -> list[str]:
    """Return all known name variants for a team abbreviation.

    Includes the full name, the abbreviation itself, and the short nickname
    used in watched_playoff_games (e.g. 'Spurs', 'Cavs').  This is used when
    cross-referencing against the watch log, which may store any of these forms.
    """
    variants: list[str] = []
    full = abbr_to_team_name(abbr, season_year)
    if full:
        variants.append(full)
    a = abbr.upper()
    if a not in variants:
        variants.append(a)
    nick = _ABBR_TO_NICKNAME.get(a)
    if nick and nick not in variants:
        variants.append(nick)
    # Also include 'Sixers' as an alias for 76ers
    if nick == "76ers" and "Sixers" not in variants:
        variants.append("Sixers")
    return variants


# ---------------------------------------------------------------------------
# BBRef playoff gamelog fallback (for pre-nba_api era players)
# ---------------------------------------------------------------------------

def _fetch_bbref_playoff_gamelog(player_id: int, bbref_url: str, season_year: int, conn) -> set[str]:
    """
    Fetch a player's BBRef playoff gamelog for season_year and cache into
    player_game_appearances.  Returns set of game_dates.

    bbref_url example: '/players/j/jordami01.html'
    BBRef gamelog URL:  '/players/j/jordami01/gamelog/1991'  (for the 1990-91 season)
    """
    # Check cache first — skip re-fetch only if all cached rows already have opp_abbr
    null_count = conn.execute(
        "SELECT COUNT(*) FROM player_game_appearances "
        "WHERE player_id=? AND season_year=? AND season_type='playoffs' AND opp_abbr IS NULL",
        (player_id, season_year),
    ).fetchone()[0]
    all_cached = conn.execute(
        "SELECT game_date FROM player_game_appearances "
        "WHERE player_id=? AND season_year=? AND season_type='playoffs'",
        (player_id, season_year),
    ).fetchall()
    if all_cached and null_count == 0:
        return {r["game_date"] for r in all_cached}

    # Build gamelog URL from bbref_url
    # /players/j/jordami01.html  →  /players/j/jordami01/gamelog/{year}
    bbref_id = bbref_url.rstrip("/").split("/")[-1].replace(".html", "")
    first_letter = bbref_id[0]
    gamelog_url = f"{BASE_URL}/players/{first_letter}/{bbref_id}/gamelog/{season_year}"

    try:
        soup = _get(gamelog_url)
        time.sleep(2)
    except Exception as exc:
        logger.warning("BBRef gamelog fetch failed for %s year=%s: %s", bbref_url, season_year, exc)
        return set()

    try:
        rows = _parse_table(soup, "pgl_basic_playoffs")
    except ValueError:
        logger.info("No pgl_basic_playoffs table for %s year=%s", bbref_id, season_year)
        return set()

    cur = conn.cursor()
    dates: set[str] = set()
    for row in rows:
        date_str = row.get("date_game", "").strip()
        if not date_str or date_str.lower() in ("", "date"):
            continue
        # BBRef dates look like "1991-05-25" or "May 25, 1991"
        game_date = None
        try:
            # Try ISO format first
            datetime.strptime(date_str, "%Y-%m-%d")
            game_date = date_str
        except ValueError:
            try:
                dt = datetime.strptime(date_str, "%B %d, %Y")
                game_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                logger.debug("Unparseable date '%s' in BBRef gamelog %s %s", date_str, bbref_id, season_year)
                continue

        team_abbr = (row.get("team_id") or row.get("team_name_abbr") or "").strip()
        opp_abbr  = (row.get("opp_id")  or row.get("opp") or "").strip() or None
        inactive = row.get("reason", "").strip()
        if inactive.lower() in ("inactive", "did not play", "dnp", "not with team", "suspended"):
            continue
        mp = row.get("mp", "").strip()
        if not mp or mp == "0:00":
            continue

        cur.execute(
            "INSERT OR IGNORE INTO player_game_appearances "
            "(player_id, season_year, season_type, team_abbr, opp_abbr, game_date) VALUES (?,?,?,?,?,?)",
            (player_id, season_year, "playoffs", team_abbr, opp_abbr, game_date),
        )
        # Backfill opp_abbr for any existing row that was cached without it
        if opp_abbr:
            cur.execute(
                "UPDATE player_game_appearances SET opp_abbr=? "
                "WHERE player_id=? AND season_year=? AND season_type='playoffs' AND game_date=? AND opp_abbr IS NULL",
                (opp_abbr, player_id, season_year, game_date),
            )
        dates.add(game_date)

    conn.commit()
    logger.info("BBRef playoff gamelog: cached %d appearances for player_id=%s year=%s", len(dates), player_id, season_year)
    return dates


def _nba_season_str(season_year: int) -> str:
    """Convert DB season_year (ending year) to nba_api season string, e.g. 1978 → '1977-78'."""
    return f"{season_year - 1}-{str(season_year)[-2:]}"


def _nba_season_type(season_type: str) -> str:
    """Convert internal 'regular'/'playoffs' to nba_api string."""
    return "Regular Season" if season_type == "regular" else "Playoffs"


def _get_nba_id_for_player(player_name: str, conn) -> int | None:
    """
    Return the stats.nba.com player ID for a player.
    Checks the DB first; if not found, tries nba_api static data by exact name,
    then by case-insensitive match.  Persists any found nba_id back to the DB.
    """
    row = conn.execute("SELECT nba_id FROM players WHERE name = ?", (player_name,)).fetchone()
    if row and row["nba_id"]:
        return row["nba_id"]

    if not _NBA_API_AVAILABLE:
        return None

    # Try exact match
    matches = _nba_players_static.find_players_by_full_name(player_name)
    if not matches:
        # Try case-insensitive / partial — find_players_by_full_name accepts regex
        try:
            matches = _nba_players_static.find_players_by_full_name(
                re.escape(player_name), case_sensitive=False
            )
        except Exception:
            matches = []

    if matches:
        nba_id = matches[0]["id"]
        conn.execute(
            "UPDATE players SET nba_id = ? WHERE name = ? AND (nba_id IS NULL OR nba_id = 0)",
            (nba_id, player_name),
        )
        conn.commit()
        return nba_id

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, max_retries: int = 5) -> BeautifulSoup:
    """Fetch a page and return a BeautifulSoup object.

    Automatically retries with exponential backoff on 429 Too Many Requests.
    """
    logger.info("Fetching %s", url)
    for attempt in range(max_retries):
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            wait = 60 * (2 ** attempt)  # 60s, 120s, 240s, ...
            logger.warning(
                "429 Too Many Requests for %s — waiting %ds before retry %d/%d",
                url, wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "html.parser")
    # Final attempt — let raise_for_status surface the error
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


def _scrape_advanced(year: int, season_type: str) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Returns ({player_name: {usg_pct, ast_pct, tov_pct, bpm, ts_pct}},
                {player_name: [all raw rows for that player]})"""
    url = _base_url(year, season_type, "advanced")
    soup = _get(url)
    time.sleep(2)
    # Playoffs pages use table id="advanced_stats"; regular season uses "advanced"
    table_id = "advanced_stats" if season_type == "playoffs" else "advanced"
    rows = _parse_table(soup, table_id)
    result = {}
    raw_by_player: dict[str, list[dict]] = {}
    for r in rows:
        name = (r.get("name_display") or r.get("player") or "").strip()
        # Remove asterisk (HOF marker) from name
        name = name.replace("*", "").strip()
        if not name:
            continue
        raw_by_player.setdefault(name, []).append(r)
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
    return result, raw_by_player


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
# Pre-1997 on/off approximation helpers (nba_api-based)
# ---------------------------------------------------------------------------

# Historical team abbreviation normalisations (bbref uses different codes per era).
_TEAM_ABBR_ALIASES: dict[str, str] = {
    "NJN": "NJN", "BRK": "BRK",
    "NOH": "NOH", "NOK": "NOH", "NOP": "NOP",
    "SEA": "SEA", "OKC": "OKC",
    "VAN": "VAN", "MEM": "MEM",
    "CHH": "CHH", "CHA": "CHA", "CHO": "CHO",
    "KCK": "KCK",
    "SDC": "SDC",
    "WSB": "WSB",
    "HOU": "HOU",
    "IND": "IND",
}


def _fetch_league_game_log_nba(season_year: int, season_type: str, player_or_team: str, conn):
    """
    Fetch one season's LeagueGameLog from stats.nba.com and cache results in DB.

    player_or_team: "T" or "P"

    For "T": upserts all rows into team_game_logs; returns dict[(team_abbr, game_date) → margin]
    For "P": upserts appearances for matched players into player_game_appearances;
             returns dict[nba_player_id → list[{game_date, team_abbr}]]
    """
    cache_key = (season_year, season_type, player_or_team)
    if cache_key in _fetched_seasons:
        # Already fetched this run — data is in DB / in-memory cache
        if player_or_team == "P":
            return _p_mode_cache.get((season_year, season_type), {})
        return {}  # T-mode callers read from DB directly

    # stats.nba.com LeagueGameLog is unreliable for seasons before 2000 —
    # the endpoint consistently returns an empty body for old data.
    if season_year < 2000:
        logger.info(
            "Skipping LeagueGameLog for season=%s (pre-2000, use BBRef instead)",
            season_year,
        )
        _fetched_seasons.add(cache_key)
        if player_or_team == "P":
            _p_mode_cache[(season_year, season_type)] = {}
            return {}
        return {}

    logger.info(
        "Fetching LeagueGameLog season=%s type=%s mode=%s from stats.nba.com",
        season_year, season_type, player_or_team,
    )
    time.sleep(2)

    df = None
    for _attempt in range(3):
        try:
            log = _leaguegamelog.LeagueGameLog(
                season=_nba_season_str(season_year),
                season_type_all_star=_nba_season_type(season_type),
                player_or_team_abbreviation=player_or_team,
                league_id="00",
                timeout=60,
            )
            df = log.league_game_log.get_data_frame()
            break
        except Exception as exc:
            logger.warning(
                "LeagueGameLog attempt %d failed for %s %s mode=%s: %s",
                _attempt + 1, season_year, season_type, player_or_team, exc,
            )
            if _attempt < 2:
                time.sleep(5 * (_attempt + 1))
            else:
                logger.error(
                    "All retries exhausted for LeagueGameLog %s %s mode=%s",
                    season_year, season_type, player_or_team,
                )
                if player_or_team == "P":
                    _p_mode_cache[(season_year, season_type)] = {}
                    return {}
                return {}
    _fetched_seasons.add(cache_key)

    if df.empty:
        logger.warning("Empty LeagueGameLog response for %s %s mode=%s", season_year, season_type, player_or_team)
        if player_or_team == "P":
            _p_mode_cache[(season_year, season_type)] = {}
            return {}
        return {}

    cur = conn.cursor()

    if player_or_team == "T":
        for _, row in df.iterrows():
            game_date = str(row["GAME_DATE"])[:10]  # "YYYY-MM-DD"
            team_abbr = str(row["TEAM_ABBREVIATION"])
            margin = float(row["PLUS_MINUS"]) if row["PLUS_MINUS"] is not None else 0.0
            cur.execute(
                "INSERT OR IGNORE INTO team_game_logs "
                "(team_abbr, season_year, season_type, game_date, margin) VALUES (?,?,?,?,?)",
                (team_abbr, season_year, season_type, game_date, margin),
            )
        conn.commit()
        return {}

    else:  # "P"
        result: dict[int, list[dict]] = {}
        for _, row in df.iterrows():
            # Filter out DNP / inactive rows (MIN is None, "0:00", or empty)
            min_val = row.get("MIN")
            if min_val is None or str(min_val).strip() in ("", "0:00", "0"):
                continue

            nba_pid = int(row["PLAYER_ID"])
            game_date = str(row["GAME_DATE"])[:10]
            team_abbr = str(row["TEAM_ABBREVIATION"])
            # MATCHUP is "CHI vs. LAL" (home) or "CHI @ LAL" (away) — opponent is always last token
            matchup = str(row.get("MATCHUP", ""))
            parts = matchup.replace("vs. ", "@").split("@")
            opp_abbr = parts[-1].strip() if len(parts) > 1 else ""

            result.setdefault(nba_pid, []).append({"game_date": game_date, "team_abbr": team_abbr, "opp_abbr": opp_abbr})

        # Upsert appearances for players already in our DB (matched by nba_id)
        nba_to_internal: dict[int, int] = {}
        for db_row in conn.execute("SELECT id, nba_id FROM players WHERE nba_id IS NOT NULL").fetchall():
            nba_to_internal[db_row["nba_id"]] = db_row["id"]

        for nba_pid, appearances in result.items():
            internal_id = nba_to_internal.get(nba_pid)
            if internal_id is None:
                continue  # player not yet in our DB — will be inserted later
            for app in appearances:
                cur.execute(
                    "INSERT OR IGNORE INTO player_game_appearances "
                    "(player_id, season_year, season_type, team_abbr, opp_abbr, game_date) VALUES (?,?,?,?,?,?)",
                    (internal_id, season_year, season_type, app["team_abbr"], app.get("opp_abbr") or None, app["game_date"]),
                )
                # Backfill opp_abbr for any existing row cached without it
                if app.get("opp_abbr"):
                    cur.execute(
                        "UPDATE player_game_appearances SET opp_abbr=? "
                        "WHERE player_id=? AND season_year=? AND season_type=? AND game_date=? AND opp_abbr IS NULL",
                        (app["opp_abbr"], internal_id, season_year, season_type, app["game_date"]),
                    )
        conn.commit()

        _p_mode_cache[(season_year, season_type)] = result
        return result


def _backfill_opp_abbr(player_id: int, nba_id: int | None, bbref_url: str | None,
                       player_name: str, season_year: int, conn) -> bool:
    """
    Lazily backfill opp_abbr for any player_game_appearances rows that are NULL.

    Uses the nba_api PlayerGameLog endpoint (single-player, cheap call) for
    players with an nba_id, or re-fetches the BBRef playoff gamelog otherwise.
    Returns True if any backfill was performed.
    """
    null_rows = conn.execute(
        "SELECT COUNT(*) FROM player_game_appearances "
        "WHERE player_id=? AND season_year=? AND season_type='playoffs' AND opp_abbr IS NULL",
        (player_id, season_year),
    ).fetchone()[0]
    if not null_rows:
        return False

    if nba_id and _NBA_API_AVAILABLE:
        try:
            from nba_api.stats.endpoints import playergamelog as _playergamelog
            logger.info(
                "Backfilling opp_abbr for %s year=%s via PlayerGameLog", player_name, season_year
            )
            time.sleep(2)
            log = _playergamelog.PlayerGameLog(
                player_id=nba_id,
                season=_nba_season_str(season_year),
                season_type_all_star=_nba_season_type("playoffs"),
                timeout=60,
            )
            df = log.player_game_log.get_data_frame()
        except Exception as exc:
            logger.warning(
                "PlayerGameLog backfill failed for %s year=%s: %s", player_name, season_year, exc
            )
            return False

        if df.empty:
            return False

        cur = conn.cursor()
        for _, row in df.iterrows():
            game_date = str(row["GAME_DATE"])[:10]
            matchup = str(row.get("MATCHUP", ""))
            parts = matchup.replace("vs. ", "@").split("@")
            opp = parts[-1].strip() if len(parts) > 1 else ""
            if opp:
                cur.execute(
                    "UPDATE player_game_appearances SET opp_abbr=? "
                    "WHERE player_id=? AND season_year=? AND season_type='playoffs' AND game_date=? AND opp_abbr IS NULL",
                    (opp, player_id, season_year, game_date),
                )
        conn.commit()
        logger.info("opp_abbr backfill complete for %s year=%s", player_name, season_year)
        return True

    elif bbref_url:
        # Re-call the BBRef fetcher — it now handles UPDATE of NULL rows
        _fetch_bbref_playoff_gamelog(player_id, bbref_url, season_year, conn)
        return True

    return False


def _get_team_margins(team_abbr: str, season_year: int, season_type: str, conn) -> dict[str, float]:
    """
    Return {game_date: margin} for the given team-season.
    Uses DB cache if available; otherwise fetches from stats.nba.com.
    team_abbr should be the nba_api abbreviation.
    """
    existing = conn.execute(
        "SELECT game_date, margin FROM team_game_logs "
        "WHERE team_abbr=? AND season_year=? AND season_type=?",
        (team_abbr, season_year, season_type),
    ).fetchall()
    if existing:
        return {row["game_date"]: row["margin"] for row in existing}

    # Fetch the full league-wide team log (covers all teams)
    _fetch_league_game_log_nba(season_year, season_type, "T", conn)

    existing = conn.execute(
        "SELECT game_date, margin FROM team_game_logs "
        "WHERE team_abbr=? AND season_year=? AND season_type=?",
        (team_abbr, season_year, season_type),
    ).fetchall()
    return {row["game_date"]: row["margin"] for row in existing}


def _get_player_appearances(
    player_name: str,
    player_id: int,
    nba_id: int | None,
    season_year: int,
    season_type: str,
    team_abbrs: list[str],
    conn,
) -> set[str]:
    """
    Return the set of game_dates on which the player appeared (MIN > 0).
    Uses DB cache if available; otherwise fetches from stats.nba.com.
    """
    cached = conn.execute(
        "SELECT game_date FROM player_game_appearances "
        "WHERE player_id=? AND season_year=? AND season_type=?",
        (player_id, season_year, season_type),
    ).fetchall()
    if cached:
        return {r["game_date"] for r in cached}

    if nba_id is None:
        logger.warning("No nba_id for player '%s' — cannot fetch appearances from nba_api", player_name)
        return set()

    # Fetch full season P-mode (all players in one call)
    p_data = _fetch_league_game_log_nba(season_year, season_type, "P", conn)

    if not p_data:
        # Already fetched this run but no data
        return set()

    player_apps = p_data.get(nba_id, [])

    # Upsert this player's appearances now that we know their internal_id
    cur = conn.cursor()
    for app in player_apps:
        cur.execute(
            "INSERT OR IGNORE INTO player_game_appearances "
            "(player_id, season_year, season_type, team_abbr, game_date) VALUES (?,?,?,?,?)",
            (player_id, season_year, season_type, app["team_abbr"], app["game_date"]),
        )
    conn.commit()

    return {a["game_date"] for a in player_apps}


def _get_player_team_stints(advanced_rows_for_player: list[dict]) -> list[str]:
    """
    Given all advanced-table rows for a single player in a season
    (may include a TOT row and per-team rows), return the list of
    individual team abbreviations in order (excluding 'TOT').
    """
    teams = []
    for r in advanced_rows_for_player:
        team = (r.get("team_name_abbr") or r.get("team_id") or "").strip()
        if team and team != "TOT":
            teams.append(team)
    return teams if teams else []


def _compute_pre97_on_off(
    player_name: str,
    player_id: int,
    year: int,
    season_type: str,
    team_stints: list[str],
    conn,
) -> tuple[float | None, float | None, bool]:
    """
    Compute approximated on_court_rating and on_off_diff for a pre-1997 player
    using stats.nba.com data via nba_api.
    Returns (on_court_rating, on_off_diff, asterisk_flag).
    """
    if not team_stints:
        return None, None, False

    # Resolve nba_id (needed for player appearances lookup)
    nba_id = _get_nba_id_for_player(player_name, conn)

    # Convert bbref team abbreviations to nba_api equivalents
    nba_team_stints = [_to_nba_abbr(t) for t in team_stints]

    # Build team game logs for each stint
    all_team_margins: dict[str, float] = {}
    for team in nba_team_stints:
        team_log = _get_team_margins(team, year, season_type, conn)
        all_team_margins.update(team_log)

    if not all_team_margins:
        logger.warning(
            "No team game log data for %s stints=%s year=%s type=%s",
            player_name, nba_team_stints, year, season_type,
        )
        return None, None, False

    # Get player appearances
    appearance_dates = _get_player_appearances(
        player_name, player_id, nba_id, year, season_type, nba_team_stints, conn
    )

    game_log_missing = len(appearance_dates) == 0

    if game_log_missing:
        if nba_id is None:
            logger.warning(
                "No nba_id and no cached appearances for '%s' year=%s type=%s — "
                "falling back to full team schedule (asterisk set)",
                player_name, year, season_type,
            )
        appearance_dates = set(all_team_margins.keys())

    on_margins = [m for d, m in all_team_margins.items() if d in appearance_dates]
    off_margins = [m for d, m in all_team_margins.items() if d not in appearance_dates]

    total_team_games = len(all_team_margins)
    missed_games = len(off_margins)

    on_court_rating = (sum(on_margins) / len(on_margins)) if on_margins else None

    asterisk = False
    if game_log_missing:
        on_off_diff = None
        asterisk = True
    elif total_team_games > 0 and (missed_games / total_team_games) < 0.03:
        on_off_diff = 0.0
        asterisk = True
    else:
        off_avg = (sum(off_margins) / len(off_margins)) if off_margins else None
        if on_court_rating is not None and off_avg is not None:
            on_off_diff = on_court_rating - off_avg
        else:
            on_off_diff = None

    return on_court_rating, on_off_diff, asterisk


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

    advanced, raw_by_player = _scrape_advanced(year, season_type)
    totals = _scrape_totals(year, season_type)

    if year < 1997:
        pbp = {}  # will be filled per-player below via _compute_pre97_on_off
    else:
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

        # Skip players with no meaningful data
        if mp is None and not adv:
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

        # For pre-1997 seasons, compute approximated on/off
        on_off_asterisk = 0
        if year < 1997:
            stints = _get_player_team_stints(raw_by_player.get(name, []))
            on_court, on_off, asterisk = _compute_pre97_on_off(
                name, player_id, year, season_type, stints, conn
            )
            pbp_row = {"on_court": on_court, "on_off": on_off}
            on_off_asterisk = 1 if asterisk else 0
        else:
            pbp_row = pbp.get(name, {})
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
                 defense, position, playoff_games, on_off_asterisk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                playoff_games     = excluded.playoff_games,
                on_off_asterisk   = excluded.on_off_asterisk
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
                on_off_asterisk,
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
    parser.add_argument("--backfill", action="store_true",
                        help="Backfill all seasons from --start to --end (default 1978–1996)")
    parser.add_argument("--start", type=int, default=1978, help="First year for backfill")
    parser.add_argument("--end",   type=int, default=1996, help="Last year for backfill")
    args = parser.parse_args()

    init_db()

    if args.backfill:
        from db import get_conn as _gc
        for yr in range(args.start, args.end + 1):
            for stype in ("regular", "playoffs"):
                _conn = _gc()
                row = _conn.execute(
                    "SELECT id FROM seasons WHERE season_year=? AND season_type=?",
                    (yr, stype),
                ).fetchone()
                if row is None:
                    type_label = "Regular Season" if stype == "regular" else "Playoffs"
                    cur = _conn.execute(
                        "INSERT INTO seasons (label, season_year, season_type) VALUES (?,?,?)",
                        (f"{yr} {type_label}", yr, stype),
                    )
                    _conn.commit()
                    season_id = cur.lastrowid
                else:
                    season_id = row["id"]
                _conn.close()
                print(f"--- Scraping {yr} {stype} (season_id={season_id}) ---", flush=True)
                try:
                    n = run_scrape(season_id)
                    print(f"    Done. {n} players upserted.", flush=True)
                except Exception as exc:
                    print(f"    ERROR: {exc}", flush=True)
    else:
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
