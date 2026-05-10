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
from scraper import abbr_to_team_name_variants, scrape_player_birthdate
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


def get_peak_opponent_games(conn, player_id: int, window: int) -> dict:
    """Return playoff games for player_id that include at least one peak opponent.

    Response shape::

        {
          "player_id": int,
          "window": int,
          "games": [
            {
              "game_year": int,
              "game_date": str,
              "team_abbr": str,
              "opp_abbr": str,
              "watched": bool,
              "round": str | None,
              "game_of_round": int | None,
              "conference": str | None,
              "best_player_id": int | None,
              "best_player_name": str | None,
              "peak_opponents": [{"player_id": int, "name": str, "peak_start": int, "peak_end": int}]
            }
          ],
          "all_peak_opponents": [{"player_id": int, "name": str}]
        }
    """
    from flask import abort

    player = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        abort(404)

    # 1. Compute peak windows for all selected players.
    from services import kyle_service
    peaks_list, _ = kyle_service.compute_peak_windows(conn, window)
    # Build map: player_id -> (best_start_year, best_end_year)
    peak_map = {
        p["player_id"]: (p["best_start_year"], p["best_end_year"])
        for p in peaks_list
        if p["player_id"] != player_id
    }

    # 2. Fetch focal player's playoff appearances.
    focal_rows = conn.execute(
        """
        SELECT season_year, game_date, team_abbr, opp_abbr
        FROM player_game_appearances
        WHERE player_id = ? AND season_type = 'playoffs'
        ORDER BY season_year, game_date
        """,
        (player_id,),
    ).fetchall()

    if not focal_rows:
        return {
            "player_id": player_id,
            "window": window,
            "games": [],
            "all_peak_opponents": [],
        }

    # 3. Batch query: find all other selected players appearing in the same
    #    (season_year, game_date, team_abbr, opp_abbr) game as the focal player.
    #    We must match on both teams to avoid false positives when multiple
    #    games are played on the same date.
    game_keys = list({(r["season_year"], r["game_date"], r["team_abbr"], r["opp_abbr"]) for r in focal_rows})

    # Build OR clause matching the exact game by teams (either side of the matchup).
    if game_keys:
        or_clauses = " OR ".join(
            """(pga.season_year = ? AND pga.game_date = ?
                AND ((pga.team_abbr = ? AND pga.opp_abbr = ?)
                  OR (pga.team_abbr = ? AND pga.opp_abbr = ?)))"""
            for _ in game_keys
        )
        params: list = [player_id]
        for sy, gd, ta, oa in game_keys:
            params += [sy, gd, ta, oa, oa, ta]

        other_appearances = conn.execute(
            f"""
            SELECT DISTINCT pga.player_id, p.name, pga.season_year, pga.game_date,
                            pga.team_abbr, pga.opp_abbr
            FROM player_game_appearances pga
            JOIN players p ON p.id = pga.player_id
            WHERE pga.season_type = 'playoffs'
              AND pga.player_id != ?
              AND ({or_clauses})
            """,
            params,
        ).fetchall()
    else:
        other_appearances = []

    # Group by (season_year, game_date, team_abbr, opp_abbr) normalized so
    # both sides of the matchup map to the same key as the focal player's rows.
    from collections import defaultdict
    game_opponents: dict[tuple, list[dict]] = defaultdict(list)
    for row in other_appearances:
        pid = row["player_id"]
        if pid not in peak_map:
            continue
        year = row["season_year"]
        start, end = peak_map[pid]
        if start <= year <= end:
            # Normalise the key to match what focal_rows uses: (year, date, team, opp)
            # The focal row's team_abbr/opp_abbr may be either orientation.
            ta, oa = row["team_abbr"], row["opp_abbr"]
            # Add under both orientations so the lookup below always hits.
            game_opponents[(year, row["game_date"], ta, oa)].append({
                "player_id": pid,
                "name": row["name"],
                "peak_start": start,
                "peak_end": end,
            })
            game_opponents[(year, row["game_date"], oa, ta)].append({
                "player_id": pid,
                "name": row["name"],
                "peak_start": start,
                "peak_end": end,
            })

    def _dedup_opps(opps: list[dict]) -> list[dict]:
        seen: dict[int, dict] = {}
        for o in opps:
            seen.setdefault(o["player_id"], o)
        return list(seen.values())

    # 4. Filter focal games to those with at least one peak opponent.
    qualifying = [
        r for r in focal_rows
        if game_opponents[(r["season_year"], r["game_date"], r["team_abbr"], r["opp_abbr"])]
    ]

    if not qualifying:
        return {
            "player_id": player_id,
            "window": window,
            "games": [],
            "all_peak_opponents": [],
        }

    # 5. Left-join to watched_playoff_games for each qualifying game.
    # Pre-compute game_of_round by ranking each focal game within its series.
    series_game_count: dict[tuple, int] = defaultdict(int)

    games_out = []
    for row in qualifying:
        sy = row["season_year"]
        ta = row["team_abbr"]
        oa = row["opp_abbr"]
        series_key = (sy, ta, oa)
        series_game_count[series_key] += 1
        game_of_round = series_game_count[series_key]

        ta_variants = abbr_to_team_name_variants(ta, sy)
        oa_variants = abbr_to_team_name_variants(oa, sy)
        t1_ph = ",".join("?" * len(ta_variants))
        t2_ph = ",".join("?" * len(oa_variants))

        watched_row = conn.execute(
            f"""
            SELECT w.id, w.round, w.game_of_round, w.conference,
                   w.best_player_id, p.name AS best_player_name
            FROM watched_playoff_games w
            LEFT JOIN players p ON p.id = w.best_player_id
            WHERE w.game_year = ?
              AND w.game_of_round = ?
              AND (
                    (w.home_team IN ({t1_ph}) AND w.away_team IN ({t2_ph}))
                 OR (w.home_team IN ({t2_ph}) AND w.away_team IN ({t1_ph}))
              )
            LIMIT 1
            """,
            [sy, game_of_round,
             *ta_variants, *oa_variants,
             *oa_variants, *ta_variants],
        ).fetchone()

        peak_opps = _dedup_opps(game_opponents[(sy, row["game_date"], ta, oa)])

        games_out.append({
            "game_year":        sy,
            "game_date":        row["game_date"],
            "team_abbr":        ta,
            "opp_abbr":         oa,
            "watched":          watched_row is not None,
            "round":            watched_row["round"]            if watched_row else None,
            "game_of_round":    watched_row["game_of_round"]    if watched_row else None,
            "conference":       watched_row["conference"]       if watched_row else None,
            "best_player_id":   watched_row["best_player_id"]  if watched_row else None,
            "best_player_name": watched_row["best_player_name"] if watched_row else None,
            "peak_opponents":   peak_opps,
        })

    # 6. Build de-duplicated all_peak_opponents list.
    seen_opp: dict[int, str] = {}
    for g in games_out:
        for op in g["peak_opponents"]:
            seen_opp[op["player_id"]] = op["name"]
    all_peak_opponents = sorted(
        [{"player_id": pid, "name": name} for pid, name in seen_opp.items()],
        key=lambda x: x["name"],
    )

    return {
        "player_id": player_id,
        "window": window,
        "games": games_out,
        "all_peak_opponents": all_peak_opponents,
    }


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
