import logging
from datetime import date as _date
from flask import Flask, jsonify, request, render_template, abort

from db import init_db, get_conn
from scraper import run_scrape
import kyle

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        "SELECT season_type FROM seasons WHERE id = ?", (season_id,)
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
    conn.close()

    season_type = season_row["season_type"] if season_row else "regular"
    player_dicts = [_row_to_dict(r) for r in rows]
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
        "SELECT season_type FROM seasons WHERE id = ?", (season_id,)
    ).fetchone()
    season_type = season_row["season_type"] if season_row else "regular"

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

    conn.close()

    # Build bounds from selected set
    selected_dicts = [_row_to_dict(r) for r in selected_rows]
    kyle._add_derived(selected_dicts)
    exclude = {"minutes"} if season_type == "playoffs" else set()
    bounds = kyle.compute_bounds(selected_dicts, exclude_fields=exclude)

    # Score all players with those bounds (no clamping)
    all_dicts = [_row_to_dict(r) for r in all_rows]
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
        SELECT DISTINCT s.id, s.season_type
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
        calculated   = kyle.calculate(player_dicts, season_type=season_type)

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

    conn.close()

    result = []
    for pid, pdata in player_years.items():
        name  = pdata["name"]
        years = pdata["years"]  # {year: {regular, playoffs}}
        sorted_years = sorted(years.keys())

        if len(sorted_years) < window:
            continue  # not enough distinct years

        best_window_entry = None

        for i in range(len(sorted_years) - window + 1):
            window_years = sorted_years[i : i + window]
            # Only consider truly consecutive years
            if window_years[-1] - window_years[0] != window - 1:
                continue

            reg   = sum(years[y]["regular"]  for y in window_years)
            play  = sum(years[y]["playoffs"] for y in window_years)
            total = reg + play

            if best_window_entry is None or total > best_window_entry["best_window_total"]:
                best_window_entry = {
                    "player_id":        pid,
                    "name":             name,
                    "best_start_year":  window_years[0],
                    "best_end_year":    window_years[-1],
                    "regular_total":    round(reg,   4),
                    "playoffs_total":   round(play,  4),
                    "best_window_total": round(total, 4),
                    "window":           window,
                }

        if best_window_entry:
            result.append(best_window_entry)

    result.sort(key=lambda x: x["best_window_total"], reverse=True)
    return jsonify(result)


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
            kyle._add_derived(sel_dicts)
            bounds     = kyle.compute_bounds(sel_dicts, exclude_fields=exclude)
            target     = dict(row_dict)
            target["player_id"] = player_id
            target["name"]      = player_dict["name"]
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
    rows = conn.execute(
        """
        SELECT p.id AS player_id, p.name,
               COUNT(*) AS best_player_count
        FROM watched_playoff_games g
        JOIN players p ON p.id = g.best_player_id
        GROUP BY g.best_player_id
        ORDER BY best_player_count DESC, p.name
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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
    return jsonify({
        "best_player_count": best_count,
        "best_player_games": [dict(r) for r in best_games],
        "important_player_games": [dict(r) for r in important_games],
    })


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
