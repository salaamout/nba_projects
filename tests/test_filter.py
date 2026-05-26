"""
Tests for the player filter feature.

Covers:
  - services/filter_service.py  (validate_filters, filter_players, _matches)
  - GET  /filter            (page route)
  - POST /api/filter_players (API endpoint)
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import db as db_module
from db import init_db
import app as app_module
import services.filter_service as filter_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def client(tmp_path):
    db_file = str(tmp_path / "test_filter.db")
    original_path = db_module.DB_PATH
    db_module.DB_PATH = db_file

    init_db()

    with app_module.app.test_client() as c:
        yield c

    db_module.DB_PATH = original_path


def _json(response):
    return json.loads(response.data)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_two_seasons(db_path: str):
    """Seed two players in both a regular and a playoffs season.

    Regular-season stats:  player A bpm=7.0, player B bpm=1.0
    Playoff stats:         player A bpm=9.0, player B bpm=0.5

    Returns (conn, player_a_id, player_b_id, regular_season_id, playoff_season_id).
    The conn is left open so the caller can do further queries; caller must close it.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    pid_a = conn.execute("INSERT INTO players (name) VALUES ('Alpha')", ).lastrowid
    pid_b = conn.execute("INSERT INTO players (name) VALUES ('Beta')",  ).lastrowid

    reg_id = conn.execute(
        "INSERT INTO seasons (label, season_year, season_type) VALUES ('2020 Regular', 2020, 'regular')"
    ).lastrowid

    poff_id = conn.execute(
        "INSERT INTO seasons (label, season_year, season_type) VALUES ('2020 Playoffs', 2020, 'playoffs')"
    ).lastrowid

    # Regular-season stats
    reg_stats = [
        (pid_a, reg_id,  3000, 30.0, 0.60, 28.0, 10.0, 8.0, 6.0, 7.0, 2.0),
        (pid_b, reg_id,  2000, 20.0, 0.52, 15.0, 14.0, 2.0, 1.0, 1.0, 1.0),
    ]
    # Playoff stats
    poff_stats = [
        (pid_a, poff_id, 2500, 32.0, 0.62, 30.0,  9.0, 9.0, 7.0, 9.0, 3.0),
        (pid_b, poff_id, 1800, 18.0, 0.50, 12.0, 15.0, 1.5, 0.5, 0.5, 0.5),
    ]

    for row in reg_stats + poff_stats:
        conn.execute(
            """
            INSERT INTO player_stats
                (player_id, season_id, minutes, usage_rate, true_shooting_pct,
                 assist_rate, turnover_pct, on_court_rating, on_off_diff, bpm, defense)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    for pid in (pid_a, pid_b):
        for sid in (reg_id, poff_id):
            conn.execute(
                "INSERT INTO selected_players (player_id, season_id) VALUES (?, ?)",
                (pid, sid),
            )

    conn.commit()
    return conn, pid_a, pid_b, reg_id, poff_id


# ---------------------------------------------------------------------------
# Unit tests: validate_filters
# ---------------------------------------------------------------------------

class TestValidateFilters:
    def test_empty_filters_is_valid(self):
        assert filter_service.validate_filters([]) == []

    def test_valid_filter_passes(self):
        errors = filter_service.validate_filters([
            {"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "regular"},
        ])
        assert errors == []

    def test_valid_norm_field_passes(self):
        errors = filter_service.validate_filters([
            {"field": "bpm_norm", "operator": "<=", "value": 0.5, "season_type": "either"},
        ])
        assert errors == []

    def test_unknown_field_returns_error(self):
        errors = filter_service.validate_filters([
            {"field": "magic_stat", "operator": ">=", "value": 1.0, "season_type": "either"},
        ])
        assert len(errors) == 1
        assert "unknown field" in errors[0]

    def test_unknown_operator_returns_error(self):
        errors = filter_service.validate_filters([
            {"field": "bpm", "operator": "!=", "value": 0.0, "season_type": "either"},
        ])
        assert len(errors) == 1
        assert "unknown operator" in errors[0]

    def test_non_numeric_value_returns_error(self):
        errors = filter_service.validate_filters([
            {"field": "bpm", "operator": ">=", "value": "high", "season_type": "either"},
        ])
        assert len(errors) == 1
        assert "value must be a number" in errors[0]

    def test_invalid_season_type_returns_error(self):
        errors = filter_service.validate_filters([
            {"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "postseason"},
        ])
        assert len(errors) == 1
        assert "season_type" in errors[0]

    def test_multiple_errors_all_reported(self):
        errors = filter_service.validate_filters([
            {"field": "bad_field", "operator": "??", "value": "nope", "season_type": "nope"},
        ])
        # Should have errors for field, operator, value, AND season_type
        assert len(errors) >= 3

    def test_multiple_filters_each_labeled(self):
        errors = filter_service.validate_filters([
            {"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"},
            {"field": "INVALID", "operator": ">=", "value": 5.0, "season_type": "either"},
        ])
        assert len(errors) == 1
        assert "Filter 2" in errors[0]


# ---------------------------------------------------------------------------
# Unit tests: _matches helper
# ---------------------------------------------------------------------------

class TestMatches:
    def _row(self, **kwargs):
        base = {
            "season_type": "regular",
            "bpm": 5.0,
            "kyle_rating": 3.0,
            "minutes": 2500,
        }
        base.update(kwargs)
        return base

    def test_passes_matching_criterion(self):
        row = self._row(bpm=6.0)
        f = [{"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"}]
        assert filter_service._matches(row, f) is True

    def test_fails_non_matching_criterion(self):
        row = self._row(bpm=3.0)
        f = [{"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"}]
        assert filter_service._matches(row, f) is False

    def test_null_field_excluded(self):
        row = self._row(bpm=None)
        f = [{"field": "bpm", "operator": ">=", "value": 0.0, "season_type": "either"}]
        assert filter_service._matches(row, f) is False

    def test_no_applicable_criteria_excluded(self):
        """Row is 'regular'; all criteria target 'playoffs' → excluded."""
        row = self._row(season_type="regular", bpm=10.0)
        f = [{"field": "bpm", "operator": ">=", "value": 0.0, "season_type": "playoffs"}]
        assert filter_service._matches(row, f) is False

    def test_either_applies_to_both_types(self):
        for stype in ("regular", "playoffs"):
            row = self._row(season_type=stype, bpm=6.0)
            f = [{"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"}]
            assert filter_service._matches(row, f) is True

    def test_all_criteria_must_pass(self):
        row = self._row(bpm=6.0, kyle_rating=1.0)
        f = [
            {"field": "bpm",         "operator": ">=", "value": 5.0, "season_type": "either"},
            {"field": "kyle_rating", "operator": ">=", "value": 3.0, "season_type": "either"},
        ]
        assert filter_service._matches(row, f) is False

    def test_all_operators(self):
        for op, val, expected in [
            (">",  4.9, True),
            (">",  5.0, False),
            ("<",  5.1, True),
            ("<",  5.0, False),
            (">=", 5.0, True),
            (">=", 5.1, False),
            ("<=", 5.0, True),
            ("<=", 4.9, False),
            ("=",  5.0, True),
            ("=",  5.1, False),
        ]:
            row = self._row(bpm=5.0)
            f = [{"field": "bpm", "operator": op, "value": val, "season_type": "either"}]
            assert filter_service._matches(row, f) is expected, (
                f"op={op!r} val={val}: expected {expected}"
            )


# ---------------------------------------------------------------------------
# Integration tests: filter_players (service layer)
# ---------------------------------------------------------------------------

class TestFilterPlayersService:
    def test_empty_filters_returns_all_rows(self, tmp_path):
        db_path = str(tmp_path / "svc.db")
        db_module_path_orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, pid_a, pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            result = filter_service.filter_players(conn, [])
            conn.close()

            # 2 players × 2 seasons = 4 rows
            assert len(result) == 4
            # Sorted by kyle_rating desc
            ratings = [r["kyle_rating"] for r in result]
            assert ratings == sorted(ratings, reverse=True)
        finally:
            db_module.DB_PATH = db_module_path_orig

    def test_bpm_filter_returns_only_matching(self, tmp_path):
        db_path = str(tmp_path / "svc2.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, pid_a, pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            # Only Alpha's rows have bpm >= 5.0
            result = filter_service.filter_players(conn, [
                {"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"},
            ])
            conn.close()

            assert len(result) > 0
            for row in result:
                assert row["bpm"] >= 5.0
        finally:
            db_module.DB_PATH = orig

    def test_season_type_regular_excludes_playoff_rows(self, tmp_path):
        db_path = str(tmp_path / "svc3.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, _pid_a, _pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            result = filter_service.filter_players(conn, [
                {"field": "bpm", "operator": ">", "value": 0.0, "season_type": "regular"},
            ])
            conn.close()

            assert len(result) > 0
            for row in result:
                assert row["season_type"] == "regular"
        finally:
            db_module.DB_PATH = orig

    def test_season_type_playoffs_excludes_regular_rows(self, tmp_path):
        db_path = str(tmp_path / "svc4.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, _pid_a, _pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            result = filter_service.filter_players(conn, [
                {"field": "bpm", "operator": ">", "value": 0.0, "season_type": "playoffs"},
            ])
            conn.close()

            assert len(result) > 0
            for row in result:
                assert row["season_type"] == "playoffs"
        finally:
            db_module.DB_PATH = orig

    def test_no_matches_returns_empty_list(self, tmp_path):
        db_path = str(tmp_path / "svc5.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, _pid_a, _pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            result = filter_service.filter_players(conn, [
                {"field": "bpm", "operator": ">=", "value": 999.0, "season_type": "either"},
            ])
            conn.close()

            assert result == []
        finally:
            db_module.DB_PATH = orig

    def test_invalid_filters_raise_value_error(self, tmp_path):
        db_path = str(tmp_path / "svc6.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn = sqlite3.connect(db_path)
            with pytest.raises(ValueError):
                filter_service.filter_players(conn, [
                    {"field": "INVALID_FIELD", "operator": ">=", "value": 1.0, "season_type": "either"},
                ])
            conn.close()
        finally:
            db_module.DB_PATH = orig

    def test_result_rows_have_expected_keys(self, tmp_path):
        db_path = str(tmp_path / "svc7.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, _pid_a, _pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            result = filter_service.filter_players(conn, [])
            conn.close()

            required_keys = {
                "player_id", "name", "season_year", "season_type", "season_label",
                "bpm", "kyle_rating", "bpm_norm", "minutes", "usage_rate",
            }
            for row in result:
                assert required_keys.issubset(row.keys()), (
                    f"Row missing keys: {required_keys - row.keys()}"
                )
        finally:
            db_module.DB_PATH = orig

    def test_either_season_type_returns_both_types(self, tmp_path):
        db_path = str(tmp_path / "svc8.db")
        orig = db_module.DB_PATH
        db_module.DB_PATH = db_path
        init_db()

        try:
            conn, pid_a, _pid_b, _reg_id, _poff_id = _seed_two_seasons(db_path)
            # Filter to just player A (high bpm) using "either" → should get both regular + playoff rows
            result = filter_service.filter_players(conn, [
                {"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"},
            ])
            conn.close()

            season_types = {r["season_type"] for r in result if r["player_id"] == pid_a}
            assert "regular" in season_types
            assert "playoffs" in season_types
        finally:
            db_module.DB_PATH = orig


# ---------------------------------------------------------------------------
# API endpoint tests: GET /filter and POST /api/filter_players
# ---------------------------------------------------------------------------

class TestFilterPage:
    def test_get_filter_page_returns_200(self, client):
        resp = client.get("/filter")
        assert resp.status_code == 200


class TestFilterPlayersEndpoint:
    def _post(self, client, filters):
        return client.post(
            "/api/filter_players",
            data=json.dumps({"filters": filters}),
            content_type="application/json",
        )

    def test_empty_filters_returns_200_with_count(self, client):
        resp = self._post(client, [])
        assert resp.status_code == 200
        data = _json(resp)
        assert "results" in data
        assert "count" in data
        assert isinstance(data["results"], list)
        assert data["count"] == len(data["results"])

    def test_empty_db_empty_filters_returns_zero_results(self, client):
        resp = self._post(client, [])
        assert resp.status_code == 200
        data = _json(resp)
        assert data["results"] == []
        assert data["count"] == 0

    def test_unknown_field_returns_400(self, client):
        resp = self._post(client, [
            {"field": "xyz_made_up", "operator": ">=", "value": 1.0, "season_type": "either"},
        ])
        assert resp.status_code == 400
        assert "error" in _json(resp)

    def test_unknown_operator_returns_400(self, client):
        resp = self._post(client, [
            {"field": "bpm", "operator": "??", "value": 1.0, "season_type": "either"},
        ])
        assert resp.status_code == 400

    def test_non_list_filters_returns_400(self, client):
        resp = client.post(
            "/api/filter_players",
            data=json.dumps({"filters": "not a list"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_season_type_returns_400(self, client):
        resp = self._post(client, [
            {"field": "bpm", "operator": ">=", "value": 1.0, "season_type": "postseason"},
        ])
        assert resp.status_code == 400

    def test_filter_returns_matching_results(self, client):
        """End-to-end: seed data, run filter, confirm results match criteria."""
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [
            {"field": "bpm", "operator": ">=", "value": 5.0, "season_type": "either"},
        ])
        assert resp.status_code == 200
        data = _json(resp)

        assert data["count"] > 0
        for row in data["results"]:
            assert row["bpm"] >= 5.0

    def test_filter_no_results_returns_empty(self, client):
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [
            {"field": "bpm", "operator": ">=", "value": 9999.0, "season_type": "either"},
        ])
        assert resp.status_code == 200
        data = _json(resp)
        assert data["results"] == []
        assert data["count"] == 0

    def test_filter_by_season_type_regular(self, client):
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [
            {"field": "bpm", "operator": ">", "value": 0.0, "season_type": "regular"},
        ])
        assert resp.status_code == 200
        data = _json(resp)
        assert data["count"] > 0
        for row in data["results"]:
            assert row["season_type"] == "regular"

    def test_filter_by_season_type_playoffs(self, client):
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [
            {"field": "bpm", "operator": ">", "value": 0.0, "season_type": "playoffs"},
        ])
        assert resp.status_code == 200
        data = _json(resp)
        assert data["count"] > 0
        for row in data["results"]:
            assert row["season_type"] == "playoffs"

    def test_multiple_filters_all_must_match(self, client):
        """Two filters: only rows that satisfy BOTH should be returned."""
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [
            {"field": "bpm",         "operator": ">=", "value": 5.0, "season_type": "either"},
            {"field": "kyle_rating", "operator": ">=", "value": 0.0, "season_type": "either"},
        ])
        assert resp.status_code == 200
        data = _json(resp)
        for row in data["results"]:
            assert row["bpm"] >= 5.0
            assert row["kyle_rating"] >= 0.0

    def test_results_sorted_by_kyle_rating_desc(self, client):
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [])
        data = _json(resp)
        ratings = [r["kyle_rating"] for r in data["results"]]
        assert ratings == sorted(ratings, reverse=True)

    def test_count_matches_results_length(self, client):
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [
            {"field": "bpm", "operator": ">", "value": 0.0, "season_type": "either"},
        ])
        data = _json(resp)
        assert data["count"] == len(data["results"])

    def test_response_row_has_player_name_and_season_label(self, client):
        _seed_two_seasons(db_module.DB_PATH)

        resp = self._post(client, [])
        data = _json(resp)
        assert data["count"] > 0
        row = data["results"][0]
        assert row["name"] is not None and row["name"] != ""
        assert row["season_label"] is not None and row["season_label"] != ""
        assert isinstance(row["season_year"], int)
