import logging
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
            ps.playoff_games
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
            ps.on_off_diff, ps.bpm, ps.defense, ps.position, ps.playoff_games
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
            ps.on_off_diff, ps.bpm, ps.defense, ps.position, ps.playoff_games
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
                ps.on_off_diff, ps.bpm, ps.defense
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


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
