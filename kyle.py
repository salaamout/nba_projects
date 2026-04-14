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


def calculate(player_stats_rows):
    """
    Parameters
    ----------
    player_stats_rows : list[dict-like]
        Each row must have the player_stats columns plus 'name'.

    Returns
    -------
    list[dict]
        Each dict has all original fields plus derived/normalised ones.
    """
    # Convert to plain dicts and add derived field
    rows = []
    for r in player_stats_rows:
        d = dict(r)
        ts = _safe(d.get("true_shooting_pct"))
        d["points_per_shot"] = ts * 2 if ts is not None else None
        rows.append(d)

    if not rows:
        return rows

    # Compute best/worst per field (only from rows where value is not None)
    bounds = {}
    for field in FIELDS:
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

    # Normalise each field and sum
    for row in rows:
        total = 0.0
        for field in FIELDS:
            raw = _safe(row.get(field))
            norm_key = field + "_norm"
            if raw is None or bounds[field] is None:
                row[norm_key] = None
                continue

            best, worst = bounds[field]
            span = best - worst

            if span == 0:
                norm = 0.0
            else:
                norm = (raw - worst) / span * 2 - 1  # maps worst→-1, best→+1

            # Clamp to [-1, +1]
            norm = max(-1.0, min(1.0, norm))
            row[norm_key] = round(norm, 4)
            total += norm

        row["kyle_rating"] = round(total, 4)

    return rows
