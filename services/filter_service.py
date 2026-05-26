"""Player filter service.

Public API
----------
filter_players(conn, filters) -> list[dict]

Each filter is a dict:
    {
        "field":       str,   # raw or norm field name
        "operator":    str,   # one of: >, <, >=, <=, =
        "value":       float,
        "season_type": str,   # "regular" | "playoff" | "either"
    }

A player-season row matches when it satisfies every criterion whose
season_type matches the row's season_type (or is "either").

If filters is empty every player-season row is returned (sorted by
kyle_rating desc).

If a row has no applicable criteria (all filters target the other
season type) it is excluded.
"""
from __future__ import annotations

import operator as _op

from services.kyle_service import fetch_selected_player_dicts, _compute_season_kyle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPS: dict[str, object] = {
    ">":  _op.gt,
    "<":  _op.lt,
    ">=": _op.ge,
    "<=": _op.le,
    "=":  _op.eq,
}

VALID_FIELDS: set[str] = {
    # raw stats
    "minutes",
    "usage_rate",
    "points_per_shot",
    "assist_rate",
    "turnover_pct",
    "on_court_rating",
    "on_off_diff",
    "bpm",
    "defense",
    "kyle_rating",
    # kyle-points (norm) values
    "minutes_norm",
    "usage_rate_norm",
    "points_per_shot_norm",
    "assist_rate_norm",
    "turnover_pct_norm",
    "on_court_rating_norm",
    "on_off_diff_norm",
    "bpm_norm",
    "defense_norm",
}

VALID_OPERATORS: set[str] = set(_OPS.keys())
VALID_SEASON_TYPES: set[str] = {"regular", "playoffs", "either"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_filters(filters: list[dict]) -> list[str]:
    """Return a list of validation error messages (empty = all ok)."""
    errors: list[str] = []
    for i, f in enumerate(filters):
        label = f"Filter {i + 1}"
        if f.get("field") not in VALID_FIELDS:
            errors.append(f"{label}: unknown field {f.get('field')!r}")
        if f.get("operator") not in VALID_OPERATORS:
            errors.append(f"{label}: unknown operator {f.get('operator')!r}")
        try:
            float(f["value"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label}: value must be a number")
        if f.get("season_type", "either") not in VALID_SEASON_TYPES:
            errors.append(f"{label}: season_type must be 'regular', 'playoffs', or 'either'")
    return errors


def filter_players(conn, filters: list[dict]) -> list[dict]:
    """Compute K.Y.L.E. for every season and return matching player-season rows.

    Raises ValueError for invalid filter definitions.
    """
    errors = validate_filters(filters)
    if errors:
        raise ValueError("; ".join(errors))

    # Fetch all seasons that have at least one selected player
    seasons = conn.execute(
        """
        SELECT DISTINCT s.id, s.season_year, s.season_type, s.label
        FROM seasons s
        JOIN selected_players sp ON sp.season_id = s.id
        ORDER BY s.season_year, s.season_type
        """
    ).fetchall()

    all_rows: list[dict] = []

    for season in seasons:
        season_id    = season["id"]
        season_year  = season["season_year"]
        season_type  = season["season_type"]
        season_label = season["label"]

        player_dicts = fetch_selected_player_dicts(conn, season_id)
        if not player_dicts:
            continue

        calculated = _compute_season_kyle(conn, player_dicts, season_type, season_year)
        for p in calculated:
            p["season_year"]  = season_year
            p["season_type"]  = season_type
            p["season_label"] = season_label
            all_rows.append(p)

    # Short-circuit: empty filters → return everything
    if not filters:
        all_rows.sort(key=lambda x: (x.get("kyle_rating") or 0.0), reverse=True)
        return all_rows

    result = [row for row in all_rows if _matches(row, filters)]
    result.sort(key=lambda x: (x.get("kyle_rating") or 0.0), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matches(row: dict, filters: list[dict]) -> bool:
    """Return True iff the row satisfies all applicable filter criteria.

    A criterion is "applicable" to a row when its season_type is "either" or
    matches the row's season_type.  If no criteria are applicable the row is
    excluded (treated as not matching).
    """
    applicable = 0
    for f in filters:
        criterion_season = f.get("season_type", "either")

        # Determine applicability
        if criterion_season != "either" and criterion_season != row.get("season_type"):
            continue  # criterion doesn't apply to this row's season type
        applicable += 1

        op_fn     = _OPS[f["operator"]]
        threshold = float(f["value"])
        val       = row.get(f["field"])

        if val is None:
            return False
        try:
            if not op_fn(float(val), threshold):
                return False
        except (TypeError, ValueError):
            return False

    # If no criteria applied to this row, exclude it
    return applicable > 0
