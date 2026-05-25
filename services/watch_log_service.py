"""Watch-log scoring, leaderboard computation, and shared constants.

Public API
----------
ROUND_WEIGHTS        : dict mapping round name → weight for K.Y.L.E. scoring
ROUND_MAP            : dict mapping round name → ordinal rank (for SQL ordering)
get_watch_kyle_by_player(conn, season_year) -> dict[int, dict]
attach_watch_kyle(d, wk) -> None
compute_leaderboard(conn) -> list[dict]
"""
from __future__ import annotations

from collections import defaultdict

# ---------------------------------------------------------------------------
# Constants — single source of truth for round ordering and weights
# ---------------------------------------------------------------------------

ROUND_WEIGHTS: dict[str, int] = {
    "First Round":       1,
    "Second Round":      2,
    "Conference Finals": 4,
    "NBA Finals":        8,
}

# Ordinal rank used inside SQL CASE expressions (1 = earliest, 4 = Finals)
ROUND_MAP: dict[str, int] = {
    "First Round":       1,
    "Second Round":      2,
    "Conference Finals": 3,
    "NBA Finals":        4,
}

# Short labels used by the leaderboard endpoint
_ROUND_KEY: dict[str, str] = {
    "First Round":       "r1",
    "Second Round":      "r2",
    "Conference Finals": "r3",
    "NBA Finals":        "r4",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_watch_kyle_by_player(conn, season_year: int) -> dict[int, dict]:
    """Return dict of player_id → watch info for a given playoff year.

    Uses points-above-expectation (PAE) scoring:
      weighted_best = sum of round ordinal (1/2/3/4) for games where the
                      player was Best Player
      total_watched = total unweighted games watched
      watch_kyle    = weighted_best − total_watched

    Round ordinals: First Round=1, Second Round=2, Conference Finals=3,
    NBA Finals=4.  Expectation is 1 point per game watched.  A player not
    present in the watch log contributes 0 to their K.Y.L.E. rating.
    """
    rows = conn.execute(
        """
        SELECT
            wgp.player_id,
            COUNT(DISTINCT wgp.game_id) AS total_watched,
            SUM(CASE
                WHEN g.best_player_id = wgp.player_id THEN
                    CASE g.round
                        WHEN 'First Round'       THEN 1
                        WHEN 'Second Round'      THEN 2
                        WHEN 'Conference Finals' THEN 3
                        WHEN 'NBA Finals'        THEN 4
                        ELSE 1
                    END
                ELSE 0
            END) AS weighted_best
        FROM watched_game_players wgp
        JOIN watched_playoff_games g ON g.id = wgp.game_id
        WHERE g.game_year = ?
        GROUP BY wgp.player_id
        """,
        (season_year,),
    ).fetchall()

    # Pass 1 — compute PAE (points above expectation) for each player
    players = []
    for r in rows:
        M = r["total_watched"] or 0
        if M == 0:
            continue
        N = r["weighted_best"] or 0.0
        players.append({
            "player_id":     r["player_id"],
            "total_watched": M,
            "weighted_best": N,
            "pae":           N - M,
        })

    if not players:
        return {}

    # Pass 2 — min-max normalise PAE within this year: min→-1, max→+1
    min_pae = min(p["pae"] for p in players)
    max_pae = max(p["pae"] for p in players)
    span = max_pae - min_pae

    result: dict[int, dict] = {}
    for p in players:
        if span == 0:
            norm = 0.0
        else:
            norm = (p["pae"] - min_pae) / span * 2 - 1
        result[p["player_id"]] = {
            "watch_kyle":    round(norm, 3),
            "best_count":    p["weighted_best"],
            "total_watched": p["total_watched"],
            "raw_score":     round(p["pae"], 4),   # raw PAE stored for reference
        }
    return result


def attach_watch_kyle(d: dict, wk: dict | None) -> None:
    """Attach all four watch_kyle fields from a watch-map entry onto a player dict.

    Sets watch_kyle, watch_best_count, watch_total_watched, and watch_raw_score
    to None when ``wk`` is None (player has no watch-log data for that year).
    """
    d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
    d["watch_best_count"]    = wk["best_count"]    if wk else None
    d["watch_total_watched"] = wk["total_watched"] if wk else None
    d["watch_raw_score"]     = wk["raw_score"]     if wk else None


def compute_leaderboard(conn) -> list[dict]:
    """Compute the best-player leaderboard across all watched playoff games.

    Returns a list of dicts (one per player) sorted by best_player_pct desc.
    Each dict contains: player_id, name, total_watched_games, best_player_count,
    watch_kyle (sum across years), best_player_pct, r1/r2/r3/r4 breakdown strings.
    """
    rows = conn.execute(
        """
        SELECT
            wgp.player_id,
            p.name,
            g.game_year,
            COUNT(DISTINCT wgp.game_id) AS total_watched,
            SUM(CASE
                WHEN g.best_player_id = wgp.player_id THEN
                    CASE g.round
                        WHEN 'First Round'       THEN 1
                        WHEN 'Second Round'      THEN 2
                        WHEN 'Conference Finals' THEN 3
                        WHEN 'NBA Finals'        THEN 4
                        ELSE 1
                    END
                ELSE 0
            END) AS weighted_best
        FROM watched_game_players wgp
        JOIN players p ON p.id = wgp.player_id
        JOIN watched_playoff_games g ON g.id = wgp.game_id
        GROUP BY wgp.player_id, g.game_year
        """
    ).fetchall()

    round_rows = conn.execute(
        """
        SELECT
            wgp.player_id,
            g.round,
            COUNT(DISTINCT wgp.game_id) AS games_in_round,
            SUM(CASE WHEN g.best_player_id = wgp.player_id THEN 1 ELSE 0 END) AS best_in_round
        FROM watched_game_players wgp
        JOIN watched_playoff_games g ON g.id = wgp.game_id
        GROUP BY wgp.player_id, g.round
        """
    ).fetchall()

    # Per-round breakdown
    player_round_data: dict = defaultdict(
        lambda: {k: {"best": 0, "games": 0} for k in ("r1", "r2", "r3", "r4")}
    )
    for rr in round_rows:
        key = _ROUND_KEY.get(rr["round"])
        if key:
            player_round_data[rr["player_id"]][key]["best"]  = rr["best_in_round"]
            player_round_data[rr["player_id"]][key]["games"] = rr["games_in_round"]

    # Accumulate per-player totals and per-year PAE for normalisation
    year_player_info: dict[int, str] = {}
    player_year_data: dict = defaultdict(lambda: {"total_watched_games": 0, "weighted_best_total": 0.0})
    year_player_pae: dict[int, dict[int, float]] = defaultdict(dict)  # year → {pid: pae}

    for r in rows:
        M = r["total_watched"] or 0
        N = r["weighted_best"] or 0.0
        year_player_info[r["player_id"]] = r["name"]
        player_year_data[r["player_id"]]["total_watched_games"] += M
        player_year_data[r["player_id"]]["weighted_best_total"] += N
        if M > 0:
            year_player_pae[r["game_year"]][r["player_id"]] = N - M

    # Normalise PAE per year (min→-1, max→+1) and sum across years
    player_watch_kyle_sum: dict[int, float] = defaultdict(float)
    for yr, pae_map in year_player_pae.items():
        if not pae_map:
            continue
        min_pae = min(pae_map.values())
        max_pae = max(pae_map.values())
        span = max_pae - min_pae
        for pid, pae in pae_map.items():
            norm = (pae - min_pae) / span * 2 - 1 if span != 0 else 0.0
            player_watch_kyle_sum[pid] += norm

    result = []
    for pid, name in year_player_info.items():
        total         = player_year_data[pid]["total_watched_games"]
        weighted_best = player_year_data[pid]["weighted_best_total"]
        rd            = player_round_data[pid]

        def _cell(key: str, _rd: dict = rd) -> str:
            b = _rd[key]["best"]
            g = _rd[key]["games"]
            return f"{b}/{g}" if g > 0 else "—"

        result.append({
            "player_id":           pid,
            "name":                name,
            "total_watched_games": total,
            "best_player_count":   weighted_best,
            "watch_kyle":          round(player_watch_kyle_sum.get(pid, 0.0), 3),
            "best_player_pct":     round(weighted_best / total, 1) if total > 0 else None,
            "r1":                  _cell("r1"),
            "r2":                  _cell("r2"),
            "r3":                  _cell("r3"),
            "r4":                  _cell("r4"),
        })

    result.sort(key=lambda x: (x["best_player_pct"] or 0), reverse=True)
    return result
