# Plan: Fix Corrupt bbref_playoff_fetch_log Entries (Rate-Limited Data)

## Root Cause

The `bbref_playoff_fetch_log` table is used as a "don't re-fetch" guard: once a
`(player_id, season_year)` row exists, the scraper assumes the BBRef gamelog was
successfully loaded and never retries it.

**The problem:** a row is written to `bbref_playoff_fetch_log` in *every* code
path inside `_get_player_playoff_appearances_bbref`, including failure paths:

1. **Exception during fetch** – if `_get()` raises (e.g. after exhausting
   retries on 429 responses), the `except` block catches it, logs a warning, and
   still writes the "already fetched" row. No game appearances are inserted.
2. **No table found on the page** – if Basketball Reference returned a rate-limit
   page or redirect instead of the real page, `_parse_table()` raises `ValueError`
   ("Table #pgl_basic_playoffs not found on page"), and the `except ValueError`
   block again writes the row and returns an empty set.
3. **Zero rows parsed** – even on a real successful response, if parsing yields
   no usable rows (e.g. because the response HTML was truncated / garbled), the
   row is still committed with no appearances.

After any of these failures the player+year is permanently locked out of
re-fetching, so the suggest-game feature sees no appearances and cannot propose
those games.

---

## Affected Data (Immediate Problem)

Players like Hakeem Olajuwon and Gary Payton (and likely all pre-1997 players
whose BBRef gamelog was fetched during a period of heavy rate-limiting) have rows
in `bbref_playoff_fetch_log` but zero (or incomplete) rows in
`player_game_appearances` for those years.

---

## Fix Plan

### Step 1 – Schema migration: add `fetch_status` to `bbref_playoff_fetch_log`

Alter the table to add a `fetch_status TEXT NOT NULL DEFAULT 'success'` column
and an optional `fetched_at TEXT` timestamp column.

```sql
ALTER TABLE bbref_playoff_fetch_log ADD COLUMN fetch_status TEXT NOT NULL DEFAULT 'success';
ALTER TABLE bbref_playoff_fetch_log ADD COLUMN fetched_at   TEXT;
```

Possible values for `fetch_status`:
- `'success'` – page fetched and parsed correctly; result may legitimately be
  empty (player truly had no playoff games).
- `'error'` – HTTP error or exception during fetch (should be retried).
- `'no_table'` – page loaded but `pgl_basic_playoffs` table was missing (likely
  rate-limit or redirect page; should be retried).

Add this migration to `db.py → init_db()` (alongside the existing column
migrations) so it runs automatically on next startup.

### Step 2 – Update `_get_player_playoff_appearances_bbref` in `scraper.py`

**2a. Only skip re-fetching for `fetch_status = 'success'`.**

Change the DB guard check from:
```python
already_fetched = conn.execute(
    "SELECT 1 FROM bbref_playoff_fetch_log WHERE player_id=? AND season_year=?", ...
).fetchone()
if already_fetched:
    ...
```
to:
```python
already_fetched = conn.execute(
    "SELECT fetch_status FROM bbref_playoff_fetch_log WHERE player_id=? AND season_year=?", ...
).fetchone()
if already_fetched and already_fetched["fetch_status"] == "success":
    ...
# If status is 'error' or 'no_table', fall through and retry.
```

Do the same for the in-memory `_fetched_bbref_seasons` set — it should only be
populated on a *successful* fetch.

**2b. Write the correct status on each code path.**

- On `except Exception` (fetch failure): write `fetch_status='error'` and do
  **not** add to `_fetched_bbref_seasons`.
- On `except ValueError` (no table found): write `fetch_status='no_table'` and
  do **not** add to `_fetched_bbref_seasons`.
- On successful parse: write `fetch_status='success'` and add to
  `_fetched_bbref_seasons`.

**2c. Add a sanity check for "suspiciously empty" successful responses.**

After parsing rows but before writing `'success'`, check whether the page looks
like a real BBRef gamelog or a rate-limit / anti-bot page. A simple heuristic:
if the soup's `<title>` contains "429", "Too Many Requests", or "Access Denied",
treat it as `'no_table'` and do not mark as success.

```python
page_title = (soup.find("title") or {}).get_text(strip=True).lower()
if any(x in page_title for x in ("429", "too many requests", "access denied", "robot")):
    logger.warning("Rate-limit page detected for %s year=%s", bbref_id, season_year)
    conn.execute(
        "INSERT INTO bbref_playoff_fetch_log (player_id, season_year, fetch_status, fetched_at) "
        "VALUES (?,?,?,?) ON CONFLICT(player_id, season_year) DO UPDATE SET fetch_status=excluded.fetch_status, fetched_at=excluded.fetched_at",
        (player_id, season_year, "no_table", datetime.utcnow().isoformat()),
    )
    conn.commit()
    return set()
```

### Step 3 – One-time DB repair: mark known-bad rows for retry

Write a small standalone script (`repair_fetch_log.py`) that uses **two
complementary queries** to identify corrupt entries:

**Query A — Definitive corruption (strongest signal):** Any
`(player_id, season_year)` that is marked as fetched but has
`player_stats.playoff_games > 0` (the DB already knows the player played
playoff games that year) yet has **zero** rows in `player_game_appearances`
for `season_type='playoffs'`. This is unambiguous — the player definitely
played, and we have no game-level records for them.

**Query B — Possible corruption (weaker signal, opt-in):** Any
`(player_id, season_year)` marked as fetched that has zero appearances
*and* `player_stats.playoff_games IS NULL` (playoff_games count not yet
loaded). These are ambiguous — the player may have had no playoff games —
so they are only reset if `--include-unknown` is passed.

```python
# repair_fetch_log.py
import sqlite3, argparse
from db import DB_PATH

def repair(min_year=1947, max_year=9999, include_unknown=False):
    conn = sqlite3.connect(DB_PATH)

    # Query A: definitive — playoff_games > 0 but no appearances
    definitive = conn.execute("""
        SELECT f.player_id, f.season_year
        FROM bbref_playoff_fetch_log f
        JOIN seasons s ON s.season_year = f.season_year AND s.season_type = 'playoffs'
        JOIN player_stats ps ON ps.player_id = f.player_id AND ps.season_id = s.id
        LEFT JOIN player_game_appearances pga
               ON pga.player_id  = f.player_id
              AND pga.season_year = f.season_year
              AND pga.season_type = 'playoffs'
        WHERE ps.playoff_games > 0
          AND pga.id IS NULL
          AND f.season_year BETWEEN ? AND ?
    """, (min_year, max_year)).fetchall()
    print(f"[Definitive] {len(definitive)} rows with playoff_games > 0 but no appearances — marking for retry")

    rows_to_fix = list(definitive)

    if include_unknown:
        # Query B: ambiguous — no appearances, playoff_games unknown
        unknown = conn.execute("""
            SELECT f.player_id, f.season_year
            FROM bbref_playoff_fetch_log f
            LEFT JOIN player_game_appearances pga
                   ON pga.player_id  = f.player_id
                  AND pga.season_year = f.season_year
                  AND pga.season_type = 'playoffs'
            LEFT JOIN (
                SELECT ps.player_id, s.season_year
                FROM player_stats ps
                JOIN seasons s ON s.id = ps.season_id AND s.season_type = 'playoffs'
                WHERE ps.playoff_games > 0
            ) has_games ON has_games.player_id = f.player_id AND has_games.season_year = f.season_year
            WHERE pga.id IS NULL
              AND has_games.player_id IS NULL
              AND f.season_year BETWEEN ? AND ?
        """, (min_year, max_year)).fetchall()
        print(f"[Unknown]    {len(unknown)} rows with no appearances and unknown playoff_games — marking for retry")
        rows_to_fix += list(unknown)

    # Deduplicate
    rows_to_fix = list({(r[0], r[1]) for r in rows_to_fix})
    conn.executemany(
        "UPDATE bbref_playoff_fetch_log SET fetch_status='no_table' WHERE player_id=? AND season_year=?",
        rows_to_fix
    )
    conn.commit()
    conn.close()
    print(f"Done. {len(rows_to_fix)} total rows reset to fetch_status='no_table'.")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--min-year", type=int, default=1947)
    p.add_argument("--max-year", type=int, default=9999)
    p.add_argument("--include-unknown", action="store_true",
                   help="Also reset rows where playoff_games is unknown (ambiguous)")
    args = p.parse_args()
    repair(args.min_year, args.max_year, args.include_unknown)
```

The default invocation (`python repair_fetch_log.py`) will only reset the
unambiguous cases where `playoff_games > 0` proves the player definitely
played and the data is corrupt.

### Step 4 – Improve rate-limit handling in `_get()`

Currently `_get()` retries up to 5 times with exponential backoff (60s, 120s,
240s, …). After all retries it calls `raise_for_status()` and lets the exception
propagate. This is fine. However, consider:

- **Raise a custom `RateLimitError`** (subclass of `Exception`) when all retries
  are exhausted on a 429, distinct from other HTTP errors. This lets callers
  distinguish a transient network failure from a BBRef rate-limit and log it more
  clearly.
- **Add a jittered sleep** (e.g. `time.sleep(3 + random.uniform(0, 2))`) after
  every *successful* 200 response in `_get_player_playoff_appearances_bbref` to
  avoid triggering rate limits in the first place during bulk pre-1997 backfills.

### Step 5 – Validate data completeness after a bulk backfill

Extend `repair_fetch_log.py` with a `--check` mode that queries:

```sql
SELECT p.name, ps.playoff_games, COUNT(pga.id) AS appearances, f.season_year
FROM bbref_playoff_fetch_log f
JOIN players p ON p.id = f.player_id
JOIN seasons s ON s.season_year = f.season_year AND s.season_type = 'playoffs'
JOIN player_stats ps ON ps.player_id = f.player_id AND ps.season_id = s.id
LEFT JOIN player_game_appearances pga
       ON pga.player_id = f.player_id AND pga.season_year = f.season_year AND pga.season_type = 'playoffs'
WHERE f.fetch_status = 'success'
GROUP BY f.player_id, f.season_year
HAVING ps.playoff_games > 0 AND COUNT(pga.id) = 0
```

Any row returned here is a post-fix anomaly — `player_stats.playoff_games`
confirms the player played, the fetch was marked successful, but no game-level
rows exist. This catches future partial-parse corruption and should be run after
every bulk pre-1997 backfill as a sanity check.

---

## Execution Order

1. Run `repair_fetch_log.py` (Step 3) immediately to unlock the bad rows in the
   current DB.
2. Apply schema migration in `db.py` (Step 1) and code changes in `scraper.py`
   (Steps 2 & 4).
3. Restart the app — the next time the suggest-game feature triggers a lookup for
   a pre-1997 player (e.g. Hakeem Olajuwon 1996), the `fetch_status='no_table'`
   guard will allow a retry.
4. Optionally run `repair_fetch_log.py` again after the first retry wave to
   confirm all rows have been promoted to `'success'`.

---

## Files to Change

| File | Change |
|------|--------|
| `db.py` | Add `fetch_status` + `fetched_at` column migrations in `init_db()` |
| `scraper.py` | Update `_get_player_playoff_appearances_bbref` to use status-aware guard; add rate-limit page detection; add custom `RateLimitError` in `_get()` |
| `repair_fetch_log.py` | **New file** — one-time repair script (can be re-run safely) |
