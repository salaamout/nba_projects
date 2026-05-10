# Plan: Live Scraping for the Active Playoffs Season

## Problem

The `league_game_log_fetch_log` table acts as a permanent "already fetched" cache.
Once a `(season_year, season_type, player_or_team)` row is recorded, `_fetch_league_game_log_nba`
skips the API call entirely — forever.  During an **active** playoffs this means any game
that happened after the first fetch (e.g. Game 3 of SAS vs MIN) is silently missed and
never inserted into `player_game_appearances`.

The in-memory `_fetched_seasons` set compounds this: even if the DB row were cleared,
re-fetching won't happen within the same server process lifetime.

---

## Root Cause Locations

| File | Location | Issue |
|------|----------|-------|
| `scraper.py` | `_fetch_league_game_log_nba` (~line 1020) | DB cache check short-circuits for already-logged seasons |
| `scraper.py` | `_fetched_seasons` in-memory set | Process-level cache prevents any re-fetch within a run |
| `scraper.py` | `_record_league_game_log_fetch` (~line 986) | Called immediately after a successful fetch, sealing the cache forever |

---

## Proposed Solution

### 1. Distinguish "Active Season" from "Historical Season"

Add a helper `_is_active_season(season_year: int, season_type: str) -> bool` that
returns `True` when the season is the **current** year and the **playoffs** are
known to still be ongoing.  The simplest implementation: compare `season_year` to
the current calendar year (and optionally a hard-coded "playoffs end month", e.g.
June).

```python
from datetime import date

def _is_active_season(season_year: int, season_type: str) -> bool:
    if season_type != "playoffs":
        return False
    today = date.today()
    # NBA playoffs run April–June; season_year matches the calendar year of the finals.
    return season_year == today.year and today.month in range(4, 8)
```

### 2. Time-Based Cache Expiry in `league_game_log_fetch_log`

Instead of treating the fetch log as permanent, store a `fetched_at` timestamp
(already present in the schema) and **skip the cache when the season is active and
the last fetch is older than a configurable TTL** (e.g. 4 hours).

Changes to `_fetch_league_game_log_nba`:

```python
ACTIVE_SEASON_TTL_HOURS = 4

# Replace the current "already_logged" short-circuit:
already_logged = conn.execute(
    "SELECT fetched_at FROM league_game_log_fetch_log "
    "WHERE season_year=? AND season_type=? AND player_or_team=?",
    (season_year, season_type, player_or_team),
).fetchone()

if already_logged:
    if _is_active_season(season_year, season_type):
        # For an active season, honour the cache only if it is fresh enough.
        fetched_at = datetime.fromisoformat(already_logged["fetched_at"])
        age_hours = (datetime.utcnow() - fetched_at).total_seconds() / 3600
        if age_hours < ACTIVE_SEASON_TTL_HOURS:
            # Still fresh — skip
            ...return cached data...
        # Cache is stale — fall through to re-fetch
        logger.info("Active season cache stale (%.1fh old), re-fetching...", age_hours)
    else:
        # Historical season — cache is permanent
        ...return cached data...
```

### 3. Clear the In-Memory `_fetched_seasons` Cache for Active Seasons

After a successful re-fetch for an active season, **do not add** the cache key to
`_fetched_seasons` (or remove it if present).  This allows the next request within
the same process to re-check the DB TTL and re-fetch again if enough time has passed.

```python
if not _is_active_season(season_year, season_type):
    with _fetched_seasons_lock:
        _fetched_seasons.add(cache_key)
```

### 4. Upsert (not Ignore) `league_game_log_fetch_log` for Active Seasons

`_record_league_game_log_fetch` already uses `INSERT OR REPLACE`, so the timestamp
will be updated on every successful re-fetch.  No change needed there.

### 5. Round Backfill Must Also Re-Run

`_fetch_series_round_map` and `_apply_series_rounds_to_appearances` are called after
a "P" fetch.  Since new games won't have `round` set yet, they will already be
picked up by the existing `WHERE round IS NULL` logic in `_apply_series_rounds_to_appearances`.
No change needed — it just needs the re-fetch to happen (fixed by steps 2–3).

---

## Schema Change — None Required

`league_game_log_fetch_log` already has a `fetched_at` column.  No migration needed.

---

## Manual Trigger / Admin Endpoint (Optional Enhancement)

Add a lightweight Flask route so you can force a re-fetch on demand without waiting
for the TTL:

```
POST /api/admin/refresh-active-season
```

Implementation: delete the `league_game_log_fetch_log` rows for
`(current_year, "playoffs", "T")` and `(current_year, "playoffs", "P")`, then also
remove those keys from `_fetched_seasons`.  The next page load will then re-fetch
automatically.

---

## Implementation Order

1. **Add `_is_active_season` helper** — pure function, no side effects, easy to test.
2. **Modify `_fetch_league_game_log_nba`** — apply TTL check for active seasons; skip
   adding to `_fetched_seasons` when active.
3. **Add `ACTIVE_SEASON_TTL_HOURS` constant** at the top of `scraper.py`.
4. **Test manually** — delete the 2026 playoffs rows from `league_game_log_fetch_log`
   and hit a player page; confirm Game 3 appears.
5. **(Optional)** Add `/api/admin/refresh-active-season` route to `app.py`.
6. **(Optional)** Add a unit test mocking `date.today()` to cover `_is_active_season`.

---

## What This Does NOT Change

- Historical seasons (any completed year) retain their permanent cache — no extra
  API calls for seasons you have already fully scraped.
- BBRef per-player scraping (`bbref_playoff_fetch_log`) is unaffected; that cache
  is already player-scoped and works correctly.
- The watched-game / watch-log flow is unaffected.
