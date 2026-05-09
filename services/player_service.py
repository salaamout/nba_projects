"""Player history, per-season K.Y.L.E. scoring, and birthdate scraping.

Public API
----------
ensure_birthdate(conn, player_dict) -> None
get_player_history(conn, player_id) -> dict
get_player_watch_log(conn, player_id) -> dict
"""
from __future__ import annotations

import logging
from datetime import date as _date

import kyle
from scraper import scrape_player_birthdate
from services.watch_log_service import get_watch_kyle_by_player

logger = logging.getLogger(__name__)


def ensure_birthdate(conn, player_dict: dict) -> None:
    """Scrape and persist birthdate if missing, updating *player_dict* in-place.

    Requires ``player_dict`` to have at least ``id`` and ``bbref_url`` keys.
    Silently logs and continues on any scraping failure.
    """
    if player_dict.get("birthdate") or not player_dict.get("bbref_url"):
        return
    try:
        bd = scrape_player_birthdate(player_dict["bbref_url"])
        if bd:
            conn.execute(
                "UPDATE players SET birthdate = ? WHERE id = ?",
                (bd, player_dict["id"]),
            )
            conn.commit()
            player_dict["birthdate"] = bd
    except Exception:
        logger.exception("Failed to scrape birthdate for player id=%s", player_dict.get("id"))


def get_player_history(conn, player_id: int) -> dict:
    """Return a player's info and per-season K.Y.L.E. history.

    Response shape::

        {
          "player": {id, name, bbref_url, birthdate, ...},
          "seasons": [
            {season_id, season_year, season_type, season_label, age,
             minutes, usage_rate, true_shooting_pct, assist_rate,
             turnover_pct, on_court_rating, on_off_diff, bpm,
             defense, position, playoff_games, kyle_rating,
             on_off_asterisk},
            ...
          ]
        }

    Raises a 404 abort if the player is not found.
    """
    from flask import abort

    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        abort(404)

    player_dict = dict(player)

    season_rows = conn.execute(
        """
        SELECT s.id AS season_id, s.season_year, s.season_type, s.label,
               ps.minutes, ps.usage_rate, ps.true_shooting_pct,
               ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
               ps.on_off_diff, ps.bpm, ps.defense, ps.position, ps.playoff_games,
               ps.on_off_asterisk
        FROM player_stats ps
        JOIN seasons s ON s.id = ps.season_id
        WHERE ps.player_id = ?
        ORDER BY s.season_year, s.season_type
        """,
        (player_id,),
    ).fetchall()

    season_ids = [r["season_id"] for r in season_rows]

    # Batch-fetch selected-player stats for all relevant seasons
    selected_by_season: dict[int, list[dict]] = {}
    if season_ids:
        placeholders = ",".join("?" * len(season_ids))
        selected_all = conn.execute(
            f"""
            SELECT sp.season_id,
                   p.id AS player_id, p.name,
                   ps.minutes, ps.usage_rate, ps.true_shooting_pct,
                   ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
                   ps.on_off_diff, ps.bpm, ps.defense, ps.on_off_asterisk
            FROM selected_players sp
            JOIN players p       ON p.id  = sp.player_id
            JOIN player_stats ps ON ps.player_id = sp.player_id
                                 AND ps.season_id = sp.season_id
            WHERE sp.season_id IN ({placeholders})
            """,
            season_ids,
        ).fetchall()
        for r in selected_all:
            selected_by_season.setdefault(r["season_id"], []).append(dict(r))

    # Per-year watch_kyle for this player (simple pct-of-best, not year-normalised)
    watch_kyle_per_year: dict[int, float] = {}
    for r in conn.execute(
        """
        SELECT
            g.game_year,
            COUNT(DISTINCT wgp.game_id) AS total_watched,
            COUNT(DISTINCT CASE WHEN g.best_player_id = ? THEN g.id END) AS best_count
        FROM watched_game_players wgp
        JOIN watched_playoff_games g ON g.id = wgp.game_id
        WHERE wgp.player_id = ?
        GROUP BY g.game_year
        """,
        (player_id, player_id),
    ).fetchall():
        total = r["total_watched"] or 0
        best  = r["best_count"] or 0
        if total > 0:
            watch_kyle_per_year[r["game_year"]] = round(best / total * 2 - 1, 3)

    # Batch watch-maps for all playoff years (needed for selected-set bounds)
    playoff_years = {r["season_year"] for r in season_rows if r["season_type"] == "playoffs"}
    watch_map_by_year = {yr: get_watch_kyle_by_player(conn, yr) for yr in playoff_years}

    # Scrape birthdate on first visit if missing
    ensure_birthdate(conn, player_dict)

    seasons_out = []
    for row in season_rows:
        row_dict    = dict(row)
        season_id   = row_dict["season_id"]
        season_type = row_dict["season_type"]
        season_year = row_dict["season_year"]

        kyle_rating = None
        selected = selected_by_season.get(season_id, [])
        if selected:
            exclude   = {"minutes"} if season_type == "playoffs" else set()
            sel_dicts = [dict(r) for r in selected]
            if season_type == "playoffs":
                watch_map = watch_map_by_year.get(season_year, {})
                for d in sel_dicts:
                    wk = watch_map.get(d["player_id"])
                    d["watch_kyle"] = wk["watch_kyle"] if wk else None
            kyle._add_derived(sel_dicts)
            bounds = kyle.compute_bounds(sel_dicts, exclude_fields=exclude)
            target = dict(row_dict)
            target["player_id"] = player_id
            target["name"]      = player_dict["name"]
            if season_type == "playoffs":
                target["watch_kyle"] = watch_kyle_per_year.get(season_year)
            kyle._add_derived([target])
            kyle._apply_bounds([target], bounds, clamp=True)
            kyle_rating = target.get("kyle_rating")

        age = None
        if player_dict.get("birthdate"):
            try:
                bd  = _date.fromisoformat(player_dict["birthdate"])
                mid = _date(season_year, 2, 1)
                age = mid.year - bd.year - (
                    1 if (mid.month, mid.day) < (bd.month, bd.day) else 0
                )
            except Exception:
                pass

        seasons_out.append({
            "season_id":         season_id,
            "season_year":       season_year,
            "season_type":       season_type,
            "season_label":      row_dict["label"],
            "age":               age,
            "minutes":           row_dict["minutes"],
            "usage_rate":        row_dict["usage_rate"],
            "true_shooting_pct": row_dict["true_shooting_pct"],
            "assist_rate":       row_dict["assist_rate"],
            "turnover_pct":      row_dict["turnover_pct"],
            "on_court_rating":   row_dict["on_court_rating"],
            "on_off_diff":       row_dict["on_off_diff"],
            "bpm":               row_dict["bpm"],
            "defense":           row_dict["defense"],
            "position":          row_dict["position"],
            "playoff_games":     row_dict["playoff_games"],
            "kyle_rating":       kyle_rating,
            "on_off_asterisk":   row_dict.get("on_off_asterisk", 0),
        })

    return {"player": player_dict, "seasons": seasons_out}


def get_player_watch_log(conn, player_id: int) -> dict:
    """Return watch-log data for a single player.

    Response shape::

        {
          "best_player_count":      int,
          "best_player_games":      [game dicts where player was best],
          "important_player_games": [game dicts where player appeared but wasn't best],
          "watch_by_year":          {year: {best_count, total_watched, raw_score,
                                           watch_kyle, best_pct}},
          "total_watched":          int,
          "best_player_pct":        float | None,
          "watch_kyle":             float,
        }

    Raises a 404 abort if the player is not found.
    """
    from flask import abort

    player = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        abort(404)

    best_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM watched_playoff_games WHERE best_player_id = ?",
        (player_id,),
    ).fetchone()["cnt"]

    best_games = conn.execute(
        """
        SELECT g.id, g.game_year, g.home_team, g.away_team, g.winner_team,
               g.round, g.game_of_round, g.conference, g.date_watched, g.notes
        FROM watched_playoff_games g
        WHERE g.best_player_id = ?
        ORDER BY g.game_year DESC, g.date_watched DESC
        """,
        (player_id,),
    ).fetchall()

    important_games = conn.execute(
        """
        SELECT g.id, g.game_year, g.home_team, g.away_team, g.winner_team,
               g.round, g.game_of_round, g.conference, g.date_watched,
               g.best_player_id,
               bp.name AS best_player_name
        FROM watched_game_players wgp
        JOIN watched_playoff_games g ON g.id = wgp.game_id
        LEFT JOIN players bp ON bp.id = g.best_player_id
        WHERE wgp.player_id = ? AND g.best_player_id != ?
        ORDER BY g.game_year DESC, g.date_watched DESC
        """,
        (player_id, player_id),
    ).fetchall()

    all_years = {g["game_year"] for g in best_games} | {g["game_year"] for g in important_games}

    watch_by_year: dict[int, dict] = {}
    for yr in sorted(all_years):
        yr_map = get_watch_kyle_by_player(conn, yr)
        entry  = yr_map.get(player_id)
        if entry:
            watch_by_year[yr] = {
                "best_count":    entry["best_count"],
                "total_watched": entry["total_watched"],
                "raw_score":     entry["raw_score"],
                "watch_kyle":    entry["watch_kyle"],
                "best_pct":      round(entry["raw_score"] * 100, 1),
            }
        else:
            watch_by_year[yr] = {
                "best_count": 0, "total_watched": 0,
                "raw_score": 0.0, "watch_kyle": None, "best_pct": 0.0,
            }

    total_watched_overall = sum(v["total_watched"] for v in watch_by_year.values())
    if total_watched_overall > 0:
        overall_watch_kyle = round(
            sum(v["watch_kyle"] for v in watch_by_year.values() if v["watch_kyle"] is not None),
            3,
        )
        overall_best_pct = round(best_count / total_watched_overall * 100, 1)
    else:
        overall_watch_kyle = 0.0
        overall_best_pct   = None

    return {
        "best_player_count":      best_count,
        "best_player_games":      [dict(r) for r in best_games],
        "important_player_games": [dict(r) for r in important_games],
        "watch_by_year":          watch_by_year,
        "total_watched":          total_watched_overall,
        "best_player_pct":        overall_best_pct,
        "watch_kyle":             overall_watch_kyle,
    }
