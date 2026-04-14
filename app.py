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
            ps.position
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

    player_dicts = [_row_to_dict(r) for r in rows]
    calculated = kyle.calculate(player_dicts)
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
