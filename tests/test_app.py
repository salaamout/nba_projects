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


# ---------------------------------------------------------------------------
# suggest_game_for_player
# ---------------------------------------------------------------------------

def _seed_suggest_fixture(db_path: str) -> tuple[int, int]:
    """Seed minimal data for suggest_game_for_player tests.

    Seeds two players (focal + opponent) with playoff seasons and
    player_game_appearances rows so the suggest service can find a candidate
    game.  The focal player's appearance has round='First Round' so that
    round_known is True in the response.

    Returns (focal_player_id, opp_player_id).
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Seasons
    cur = conn.execute(
        "INSERT INTO seasons (label, season_year, season_type) VALUES ('2010 Playoffs', 2010, 'playoffs')"
    )
    season_id = cur.lastrowid

    # Players
    focal_id = conn.execute(
        "INSERT INTO players (name) VALUES ('Focal Player')"
    ).lastrowid
    opp_id = conn.execute(
        "INSERT INTO players (name) VALUES ('Opp Player')"
    ).lastrowid

    # player_stats (needed for compute_peak_windows)
    for pid in (focal_id, opp_id):
        conn.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm, defense)
            VALUES (?, ?, 32.0, 25.0, 0.60, 20.0, 12.0, 5.0, 4.0, 3.0, 1.5)
            """,
            (pid, season_id),
        )
        conn.execute("INSERT INTO selected_players (player_id, season_id) VALUES (?, ?)", (pid, season_id))

    # player_game_appearances — one shared game, focal on LAL vs BOS
    for pid, team, opp_team in [
        (focal_id, "LAL", "BOS"),
        (opp_id,   "BOS", "LAL"),
    ]:
        conn.execute(
            """
            INSERT INTO player_game_appearances
                (player_id, season_year, season_type, game_date, team_abbr, opp_abbr, round)
            VALUES (?, 2010, 'playoffs', '2010-04-20', ?, ?, 'First Round')
            """,
            (pid, team, opp_team),
        )

    conn.commit()
    conn.close()
    return focal_id, opp_id


def test_suggest_game_for_player_missing_param(client):
    resp = client.get("/api/suggest_game_for_player")
    assert resp.status_code == 400


def test_suggest_game_for_player_not_found(client):
    resp = client.get("/api/suggest_game_for_player?player_id=999999")
    data = _json(resp)
    assert data["result"] == "error"


def test_suggest_game_for_player_round_known(client):
    """When appearance data is pre-seeded with a known round, round_known should be True."""
    from unittest.mock import patch as mock_patch

    focal_id, _ = _seed_suggest_fixture(db_module.DB_PATH)

    # Patch _ensure_appearances to be a no-op so the test doesn't hit the network
    with mock_patch(
        "services.suggest_service._ensure_appearances", return_value=False
    ):
        resp = client.get(
            f"/api/suggest_game_for_player?player_id={focal_id}&window=1"
        )

    assert resp.status_code == 200
    data = _json(resp)
    assert data["result"] == "found"
    assert data["game"]["round"] == "First Round"
    assert data["game"]["round_known"] is True


# ---------------------------------------------------------------------------
# peak-games endpoint
# ---------------------------------------------------------------------------

def _seed_peak_games_fixture(db_path: str):
    """Seed two players, one playoff season, and game appearances for peak-games tests.

    Returns (focal_id, opp_id).
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    season_id = conn.execute(
        "INSERT INTO seasons (label, season_year, season_type) VALUES ('1992 Playoffs', 1992, 'playoffs')"
    ).lastrowid

    focal_id = conn.execute("INSERT INTO players (name) VALUES ('Focal Player')").lastrowid
    opp_id   = conn.execute("INSERT INTO players (name) VALUES ('Peak Opp')").lastrowid

    for pid in (focal_id, opp_id):
        conn.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm, defense)
            VALUES (?, ?, 32.0, 25.0, 0.60, 20.0, 12.0, 5.0, 4.0, 3.0, 1.5)
            """,
            (pid, season_id),
        )
        conn.execute(
            "INSERT INTO selected_players (player_id, season_id) VALUES (?, ?)",
            (pid, season_id),
        )

    # Shared game on 1992-06-03
    for pid, team, opp_team in [(focal_id, "CHI", "POR"), (opp_id, "POR", "CHI")]:
        conn.execute(
            """
            INSERT INTO player_game_appearances
                (player_id, season_year, season_type, game_date, team_abbr, opp_abbr, round)
            VALUES (?, 1992, 'playoffs', '1992-06-03', ?, ?, 'NBA Finals')
            """,
            (pid, team, opp_team),
        )

    # Game that only the focal player appears in (no peak opponent)
    conn.execute(
        """
        INSERT INTO player_game_appearances
            (player_id, season_year, season_type, game_date, team_abbr, opp_abbr, round)
        VALUES (?, 1992, 'playoffs', '1992-05-10', 'CHI', 'MIL', 'Second Round')
        """,
        (focal_id,),
    )

    conn.commit()
    conn.close()
    return focal_id, opp_id


def test_peak_games_no_player(client):
    resp = client.get("/api/player/999999/peak-games")
    assert resp.status_code == 404


def test_peak_games_no_data(client):
    """Player with no appearances returns empty games list."""
    import sqlite3
    conn = sqlite3.connect(db_module.DB_PATH)
    pid = conn.execute("INSERT INTO players (name) VALUES ('Empty')", ).lastrowid
    conn.commit()
    conn.close()

    resp = client.get(f"/api/player/{pid}/peak-games?window=3")
    assert resp.status_code == 200
    data = _json(resp)
    assert data["games"] == []
    assert data["all_peak_opponents"] == []


def test_peak_games_basic(client):
    """Games with a peak opponent appear; games without do not."""
    focal_id, opp_id = _seed_peak_games_fixture(db_module.DB_PATH)

    resp = client.get(f"/api/player/{focal_id}/peak-games?window=1")
    assert resp.status_code == 200
    data = _json(resp)

    assert data["player_id"] == focal_id
    assert len(data["games"]) == 1
    game = data["games"][0]
    assert game["game_date"] == "1992-06-03"
    assert any(o["player_id"] == opp_id for o in game["peak_opponents"])

    # all_peak_opponents should contain opp
    opp_ids = [o["player_id"] for o in data["all_peak_opponents"]]
    assert opp_id in opp_ids


def test_peak_games_watched(client):
    """watched=True when a matching watched_playoff_games row exists."""
    import sqlite3
    focal_id, _ = _seed_peak_games_fixture(db_module.DB_PATH)

    conn = sqlite3.connect(db_module.DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        INSERT INTO watched_playoff_games
            (home_team, away_team, game_year, round, game_of_round, conference, date_watched)
        VALUES ('CHI', 'POR', 1992, 'NBA Finals', 1, 'NBA Finals', '2024-01-01')
        """
    )
    conn.commit()
    conn.close()

    resp = client.get(f"/api/player/{focal_id}/peak-games?window=1")
    data = _json(resp)
    assert len(data["games"]) == 1
    assert data["games"][0]["watched"] is True
    assert data["games"][0]["round"] == "NBA Finals"


def test_peak_games_window(client):
    """Changing window to 17 may yield different (or no) qualifying opponents."""
    focal_id, _ = _seed_peak_games_fixture(db_module.DB_PATH)

    resp = client.get(f"/api/player/{focal_id}/peak-games?window=17")
    assert resp.status_code == 200
    data = _json(resp)
    # With window=17 the single season (1992) cannot form a 17-year window, so
    # compute_peak_windows returns no peaks → no qualifying games.
    assert data["games"] == []


# ---------------------------------------------------------------------------
# Regression: player history playoff K.Y.L.E. must match compute_peak_windows
# ---------------------------------------------------------------------------

def _seed_playoff_kyle_fixture(db_path):
    """
    Seed two players + a playoff season with watched games so that
    watch_kyle is year-normalised (relative to the other selected player).
    Returns (focal_player_id, other_player_id, season_year).
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Players
    focal_id = conn.execute("INSERT INTO players (name) VALUES ('Focal Player')").lastrowid
    other_id = conn.execute("INSERT INTO players (name) VALUES ('Other Player')").lastrowid

    # Playoff season
    season_year = 2023
    season_id = conn.execute(
        "INSERT INTO seasons (label, season_year, season_type) VALUES (?, ?, 'playoffs')",
        (f"{season_year} Playoffs", season_year),
    ).lastrowid

    # Stats — both players need full stats so kyle_rating can be computed
    for pid in (focal_id, other_id):
        conn.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm, defense,
                 playoff_games)
            VALUES (?, ?, 32.0, 25.0, 0.60, 20.0, 12.0, 3.0, 4.0, 2.0, 1.5, 10)
            """,
            (pid, season_id),
        )

    # Select both players for this season
    for pid in (focal_id, other_id):
        conn.execute(
            "INSERT INTO selected_players (player_id, season_id) VALUES (?, ?)",
            (pid, season_id),
        )

    # Watched games: focal player is best in 3 of 4 games watched;
    # other player is best in the remaining 1.  This creates an asymmetric
    # raw score so year-normalised watch_kyle ≠ simple best/total.
    game_ids = []
    for i in range(4):
        best = focal_id if i < 3 else other_id
        gid = conn.execute(
            """
            INSERT INTO watched_playoff_games
                (home_team, away_team, game_year, round, game_of_round,
                 conference, date_watched, best_player_id)
            VALUES ('AAA', 'BBB', ?, 'First Round', ?, 'East', '2023-05-01', ?)
            """,
            (season_year, i + 1, best),
        ).lastrowid
        game_ids.append(gid)

    for gid in game_ids:
        for pid in (focal_id, other_id):
            conn.execute(
                "INSERT INTO watched_game_players (game_id, player_id) VALUES (?, ?)",
                (gid, pid),
            )

    conn.commit()
    conn.close()
    return focal_id, other_id, season_year


def test_player_history_playoff_kyle_matches_peak_windows(client):
    """
    Regression / wire-up test: get_player_history and compute_peak_windows must
    produce identical playoff K.Y.L.E. for a given player/season.

    After the refactor both code paths delegate to _compute_season_kyle in
    kyle_service, so this test verifies that the shared helper is correctly
    wired into both callers (i.e. neither caller has fallen back to its own
    inline implementation).
    """
    import sqlite3
    import services.kyle_service as kyle_service

    focal_id, _other_id, season_year = _seed_playoff_kyle_fixture(db_module.DB_PATH)

    # -- player history path --------------------------------------------------
    resp = client.get(f"/api/player/{focal_id}")
    assert resp.status_code == 200
    history = _json(resp)
    season_row = next(
        (s for s in history["seasons"] if s["season_year"] == season_year),
        None,
    )
    assert season_row is not None, "Expected a season row for the seeded year"
    history_playoff_kyle = season_row["kyle_rating"]
    assert history_playoff_kyle is not None, "Expected a non-None playoff K.Y.L.E."

    # -- peaks path -----------------------------------------------------------
    conn = sqlite3.connect(db_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    _peaks, player_years = kyle_service.compute_peak_windows(conn, window=1)
    conn.close()

    peaks_playoff_kyle = player_years[focal_id]["years"][season_year]["playoffs"]

    assert history_playoff_kyle == pytest.approx(peaks_playoff_kyle, abs=1e-4), (
        f"Player history playoff K.Y.L.E. ({history_playoff_kyle}) does not match "
        f"compute_peak_windows ({peaks_playoff_kyle}) for year {season_year}. "
        "This likely means watch_kyle is being computed by two different formulas."
    )


# ---------------------------------------------------------------------------
# Unit tests for _compute_season_kyle / _compute_season_kyle_for_player
# ---------------------------------------------------------------------------

def _seed_two_player_regular_season(db_path):
    """Seed two selected players in a regular season; return (conn, player_ids, season_id, year)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    year = 2010
    season_id = conn.execute(
        "INSERT INTO seasons (label, season_year, season_type) VALUES (?, ?, 'regular')",
        (f"{year} Regular", year),
    ).lastrowid

    pids = []
    for name in ("Alpha", "Beta"):
        pid = conn.execute("INSERT INTO players (name) VALUES (?)", (name,)).lastrowid
        conn.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm, defense)
            VALUES (?, ?, 32.0, 25.0, 0.58, 18.0, 11.0, 2.5, 3.5, 1.8, 1.2)
            """,
            (pid, season_id),
        )
        conn.execute(
            "INSERT INTO selected_players (player_id, season_id) VALUES (?, ?)",
            (pid, season_id),
        )
        pids.append(pid)

    conn.commit()
    return conn, pids, season_id, year


def test_compute_season_kyle_regular_returns_ratings(client):
    """_compute_season_kyle returns a list with kyle_rating for each selected player."""
    import sqlite3
    from services.kyle_service import _compute_season_kyle, fetch_selected_player_dicts

    conn, pids, season_id, year = _seed_two_player_regular_season(db_module.DB_PATH)
    selected = fetch_selected_player_dicts(conn, season_id)
    results = _compute_season_kyle(conn, selected, "regular", year)
    conn.close()

    assert len(results) == 2
    result_pids = {r["player_id"] for r in results}
    assert result_pids == set(pids)
    for r in results:
        assert r["kyle_rating"] is not None
        assert isinstance(r["kyle_rating"], float)


def test_compute_season_kyle_empty_returns_empty(client):
    """_compute_season_kyle with an empty selected list returns []."""
    from services.kyle_service import _compute_season_kyle

    # conn is not accessed when selected_dicts is empty; pass None
    result = _compute_season_kyle(None, [], "regular", 2020)
    assert result == []


def test_compute_season_kyle_does_not_mutate_input(client):
    """_compute_season_kyle must not mutate the caller's selected_dicts list."""
    import sqlite3
    from services.kyle_service import _compute_season_kyle, fetch_selected_player_dicts

    conn, _pids, season_id, year = _seed_two_player_regular_season(db_module.DB_PATH)
    selected = fetch_selected_player_dicts(conn, season_id)

    # Snapshot values before the call
    before = [dict(d) for d in selected]
    _compute_season_kyle(conn, selected, "regular", year)
    conn.close()

    # The original dicts must be unchanged
    for orig, after in zip(before, selected):
        assert orig == after, "Input dict was mutated by _compute_season_kyle"


def test_compute_season_kyle_for_player_returns_rating(client):
    """_compute_season_kyle_for_player returns the correct float for a present player."""
    import sqlite3
    from services.kyle_service import (
        _compute_season_kyle,
        _compute_season_kyle_for_player,
        fetch_selected_player_dicts,
    )

    conn, pids, season_id, year = _seed_two_player_regular_season(db_module.DB_PATH)
    selected = fetch_selected_player_dicts(conn, season_id)

    for pid in pids:
        rating = _compute_season_kyle_for_player(conn, pid, selected, "regular", year)
        assert rating is not None
        assert isinstance(rating, float)

    conn.close()


def test_compute_season_kyle_for_player_absent_returns_none(client):
    """_compute_season_kyle_for_player returns None when the player is not in the selected set."""
    import sqlite3
    from services.kyle_service import _compute_season_kyle_for_player, fetch_selected_player_dicts

    conn, _pids, season_id, year = _seed_two_player_regular_season(db_module.DB_PATH)
    selected = fetch_selected_player_dicts(conn, season_id)
    conn.close()

    # player_id=99999 is not in the selected set
    rating = _compute_season_kyle_for_player(None, 99999, selected, "regular", year)
    assert rating is None


def test_compute_season_kyle_for_player_matches_compute_season_kyle(client):
    """_compute_season_kyle_for_player(pid) == the same player's rating from _compute_season_kyle."""
    import sqlite3
    from services.kyle_service import (
        _compute_season_kyle,
        _compute_season_kyle_for_player,
        fetch_selected_player_dicts,
    )

    conn, pids, season_id, year = _seed_two_player_regular_season(db_module.DB_PATH)
    selected = fetch_selected_player_dicts(conn, season_id)

    batch = {r["player_id"]: r["kyle_rating"] for r in _compute_season_kyle(conn, selected, "regular", year)}
    for pid in pids:
        single = _compute_season_kyle_for_player(conn, pid, selected, "regular", year)
        assert single == pytest.approx(batch[pid], abs=1e-6), (
            f"_compute_season_kyle_for_player ({single}) != _compute_season_kyle ({batch[pid]}) "
            f"for player {pid}"
        )

    conn.close()
