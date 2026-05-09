"""
Flask API tests — tests/test_app.py
Run with: pytest tests/test_app.py

Uses a temp-file SQLite database so that db_conn() and init_db() work as-is
without modifying any application code.
"""

import json
import os
import tempfile

import pytest

import db as db_module
from db import init_db
import app as app_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def client(tmp_path):
    """
    Create a fresh temp SQLite DB for each test, redirect db.DB_PATH to it,
    run init_db(), and return a Flask test client.
    """
    db_file = str(tmp_path / "test_nba.db")

    # Override the module-level DB_PATH so every get_conn() / db_conn() call
    # picks up the temp file instead of nba.db.
    original_path = db_module.DB_PATH
    db_module.DB_PATH = db_file

    init_db()

    with app_module.app.test_client() as c:
        yield c

    # Restore so other test sessions are unaffected.
    db_module.DB_PATH = original_path


def _json(response):
    """Decode response data as JSON."""
    return json.loads(response.data)


# ---------------------------------------------------------------------------
# Helper: seed common data
# ---------------------------------------------------------------------------

def _create_season(client, year=2020, season_type="playoffs"):
    return client.post(
        "/api/seasons",
        data=json.dumps({"season_year": year, "season_type": season_type}),
        content_type="application/json",
    )


def _seed_player_and_stats(client, season_id, player_name="LeBron James"):
    """
    Insert a player + player_stats row directly via the DB so we have
    something to select without needing the scraper.
    """
    import sqlite3

    conn = sqlite3.connect(db_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    cur = conn.execute(
        "INSERT OR IGNORE INTO players (name) VALUES (?)", (player_name,)
    )
    player_id = cur.lastrowid or conn.execute(
        "SELECT id FROM players WHERE name = ?", (player_name,)
    ).fetchone()["id"]

    conn.execute(
        """
        INSERT OR IGNORE INTO player_stats
            (player_id, season_id, minutes, usage_rate, true_shooting_pct,
             assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm, defense)
        VALUES (?, ?, 32.0, 25.0, 0.60, 20.0, 12.0, 3.0, 4.0, 2.0, 1.5)
        """,
        (player_id, season_id),
    )
    stats_id = conn.execute(
        "SELECT id FROM player_stats WHERE player_id = ? AND season_id = ?",
        (player_id, season_id),
    ).fetchone()["id"]

    conn.commit()
    conn.close()
    return player_id, stats_id


# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------

def test_get_seasons_empty(client):
    """GET /api/seasons with a fresh DB returns an empty list (no seed rows)."""
    resp = client.get("/api/seasons")
    assert resp.status_code == 200
    data = _json(resp)
    assert isinstance(data, list)
    assert data == []


def test_create_season_success(client):
    resp = _create_season(client, year=2019, season_type="playoffs")
    assert resp.status_code == 201
    data = _json(resp)
    assert data["season_year"] == 2019
    assert data["season_type"] == "playoffs"
    assert "id" in data


def test_create_season_duplicate(client):
    _create_season(client, year=2019, season_type="playoffs")
    resp = _create_season(client, year=2019, season_type="playoffs")
    assert resp.status_code == 409


def test_create_season_bad_type(client):
    resp = client.post(
        "/api/seasons",
        data=json.dumps({"season_year": 2019, "season_type": "invalid"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_delete_season(client):
    create_resp = _create_season(client, year=2018, season_type="regular")
    season_id = _json(create_resp)["id"]

    del_resp = client.delete(f"/api/seasons/{season_id}")
    assert del_resp.status_code == 200
    assert _json(del_resp)["ok"] is True

    # Confirm it's gone
    seasons = _json(client.get("/api/seasons"))
    ids = [s["id"] for s in seasons]
    assert season_id not in ids


def test_delete_season_not_found(client):
    resp = client.delete("/api/seasons/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Selected players
# ---------------------------------------------------------------------------

def test_get_selected_empty(client):
    season_resp = _create_season(client, year=2021, season_type="regular")
    season_id = _json(season_resp)["id"]

    resp = client.get(f"/api/selected?season_id={season_id}")
    assert resp.status_code == 200
    assert _json(resp) == []


def test_get_selected_missing_param(client):
    resp = client.get("/api/selected")
    assert resp.status_code == 400


def test_add_and_remove_selected(client):
    # Create a season and a player with stats
    season_resp = _create_season(client, year=2022, season_type="regular")
    season_id = _json(season_resp)["id"]
    player_id, _ = _seed_player_and_stats(client, season_id)

    # Add to selected
    add_resp = client.post(
        "/api/selected",
        data=json.dumps({"player_id": player_id, "season_id": season_id}),
        content_type="application/json",
    )
    assert add_resp.status_code == 201

    # Confirm appears in selected list
    selected = _json(client.get(f"/api/selected?season_id={season_id}"))
    player_ids = [p["player_id"] for p in selected]
    assert player_id in player_ids

    # Grab the selected row id
    selected_id = next(p["selected_id"] for p in selected if p["player_id"] == player_id)

    # Remove
    del_resp = client.delete(f"/api/selected/{selected_id}")
    assert del_resp.status_code == 200
    assert _json(del_resp)["ok"] is True

    # Confirm gone
    selected_after = _json(client.get(f"/api/selected?season_id={season_id}"))
    assert all(p["player_id"] != player_id for p in selected_after)


def test_remove_selected_not_found(client):
    # Deleting a non-existent selected row should return 404
    resp = client.delete("/api/selected/999999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# All players
# ---------------------------------------------------------------------------

def test_get_all_players_no_season(client):
    resp = client.get("/api/all_players")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Stats patch
# ---------------------------------------------------------------------------

def test_patch_stats_defense(client):
    season_resp = _create_season(client, year=2023, season_type="regular")
    season_id = _json(season_resp)["id"]
    _, stats_id = _seed_player_and_stats(client, season_id)

    resp = client.patch(
        f"/api/stats/{stats_id}",
        data=json.dumps({"defense": 3.5}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert _json(resp)["ok"] is True


def test_patch_stats_disallowed_field(client):
    season_resp = _create_season(client, year=2023, season_type="regular")
    season_id = _json(season_resp)["id"]
    _, stats_id = _seed_player_and_stats(client, season_id)

    resp = client.patch(
        f"/api/stats/{stats_id}",
        data=json.dumps({"minutes": 40.0}),  # 'minutes' is not in allowed set
        content_type="application/json",
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Watched games
# ---------------------------------------------------------------------------

def _watched_game_payload(**overrides):
    base = {
        "home_team": "Lakers",
        "away_team": "Celtics",
        "date_watched": "2024-06-01",
        "game_year": 2024,
        "conference": "Finals",
        "round": "Finals",
        "game_of_round": 1,
    }
    base.update(overrides)
    return base


def test_create_watched_game(client):
    resp = client.post(
        "/api/watched_games",
        data=json.dumps(_watched_game_payload()),
        content_type="application/json",
    )
    assert resp.status_code == 201
    data = _json(resp)
    assert data["home_team"] == "Lakers"
    assert data["away_team"] == "Celtics"
    assert "id" in data


def test_create_watched_game_with_players(client):
    """POST /api/watched_games with player_ids links them correctly."""
    # Seed a player (no stats needed for watch log linking)
    import sqlite3
    conn = sqlite3.connect(db_module.DB_PATH)
    cur = conn.execute("INSERT INTO players (name) VALUES (?)", ("Kobe Bryant",))
    pid = cur.lastrowid
    conn.commit()
    conn.close()

    resp = client.post(
        "/api/watched_games",
        data=json.dumps(_watched_game_payload(player_ids=[pid])),
        content_type="application/json",
    )
    assert resp.status_code == 201
    game_id = _json(resp)["id"]

    # Fetch the game and confirm player link
    get_resp = client.get(f"/api/watched_games/{game_id}")
    assert get_resp.status_code == 200
    game_data = _json(get_resp)
    linked_ids = [p["player_id"] for p in game_data["important_players"]]
    assert pid in linked_ids


def test_delete_watched_game_not_found(client):
    resp = client.delete("/api/watched_games/99999")
    assert resp.status_code == 404


def test_delete_watched_game_success(client):
    create_resp = client.post(
        "/api/watched_games",
        data=json.dumps(_watched_game_payload()),
        content_type="application/json",
    )
    game_id = _json(create_resp)["id"]

    del_resp = client.delete(f"/api/watched_games/{game_id}")
    assert del_resp.status_code == 200
    assert _json(del_resp)["ok"] is True

    # Confirm 404 on subsequent fetch
    get_resp = client.get(f"/api/watched_games/{game_id}")
    assert get_resp.status_code == 404
