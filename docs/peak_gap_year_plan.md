# Plan: Allow Gap Years (Zero-Score Seasons) in Peak Window Calculation

## Current Behaviour — Verified

There are **two places** in `app.py` that compute a player's peak N-year window,
and both use the same pattern:

### 1. `best3year()` — `/api/best3year` (lines ~693–724)

```python
sorted_years = sorted(years.keys())          # only years that have a score

for i in range(len(sorted_years) - window + 1):
    window_years = sorted_years[i : i + window]
    # Only consider truly consecutive years  ← THIS IS THE CAP
    if window_years[-1] - window_years[0] != window - 1:
        continue
    ...
```

`years` is built by accumulating `kyle_rating` from each season.  A player only
gets an entry in `years` for a season year **if their `kyle_rating` is not
`None`** (see lines ~660–668: `if rating is None: continue`).  So a year where
the player has no score simply does not appear in `sorted_years`.

The check `window_years[-1] - window_years[0] != window - 1` then rejects any
slice of `sorted_years` that contains an implicit gap — i.e. any window that
would span a no-score year is silently skipped.  This caps the peak to only
runs of perfectly consecutive scored seasons.

### 2. `suggest_game()` — `/api/suggest_game` (lines ~939–952)

Identical pattern — same `sorted_years` / `window - 1` guard — with the same
effect.

---

## Desired Behaviour

Gap years (seasons where the player has no K.Y.L.E. score) should be counted
as **zero** rather than breaking the window.  A window of `[2001, 2002, 2003]`
should be valid even if 2002 produced no score; that year simply contributes
`0` to the window total.

---

## Implementation Plan

### Change 1 — `best3year()` in `app.py`

**What to change:**

Replace the sliding window over `sorted_years` with a sliding window over a
**dense integer range** from the player's first scored year to their last
scored year.  For any year in the range that is not in `years`, treat it as
`{"regular": 0.0, "playoffs": 0.0, "watch_kyle": None, ...}`.

**Before** (lines ~693–698):

```python
sorted_years = sorted(years.keys())

if len(sorted_years) < window:
    continue  # not enough distinct years

best_window_entry = None

for i in range(len(sorted_years) - window + 1):
    window_years = sorted_years[i : i + window]
    if window_years[-1] - window_years[0] != window - 1:
        continue
```

**After:**

```python
sorted_years = sorted(years.keys())

if len(sorted_years) < window:
    continue  # not enough distinct years with actual scores

first_year = sorted_years[0]
last_year  = sorted_years[-1]
all_years  = list(range(first_year, last_year + 1))  # dense range

if len(all_years) < window:
    continue

best_window_entry = None

ZERO_YEAR = {"regular": 0.0, "playoffs": 0.0, "watch_kyle": None,
             "playoff_watched": 0, "playoff_played": 0}

for i in range(len(all_years) - window + 1):
    window_years = all_years[i : i + window]
    # No consecutive-check needed — all_years is already dense
```

Then update the inner body to use `years.get(y, ZERO_YEAR)` instead of
`years[y]`:

```python
    reg   = sum(years.get(y, ZERO_YEAR)["regular"]  for y in window_years)
    play  = sum(years.get(y, ZERO_YEAR)["playoffs"] for y in window_years)
    total = reg + play

    wk_vals = [years[y]["watch_kyle"] for y in window_years
               if y in years and years[y].get("watch_kyle") is not None]
    wk_total = round(sum(wk_vals), 4) if wk_vals else None

    pw_watched = sum(all_playoff_watched.get((pid, y), 0) for y in window_years)
    pw_played  = sum(all_playoff_played.get((pid, y), 0)  for y in window_years)
```

The guard `if len(sorted_years) < window` is kept so that players with fewer
than `window` *actual scored seasons* (across their entire career) are still
excluded — they do not have enough data for a meaningful peak.

---

### Change 2 — `suggest_game()` in `app.py`

Same transformation in the inner peak-building loop (lines ~939–952):

**Before:**

```python
sorted_years = sorted(years.keys())

if len(sorted_years) < window:
    continue

best_entry = None
for i in range(len(sorted_years) - window + 1):
    window_years = sorted_years[i : i + window]
    if window_years[-1] - window_years[0] != window - 1:
        continue
    total = sum(years[y]["regular"] + years[y]["playoffs"] for y in window_years)
```

**After:**

```python
sorted_years = sorted(years.keys())

if len(sorted_years) < window:
    continue

first_year = sorted_years[0]
last_year  = sorted_years[-1]
all_years  = list(range(first_year, last_year + 1))

if len(all_years) < window:
    continue

ZERO_YEAR = {"regular": 0.0, "playoffs": 0.0}
best_entry = None
for i in range(len(all_years) - window + 1):
    window_years = all_years[i : i + window]
    total = sum(
        years.get(y, ZERO_YEAR)["regular"] + years.get(y, ZERO_YEAR)["playoffs"]
        for y in window_years
    )
```

---

## Edge Cases to Consider

| Scenario | Behaviour after change |
|---|---|
| Player has 3 scored years, all consecutive | Identical to current — no gap years to fill |
| Player has 5 scored years across a 7-year span (2 gaps) | Previously only windows within fully consecutive sub-runs were evaluated; now all 7-year dense windows are candidates, with gaps counting as 0 |
| Player has only 1 or 2 scored seasons total (< window) | Still excluded by the `len(sorted_years) < window` guard |
| Player has scored seasons at year 1990 and year 2010 (huge gap) | Now eligible for any `window`-year window within that 21-year range — gap years count as 0, so the window total may be very low, which is correct |
| `watch_kyle` for a gap year | Already handled — `wk_vals` only collects values from years that are in `years` dict |

---

## Files to Change

| File | Location | Change |
|---|---|---|
| `app.py` | `best3year()` ~lines 693–720 | Replace `sorted_years` sliding window with dense `all_years` range; use `years.get(y, ZERO_YEAR)` |
| `app.py` | `suggest_game()` ~lines 935–952 | Same transformation |

No database changes, no frontend changes, no schema changes needed.

---

## Testing / Verification

After the change:

1. Find a player in the database who has a gap year (a year where they appear
   in the DB but have no `kyle_rating` because they weren't in a selected set,
   or were not scored that season).  Confirm their peak window can now span
   that year.
2. Confirm players with no gap years have identical peak windows as before.
3. Confirm the `/api/suggest_game` endpoint still returns valid, overlapping
   peak pairs.
