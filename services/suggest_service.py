"""Suggest-game candidate logic.

Public API
----------
get_suggestions(conn, window, skip) -> dict
get_suggestions_for_player(conn, player_id, window, skip, player_row) -> dict
"""
from __future__ import annotations

import itertools
import logging
from collections import OrderedDict

from scraper import (
    _backfill_opp_abbr,
    _fetch_bbref_playoff_gamelog,
    _get_nba_id_for_player,
    _get_player_appearances,
    abbr_to_team_name_variants,
)
from services.kyle_service import compute_peak_windows

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# key : (window, selected_ids_tuple, watch_log_count) or
#       ("player", player_id, window, selected_ids_tuple, watch_log_count)
# value: fully-built, ordered list of candidate dicts
#
# Bounded to _SUGGEST_CACHE_MAX_SIZE entries (LRU eviction via OrderedDict).
# ---------------------------------------------------------------------------
_SUGGEST_CACHE_MAX_SIZE = 128
_suggest_cache: OrderedDict = OrderedDict()


def _cache_set(key, value) -> None:
    """Insert *value* at *key*, evicting the oldest entry when over the limit."""
    if key in _suggest_cache:
        _suggest_cache.move_to_end(key)
    _suggest_cache[key] = value
    while len(_suggest_cache) > _SUGGEST_CACHE_MAX_SIZE:
        _suggest_cache.popitem(last=False)


def _cache_get(key):
    """Return the cached value for *key* (moving it to MRU position), or ``None``."""
    if key not in _suggest_cache:
        return None
    _suggest_cache.move_to_end(key)
    return _suggest_cache[key]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _cache_key(conn, window: int) -> tuple:
    selected_ids = tuple(sorted(
        r[0] for r in conn.execute("SELECT DISTINCT player_id FROM selected_players").fetchall()
    ))
    watch_log_count = conn.execute("SELECT COUNT(*) FROM watched_playoff_games").fetchone()[0]
    return (window, selected_ids, watch_log_count)


def _player_cache_key(conn, player_id: int, window: int) -> tuple:
    selected_ids = tuple(sorted(
        r[0] for r in conn.execute("SELECT DISTINCT player_id FROM selected_players").fetchall()
    ))
    watch_log_count = conn.execute("SELECT COUNT(*) FROM watched_playoff_games").fetchone()[0]
    return ("player", player_id, window, selected_ids, watch_log_count)


def _ensure_appearances(conn, pid: int, meta: dict, player_name: str, years: list[int]) -> bool:
    """Fetch and cache playoff appearances for *pid* in the given *years*.

    Returns ``True`` if any year was missing data after the fetch attempt.
    Updates ``meta["nba_id"]`` in-place when auto-discovery succeeds.
    """
    nba_id    = meta.get("nba_id")
    bbref_url = meta.get("bbref_url")
    any_missing = False

    # Batch check which years are already cached — one query instead of one per year
    if years:
        ph = ",".join("?" * len(years))
        cached_years: set[int] = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT season_year FROM player_game_appearances "
                f"WHERE player_id=? AND season_year IN ({ph}) AND season_type='playoffs'",
                (pid, *years),
            ).fetchall()
        }
    else:
        cached_years = set()

    for year in years:
        if year in cached_years:
            continue  # already cached

        if not nba_id:
            discovered = _get_nba_id_for_player(player_name, conn)
            if discovered:
                nba_id = discovered
                meta["nba_id"] = discovered

        if nba_id and year >= 2000:
            _get_player_appearances(player_name, pid, nba_id, year, "playoffs", [], conn)
            _backfill_opp_abbr(pid, nba_id, bbref_url, player_name, year, conn)
        elif bbref_url:
            _fetch_bbref_playoff_gamelog(pid, bbref_url, year, conn)
        else:
            logger.warning(
                "No nba_id or bbref_url for player '%s' year=%s — skipping", player_name, year
            )
            any_missing = True

    return any_missing


def _find_co_appearance_games(conn, p1_id: int, p2_id: int,
                               start_year: int, end_year: int) -> list:
    """Return all playoff co-appearance games for two players in a year range."""
    return conn.execute(
        """
        SELECT a1.game_date, a1.season_year,
               a1.team_abbr AS team1_abbr, a2.team_abbr AS team2_abbr,
               ROW_NUMBER() OVER (
                   PARTITION BY a1.season_year, a1.team_abbr, a1.opp_abbr
                   ORDER BY a1.game_date
               ) AS game_of_round
        FROM player_game_appearances a1
        JOIN player_game_appearances a2
          ON a1.game_date    = a2.game_date
         AND a1.season_year  = a2.season_year
         AND a1.opp_abbr     = a2.team_abbr
        WHERE a1.player_id   = ?
          AND a2.player_id   = ?
          AND a1.season_type = 'playoffs'
          AND a2.season_type = 'playoffs'
          AND a1.season_year BETWEEN ? AND ?
        ORDER BY a1.season_year ASC, a1.game_date ASC
        """,
        (p1_id, p2_id, start_year, end_year),
    ).fetchall()


def _find_co_appearance_games_in_years(conn, p1_id: int, p2_id: int,
                                        years: list[int]) -> list:
    """Like ``_find_co_appearance_games`` but restricted to an explicit year list."""
    ph = ",".join("?" * len(years))
    return conn.execute(
        f"""
        SELECT a1.game_date, a1.season_year,
               a1.team_abbr AS team1_abbr, a2.team_abbr AS team2_abbr,
               ROW_NUMBER() OVER (
                   PARTITION BY a1.season_year, a1.team_abbr, a1.opp_abbr
                   ORDER BY a1.game_date
               ) AS game_of_round
        FROM player_game_appearances a1
        JOIN player_game_appearances a2
          ON a1.game_date    = a2.game_date
         AND a1.season_year  = a2.season_year
         AND a1.opp_abbr     = a2.team_abbr
        WHERE a1.player_id   = ?
          AND a2.player_id   = ?
          AND a1.season_type = 'playoffs'
          AND a2.season_type = 'playoffs'
          AND a1.season_year IN ({ph})
        ORDER BY a1.season_year ASC, a1.game_date ASC
        """,
        [p1_id, p2_id, *years],
    ).fetchall()


def _game_already_watched(conn, season_year: int, game_of_round: int,
                           team1_variants: list[str], team2_variants: list[str]) -> bool:
    """Return True if this game is already in the watch log."""
    t1_ph = ",".join("?" * len(team1_variants))
    t2_ph = ",".join("?" * len(team2_variants))
    return bool(conn.execute(
        f"""
        SELECT 1 FROM watched_playoff_games
        WHERE game_year = ?
          AND game_of_round = ?
          AND (
            (home_team IN ({t1_ph}) AND away_team IN ({t2_ph}))
            OR (home_team IN ({t2_ph}) AND away_team IN ({t1_ph}))
          )
        LIMIT 1
        """,
        [season_year, game_of_round,
         *team1_variants, *team2_variants,
         *team2_variants, *team1_variants],
    ).fetchone())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_suggestions(conn, window: int, skip: int) -> dict:
    """Return a single suggest-game candidate for a given window + skip offset.

    Builds the full ordered candidate list on first call and caches it.
    Subsequent calls for the same (window, selected players, watch log count)
    are served from cache in O(1).

    Returns a result dict with ``"result": "found"`` on success, or a dict
    with ``"result": "none"`` when no unwatched games remain.
    """
    cache_key = _cache_key(conn, window)

    cached = _cache_get(cache_key)
    if cached is not None:
        if skip < len(cached):
            return cached[skip]
        return {"result": "none", "message": "No unwatched games found for any overlapping pair."}

    # ── Cache miss — build the full candidate list ───────────────────────────
    peaks, _ = compute_peak_windows(conn, window)

    if len(peaks) < 2:
        _cache_set(cache_key, [])
        return {"result": "none", "message": "No overlapping peak windows."}

    player_ids = [p["player_id"] for p in peaks]
    player_meta_rows = conn.execute(
        f"SELECT id, nba_id, bbref_url FROM players WHERE id IN ({','.join('?' for _ in player_ids)})",
        player_ids,
    ).fetchall()
    player_meta = {
        r["id"]: {"nba_id": r["nba_id"], "bbref_url": r["bbref_url"]}
        for r in player_meta_rows
    }

    # Build overlapping pairs sorted by min(peak_score) desc
    pairs = []
    for a, b in itertools.combinations(peaks, 2):
        if a["best_start_year"] <= b["best_end_year"] and b["best_start_year"] <= a["best_end_year"]:
            pair_score    = min(a["best_window_total"], b["best_window_total"])
            overlap_start = max(a["best_start_year"], b["best_start_year"])
            overlap_end   = min(a["best_end_year"],   b["best_end_year"])
            pairs.append((pair_score, a, b, overlap_start, overlap_end))

    if not pairs:
        _cache_set(cache_key, [])
        return {"result": "none", "message": "No players with overlapping peak windows."}

    pairs.sort(key=lambda x: x[0], reverse=True)

    candidates: list[dict] = []
    for pair_score, pa, pb, overlap_start, overlap_end in pairs:
        p1_id   = pa["player_id"]
        p2_id   = pb["player_id"]
        p1_meta = player_meta.get(p1_id, {})
        p2_meta = player_meta.get(p2_id, {})

        # Ensure appearances cached for every overlap year — one batch call per player
        overlap_years_list = list(range(overlap_start, overlap_end + 1))
        _ensure_appearances(conn, p1_id, p1_meta, pa["name"], overlap_years_list)
        _ensure_appearances(conn, p2_id, p2_meta, pb["name"], overlap_years_list)

        for game in _find_co_appearance_games(conn, p1_id, p2_id, overlap_start, overlap_end):
            season_year    = game["season_year"]
            game_of_round  = game["game_of_round"]
            team1_variants = abbr_to_team_name_variants(game["team1_abbr"], season_year)
            team2_variants = abbr_to_team_name_variants(game["team2_abbr"], season_year)

            if _game_already_watched(conn, season_year, game_of_round,
                                     team1_variants, team2_variants):
                continue

            candidates.append({
                "result": "found",
                "player1": {
                    "name":  pa["name"],
                    "id":    p1_id,
                    "peak":  f"{pa['best_start_year']}–{pa['best_end_year']}",
                    "score": pa["best_window_total"],
                },
                "player2": {
                    "name":  pb["name"],
                    "id":    p2_id,
                    "peak":  f"{pb['best_start_year']}–{pb['best_end_year']}",
                    "score": pb["best_window_total"],
                },
                "pair_score": round(pair_score, 4),
                "game": {
                    "year":        season_year,
                    "game_date":   game["game_date"],
                    "team1":       team1_variants[0] if team1_variants else game["team1_abbr"],
                    "team2":       team2_variants[0] if team2_variants else game["team2_abbr"],
                    "round":       None,
                    "round_known": False,
                },
            })

    _cache_set(cache_key, candidates)

    if skip < len(candidates):
        return candidates[skip]
    return {"result": "none", "message": "No unwatched games found for any overlapping pair."}


def get_suggestions_for_player(conn, player_id: int, window: int,
                                skip: int, player_row) -> dict:
    """Return a suggest-game candidate for a focal player.

    *player_row* must be a mapping with at least ``name``.

    Returns a result dict with ``"result": "found"`` on success,
    ``"result": "none"``, ``"result": "missing_data"``, or
    ``"result": "error"`` otherwise.
    """
    cache_key = _player_cache_key(conn, player_id, window)

    cached = _cache_get(cache_key)
    if cached is not None:
        if skip < len(cached):
            return cached[skip]
        return {
            "result": "none",
            "message": "All games featuring this player have been watched. Impressive.",
        }

    # ── Focal player's playoff years ─────────────────────────────────────────
    playoff_season_rows = conn.execute(
        """
        SELECT DISTINCT s.season_year
        FROM player_stats ps
        JOIN seasons s ON s.id = ps.season_id
        WHERE ps.player_id = ? AND s.season_type = 'playoffs'
        ORDER BY s.season_year
        """,
        (player_id,),
    ).fetchall()

    if not playoff_season_rows:
        _cache_set(cache_key, [])
        return {"result": "none", "message": "Focal player has no playoff seasons in the database."}

    playoff_years = {r["season_year"] for r in playoff_season_rows}

    # ── Compute peaks for all selected players ────────────────────────────────
    all_peaks, _ = compute_peak_windows(conn, window)

    focal_peak_entry = next((p for p in all_peaks if p["player_id"] == player_id), None)
    if focal_peak_entry is None:
        _cache_set(cache_key, [])
        return {
            "result": "none",
            "message": "Focal player has fewer consecutive seasons than the selected window.",
        }

    focal_start = focal_peak_entry["best_start_year"]
    focal_end   = focal_peak_entry["best_end_year"]
    focal_score = focal_peak_entry["best_window_total"]

    overlapping_opps = sorted(
        [
            p for p in all_peaks
            if p["player_id"] != player_id
            and p["best_start_year"] <= focal_end
            and focal_start <= p["best_end_year"]
        ],
        key=lambda x: x["best_window_total"],
        reverse=True,
    )

    if not overlapping_opps:
        _cache_set(cache_key, [])
        return {"result": "none", "message": "No selected players with overlapping peak windows."}

    # Meta for all players involved
    all_pids = [opp["player_id"] for opp in overlapping_opps] + [player_id]
    meta_rows = conn.execute(
        f"SELECT id, nba_id, bbref_url, name FROM players"
        f" WHERE id IN ({','.join('?' * len(all_pids))})",
        all_pids,
    ).fetchall()
    player_meta = {
        r["id"]: {"nba_id": r["nba_id"], "bbref_url": r["bbref_url"], "name": r["name"]}
        for r in meta_rows
    }

    candidates: list[dict] = []
    first_missing_opp: str | None = None

    for opp in overlapping_opps:
        opp_id    = opp["player_id"]
        opp_start = opp["best_start_year"]
        opp_end   = opp["best_end_year"]

        overlap_start = max(focal_start, opp_start)
        overlap_end   = min(focal_end,   opp_end)
        overlap_years = [y for y in range(overlap_start, overlap_end + 1) if y in playoff_years]
        if not overlap_years:
            continue

        # Ensure appearances cached; track whether any data is missing
        any_data_missing = False
        for pid, role_name in [(player_id, player_row["name"]), (opp_id, opp["name"])]:
            meta  = player_meta.get(pid, {})
            pname = meta.get("name", role_name)
            if _ensure_appearances(conn, pid, meta, pname, overlap_years):
                any_data_missing = True
            # Re-check after fetch attempt — one batch query per player
            if overlap_years:
                ph = ",".join("?" * len(overlap_years))
                present = {
                    row[0]
                    for row in conn.execute(
                        f"SELECT DISTINCT season_year FROM player_game_appearances "
                        f"WHERE player_id=? AND season_year IN ({ph}) AND season_type='playoffs'",
                        (pid, *overlap_years),
                    ).fetchall()
                }
                if any(yr not in present for yr in overlap_years):
                    any_data_missing = True

        if any_data_missing and first_missing_opp is None:
            first_missing_opp = opp["name"]

        for game in _find_co_appearance_games_in_years(conn, player_id, opp_id, overlap_years):
            season_year    = game["season_year"]
            game_of_round  = game["game_of_round"]
            team1_variants = abbr_to_team_name_variants(game["team1_abbr"], season_year)
            team2_variants = abbr_to_team_name_variants(game["team2_abbr"], season_year)

            if _game_already_watched(conn, season_year, game_of_round,
                                     team1_variants, team2_variants):
                continue

            candidates.append({
                "result": "found",
                "_opp_score": opp["best_window_total"],
                "focal_player": {
                    "id":    player_id,
                    "name":  player_row["name"],
                    "peak":  f"{focal_start}–{focal_end}",
                    "score": focal_score,
                },
                "opponent": {
                    "id":    opp_id,
                    "name":  opp["name"],
                    "peak":  f"{opp_start}–{opp_end}",
                    "score": opp["best_window_total"],
                },
                "game": {
                    "year":      season_year,
                    "game_date": game["game_date"],
                    "team1":     team1_variants[0] if team1_variants else game["team1_abbr"],
                    "team2":     team2_variants[0] if team2_variants else game["team2_abbr"],
                    "round":     None,
                },
            })

    # Sort: opponent peak score desc, game_date asc as tiebreaker
    candidates.sort(key=lambda c: (-c["_opp_score"], c["game"]["game_date"]))
    for c in candidates:
        del c["_opp_score"]

    _cache_set(cache_key, candidates)

    if candidates:
        if skip < len(candidates):
            return candidates[skip]
        return {
            "result": "none",
            "message": "All games featuring this player have been watched. Impressive.",
        }

    if first_missing_opp:
        return {"result": "missing_data", "player": first_missing_opp}
    return {"result": "none", "message": "No overlapping opponents found."}
