# Player Filter Page — Feature Plan

## Overview

A new top-level page (`/filter`) where the user builds one or more filter criteria against any player-season row and gets back a table of all matching (player, season) pairs.

---

## Stats Available

Filters can be written against the **raw stat** or the **kyle_points value** (the `_norm` field, ranging from −1 to +1) for each of the following:

| Display Name   | Raw field (`player_stats`) | Kyle-points field |
|---|---|---|
| Minutes        | `minutes`                  | `minutes_norm`        |
| Usage %        | `usage_rate`               | `usage_rate_norm`     |
| Pts/Shot       | derived: `true_shooting_pct * 2` | `points_per_shot_norm` |
| Assist %       | `assist_rate`              | `assist_rate_norm`    |
| TOV %          | `turnover_pct`             | `turnover_pct_norm`   |
| On-Court       | `on_court_rating`          | `on_court_rating_norm`|
| On/Off Diff    | `on_off_diff`              | `on_off_diff_norm`    |
| BPM            | `bpm`                      | `bpm_norm`            |
| Defense        | `defense`                  | `defense_norm`        |
| K.Y.L.E.       | `kyle_rating` (sum)        | *(is itself already a points value)* |

Both Regular Season and Playoff stat rows are searchable. Each filter criterion includes a **season type** selector (`Regular`, `Playoff`, or `Either`).

---

## Filter Criteria Model

Each criterion is an object:

```json
{
  "field":       "bpm",          // raw or norm field name (see table above)
  "operator":    ">=",           // one of: >, <, >=, <=, =
  "value":       5.0,            // numeric threshold
  "season_type": "regular"       // "regular" | "playoff" | "either"
}
```

Multiple criteria are **AND**-ed together. A player-season row must satisfy **all** criteria to appear in results.

The results show **all matching (player, season) pairs** — not just one row per player — so a player who had multiple qualifying seasons appears multiple times.

---

## Architecture

### 1. Backend — Service: `services/filter_service.py` (new file)

```
filter_players(conn, filters: list[dict]) -> list[dict]
```

**Algorithm:**

1. Fetch all `player_stats` rows joined to `seasons` and `players` (same join used by `fetch_selected_player_dicts` in `kyle_service.py`), grouped by season.
2. For each season (year + type), run `kyle.calculate()` on the full selected-player set for that season to produce `_norm` values and `kyle_rating` — exactly as the existing per-season computation works. Cache by `(season_year, season_type)` to avoid recomputing.
3. Flatten all resulting rows into a single list (each row = one player-season with all raw and norm fields populated).
4. Apply the filter criteria: keep only rows where **every** criterion is satisfied.
5. Return the filtered rows sorted by `kyle_rating DESC` by default.

**Key notes:**
- Norm values are **relative to the season's selected-player pool**, same as every other view in the app.
- `on_off_asterisk` rows get the substituted norm (average of other norms) just as in the normal calculation — no special casing needed here.
- Playoff rows exclude `minutes` from the kyle_rating sum, same as the existing logic.

### 2. Backend — API endpoint in `app.py`

```
POST /api/filter_players
```

**Request body:**
```json
{
  "filters": [
    { "field": "bpm", "operator": ">=", "value": 5.0, "season_type": "regular" },
    { "field": "kyle_rating", "operator": ">=", "value": 3.0, "season_type": "either" }
  ]
}
```

**Response:**
```json
{
  "results": [
    {
      "player_id": 123,
      "player_name": "LeBron James",
      "season_year": 2016,
      "season_type": "regular",
      "season_label": "2016 Regular Season",
      "minutes": 2700,
      "usage_rate": 31.5,
      "points_per_shot": 1.18,
      "assist_rate": 35.0,
      "turnover_pct": 12.5,
      "on_court_rating": 10.5,
      "on_off_diff": 9.8,
      "bpm": 7.2,
      "defense": 1.5,
      "kyle_rating": 5.43,
      "minutes_norm": 0.72,
      "usage_rate_norm": 0.65,
      "points_per_shot_norm": 0.44,
      "assist_rate_norm": 0.91,
      "turnover_pct_norm": -0.12,
      "on_court_rating_norm": 0.88,
      "on_off_diff_norm": 0.77,
      "bpm_norm": 0.83,
      "defense_norm": 0.15
    },
    ...
  ],
  "count": 42
}
```

Validation: reject unknown field names and unknown operators with a `400` response.

### 3. Frontend — `static/filter.js` (new file)

**Filter builder UI:**
- A "Add Filter" button appends a new filter row to the UI.
- Each filter row has:
  - A **Stat** dropdown (grouped: "Raw Stats" / "Kyle Points") — labels match the display names in the table above
  - An **Operator** dropdown (`>`, `<`, `>=`, `<=`, `=`)
  - A numeric **Value** input
  - A **Season Type** dropdown (`Regular`, `Playoff`, `Either`)
  - A **Remove** (×) button
- A **Search** button POSTs the criteria to `/api/filter_players` and renders results.
- A **Clear** button resets the form.

**Results table:**
- Columns: Player (links to `/player/<id>`), Season, Type, Minutes, Usage%, Pts/Shot, Assist%, TOV%, On-Court, On/Off Diff, BPM, Defense, K.Y.L.E.
- Rows are sortable client-side by clicking column headers (same pattern as `all_players.js`).
- Show total match count above the table ("X player-seasons matched").
- If no results: show "No player-seasons matched your filters."

### 4. Frontend — `templates/filter.html` (new file)

- Follows the same base layout/style as `all_players.html` and `best3year_kyle.html`.
- Includes `filter.js`.
- Top nav link added alongside the other page links.

### 5. Route in `app.py`

```python
@app.route("/filter")
def filter_page():
    return render_template("filter.html")
```

---

## File Summary

| File | Action |
|---|---|
| `services/filter_service.py` | **New** — `filter_players(conn, filters)` |
| `app.py` | **Edit** — add `/filter` page route + `POST /api/filter_players` endpoint |
| `templates/filter.html` | **New** — page template |
| `static/filter.js` | **New** — filter builder + results rendering |
| `templates/*.html` | **Edit** — add nav link to Filter page on all existing templates |

---

## Edge Cases & Notes

- **Empty filter list**: return all player-season rows (no filtering), sorted by `kyle_rating DESC`. This gives the user a useful default view of all data.
- **NULL stats**: if a player-season has `NULL` for the filtered field, that row is excluded from results (treat as "does not satisfy").
- **`on_off_asterisk`**: the asterisk flag is surfaced in the results table with the same visual indicator used on the player page (e.g. a `*` next to the On/Off Diff value).
- **Performance**: the full computation iterates over ~30k `player_stats` rows and groups them into ~98 season buckets. Each bucket's K.Y.L.E. computation is O(n) and already fast. A simple in-memory season cache (dict keyed by `(season_year, season_type)`) avoids redundant recomputation within a single request. No DB schema changes required.
- **`season_type: "either"`**: a row matches if it satisfies the value threshold regardless of whether it's a regular-season or playoff row.
