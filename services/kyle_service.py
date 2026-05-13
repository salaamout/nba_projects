"""K.Y.L.E. computation and orchestration.

Public API
----------
fetch_selected_player_dicts(conn, season_id) -> list[dict]
compute_cumulative(conn) -> list[dict]
compute_peak_windows(conn, window) -> tuple[list[dict], dict]
compute_best3year(conn, window) -> list[dict]
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict

import kyle
from services.watch_log_service import get_watch_kyle_by_player

# ---------------------------------------------------------------------------
# LRU cache for computed ratings (keyed by DB fingerprint)
# Avoids re-computing from scratch on every HTTP request when data hasn't
# changed.  The cache key encodes the selected-player set and watch-log count
# so any mutation to the DB automatically produces a cache miss.
# ---------------------------------------------------------------------------

_KYLE_CACHE_MAX_SIZE = 32
_kyle_cache: OrderedDict = OrderedDict()

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Default per-year accumulator used when a player has no data for a given year.
ZERO_YEAR: dict = {"regular": 0.0, "playoffs": 0.0, "watch_kyle": None}

# Minimum number of playoff games watched (within a player's peak window) for
# that player to be included in the Least Squares ranking.
MIN_GAMES_WATCHED: int = 5


def _kyle_cache_set(key, value) -> None:
    if key in _kyle_cache:
        _kyle_cache.move_to_end(key)
    _kyle_cache[key] = value
    while len(_kyle_cache) > _KYLE_CACHE_MAX_SIZE:
        _kyle_cache.popitem(last=False)


def _kyle_cache_get(key):
    if key not in _kyle_cache:
        return None
    _kyle_cache.move_to_end(key)
    return _kyle_cache[key]


def _db_fingerprint(conn) -> tuple:
    """Return a hashable fingerprint of the mutable DB state.

    Encodes the full set of selected-player rows (by player_id + season_id)
    and the count of watched playoff games so that any change to either
    produces a different key and forces a cache miss.
    """
    selected = tuple(sorted(
        (r[0], r[1])
        for r in conn.execute(
            "SELECT player_id, season_id FROM selected_players"
        ).fetchall()
    ))
    watch_count = conn.execute(
        "SELECT COUNT(*) FROM watched_playoff_games"
    ).fetchone()[0]
    return (selected, watch_count)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_season_kyle(
    conn,
    selected_dicts: list[dict],
    season_type: str,
    season_year: int,
) -> list[dict]:
    """Annotate *selected_dicts* with watch data and compute K.Y.L.E. ratings.

    This is the single source of truth for turning a season's selected-player
    set into fully-computed K.Y.L.E. result dicts.  Both ``compute_cumulative``
    and ``compute_peak_windows`` delegate to this helper so the two code paths
    can never drift.

    Parameters
    ----------
    conn           : active DB connection
    selected_dicts : list of player-stat dicts for the season (will be copied)
    season_type    : ``"regular"`` or ``"playoffs"``
    season_year    : calendar year of the season (used to look up watch data)

    Returns
    -------
    list[dict]
        Each dict is a player result with all original fields plus derived /
        normalised ones, including ``kyle_rating``.
    """
    if not selected_dicts:
        return []
    dicts = [dict(d) for d in selected_dicts]
    if season_type == "playoffs":
        watch_map = get_watch_kyle_by_player(conn, season_year)
        for d in dicts:
            wk = watch_map.get(d["player_id"])
            d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
            d["watch_best_count"]    = wk["best_count"]    if wk else None
            d["watch_total_watched"] = wk["total_watched"] if wk else None
    return kyle.calculate(dicts, season_type=season_type)


def _compute_season_kyle_for_player(
    conn,
    player_id: int,
    selected_dicts: list[dict],
    season_type: str,
    season_year: int,
) -> float | None:
    """Return the K.Y.L.E. rating for a single player within a season's selected set.

    Delegates to ``_compute_season_kyle`` so both the per-player history view
    and the peak-window computation share identical rating logic.

    Returns ``None`` if *player_id* is not present in *selected_dicts* or if
    no rating could be computed.
    """
    results = _compute_season_kyle(conn, selected_dicts, season_type, season_year)
    for p in results:
        if p["player_id"] == player_id:
            return p.get("kyle_rating")
    return None


def fetch_selected_player_dicts(conn, season_id: int) -> list[dict]:
    """Return selected players for a season as a list of plain dicts.

    Fetches the canonical set of stat columns needed by ``kyle.calculate``,
    plus ``selected_id``, ``stats_id``, and ``position`` for endpoints that
    need them (extra fields are ignored by the rating functions).
    """
    rows = conn.execute(
        """
        SELECT
            sp.id            AS selected_id,
            p.id             AS player_id,
            p.name,
            ps.id            AS stats_id,
            ps.minutes, ps.usage_rate, ps.true_shooting_pct,
            ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
            ps.on_off_diff, ps.bpm, ps.defense, ps.position,
            ps.on_off_asterisk, ps.playoff_games
        FROM selected_players sp
        JOIN players p       ON p.id  = sp.player_id
        JOIN player_stats ps ON ps.player_id = sp.player_id
                             AND ps.season_id = sp.season_id
        WHERE sp.season_id = ?
        ORDER BY p.name
        """,
        (season_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Cumulative K.Y.L.E.
# ---------------------------------------------------------------------------

def compute_cumulative(conn) -> list[dict]:
    """Return per-player cumulative K.Y.L.E. totals across all seasons.

    Only seasons that have at least one selected player contribute a rating.
    Negative ratings are excluded (same rule as the original endpoint).

    Results are cached in an in-process LRU cache keyed by a DB fingerprint
    (selected-player set + watch-log count) so repeated requests within the
    same "state" are served from memory without re-computing.

    Returns a list sorted by total_kyle descending, each entry containing:
    player_id, name, regular_kyle, playoffs_kyle, total_kyle.
    """
    cache_key = ("cumulative", _db_fingerprint(conn))
    cached = _kyle_cache_get(cache_key)
    if cached is not None:
        return cached

    seasons = conn.execute(
        """
        SELECT DISTINCT s.id, s.season_type, s.season_year
        FROM seasons s
        JOIN selected_players sp ON sp.season_id = s.id
        """
    ).fetchall()

    totals: dict[int, dict] = {}

    for season in seasons:
        season_id   = season["id"]
        season_type = season["season_type"]

        player_dicts = fetch_selected_player_dicts(conn, season_id)
        if not player_dicts:
            continue

        calculated = _compute_season_kyle(conn, player_dicts, season_type, season["season_year"])

        for p in calculated:
            pid    = p["player_id"]
            rating = p.get("kyle_rating")
            if rating is None or rating < 0:
                continue
            if pid not in totals:
                totals[pid] = {
                    "player_id":    pid,
                    "name":         p["name"],
                    "regular_kyle": 0.0,
                    "playoffs_kyle": 0.0,
                }
            if season_type == "regular":
                totals[pid]["regular_kyle"] = round(totals[pid]["regular_kyle"] + rating, 4)
            else:
                totals[pid]["playoffs_kyle"] = round(totals[pid]["playoffs_kyle"] + rating, 4)

    result = []
    for entry in totals.values():
        entry["total_kyle"] = round(entry["regular_kyle"] + entry["playoffs_kyle"], 4)
        result.append(entry)

    result.sort(key=lambda x: x["total_kyle"], reverse=True)
    _kyle_cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Peak-window computation (shared by best3year, suggest endpoints)
# ---------------------------------------------------------------------------

def compute_peak_windows(conn, window: int) -> tuple[list[dict], dict]:
    """Compute each selected player's best consecutive N-year K.Y.L.E. window.

    This is the single source of truth for peak-window logic shared by
    ``compute_best3year``, ``suggest_service.get_suggestions``, and
    ``suggest_service.get_suggestions_for_player``.

    Parameters
    ----------
    conn   : active DB connection
    window : number of consecutive seasons in the window

    Returns
    -------
    peaks : list of dicts, each with keys:
        player_id, name, best_start_year, best_end_year, best_window_total
    player_years : dict mapping player_id →
        {"name": str, "years": {year: {"regular": float, "playoffs": float,
                                       "watch_kyle": float | None}}}
        Returned for callers that need per-year breakdown (e.g. compute_best3year).
    """
    seasons = conn.execute(
        """
        SELECT DISTINCT s.id, s.season_year, s.season_type
        FROM seasons s
        JOIN selected_players sp ON sp.season_id = s.id
        ORDER BY s.season_year
        """
    ).fetchall()

    player_years: dict[int, dict] = {}

    for season in seasons:
        season_id   = season["id"]
        season_year = season["season_year"]
        season_type = season["season_type"]

        player_dicts = fetch_selected_player_dicts(conn, season_id)
        if not player_dicts:
            continue

        calculated = _compute_season_kyle(conn, player_dicts, season_type, season_year)

        for p in calculated:
            pid    = p["player_id"]
            rating = p.get("kyle_rating")
            if rating is None:
                continue
            if pid not in player_years:
                player_years[pid] = {"name": p["name"], "years": {}}
            yr_data = player_years[pid]["years"].setdefault(
                season_year, {"regular": 0.0, "playoffs": 0.0, "watch_kyle": None}
            )
            if season_type == "regular":
                yr_data["regular"] += rating
            else:
                yr_data["playoffs"] += rating
                wk_val = p.get("watch_kyle")
                if wk_val is not None:
                    yr_data["watch_kyle"] = wk_val

    peaks: list[dict] = []

    for pid, pdata in player_years.items():
        name  = pdata["name"]
        years = pdata["years"]
        sorted_years = sorted(years.keys())

        if len(sorted_years) < window:
            continue

        first_year = sorted_years[0]
        last_year  = sorted_years[-1]
        all_years  = list(range(first_year, last_year + 1))

        if len(all_years) < window:
            continue

        best_entry: dict | None = None
        for i in range(len(all_years) - window + 1):
            window_years = all_years[i: i + window]
            total = sum(
                years.get(y, ZERO_YEAR)["regular"] + years.get(y, ZERO_YEAR)["playoffs"]
                for y in window_years
            )
            if best_entry is None or total > best_entry["best_window_total"]:
                best_entry = {
                    "player_id":         pid,
                    "name":              name,
                    "best_start_year":   window_years[0],
                    "best_end_year":     window_years[-1],
                    "best_window_total": round(total, 4),
                }
        if best_entry:
            peaks.append(best_entry)

    return peaks, player_years


# ---------------------------------------------------------------------------
# Best N-year window with supplementary fields + Least Squares
# ---------------------------------------------------------------------------

def compute_best3year(conn, window: int = 3) -> list[dict]:
    """Return each player's best consecutive N-year K.Y.L.E. window.

    Augments each peak entry with:
      regular_total, playoffs_total, watch_kyle_total,
      playoff_watched, playoff_played, window,
      ls_score, ls_wins, ls_losses, ls_comparisons.

    Results are cached in an in-process LRU cache keyed by (window, DB
    fingerprint) so repeated requests with the same window size and unchanged
    data are served from memory without re-computing.

    Returns sorted by best_window_total descending.
    """
    cache_key = ("best3year", window, _db_fingerprint(conn))
    cached = _kyle_cache_get(cache_key)
    if cached is not None:
        return cached

    # Pre-fetch playoff game counts for supplementary fields
    all_playoff_played: dict[tuple[int, int], int] = {}
    for r in conn.execute(
        """
        SELECT ps.player_id, s.season_year, ps.playoff_games
        FROM player_stats ps
        JOIN seasons s ON s.id = ps.season_id
        WHERE s.season_type = 'playoffs' AND ps.playoff_games IS NOT NULL
        """
    ).fetchall():
        all_playoff_played[(r["player_id"], r["season_year"])] = r["playoff_games"]

    all_playoff_watched: dict[tuple[int, int], int] = {}
    for r in conn.execute(
        """
        SELECT wgp.player_id, g.game_year, COUNT(DISTINCT wgp.game_id) AS cnt
        FROM watched_game_players wgp
        JOIN watched_playoff_games g ON g.id = wgp.game_id
        GROUP BY wgp.player_id, g.game_year
        """
    ).fetchall():
        all_playoff_watched[(r["player_id"], r["game_year"])] = r["cnt"]

    peaks, player_years = compute_peak_windows(conn, window)

    # Game rows needed for Least Squares computation
    game_rows = conn.execute(
        """
        SELECT
            g.id          AS game_id,
            g.game_year,
            g.best_player_id,
            wgp.player_id
        FROM watched_playoff_games g
        JOIN watched_game_players wgp ON wgp.game_id = g.id
        ORDER BY g.id
        """
    ).fetchall()

    result = []
    for entry in peaks:
        pid          = entry["player_id"]
        years        = player_years.get(pid, {}).get("years", {})
        window_years = list(range(entry["best_start_year"], entry["best_end_year"] + 1))

        reg  = sum(years.get(y, ZERO_YEAR)["regular"]  for y in window_years)
        play = sum(years.get(y, ZERO_YEAR)["playoffs"] for y in window_years)
        wk_vals = [
            years[y]["watch_kyle"] for y in window_years
            if y in years and years[y].get("watch_kyle") is not None
        ]

        entry["regular_total"]    = round(reg,  4)
        entry["playoffs_total"]   = round(play, 4)
        entry["watch_kyle_total"] = round(sum(wk_vals), 4) if wk_vals else None
        entry["playoff_watched"]  = sum(all_playoff_watched.get((pid, y), 0) for y in window_years)
        entry["playoff_played"]   = sum(all_playoff_played.get((pid, y), 0)  for y in window_years)
        entry["window"]           = window
        result.append(entry)

    # ── Least Squares scores ─────────────────────────────────────────────────
    peak_windows_map = {
        e["player_id"]: (e["best_start_year"], e["best_end_year"]) for e in result
    }

    games_map: dict = defaultdict(
        lambda: {"game_year": None, "best_player_id": None, "player_ids": []}
    )
    player_game_counts: dict[int, int] = defaultdict(int)
    for r in game_rows:
        gid = r["game_id"]
        games_map[gid]["game_year"]       = r["game_year"]
        games_map[gid]["best_player_id"]  = r["best_player_id"]
        games_map[gid]["player_ids"].append(r["player_id"])
        pid       = r["player_id"]
        game_year = r["game_year"]
        if pid in peak_windows_map and peak_windows_map[pid][0] <= game_year <= peak_windows_map[pid][1]:
            player_game_counts[pid] += 1

    qualified_players = {pid for pid, cnt in player_game_counts.items() if cnt >= MIN_GAMES_WATCHED}

    comparisons: list[tuple[int, int]] = []
    win_counts:  dict[int, int] = defaultdict(int)
    loss_counts: dict[int, int] = defaultdict(int)

    for gid, gdata in games_map.items():
        game_year = gdata["game_year"]
        best_id   = gdata["best_player_id"]
        if best_id is None:
            continue
        filtered = [
            pid for pid in gdata["player_ids"]
            if pid in peak_windows_map
            and peak_windows_map[pid][0] <= game_year <= peak_windows_map[pid][1]
            and pid in qualified_players
        ]
        if best_id not in filtered or len(filtered) < 2:
            continue
        for other_id in filtered:
            if other_id == best_id:
                continue
            comparisons.append((best_id, other_id))
            win_counts[best_id]    += 1
            loss_counts[other_id]  += 1

    ls_scores = kyle.compute_least_squares_scores(comparisons)

    for entry in result:
        pid = entry["player_id"]
        w   = win_counts.get(pid, 0)
        l   = loss_counts.get(pid, 0)
        entry["ls_score"]        = ls_scores.get(pid)
        entry["ls_wins"]         = w
        entry["ls_losses"]       = l
        entry["ls_comparisons"]  = w + l

    result.sort(key=lambda x: x["best_window_total"], reverse=True)
    _kyle_cache_set(cache_key, result)
    return result
