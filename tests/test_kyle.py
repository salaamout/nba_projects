"""
Unit tests for kyle.py — K.Y.L.E. rating calculation logic.
Run with: pytest tests/test_kyle.py
"""

import pytest
from kyle import calculate, calculate_all, compute_bounds, compute_least_squares_scores, FIELDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_player(**kwargs):
    """Return a minimal player dict with sensible defaults for all fields."""
    defaults = dict(
        player_id=1,
        name="Test Player",
        minutes=30.0,
        usage_rate=20.0,
        true_shooting_pct=0.55,
        assist_rate=15.0,
        turnover_pct=10.0,
        on_court_rating=2.0,
        on_off_diff=3.0,
        bpm=1.0,
        defense=1.0,
        on_off_asterisk=False,
        watch_kyle=None,
    )
    defaults.update(kwargs)
    return defaults


def _two_players(**overrides_p2):
    """Return a two-player list; p2 fields can be overridden."""
    p1 = _make_player(player_id=1, name="Alice")
    p2 = _make_player(player_id=2, name="Bob", **overrides_p2)
    return [p1, p2]


# ---------------------------------------------------------------------------
# test_calculate_basic
# ---------------------------------------------------------------------------

def test_calculate_basic():
    """Two fully-populated players → kyle_rating is finite."""
    rows = _two_players(
        minutes=20.0,
        usage_rate=15.0,
        true_shooting_pct=0.48,
        assist_rate=10.0,
        turnover_pct=14.0,
        on_court_rating=-1.0,
        on_off_diff=-2.0,
        bpm=-0.5,
        defense=0.0,
    )
    result = calculate(rows)
    for r in result:
        assert r["kyle_rating"] is not None
        assert isinstance(r["kyle_rating"], float)


# ---------------------------------------------------------------------------
# test_lower_is_better_turnover
# ---------------------------------------------------------------------------

def test_lower_is_better_turnover():
    """Player with lower turnover_pct should get a higher turnover_pct_norm."""
    low_tov = _make_player(player_id=1, name="Low TOV", turnover_pct=5.0)
    high_tov = _make_player(player_id=2, name="High TOV", turnover_pct=20.0)
    result = calculate([low_tov, high_tov])
    norms = {r["name"]: r["turnover_pct_norm"] for r in result}
    assert norms["Low TOV"] > norms["High TOV"]


# ---------------------------------------------------------------------------
# test_special_worst_minutes
# ---------------------------------------------------------------------------

def test_special_worst_minutes():
    """Player at max minutes should get minutes_norm = +1 (worst = max/2)."""
    hi = _make_player(player_id=1, name="Heavy Min", minutes=40.0)
    lo = _make_player(player_id=2, name="Light Min", minutes=10.0)
    result = calculate([hi, lo])
    heavy = next(r for r in result if r["name"] == "Heavy Min")
    assert heavy["minutes_norm"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# test_on_off_asterisk_substitution
# ---------------------------------------------------------------------------

def test_on_off_asterisk_substitution():
    """Asterisk'd player's on_off_diff_norm should equal average of other norms."""
    p1 = _make_player(player_id=1, name="Regular", on_off_asterisk=False)
    p2 = _make_player(player_id=2, name="Asterisk", on_off_asterisk=True)
    result = calculate([p1, p2])
    ast = next(r for r in result if r["name"] == "Asterisk")

    # Collect other norms (all fields except on_off_diff for this player)
    other_norms = [
        ast[f + "_norm"]
        for f in FIELDS
        if f != "on_off_diff" and ast.get(f + "_norm") is not None
    ]
    expected_avg = round(sum(other_norms) / len(other_norms), 4)
    assert ast["on_off_diff_norm"] == pytest.approx(expected_avg, abs=1e-3)


# ---------------------------------------------------------------------------
# test_clamp_in_calculate
# ---------------------------------------------------------------------------

def test_clamp_in_calculate():
    """calculate() must clamp all norms to [-1, +1]."""
    rows = _two_players(bpm=5.0)
    result = calculate(rows)
    for r in result:
        for field in FIELDS:
            norm = r.get(field + "_norm")
            if norm is not None:
                assert -1.0 <= norm <= 1.0, f"{field}_norm out of range: {norm}"


# ---------------------------------------------------------------------------
# test_no_clamp_in_calculate_all
# ---------------------------------------------------------------------------

def test_no_clamp_in_calculate_all():
    """calculate_all() should allow norms outside [-1, +1]."""
    # Selected players with a meaningful bpm spread so bounds span != 0
    selected = _two_players(bpm=-5.0)
    selected_rows = [dict(r) for r in selected]
    from kyle import _add_derived
    _add_derived(selected_rows)
    bounds = compute_bounds(selected_rows)

    # Outlier player far outside selected range
    outlier = _make_player(player_id=99, name="Outlier", bpm=100.0)
    result = calculate_all([outlier], bounds)
    outlier_result = result[0]
    # bpm_norm should be well above 1.0 for such an extreme value
    assert outlier_result["bpm_norm"] > 1.0


# ---------------------------------------------------------------------------
# test_playoffs_excludes_minutes
# ---------------------------------------------------------------------------

def test_playoffs_excludes_minutes():
    """calculate() with season_type='playoffs' sets minutes_norm = None."""
    rows = _two_players()
    result = calculate(rows, season_type="playoffs")
    for r in result:
        assert r.get("minutes_norm") is None


# ---------------------------------------------------------------------------
# test_watch_kyle_added_to_total
# ---------------------------------------------------------------------------

def test_watch_kyle_added_to_total():
    """watch_kyle contribution should be included in kyle_rating."""
    base = _make_player(player_id=1, name="NoWatch", watch_kyle=None)
    watched = _make_player(player_id=2, name="Watched", watch_kyle=None)

    result_base = calculate([base, watched])
    no_watch_rating = next(r["kyle_rating"] for r in result_base if r["name"] == "NoWatch")

    # Now add a watch_kyle bonus to the first player and recalculate
    base2 = _make_player(player_id=1, name="NoWatch", watch_kyle=None)
    watched2 = _make_player(player_id=2, name="Watched", watch_kyle=0.8)
    result_watched = calculate([base2, watched2])

    watched_rating = next(r["kyle_rating"] for r in result_watched if r["name"] == "Watched")
    # The watched player's rating should be higher by ~0.8
    unwatched_rating = next(r["kyle_rating"] for r in result_watched if r["name"] == "NoWatch")
    assert watched_rating > unwatched_rating


# ---------------------------------------------------------------------------
# test_single_player
# ---------------------------------------------------------------------------

def test_single_player():
    """Single player → non-special norms = 0; minutes_norm = 1.0 due to SPECIAL_WORST logic."""
    rows = [_make_player(player_id=1, name="Solo", watch_kyle=None)]
    result = calculate(rows)
    solo = result[0]
    # minutes uses SPECIAL_WORST (worst = max/2), so a single player at max gets norm = +1.0
    assert solo["minutes_norm"] == pytest.approx(1.0)
    # All other scalar fields have span=0 with one player, so their norms are 0.0
    for field in FIELDS:
        if field == "minutes":
            continue
        norm = solo.get(field + "_norm")
        if norm is not None:
            assert norm == pytest.approx(0.0), f"{field}_norm should be 0 for single player"


# ---------------------------------------------------------------------------
# test_all_none_fields
# ---------------------------------------------------------------------------

def test_all_none_fields():
    """All stat fields None → kyle_rating should be None."""
    p1 = dict(
        player_id=1, name="Empty",
        minutes=None, usage_rate=None, true_shooting_pct=None,
        assist_rate=None, turnover_pct=None, on_court_rating=None,
        on_off_diff=None, bpm=None, defense=None,
        on_off_asterisk=False, watch_kyle=None,
    )
    p2 = dict(
        player_id=2, name="Empty2",
        minutes=None, usage_rate=None, true_shooting_pct=None,
        assist_rate=None, turnover_pct=None, on_court_rating=None,
        on_off_diff=None, bpm=None, defense=None,
        on_off_asterisk=False, watch_kyle=None,
    )
    result = calculate([p1, p2])
    for r in result:
        assert r["kyle_rating"] is None


# ---------------------------------------------------------------------------
# test_least_squares_basic
# ---------------------------------------------------------------------------

def test_least_squares_basic():
    """Simple win/loss pairs → winner has higher score than loser."""
    comparisons = [(1, 2), (1, 3), (2, 3)]
    scores = compute_least_squares_scores(comparisons)
    assert scores[1] > scores[2] > scores[3]


# ---------------------------------------------------------------------------
# test_least_squares_empty
# ---------------------------------------------------------------------------

def test_least_squares_empty():
    """Empty comparisons list → empty dict."""
    assert compute_least_squares_scores([]) == {}


# ---------------------------------------------------------------------------
# test_least_squares_single_player
# ---------------------------------------------------------------------------

def test_least_squares_single_player():
    """Single unique player (same winner/loser) → empty dict (need ≥2 players)."""
    result = compute_least_squares_scores([(1, 1)])
    assert result == {}
