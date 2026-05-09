import logging
from datetime import date as _date
from flask import Flask, jsonify, request, render_template, abort

from db import init_db, get_conn
from scraper import run_scrape, _get_player_appearances, _fetch_bbref_playoff_gamelog, _backfill_opp_abbr, abbr_to_team_name, abbr_to_team_name_variants, _get_nba_id_for_player
import kyle

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Suggest-game candidate cache
# ---------------------------------------------------------------------------
# key  : (window, sorted_player_ids_tuple, watch_log_count)
# value: list of candidate dicts (fully built, ordered, ready to index by skip)
_suggest_cache: dict = {}


# ---------------------------------------------------------------------------
# Initialise DB on startup
# ---------------------------------------------------------------------------
with app.app_context():
    init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    return dict(row)


ROUND_WEIGHTS = {
    "First Round":       1,
    "Second Round":      2,
    "Conference Finals": 4,
    "NBA Finals":        8,
}


def _get_watch_kyle_by_player(conn, season_year):
    """Return dict of player_id -> watch info for a given playoff year.

    Uses round-weighted scoring:
      N = sum of round_weight for games where the player was Best Player
      M = total unweighted games watched
      raw_score = N / M
      watch_kyle = (raw_score / max_raw) * 2 - 1  (year-normalised to -1..+1)
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

    # Pass 1 — compute raw scores
    players = []
    for r in rows:
        M = r["total_watched"] or 0
        N = r["weighted_best"] or 0.0
        raw = N / M if M > 0 else 0.0
        players.append({
            "player_id":     r["player_id"],
            "total_watched": M,
            "weighted_best": N,
            "raw":           raw,
        })

    # Pass 2 — year-level normalisation
    max_raw = max((p["raw"] for p in players), default=0.0)

    result = {}
    for p in players:
        if p["total_watched"] == 0:
            continue
        if max_raw > 0:
            watch_kyle = round((p["raw"] / max_raw) * 2 - 1, 3)
        else:
            watch_kyle = 0.0
        result[p["player_id"]] = {
            "watch_kyle":    watch_kyle,
            "best_count":    p["weighted_best"],   # weighted N (for sub-label numerator)
            "total_watched": p["total_watched"],   # unweighted M (for sub-label denominator)
            "raw_score":     round(p["raw"], 4),
        }
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# --- Seasons ----------------------------------------------------------------

@app.route("/api/seasons")
def get_seasons():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM seasons ORDER BY season_year DESC, season_type").fetchall()
    conn.close()
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/seasons", methods=["POST"])
def create_season():
    """Create a new season row.

    Expects JSON: {"season_year": 2025, "season_type": "regular"|"playoffs"}
    Returns the newly created season object.
    """
    data = request.get_json(force=True)
    year = data.get("season_year")
    season_type = data.get("season_type")

    if not year or season_type not in ("regular", "playoffs"):
        return jsonify({"error": "season_year (int) and season_type ('regular' or 'playoffs') are required"}), 400

    type_label = "Regular Season" if season_type == "regular" else "Playoffs"
    label = f"{year} {type_label}"

    conn = get_conn()
    try:
        # Prevent exact duplicates
        existing = conn.execute(
            "SELECT id FROM seasons WHERE season_year = ? AND season_type = ?",
            (year, season_type),
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({"error": f"Season '{label}' already exists", "id": existing["id"]}), 409

        cur = conn.execute(
            "INSERT INTO seasons (label, season_year, season_type) VALUES (?, ?, ?)",
            (label, year, season_type),
        )
        new_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM seasons WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()

    return jsonify(_row_to_dict(row)), 201


@app.route("/api/seasons/<int:season_id>/nearest_selected", methods=["GET"])
def nearest_selected(season_id):
    """Return the selected player IDs from the nearest season.

    Priority:
    1. Another season in the same year (either type).
    2. The season with the closest year (fewest years away).
    If multiple seasons tie on distance, prefer the one with the most
    selected players.
    """
    conn = get_conn()
    try:
        # Get the year of the target season
        target = conn.execute(
            "SELECT season_year FROM seasons WHERE id = ?", (season_id,)
        ).fetchone()
        if not target:
            return jsonify({"error": "Season not found"}), 404

        target_year = target["season_year"]

        # Find all other seasons that have at least one selected player,
        # along with how many selected players they have.
        candidates = conn.execute(
            """
            SELECT s.id, s.season_year, s.season_type, COUNT(sp.id) AS cnt
            FROM seasons s
            JOIN selected_players sp ON sp.season_id = s.id
            WHERE s.id != ?
            GROUP BY s.id
            """,
            (season_id,),
        ).fetchall()

        if not candidates:
            return jsonify({"player_ids": []})

        # Score each candidate: primary key = abs(year diff), secondary = -cnt
        def sort_key(row):
            return (abs(row["season_year"] - target_year), -row["cnt"])

        best = min(candidates, key=sort_key)

        player_ids = [
            r["player_id"]
            for r in conn.execute(
                "SELECT player_id FROM selected_players WHERE season_id = ?",
                (best["id"],),
            ).fetchall()
        ]
    finally:
        conn.close()

    return jsonify({"player_ids": player_ids, "source_season_id": best["id"]})


@app.route("/api/seasons/<int:season_id>", methods=["DELETE"])
def delete_season(season_id):
    """Delete a season and all associated data.

    Removes rows from selected_players, player_stats, and finally the
    seasons table itself. Intended for cleaning up accidentally created
    or no-longer-needed seasons from the UI.
    """
    conn = get_conn()
    try:
        # Remove dependent rows first to satisfy foreign keys.
        conn.execute("DELETE FROM selected_players WHERE season_id = ?", (season_id,))
        conn.execute("DELETE FROM player_stats   WHERE season_id = ?", (season_id,))
        deleted = conn.execute("DELETE FROM seasons WHERE id = ?", (season_id,)).rowcount
        conn.commit()
    finally:
        conn.close()

    if not deleted:
        abort(404)
    return jsonify({"ok": True})


# --- Players list (all players with stats for a season) --------------------

@app.route("/api/players")
def get_players():
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT p.id, p.name
        FROM players p
        JOIN player_stats ps ON ps.player_id = p.id
        WHERE ps.season_id = ?
        ORDER BY p.name
        """,
        (season_id,),
    ).fetchall()
    conn.close()
    return jsonify([_row_to_dict(r) for r in rows])


# --- Selected set -----------------------------------------------------------

@app.route("/api/selected")
def get_selected():
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400

    conn = get_conn()
    season_row = conn.execute(
        "SELECT season_type, season_year FROM seasons WHERE id = ?", (season_id,)
    ).fetchone()
    rows = conn.execute(
        """
        SELECT
            sp.id            AS selected_id,
            p.id             AS player_id,
            p.name,
            ps.id            AS stats_id,
            ps.minutes,
            ps.usage_rate,
            ps.true_shooting_pct,
            ps.assist_rate,
            ps.turnover_pct,
            ps.on_court_rating,
            ps.on_off_diff,
            ps.bpm,
            ps.defense,
            ps.position,
            ps.playoff_games,
            ps.on_off_asterisk
        FROM selected_players sp
        JOIN players p       ON p.id  = sp.player_id
        JOIN player_stats ps ON ps.player_id = sp.player_id
                             AND ps.season_id = sp.season_id
        WHERE sp.season_id = ?
        ORDER BY p.name
        """,
        (season_id,),
    ).fetchall()

    season_type = season_row["season_type"] if season_row else "regular"
    player_dicts = [_row_to_dict(r) for r in rows]

    if season_type == "playoffs" and season_row:
        watch_map = _get_watch_kyle_by_player(conn, season_row["season_year"])
        for d in player_dicts:
            wk = watch_map.get(d["player_id"])
            d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
            d["watch_best_count"]    = wk["best_count"]    if wk else None
            d["watch_total_watched"] = wk["total_watched"] if wk else None
            d["watch_raw_score"]     = wk["raw_score"]     if wk else None

    conn.close()

    calculated = kyle.calculate(player_dicts, season_type=season_type)
    return jsonify(calculated)


@app.route("/api/selected", methods=["POST"])
def add_selected():
    data = request.get_json(force=True)
    player_id = data.get("player_id")
    season_id = data.get("season_id")
    if not player_id or not season_id:
        return jsonify({"error": "player_id and season_id required"}), 400

    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO selected_players (player_id, season_id) VALUES (?, ?)",
            (player_id, season_id),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/selected/<int:selected_id>", methods=["DELETE"])
def remove_selected(selected_id):
    conn = get_conn()
    conn.execute("DELETE FROM selected_players WHERE id = ?", (selected_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --- All-players scored view ------------------------------------------------

@app.route("/api/all_players")
def get_all_players():
    """Return every player with stats for a season, scored using the bounds
    derived from the *selected* set.  Normalised values are NOT clamped."""
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400

    conn = get_conn()

    season_row = conn.execute(
        "SELECT season_type, season_year FROM seasons WHERE id = ?", (season_id,)
    ).fetchone()
    season_type = season_row["season_type"] if season_row else "regular"
    season_year = season_row["season_year"] if season_row else None

    # Fetch selected players to compute bounds
    selected_rows = conn.execute(
        """
        SELECT
            p.id AS player_id, p.name,
            ps.id AS stats_id,
            ps.minutes, ps.usage_rate, ps.true_shooting_pct,
            ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
            ps.on_off_diff, ps.bpm, ps.defense, ps.position, ps.playoff_games,
            ps.on_off_asterisk
        FROM selected_players sp
        JOIN players p       ON p.id  = sp.player_id
        JOIN player_stats ps ON ps.player_id = sp.player_id
                             AND ps.season_id = sp.season_id
        WHERE sp.season_id = ?
        """,
        (season_id,),
    ).fetchall()

    # Fetch ALL players with stats for this season
    all_rows = conn.execute(
        """
        SELECT
            p.id AS player_id, p.name,
            ps.id AS stats_id,
            ps.minutes, ps.usage_rate, ps.true_shooting_pct,
            ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
            ps.on_off_diff, ps.bpm, ps.defense, ps.position, ps.playoff_games,
            ps.on_off_asterisk
        FROM players p
        JOIN player_stats ps ON ps.player_id = p.id
        WHERE ps.season_id = ?
        ORDER BY p.name
        """,
        (season_id,),
    ).fetchall()

    # Build bounds from selected set
    selected_dicts = [_row_to_dict(r) for r in selected_rows]

    if season_type == "playoffs" and season_year:
        watch_map = _get_watch_kyle_by_player(conn, season_year)
        for d in selected_dicts:
            wk = watch_map.get(d["player_id"])
            d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
            d["watch_best_count"]    = wk["best_count"]    if wk else None
            d["watch_total_watched"] = wk["total_watched"] if wk else None
            d["watch_raw_score"]     = wk["raw_score"]     if wk else None
    else:
        watch_map = {}

    conn.close()

    kyle._add_derived(selected_dicts)
    exclude = {"minutes"} if season_type == "playoffs" else set()
    bounds = kyle.compute_bounds(selected_dicts, exclude_fields=exclude)

    # Score all players with those bounds (no clamping)
    all_dicts = [_row_to_dict(r) for r in all_rows]

    if season_type == "playoffs" and watch_map:
        for d in all_dicts:
            wk = watch_map.get(d["player_id"])
            d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
            d["watch_best_count"]    = wk["best_count"]    if wk else None
            d["watch_total_watched"] = wk["total_watched"] if wk else None
            d["watch_raw_score"]     = wk["raw_score"]     if wk else None

    calculated = kyle.calculate_all(all_dicts, bounds, season_type=season_type)
    return jsonify(calculated)


@app.route("/all")
def all_players_page():
    return render_template("all_players.html")


@app.route("/cumulative")
def cumulative_kyle_page():
    return render_template("cumulative_kyle.html")


@app.route("/best3year")
def best3year_page():
    return render_template("best3year_kyle.html")


# --- Cumulative K.Y.L.E. ---------------------------------------------------

@app.route("/api/cumulative_kyle")
def cumulative_kyle():
    """Return per-player totals of K.Y.L.E. across all seasons.

    For every player who appears in at least one season, returns:
        - player_id, name
        - regular_kyle  : sum of kyle_rating across all regular seasons
        - playoffs_kyle : sum of kyle_rating across all playoff seasons
        - total_kyle    : regular_kyle + playoffs_kyle

    K.Y.L.E. ratings are computed from each season's *selected* set bounds
    (same as the /api/selected endpoint), so only seasons that have at least
    one selected player contribute a rating.
    """
    conn = get_conn()

    # All seasons that have at least one selected player
    seasons = conn.execute(
        """
        SELECT DISTINCT s.id, s.season_type, s.season_year
        FROM seasons s
        JOIN selected_players sp ON sp.season_id = s.id
        """
    ).fetchall()

    # Map player_id -> {"name": ..., "regular": 0.0, "playoffs": 0.0}
    totals: dict[int, dict] = {}

    for season in seasons:
        season_id = season["id"]
        season_type = season["season_type"]

        rows = conn.execute(
            """
            SELECT
                p.id AS player_id, p.name,
                ps.minutes, ps.usage_rate, ps.true_shooting_pct,
                ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
                ps.on_off_diff, ps.bpm, ps.defense, ps.on_off_asterisk
            FROM selected_players sp
            JOIN players p       ON p.id  = sp.player_id
            JOIN player_stats ps ON ps.player_id = sp.player_id
                                 AND ps.season_id = sp.season_id
            WHERE sp.season_id = ?
            """,
            (season_id,),
        ).fetchall()

        if not rows:
            continue

        player_dicts = [_row_to_dict(r) for r in rows]

        if season_type == "playoffs":
            watch_map = _get_watch_kyle_by_player(conn, season["season_year"])
            for d in player_dicts:
                wk = watch_map.get(d["player_id"])
                d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
                d["watch_best_count"]    = wk["best_count"]    if wk else None
                d["watch_total_watched"] = wk["total_watched"] if wk else None

        calculated = kyle.calculate(player_dicts, season_type=season_type)

        for p in calculated:
            pid = p["player_id"]
            rating = p.get("kyle_rating")
            if rating is None or rating < 0:
                continue
            if pid not in totals:
                totals[pid] = {"player_id": pid, "name": p["name"], "regular_kyle": 0.0, "playoffs_kyle": 0.0}
            if season_type == "regular":
                totals[pid]["regular_kyle"] = round(totals[pid]["regular_kyle"] + rating, 4)
            else:
                totals[pid]["playoffs_kyle"] = round(totals[pid]["playoffs_kyle"] + rating, 4)

    conn.close()

    result = []
    for entry in totals.values():
        entry["total_kyle"] = round(entry["regular_kyle"] + entry["playoffs_kyle"], 4)
        result.append(entry)

    result.sort(key=lambda x: x["total_kyle"], reverse=True)
    return jsonify(result)


# --- Best N-Year K.Y.L.E. --------------------------------------------------

@app.route("/api/best3year")
def best3year():
    """Return each player's best consecutive N-year K.Y.L.E. window.

    Query parameters
    ----------------
    window : int (default 3, min 1, max 20)
        Number of consecutive seasons to include in the window.

    For every player who appears in at least one selected season:
      - K.Y.L.E. ratings are computed season-by-season from the selected set
        bounds (same as cumulative), but BOTH positive and negative ratings
        are included.
      - Regular + playoff ratings for the same season_year are combined into
        a single year total.
      - All possible consecutive N-year windows are evaluated and the window
        with the highest combined total is returned.

    Returns (sorted by best_window_total desc):
        player_id, name,
        best_start_year, best_end_year,
        regular_total, playoffs_total, best_window_total, window
    """
    window = request.args.get("window", 3, type=int)
    window = max(1, min(window, 20))  # clamp to sane range

    conn = get_conn()

    # Pre-compute playoff_played (all players, not just selected) from player_stats
    # {(player_id, year) -> game_count}
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

    # Pre-compute playoff_watched for all players from watched_game_players
    # {(player_id, year) -> count}
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

    seasons = conn.execute(
        """
        SELECT DISTINCT s.id, s.season_year, s.season_type
        FROM seasons s
        JOIN selected_players sp ON sp.season_id = s.id
        ORDER BY s.season_year
        """
    ).fetchall()

    # player_id -> {year -> {"regular": float, "playoffs": float, "name": str}}
    player_years: dict[int, dict] = {}

    for season in seasons:
        season_id   = season["id"]
        season_year = season["season_year"]
        season_type = season["season_type"]

        rows = conn.execute(
            """
            SELECT
                p.id AS player_id, p.name,
                ps.minutes, ps.usage_rate, ps.true_shooting_pct,
                ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
                ps.on_off_diff, ps.bpm, ps.defense, ps.on_off_asterisk,
                ps.playoff_games
            FROM selected_players sp
            JOIN players p       ON p.id  = sp.player_id
            JOIN player_stats ps ON ps.player_id = sp.player_id
                                 AND ps.season_id = sp.season_id
            WHERE sp.season_id = ?
            """,
            (season_id,),
        ).fetchall()

        if not rows:
            continue

        player_dicts = [_row_to_dict(r) for r in rows]
        # build a quick lookup of playoff_games per player for this season
        playoff_games_map = {r["player_id"]: (r["playoff_games"] or 0) for r in rows}

        watch_map = {}
        if season_type == "playoffs":
            watch_map = _get_watch_kyle_by_player(conn, season_year)
            for d in player_dicts:
                wk = watch_map.get(d["player_id"])
                d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
                d["watch_best_count"]    = wk["best_count"]    if wk else None
                d["watch_total_watched"] = wk["total_watched"] if wk else None

        calculated   = kyle.calculate(player_dicts, season_type=season_type)

        for p in calculated:
            pid    = p["player_id"]
            rating = p.get("kyle_rating")
            if rating is None:
                continue
            if pid not in player_years:
                player_years[pid] = {"name": p["name"], "years": {}}
            yr_data = player_years[pid]["years"].setdefault(
                season_year, {"regular": 0.0, "playoffs": 0.0, "watch_kyle": None,
                              "playoff_watched": 0, "playoff_played": 0}
            )
            if season_type == "regular":
                yr_data["regular"] += rating
            else:
                yr_data["playoffs"] += rating
                wk = watch_map.get(pid)
                if wk and wk.get("watch_kyle") is not None:
                    yr_data["watch_kyle"] = wk["watch_kyle"]
                yr_data["playoff_watched"] = wk["total_watched"] if wk else 0
                yr_data["playoff_played"]  = playoff_games_map.get(pid, 0)

    conn.close()

    result = []
    for pid, pdata in player_years.items():
        name  = pdata["name"]
        years = pdata["years"]  # {year: {regular, playoffs}}
        sorted_years = sorted(years.keys())

        if len(sorted_years) < window:
            continue  # not enough distinct years with actual scores

        first_year = sorted_years[0]
        last_year  = sorted_years[-1]
        all_years  = list(range(first_year, last_year + 1))  # dense range

        if len(all_years) < window:
            continue

        best_window_entry = None

        ZERO_YEAR = {"regular": 0.0, "playoffs": 0.0, "watch_kyle": None,
                     "playoff_watched": 0, "playoff_played": 0}

        for i in range(len(all_years) - window + 1):
            window_years = all_years[i : i + window]
            # No consecutive-check needed — all_years is already dense

            reg   = sum(years.get(y, ZERO_YEAR)["regular"]  for y in window_years)
            play  = sum(years.get(y, ZERO_YEAR)["playoffs"] for y in window_years)
            total = reg + play

            wk_vals = [years[y]["watch_kyle"] for y in window_years
                       if y in years and years[y].get("watch_kyle") is not None]
            wk_total = round(sum(wk_vals), 4) if wk_vals else None

            pw_watched = sum(all_playoff_watched.get((pid, y), 0) for y in window_years)
            pw_played  = sum(all_playoff_played.get((pid, y), 0)  for y in window_years)

            if best_window_entry is None or total > best_window_entry["best_window_total"]:
                best_window_entry = {
                    "player_id":        pid,
                    "name":             name,
                    "best_start_year":  window_years[0],
                    "best_end_year":    window_years[-1],
                    "regular_total":    round(reg,   4),
                    "playoffs_total":   round(play,  4),
                    "best_window_total": round(total, 4),
                    "watch_kyle_total": wk_total,
                    "playoff_watched":  pw_watched,
                    "playoff_played":   pw_played,
                    "window":           window,
                }

        if best_window_entry:
            result.append(best_window_entry)

    # ── Compute Least Squares scores ─────────────────────────────────────────
    # Build peak-window lookup: {player_id: (start_year, end_year)}
    peak_windows = {
        entry["player_id"]: (entry["best_start_year"], entry["best_end_year"])
        for entry in result
    }

    conn2 = get_conn()
    try:
        game_rows = conn2.execute(
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
    finally:
        conn2.close()

    # Group by game_id
    from collections import defaultdict
    games_map: dict = defaultdict(lambda: {"game_year": None, "best_player_id": None, "player_ids": []})
    player_game_counts: dict[int, int] = defaultdict(int)
    for r in game_rows:
        gid = r["game_id"]
        games_map[gid]["game_year"] = r["game_year"]
        games_map[gid]["best_player_id"] = r["best_player_id"]
        games_map[gid]["player_ids"].append(r["player_id"])
        # Only count games that fall within the player's peak window
        pid = r["player_id"]
        game_year = r["game_year"]
        if pid in peak_windows and peak_windows[pid][0] <= game_year <= peak_windows[pid][1]:
            player_game_counts[pid] += 1

    # Only include players watched in at least 5 peak-window games in LS comparisons
    MIN_GAMES_WATCHED = 5
    qualified_players = {pid for pid, cnt in player_game_counts.items() if cnt >= MIN_GAMES_WATCHED}

    comparisons = []
    win_counts: dict[int, int] = defaultdict(int)
    loss_counts: dict[int, int] = defaultdict(int)

    for gid, gdata in games_map.items():
        game_year = gdata["game_year"]
        best_id   = gdata["best_player_id"]
        if best_id is None:
            continue
        # Filter players whose peak window contains this game_year
        # and who have been watched in at least MIN_GAMES_WATCHED peak-window games
        filtered = [
            pid for pid in gdata["player_ids"]
            if pid in peak_windows
            and peak_windows[pid][0] <= game_year <= peak_windows[pid][1]
            and pid in qualified_players
        ]
        if best_id not in filtered or len(filtered) < 2:
            continue
        for other_id in filtered:
            if other_id == best_id:
                continue
            comparisons.append((best_id, other_id))
            win_counts[best_id] += 1
            loss_counts[other_id] += 1

    ls_scores = kyle.compute_least_squares_scores(comparisons)

    # Attach ls_score and comparison counts to each result entry
    for entry in result:
        pid = entry["player_id"]
        score = ls_scores.get(pid)
        w = win_counts.get(pid, 0)
        l = loss_counts.get(pid, 0)
        entry["ls_score"] = score
        entry["ls_wins"]  = w
        entry["ls_losses"] = l
        entry["ls_comparisons"] = w + l

    result.sort(key=lambda x: x["best_window_total"], reverse=True)
    return jsonify(result)



# ---------------------------------------------------------------------------
# Suggest Game endpoint
# ---------------------------------------------------------------------------

@app.route("/api/suggest_game")
def suggest_game():
    """Find the best unwatched playoff game featuring two players whose peak
    windows overlap.

    Query parameters
    ----------------
    window : int (default 3)  — passed to the same peak-window logic as best3year
    skip   : int (default 0)  — number of unwatched candidates to skip (for "Next" navigation)

    The full ordered candidate list is computed once and cached server-side,
    keyed by (window, selected_player_ids, watch_log_count).  Subsequent
    "Next" clicks are served instantly from the cache; the cache is
    automatically invalidated whenever the watch log or selected-player set
    changes.
    """
    import itertools

    window = request.args.get("window", 3, type=int)
    window = max(1, min(window, 20))
    skip   = request.args.get("skip", 0, type=int)
    skip   = max(0, skip)

    conn = get_conn()
    try:
        # ── Build the cache key ──────────────────────────────────────────────
        selected_ids = tuple(sorted(
            r[0] for r in conn.execute(
                "SELECT DISTINCT player_id FROM selected_players"
            ).fetchall()
        ))
        watch_log_count = conn.execute(
            "SELECT COUNT(*) FROM watched_playoff_games"
        ).fetchone()[0]
        cache_key = (window, selected_ids, watch_log_count)

        if cache_key in _suggest_cache:
            candidates = _suggest_cache[cache_key]
            conn.close()
            if skip < len(candidates):
                return jsonify(candidates[skip])
            return jsonify({"result": "none", "message": "No unwatched games found for any overlapping pair."})

        # ── Cache miss — build the full candidate list ───────────────────────

        # Step 1: Replicate best3year logic to get each player's peak window
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

            rows = conn.execute(
                """
                SELECT
                    p.id AS player_id, p.name,
                    ps.minutes, ps.usage_rate, ps.true_shooting_pct,
                    ps.assist_rate, ps.turnover_pct, ps.on_court_rating,
                    ps.on_off_diff, ps.bpm, ps.defense, ps.on_off_asterisk,
                    ps.playoff_games
                FROM selected_players sp
                JOIN players p       ON p.id  = sp.player_id
                JOIN player_stats ps ON ps.player_id = sp.player_id
                                     AND ps.season_id = sp.season_id
                WHERE sp.season_id = ?
                """,
                (season_id,),
            ).fetchall()

            if not rows:
                continue

            player_dicts = [_row_to_dict(r) for r in rows]

            if season_type == "playoffs":
                watch_map = _get_watch_kyle_by_player(conn, season_year)
                for d in player_dicts:
                    wk = watch_map.get(d["player_id"])
                    d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
                    d["watch_best_count"]    = wk["best_count"]    if wk else None
                    d["watch_total_watched"] = wk["total_watched"] if wk else None

            calculated = kyle.calculate(player_dicts, season_type=season_type)

            for p in calculated:
                pid    = p["player_id"]
                rating = p.get("kyle_rating")
                if rating is None:
                    continue
                if pid not in player_years:
                    player_years[pid] = {"name": p["name"], "years": {}}
                yr_data = player_years[pid]["years"].setdefault(
                    season_year, {"regular": 0.0, "playoffs": 0.0}
                )
                if season_type == "regular":
                    yr_data["regular"] += rating
                else:
                    yr_data["playoffs"] += rating

        # Build peak-window entries (same as best3year)
        peaks = []
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

            ZERO_YEAR = {"regular": 0.0, "playoffs": 0.0}
            best_entry = None
            for i in range(len(all_years) - window + 1):
                window_years = all_years[i : i + window]
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

        if len(peaks) < 2:
            _suggest_cache[cache_key] = []
            conn.close()
            return jsonify({"result": "none", "message": "No overlapping peak windows."})

        # Pull nba_id and bbref_url for each player in peaks
        player_ids = [p["player_id"] for p in peaks]
        player_meta_rows = conn.execute(
            f"SELECT id, nba_id, bbref_url FROM players WHERE id IN ({','.join('?' for _ in player_ids)})",
            player_ids,
        ).fetchall()
        player_meta = {r["id"]: {"nba_id": r["nba_id"], "bbref_url": r["bbref_url"]} for r in player_meta_rows}

        # Step 2: Build overlapping pairs sorted by min(score) desc
        pairs = []
        for a, b in itertools.combinations(peaks, 2):
            if a["best_start_year"] <= b["best_end_year"] and b["best_start_year"] <= a["best_end_year"]:
                pair_score    = min(a["best_window_total"], b["best_window_total"])
                overlap_start = max(a["best_start_year"], b["best_start_year"])
                overlap_end   = min(a["best_end_year"],   b["best_end_year"])
                pairs.append((pair_score, a, b, overlap_start, overlap_end))

        if not pairs:
            _suggest_cache[cache_key] = []
            conn.close()
            return jsonify({"result": "none", "message": "No players with overlapping peak windows."})

        pairs.sort(key=lambda x: x[0], reverse=True)

        # Step 3: Iterate ALL pairs, fetch appearances, collect ALL unwatched
        # candidates into a list.  We no longer return early — the goal is to
        # build the complete ordered list once so every subsequent "Next" click
        # is served from cache without any recomputation.
        candidates = []

        for pair_score, pa, pb, overlap_start, overlap_end in pairs:
            p1_id   = pa["player_id"]
            p2_id   = pb["player_id"]
            p1_meta = player_meta.get(p1_id, {})
            p2_meta = player_meta.get(p2_id, {})

            # Fetch / ensure appearances are cached for every year in the overlap
            for year in range(overlap_start, overlap_end + 1):
                for pid, meta in [(p1_id, p1_meta), (p2_id, p2_meta)]:
                    nba_id      = meta.get("nba_id")
                    bbref_url   = meta.get("bbref_url")
                    player_name = pa["name"] if pid == p1_id else pb["name"]

                    already_cached = conn.execute(
                        "SELECT 1 FROM player_game_appearances "
                        "WHERE player_id=? AND season_year=? AND season_type='playoffs' LIMIT 1",
                        (pid, year),
                    ).fetchone()

                    if already_cached:
                        # Only backfill opp_abbr the very first time (when the
                        # appearance rows were just written).  On a cache hit we
                        # skip it to avoid the extra DB/network round-trip.
                        continue

                    # Try to auto-discover nba_id when missing
                    if not nba_id:
                        discovered_id = _get_nba_id_for_player(player_name, conn)
                        if discovered_id:
                            nba_id = discovered_id
                            meta["nba_id"] = discovered_id

                    # stats.nba.com LeagueGameLog returns empty for pre-2000
                    use_nba_api = nba_id and year >= 2000

                    if use_nba_api:
                        _get_player_appearances(
                            player_name, pid, nba_id, year, "playoffs", [], conn
                        )
                        _backfill_opp_abbr(pid, nba_id, bbref_url, player_name, year, conn)
                    elif bbref_url:
                        _fetch_bbref_playoff_gamelog(pid, bbref_url, year, conn)
                    else:
                        logger.warning(
                            "No nba_id or bbref_url for player '%s' year=%s — skipping",
                            player_name, year,
                        )

            # Find co-appearance games (players must have faced each other)
            co_games = conn.execute(
                """
                SELECT a1.game_date, a1.season_year,
                       a1.team_abbr AS team1_abbr, a2.team_abbr AS team2_abbr,
                       ROW_NUMBER() OVER (
                           PARTITION BY a1.season_year, a1.team_abbr, a1.opp_abbr
                           ORDER BY a1.game_date
                       ) AS game_of_round
                FROM player_game_appearances a1
                JOIN player_game_appearances a2
                  ON a1.game_date = a2.game_date
                 AND a1.season_year = a2.season_year
                 AND a1.opp_abbr = a2.team_abbr
                WHERE a1.player_id = ?
                  AND a2.player_id = ?
                  AND a1.season_type = 'playoffs'
                  AND a2.season_type = 'playoffs'
                  AND a1.season_year BETWEEN ? AND ?
                ORDER BY a1.season_year ASC, a1.game_date ASC
                """,
                (p1_id, p2_id, overlap_start, overlap_end),
            ).fetchall()

            for game in co_games:
                game_date     = game["game_date"]
                season_year   = game["season_year"]
                game_of_round = game["game_of_round"]
                team1_variants = abbr_to_team_name_variants(game["team1_abbr"], season_year)
                team2_variants = abbr_to_team_name_variants(game["team2_abbr"], season_year)
                team1_name = team1_variants[0] if team1_variants else game["team1_abbr"]
                team2_name = team2_variants[0] if team2_variants else game["team2_abbr"]

                t1_ph = ",".join("?" * len(team1_variants))
                t2_ph = ",".join("?" * len(team2_variants))
                watched = conn.execute(
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
                ).fetchone()

                if watched:
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
                        "game_date":   game_date,
                        "team1":       team1_name,
                        "team2":       team2_name,
                        "round":       None,
                        "round_known": False,
                    },
                })

        # Store the full list in the cache
        _suggest_cache[cache_key] = candidates

        conn.close()
        if skip < len(candidates):
            return jsonify(candidates[skip])
        return jsonify({"result": "none", "message": "No unwatched games found for any overlapping pair."})

    except Exception as exc:
        logger.exception("suggest_game error")
        conn.close()
        return jsonify({"result": "error", "message": str(exc)}), 500


@app.route("/api/selected", methods=["DELETE"])
def clear_selected():
    """Clear *all* data for a given season (player_stats + selected_players).

    This is used by the "Clear All" button in the UI, which is intended to
    fully reset the chosen season so a fresh scrape can be run.
    """
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400

    conn = get_conn()
    try:
        # Remove any selections and stats for this season.
        conn.execute("DELETE FROM selected_players WHERE season_id = ?", (season_id,))
        conn.execute("DELETE FROM player_stats   WHERE season_id = ?", (season_id,))
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True})


# --- Update (scrape) --------------------------------------------------------

@app.route("/api/update", methods=["POST"])
def update():
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400
    try:
        n = run_scrape(season_id)
        return jsonify({"ok": True, "players_upserted": n})
    except Exception as exc:
        logger.exception("Scrape failed")
        return jsonify({"error": str(exc)}), 500


# --- Stats patch (defense / position) ---------------------------------------

@app.route("/api/stats/<int:stats_id>", methods=["PATCH"])
def patch_stats(stats_id):
    data = request.get_json(force=True)
    allowed = {"defense", "position"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "No patchable fields provided"}), 400

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [stats_id]

    conn = get_conn()
    conn.execute(f"UPDATE player_stats SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --- Player profile page + history -----------------------------------------

@app.route("/player/<int:player_id>")
def player_page(player_id):
    return render_template("player.html")


@app.route("/api/player/<int:player_id>")
def player_history(player_id):
    """
    Return a player's info and their stats for every season they appear in,
    each scored with that season's selected-set K.Y.L.E. bounds.

    Response shape:
        {
          "player": {id, name, bbref_url, birthdate},
          "seasons": [
            {season_id, season_year, season_type, season_label, age,
             minutes, usage_rate, true_shooting_pct, assist_rate,
             turnover_pct, on_court_rating, on_off_diff, bpm,
             defense, position, playoff_games, kyle_rating},
            ...
          ]
        }
    """
    conn = get_conn()

    player = conn.execute(
        "SELECT * FROM players WHERE id = ?", (player_id,)
    ).fetchone()
    if not player:
        conn.close()
        abort(404)

    player_dict = _row_to_dict(player)

    # All seasons this player has stats for
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

    # Fetch all selected-player stats for every relevant season in one query
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
            sid = r["season_id"]
            selected_by_season.setdefault(sid, []).append(_row_to_dict(r))

    # Pre-fetch watch_kyle per year for this player (for playoff seasons)
    watch_kyle_per_year: dict[int, float] = {}
    watch_rows = conn.execute(
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
    ).fetchall()
    for r in watch_rows:
        total = r["total_watched"] or 0
        best  = r["best_count"] or 0
        if total > 0:
            pct = best / total
            watch_kyle_per_year[r["game_year"]] = round(pct * 2 - 1, 3)

    # Pre-fetch watch_kyle maps for all playoff season years (for selected-set bounds)
    playoff_years = list({
        r["season_year"] for r in season_rows
        if r["season_type"] == "playoffs"
    })
    watch_map_by_year: dict[int, dict] = {}
    for yr in playoff_years:
        watch_map_by_year[yr] = _get_watch_kyle_by_player(conn, yr)

    conn.close()

    # Scrape birthdate on first visit if we have a bbref_url but no date yet
    if not player_dict.get("birthdate") and player_dict.get("bbref_url"):
        try:
            from scraper import scrape_player_birthdate
            bd = scrape_player_birthdate(player_dict["bbref_url"])
            if bd:
                upd = get_conn()
                upd.execute(
                    "UPDATE players SET birthdate = ? WHERE id = ?", (bd, player_id)
                )
                upd.commit()
                upd.close()
                player_dict["birthdate"] = bd
        except Exception:
            pass

    seasons_out = []
    for row in season_rows:
        row_dict = _row_to_dict(row)
        season_id   = row_dict["season_id"]
        season_type = row_dict["season_type"]
        season_year = row_dict["season_year"]

        # Compute K.Y.L.E. against the selected-set bounds for this season
        kyle_rating = None
        selected = selected_by_season.get(season_id, [])
        if selected:
            exclude    = {"minutes"} if season_type == "playoffs" else set()
            sel_dicts  = [dict(r) for r in selected]
            # Attach watch_kyle to selected-set dicts for playoff bounds
            if season_type == "playoffs":
                watch_map = watch_map_by_year.get(season_year, {})
                for d in sel_dicts:
                    wk = watch_map.get(d["player_id"])
                    d["watch_kyle"] = wk["watch_kyle"] if wk else None
            kyle._add_derived(sel_dicts)
            bounds     = kyle.compute_bounds(sel_dicts, exclude_fields=exclude)
            target     = dict(row_dict)
            target["player_id"] = player_id
            target["name"]      = player_dict["name"]
            if season_type == "playoffs":
                target["watch_kyle"] = watch_kyle_per_year.get(season_year)
            kyle._add_derived([target])
            kyle._apply_bounds([target], bounds, clamp=True)
            kyle_rating = target.get("kyle_rating")

        # Age at the midpoint of the season (Feb 1 of the season year)
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
            "season_id":        season_id,
            "season_year":      season_year,
            "season_type":      season_type,
            "season_label":     row_dict["label"],
            "age":              age,
            "minutes":          row_dict["minutes"],
            "usage_rate":       row_dict["usage_rate"],
            "true_shooting_pct": row_dict["true_shooting_pct"],
            "assist_rate":      row_dict["assist_rate"],
            "turnover_pct":     row_dict["turnover_pct"],
            "on_court_rating":  row_dict["on_court_rating"],
            "on_off_diff":      row_dict["on_off_diff"],
            "bpm":              row_dict["bpm"],
            "defense":          row_dict["defense"],
            "position":         row_dict["position"],
            "playoff_games":    row_dict["playoff_games"],
            "kyle_rating":      kyle_rating,
            "on_off_asterisk":  row_dict.get("on_off_asterisk", 0),
        })

    return jsonify({"player": player_dict, "seasons": seasons_out})


# ---------------------------------------------------------------------------
# Watch Log routes
# ---------------------------------------------------------------------------

ROUND_MAP = {
    "First Round": 1,
    "Second Round": 2,
    "Conference Finals": 3,
    "NBA Finals": 4,
}


def _game_row_to_dict(row):
    d = dict(row)
    return d


@app.route("/watch_log")
def watch_log_page():
    return render_template("watch_log.html")


@app.route("/api/watched_games")
def list_watched_games():
    """List all watched games with optional filters: year, round, conference."""
    year       = request.args.get("year", type=int)
    round_str  = request.args.get("round")
    conference = request.args.get("conference")

    where_clauses = []
    params = []
    if year:
        where_clauses.append("g.game_year = ?")
        params.append(year)
    if round_str:
        where_clauses.append("g.round = ?")
        params.append(round_str)
    if conference:
        where_clauses.append("g.conference = ?")
        params.append(conference)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    conn = get_conn()
    rows = conn.execute(
        f"""
        SELECT g.*,
               p.name AS best_player_name
        FROM watched_playoff_games g
        LEFT JOIN players p ON p.id = g.best_player_id
        {where_sql}
        ORDER BY g.game_year DESC, g.date_watched DESC
        """,
        params,
    ).fetchall()

    game_ids = [r["id"] for r in rows]
    players_by_game: dict[int, list[dict]] = {}
    if game_ids:
        placeholders = ",".join("?" * len(game_ids))
        link_rows = conn.execute(
            f"""
            SELECT wgp.game_id, p.id AS player_id, p.name
            FROM watched_game_players wgp
            JOIN players p ON p.id = wgp.player_id
            WHERE wgp.game_id IN ({placeholders})
            ORDER BY p.name
            """,
            game_ids,
        ).fetchall()
        for lr in link_rows:
            players_by_game.setdefault(lr["game_id"], []).append(
                {"player_id": lr["player_id"], "name": lr["name"]}
            )

    conn.close()

    result = []
    for r in rows:
        d = _game_row_to_dict(r)
        d["important_players"] = players_by_game.get(d["id"], [])
        result.append(d)

    return jsonify(result)


@app.route("/api/watched_games", methods=["POST"])
def create_watched_game():
    data = request.get_json(force=True)
    required = ("home_team", "away_team", "date_watched", "game_year",
                 "conference", "round", "game_of_round")
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    conn = get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO watched_playoff_games
                (home_team, away_team, winner_team, date_watched, game_year,
                 conference, round, game_of_round, best_player_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["home_team"], data["away_team"],
                data.get("winner_team"), data["date_watched"],
                data["game_year"], data["conference"], data["round"],
                data["game_of_round"], data.get("best_player_id"),
                data.get("notes", ""),
            ),
        )
        game_id = cur.lastrowid

        # Link important players
        for pid in data.get("player_ids", []):
            conn.execute(
                "INSERT OR IGNORE INTO watched_game_players (game_id, player_id) VALUES (?, ?)",
                (game_id, pid),
            )

        conn.commit()
        row = conn.execute(
            "SELECT g.*, p.name AS best_player_name FROM watched_playoff_games g "
            "LEFT JOIN players p ON p.id = g.best_player_id WHERE g.id = ?",
            (game_id,),
        ).fetchone()
    finally:
        conn.close()

    return jsonify(_game_row_to_dict(row)), 201


@app.route("/api/watched_games/<int:game_id>")
def get_watched_game(game_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT g.*, p.name AS best_player_name FROM watched_playoff_games g "
        "LEFT JOIN players p ON p.id = g.best_player_id WHERE g.id = ?",
        (game_id,),
    ).fetchone()
    if not row:
        conn.close()
        abort(404)
    d = _game_row_to_dict(row)
    link_rows = conn.execute(
        "SELECT wgp.player_id, p.name FROM watched_game_players wgp "
        "JOIN players p ON p.id = wgp.player_id WHERE wgp.game_id = ? ORDER BY p.name",
        (game_id,),
    ).fetchall()
    conn.close()
    d["important_players"] = [{"player_id": r["player_id"], "name": r["name"]} for r in link_rows]
    return jsonify(d)


@app.route("/api/watched_games/<int:game_id>", methods=["PUT"])
def update_watched_game(game_id):
    data = request.get_json(force=True)
    allowed = {
        "home_team", "away_team", "winner_team", "date_watched",
        "game_year", "conference", "round", "game_of_round",
        "best_player_id", "notes",
    }
    updates = {k: v for k, v in data.items() if k in allowed}

    conn = get_conn()
    try:
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [game_id]
            conn.execute(
                f"UPDATE watched_playoff_games SET {set_clause} WHERE id = ?", values
            )

        if "player_ids" in data:
            conn.execute("DELETE FROM watched_game_players WHERE game_id = ?", (game_id,))
            for pid in data["player_ids"]:
                conn.execute(
                    "INSERT OR IGNORE INTO watched_game_players (game_id, player_id) VALUES (?, ?)",
                    (game_id, pid),
                )

        conn.commit()
        row = conn.execute(
            "SELECT g.*, p.name AS best_player_name FROM watched_playoff_games g "
            "LEFT JOIN players p ON p.id = g.best_player_id WHERE g.id = ?",
            (game_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        abort(404)
    return jsonify(_game_row_to_dict(row))


@app.route("/api/watched_games/<int:game_id>", methods=["DELETE"])
def delete_watched_game(game_id):
    conn = get_conn()
    try:
        deleted = conn.execute(
            "DELETE FROM watched_playoff_games WHERE id = ?", (game_id,)
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if not deleted:
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/watched_games/best_player_leaderboard")
def best_player_leaderboard():
    conn = get_conn()

    # Fetch per-player, per-year weighted counts
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

    # Fetch per-player, per-round unweighted counts (across all years)
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
    conn.close()

    # Build round lookup: {player_id: {round_label: {"best": n, "games": m}}}
    from collections import defaultdict
    ROUND_KEY = {
        'First Round':       'r1',
        'Second Round':      'r2',
        'Conference Finals': 'r3',
        'NBA Finals':        'r4',
    }
    player_round_data = defaultdict(lambda: {k: {"best": 0, "games": 0} for k in ('r1','r2','r3','r4')})
    for rr in round_rows:
        key = ROUND_KEY.get(rr["round"])
        if key:
            player_round_data[rr["player_id"]][key]["best"]  = rr["best_in_round"]
            player_round_data[rr["player_id"]][key]["games"] = rr["games_in_round"]

    # Build per-year raw scores per player
    # year_data: {year: {player_id: raw_score}}
    from collections import defaultdict
    year_player_raw = defaultdict(dict)   # year -> player_id -> raw
    year_player_info = {}                  # player_id -> name
    player_year_data = defaultdict(lambda: {"total_watched_games": 0, "weighted_best_total": 0.0})

    for r in rows:
        M = r["total_watched"] or 0
        N = r["weighted_best"] or 0.0
        raw = N / M if M > 0 else 0.0
        year_player_raw[r["game_year"]][r["player_id"]] = raw
        year_player_info[r["player_id"]] = r["name"]
        player_year_data[r["player_id"]]["total_watched_games"] += M
        player_year_data[r["player_id"]]["weighted_best_total"] += N

    # Compute per-year max_raw for normalisation
    year_max_raw = {yr: max(raws.values(), default=0.0) for yr, raws in year_player_raw.items()}

    # Sum normalised watch_kyle per player across years
    player_watch_kyle_sum = defaultdict(float)
    player_year_count = defaultdict(int)
    for yr, player_raws in year_player_raw.items():
        max_raw = year_max_raw[yr]
        for pid, raw in player_raws.items():
            M_yr = next(
                (r["total_watched"] for r in rows
                 if r["player_id"] == pid and r["game_year"] == yr), 0
            )
            if M_yr == 0:
                continue
            wk = round((raw / max_raw) * 2 - 1, 3) if max_raw > 0 else 0.0
            player_watch_kyle_sum[pid] += wk
            player_year_count[pid] += 1

    result = []
    for pid, name in year_player_info.items():
        total = player_year_data[pid]["total_watched_games"]
        weighted_best = player_year_data[pid]["weighted_best_total"]
        watch_kyle_sum = player_watch_kyle_sum.get(pid, 0.0)
        rd = player_round_data[pid]

        def round_cell(key):
            b = rd[key]["best"]
            g = rd[key]["games"]
            return f"{b}/{g}" if g > 0 else "—"

        result.append({
            "player_id":           pid,
            "name":                name,
            "total_watched_games": total,
            "best_player_count":   weighted_best,
            "watch_kyle":          round(watch_kyle_sum, 3),
            "best_player_pct":     round(weighted_best / total, 1) if total > 0 else None,
            "r1":                  round_cell("r1"),
            "r2":                  round_cell("r2"),
            "r3":                  round_cell("r3"),
            "r4":                  round_cell("r4"),
        })

    result.sort(key=lambda x: (x["best_player_pct"] or 0), reverse=True)
    return jsonify(result)


@app.route("/api/players_for_year")
def players_for_year():
    """Return all players who have stats in a given season year."""
    year = request.args.get("year", type=int)
    if not year:
        return jsonify({"error": "year required"}), 400
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT DISTINCT p.id, p.name
        FROM players p
        JOIN player_stats ps ON ps.player_id = p.id
        JOIN seasons s ON s.id = ps.season_id
        WHERE s.season_year = ?
        ORDER BY p.name
        """,
        (year,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/player/<int:player_id>/watch_log")
def player_watch_log(player_id):
    conn = get_conn()
    player = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
    if not player:
        conn.close()
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

    conn.close()

    # Gather all years this player appeared in
    all_years = set()
    for g in best_games:
        all_years.add(g["game_year"])
    for g in important_games:
        all_years.add(g["game_year"])

    # Reuse _get_watch_kyle_by_player for correct round-weighted, year-normalised scores
    conn2 = get_conn()
    watch_by_year = {}
    for yr in sorted(all_years):
        yr_map = _get_watch_kyle_by_player(conn2, yr)
        entry = yr_map.get(player_id)
        if entry:
            watch_by_year[yr] = {
                "best_count":    entry["best_count"],
                "total_watched": entry["total_watched"],
                "raw_score":     entry["raw_score"],
                "watch_kyle":    entry["watch_kyle"],
                "best_pct":      round(entry["raw_score"] * 100, 1),
            }
        else:
            # Player appeared in watched games this year but not in the map
            watch_by_year[yr] = {
                "best_count": 0, "total_watched": 0,
                "raw_score": 0.0, "watch_kyle": None, "best_pct": 0.0,
            }
    conn2.close()

    # Overall summary
    total_watched_overall = sum(v["total_watched"] for v in watch_by_year.values())
    total_best_overall    = best_count  # unweighted count from DB
    if total_watched_overall > 0:
        overall_watch_kyle = round(sum(
            v["watch_kyle"] for v in watch_by_year.values() if v["watch_kyle"] is not None
        ), 3)
        overall_best_pct   = round(total_best_overall / total_watched_overall * 100, 1)
    else:
        overall_watch_kyle = 0.0
        overall_best_pct   = None

    return jsonify({
        "best_player_count":      best_count,
        "best_player_games":      [dict(r) for r in best_games],
        "important_player_games": [dict(r) for r in important_games],
        "watch_by_year":          watch_by_year,
        "total_watched":          total_watched_overall,
        "best_player_pct":        overall_best_pct,
        "watch_kyle":             overall_watch_kyle,
    })


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
