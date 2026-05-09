import logging

from flask import Flask, jsonify, request, render_template, abort

from db import init_db, db_conn
from scraper import run_scrape
import kyle
import services.kyle_service    as kyle_service
import services.watch_log_service as watch_log_service
import services.player_service  as player_service
import services.suggest_service as suggest_service

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Initialise DB on startup
# ---------------------------------------------------------------------------
with app.app_context():
    init_db()


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    return dict(row)


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/all")
def all_players_page():
    return render_template("all_players.html")


@app.route("/cumulative")
def cumulative_kyle_page():
    return render_template("cumulative_kyle.html")


@app.route("/best3year")
def best3year_page():
    return render_template("best3year_kyle.html")


@app.route("/watch_log")
def watch_log_page():
    return render_template("watch_log.html")


@app.route("/player/<int:player_id>")
def player_page(player_id):
    return render_template("player.html")


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------

@app.route("/api/seasons")
def get_seasons():
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM seasons ORDER BY season_year DESC, season_type"
        ).fetchall()
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/seasons", methods=["POST"])
def create_season():
    """Create a new season row.

    Expects JSON: {"season_year": 2025, "season_type": "regular"|"playoffs"}
    """
    data        = request.get_json(force=True)
    year        = data.get("season_year")
    season_type = data.get("season_type")

    if not year or season_type not in ("regular", "playoffs"):
        return jsonify({"error": "season_year (int) and season_type ('regular' or 'playoffs') are required"}), 400

    type_label = "Regular Season" if season_type == "regular" else "Playoffs"
    label      = f"{year} {type_label}"

    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM seasons WHERE season_year = ? AND season_type = ?",
            (year, season_type),
        ).fetchone()
        if existing:
            return jsonify({"error": f"Season '{label}' already exists", "id": existing["id"]}), 409

        cur    = conn.execute(
            "INSERT INTO seasons (label, season_year, season_type) VALUES (?, ?, ?)",
            (label, year, season_type),
        )
        new_id = cur.lastrowid
        conn.commit()
        row    = conn.execute("SELECT * FROM seasons WHERE id = ?", (new_id,)).fetchone()

    return jsonify(_row_to_dict(row)), 201


@app.route("/api/seasons/<int:season_id>/nearest_selected", methods=["GET"])
def nearest_selected(season_id):
    """Return the selected player IDs from the nearest season."""
    with db_conn() as conn:
        target = conn.execute(
            "SELECT season_year FROM seasons WHERE id = ?", (season_id,)
        ).fetchone()
        if not target:
            return jsonify({"error": "Season not found"}), 404

        target_year = target["season_year"]
        candidates  = conn.execute(
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

        best = min(candidates, key=lambda r: (abs(r["season_year"] - target_year), -r["cnt"]))
        player_ids = [
            r["player_id"]
            for r in conn.execute(
                "SELECT player_id FROM selected_players WHERE season_id = ?",
                (best["id"],),
            ).fetchall()
        ]

    return jsonify({"player_ids": player_ids, "source_season_id": best["id"]})


@app.route("/api/seasons/<int:season_id>", methods=["DELETE"])
def delete_season(season_id):
    """Delete a season and all associated data."""
    with db_conn() as conn:
        conn.execute("DELETE FROM selected_players WHERE season_id = ?", (season_id,))
        conn.execute("DELETE FROM player_stats   WHERE season_id = ?", (season_id,))
        deleted = conn.execute("DELETE FROM seasons WHERE id = ?", (season_id,)).rowcount
        conn.commit()
    if not deleted:
        abort(404)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

@app.route("/api/players")
def get_players():
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400
    with db_conn() as conn:
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
    return jsonify([_row_to_dict(r) for r in rows])


@app.route("/api/players_for_year")
def players_for_year():
    """Return all players who have stats in a given season year."""
    year = request.args.get("year", type=int)
    if not year:
        return jsonify({"error": "year required"}), 400
    with db_conn() as conn:
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
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Selected set
# ---------------------------------------------------------------------------

@app.route("/api/selected")
def get_selected():
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400

    with db_conn() as conn:
        season_row = conn.execute(
            "SELECT season_type, season_year FROM seasons WHERE id = ?", (season_id,)
        ).fetchone()

        season_type  = season_row["season_type"] if season_row else "regular"
        player_dicts = kyle_service.fetch_selected_player_dicts(conn, season_id)

        if season_type == "playoffs" and season_row:
            watch_map = watch_log_service.get_watch_kyle_by_player(conn, season_row["season_year"])
            for d in player_dicts:
                watch_log_service.attach_watch_kyle(d, watch_map.get(d["player_id"]))

    calculated = kyle.calculate(player_dicts, season_type=season_type)
    return jsonify(calculated)


@app.route("/api/selected", methods=["POST"])
def add_selected():
    data      = request.get_json(force=True)
    player_id = data.get("player_id")
    season_id = data.get("season_id")
    if not player_id or not season_id:
        return jsonify({"error": "player_id and season_id required"}), 400

    with db_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO selected_players (player_id, season_id) VALUES (?, ?)",
            (player_id, season_id),
        )
        conn.commit()
    return jsonify({"ok": True}), 201


@app.route("/api/selected/<int:selected_id>", methods=["DELETE"])
def remove_selected(selected_id):
    with db_conn() as conn:
        conn.execute("DELETE FROM selected_players WHERE id = ?", (selected_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/selected", methods=["DELETE"])
def clear_selected():
    """Clear all data for a given season (player_stats + selected_players)."""
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400
    with db_conn() as conn:
        conn.execute("DELETE FROM selected_players WHERE season_id = ?", (season_id,))
        conn.execute("DELETE FROM player_stats   WHERE season_id = ?", (season_id,))
        conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# All-players scored view
# ---------------------------------------------------------------------------

@app.route("/api/all_players")
def get_all_players():
    """Every player with stats for a season, scored using selected-set bounds.

    Normalised values are NOT clamped (uses calculate_all).
    """
    season_id = request.args.get("season_id", type=int)
    if not season_id:
        return jsonify({"error": "season_id required"}), 400

    with db_conn() as conn:
        season_row  = conn.execute(
            "SELECT season_type, season_year FROM seasons WHERE id = ?", (season_id,)
        ).fetchone()
        season_type = season_row["season_type"] if season_row else "regular"
        season_year = season_row["season_year"] if season_row else None

        selected_rows = kyle_service.fetch_selected_player_dicts(conn, season_id)

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

        selected_dicts = selected_rows  # already list[dict] from fetch_selected_player_dicts

        watch_map = {}
        if season_type == "playoffs" and season_year:
            watch_map = watch_log_service.get_watch_kyle_by_player(conn, season_year)
            for d in selected_dicts:
                watch_log_service.attach_watch_kyle(d, watch_map.get(d["player_id"]))

    kyle._add_derived(selected_dicts)
    exclude = {"minutes"} if season_type == "playoffs" else set()
    bounds  = kyle.compute_bounds(selected_dicts, exclude_fields=exclude)

    all_dicts = [_row_to_dict(r) for r in all_rows]
    if season_type == "playoffs" and watch_map:
        for d in all_dicts:
            watch_log_service.attach_watch_kyle(d, watch_map.get(d["player_id"]))

    return jsonify(kyle.calculate_all(all_dicts, bounds, season_type=season_type))


# ---------------------------------------------------------------------------
# Cumulative K.Y.L.E.
# ---------------------------------------------------------------------------

@app.route("/api/cumulative_kyle")
def cumulative_kyle():
    """Per-player totals of K.Y.L.E. across all seasons."""
    with db_conn() as conn:
        result = kyle_service.compute_cumulative(conn)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Best N-Year K.Y.L.E.
# ---------------------------------------------------------------------------

@app.route("/api/best3year")
def best3year():
    """Each player's best consecutive N-year K.Y.L.E. window."""
    window = request.args.get("window", 3, type=int)
    window = max(1, min(window, 20))
    with db_conn() as conn:
        result = kyle_service.compute_best3year(conn, window)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Suggest Game
# ---------------------------------------------------------------------------

@app.route("/api/suggest_game")
def suggest_game():
    """Best unwatched playoff game for two players with overlapping peak windows."""
    window = max(1, min(request.args.get("window", 3, type=int), 20))
    skip   = max(0, request.args.get("skip", 0, type=int))
    try:
        with db_conn() as conn:
            result = suggest_service.get_suggestions(conn, window, skip)
        return jsonify(result)
    except Exception as exc:
        logger.exception("suggest_game error")
        return jsonify({"result": "error", "message": str(exc)}), 500


@app.route("/api/suggest_game_for_player")
def suggest_game_for_player():
    """Best unwatched playoff game for a focal player against their highest-peak opponent."""
    player_id = request.args.get("player_id", type=int)
    if not player_id:
        return jsonify({"result": "error", "message": "player_id is required."}), 400

    window = max(1, min(request.args.get("window", 3, type=int), 20))
    skip   = max(0, request.args.get("skip", 0, type=int))
    try:
        with db_conn() as conn:
            player_row = conn.execute(
                "SELECT id, name, nba_id, bbref_url FROM players WHERE id = ?", (player_id,)
            ).fetchone()
            if not player_row:
                return jsonify({"result": "error", "message": "Player not found."})
            result = suggest_service.get_suggestions_for_player(
                conn, player_id, window, skip, player_row
            )
        return jsonify(result)
    except Exception as exc:
        logger.exception("suggest_game_for_player error")
        return jsonify({"result": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# Stats patch
# ---------------------------------------------------------------------------

@app.route("/api/stats/<int:stats_id>", methods=["PATCH"])
def patch_stats(stats_id):
    data    = request.get_json(force=True)
    allowed = {"defense", "position"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "No patchable fields provided"}), 400

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [stats_id]
    with db_conn() as conn:
        conn.execute(f"UPDATE player_stats SET {set_clause} WHERE id = ?", values)
        conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Update (scrape)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Player history
# ---------------------------------------------------------------------------

@app.route("/api/player/<int:player_id>")
def player_history(player_id):
    """Player info + per-season K.Y.L.E. history."""
    with db_conn() as conn:
        result = player_service.get_player_history(conn, player_id)
    return jsonify(result)


@app.route("/api/player/<int:player_id>/watch_log")
def player_watch_log(player_id):
    """Watch-log data for a single player."""
    with db_conn() as conn:
        result = player_service.get_player_watch_log(conn, player_id)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Watch Log
# ---------------------------------------------------------------------------

@app.route("/api/watched_games")
def list_watched_games():
    """List all watched games with optional filters: year, round, conference."""
    year       = request.args.get("year", type=int)
    round_str  = request.args.get("round")
    conference = request.args.get("conference")

    where_clauses, params = [], []
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

    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT g.*, p.name AS best_player_name
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
            ph = ",".join("?" * len(game_ids))
            link_rows = conn.execute(
                f"""
                SELECT wgp.game_id, p.id AS player_id, p.name
                FROM watched_game_players wgp
                JOIN players p ON p.id = wgp.player_id
                WHERE wgp.game_id IN ({ph})
                ORDER BY p.name
                """,
                game_ids,
            ).fetchall()
            for lr in link_rows:
                players_by_game.setdefault(lr["game_id"], []).append(
                    {"player_id": lr["player_id"], "name": lr["name"]}
                )

    result = []
    for r in rows:
        d = dict(r)
        d["important_players"] = players_by_game.get(d["id"], [])
        result.append(d)
    return jsonify(result)


@app.route("/api/watched_games", methods=["POST"])
def create_watched_game():
    data     = request.get_json(force=True)
    required = ("home_team", "away_team", "date_watched", "game_year",
                 "conference", "round", "game_of_round")
    missing  = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO watched_playoff_games
                (home_team, away_team, winner_team, date_watched, game_year,
                 conference, round, game_of_round, best_player_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (data["home_team"], data["away_team"], data.get("winner_team"),
             data["date_watched"], data["game_year"], data["conference"],
             data["round"], data["game_of_round"],
             data.get("best_player_id"), data.get("notes", "")),
        )
        game_id = cur.lastrowid
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

    return jsonify(dict(row)), 201


@app.route("/api/watched_games/<int:game_id>")
def get_watched_game(game_id):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT g.*, p.name AS best_player_name FROM watched_playoff_games g "
            "LEFT JOIN players p ON p.id = g.best_player_id WHERE g.id = ?",
            (game_id,),
        ).fetchone()
        if not row:
            abort(404)
        d = dict(row)
        link_rows = conn.execute(
            "SELECT wgp.player_id, p.name FROM watched_game_players wgp "
            "JOIN players p ON p.id = wgp.player_id WHERE wgp.game_id = ? ORDER BY p.name",
            (game_id,),
        ).fetchall()
    d["important_players"] = [{"player_id": r["player_id"], "name": r["name"]} for r in link_rows]
    return jsonify(d)


@app.route("/api/watched_games/<int:game_id>", methods=["PUT"])
def update_watched_game(game_id):
    data    = request.get_json(force=True)
    allowed = {
        "home_team", "away_team", "winner_team", "date_watched",
        "game_year", "conference", "round", "game_of_round",
        "best_player_id", "notes",
    }
    updates = {k: v for k, v in data.items() if k in allowed}

    with db_conn() as conn:
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE watched_playoff_games SET {set_clause} WHERE id = ?",
                list(updates.values()) + [game_id],
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

    if not row:
        abort(404)
    return jsonify(dict(row))


@app.route("/api/watched_games/<int:game_id>", methods=["DELETE"])
def delete_watched_game(game_id):
    with db_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM watched_playoff_games WHERE id = ?", (game_id,)
        ).rowcount
        conn.commit()
    if not deleted:
        abort(404)
    return jsonify({"ok": True})


@app.route("/api/watched_games/best_player_leaderboard")
def best_player_leaderboard():
    """Best-player leaderboard across all watched playoff games."""
    with db_conn() as conn:
        result = watch_log_service.compute_leaderboard(conn)
    return jsonify(result)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
