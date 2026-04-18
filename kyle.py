"""
K.Y.L.E. calculation logic.

Input : list of dicts, each with keys matching player_stats columns plus 'name'.
Output: same list with added keys:
    - points_per_shot  (ts_pct * 2, derived)
    - <field>_norm     for every scored field
    - kyle_rating      final sum
"""

FIELDS = [
    "minutes",
    "usage_rate",
    "points_per_shot",
    "assist_rate",
    "turnover_pct",
    "on_court_rating",
    "on_off_diff",
    "bpm",
    "defense",
]

# Fields where lower is better (more is bad)
LOWER_IS_BETTER = {"turnover_pct"}

# Fields with special worst-value logic
# minutes: worst = max / 2  (not the actual minimum)
SPECIAL_WORST = {"minutes"}


def _safe(val):
    """Return float or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _add_derived(rows):
    """Add points_per_shot derived field in-place."""
    for d in rows:
        ts = _safe(d.get("true_shooting_pct"))
        d["points_per_shot"] = ts * 2 if ts is not None else None


def compute_bounds(rows, exclude_fields=None):
    """
    Compute best/worst bounds from a list of player dicts (already have
    points_per_shot populated).

    Parameters
    ----------
    exclude_fields : set[str] | None
        Fields to skip entirely (excluded from bounds and from the rating sum).
        Use ``{"minutes"}`` for playoffs, where the minute totals are too small
        to be meaningful relative to a full regular season.

    Returns
    -------
    dict mapping field → (best, worst) or None if no data.
    """
    exclude_fields = exclude_fields or set()
    bounds = {}
    for field in FIELDS:
        if field in exclude_fields:
            bounds[field] = None
            continue
        values = [_safe(r.get(field)) for r in rows]
        values = [v for v in values if v is not None]
        if not values:
            bounds[field] = None
            continue

        if field in LOWER_IS_BETTER:
            best = min(values)
            worst = max(values)
        elif field in SPECIAL_WORST:  # minutes
            best = max(values)
            worst = best / 2.0
        else:
            best = max(values)
            worst = min(values)

        bounds[field] = (best, worst)
    return bounds


def _apply_bounds(rows, bounds, clamp):
    """
    Normalise each field using pre-computed bounds and sum to kyle_rating.

    Parameters
    ----------
    rows   : list[dict]  – mutated in-place
    bounds : dict        – from compute_bounds()
    clamp  : bool        – if True, clamp norms to [-1, +1]
    """
    for row in rows:
        total = 0.0
        has_any = False
        for field in FIELDS:
            raw = _safe(row.get(field))
            norm_key = field + "_norm"
            if raw is None or bounds.get(field) is None:
                row[norm_key] = None
                continue

            best, worst = bounds[field]
            span = best - worst

            if span == 0:
                norm = 0.0
            else:
                norm = (raw - worst) / span * 2 - 1  # worst→-1, best→+1

            if clamp:
                norm = max(-1.0, min(1.0, norm))

            row[norm_key] = round(norm, 4)
            total += norm
            has_any = True

        row["kyle_rating"] = round(total, 4) if has_any else None


def calculate(player_stats_rows, season_type="regular"):
    """
    Compute K.Y.L.E. ratings for the *selected* player set.
    Bounds are derived from this set; values are clamped to [-1, +1].

    Parameters
    ----------
    player_stats_rows : list[dict-like]
        Each row must have the player_stats columns plus 'name'.
    season_type : str
        ``"playoffs"`` excludes ``minutes`` from the rating sum because
        playoff minute totals are much smaller than regular-season totals.

    Returns
    -------
    list[dict]
        Each dict has all original fields plus derived/normalised ones.
    """
    rows = [dict(r) for r in player_stats_rows]
    _add_derived(rows)

    if not rows:
        return rows

    exclude = {"minutes"} if season_type == "playoffs" else set()
    bounds = compute_bounds(rows, exclude_fields=exclude)
    _apply_bounds(rows, bounds, clamp=True)
    return rows


def calculate_all(all_player_rows, selected_bounds, season_type="regular"):
    """
    Score *all* players in a season using bounds derived from the selected set.
    Values are NOT clamped, so a player outside the selected range can exceed
    ±1 per field.

    Parameters
    ----------
    all_player_rows : list[dict-like]
        Every player with stats for the season.
    selected_bounds : dict
        Output of compute_bounds() called on the selected player set.
    season_type : str
        ``"playoffs"`` excludes ``minutes`` from the rating sum because
        playoff minute totals are much smaller than regular-season totals.

    Returns
    -------
    list[dict]
        Each dict has all original fields plus derived/normalised ones.
    """
    rows = [dict(r) for r in all_player_rows]
    _add_derived(rows)
    _apply_bounds(rows, selected_bounds, clamp=False)
    return rows
