# Technical Debt Review — `nba_projects`

**Reviewed by:** GitHub Copilot (Principal Engineer)  
**Date:** May 9, 2026  
**Codebase:** `app.py` (2,224 lines), `scraper.py` (1,252 lines), `kyle.py` (255 lines), `db.py` (158 lines)

---

## Executive Summary

The project is well-commented and functionally solid, but it has accumulated significant structural debt. The two most pressing issues are (1) **`app.py` has become a monolith** — it mixes routing, business logic, and data access — and (2) **major business-logic blocks are duplicated verbatim** across three different endpoints. Addressing those two items would make everything else (testing, documentation, performance) much easier.

---

## 1. Architecture & Code Organization

### 1.1 `app.py` is far too large (2,224 lines)
**Severity: High** (DONE)

Flask route handlers contain complex business logic that should live in service or helper modules. This makes the file hard to navigate, hard to test, and dangerous to change.

**Recommended split:**

| New module | Responsibility |
|---|---|
| `services/kyle_service.py` | `calculate`, `calculate_all`, `compute_bounds`, cumulative/best-N-year orchestration |
| `services/watch_log_service.py` | `_get_watch_kyle_by_player`, leaderboard computation |
| `services/suggest_service.py` | `suggest_game`, `suggest_game_for_player` candidate building |
| `services/player_service.py` | Player history, birthdate scrape-on-demand |
| `app.py` (slimmed) | Flask route definitions only — each handler ≤ ~20 lines |

### 1.2 Peak-window computation is copy-pasted three times
**Severity: High** (DONE)

The "iterate seasons → compute K.Y.L.E. per year → find best N-year window" logic appears nearly identically in:
- `/api/best3year` (~120 lines)
- `/api/suggest_game` (~90 lines)
- `/api/suggest_game_for_player` (~90 lines)

**Action:** Extract a single `compute_peak_windows(conn, window) -> list[PeakEntry]` function in a service module. All three callers use the result.

### 1.3 `watch_kyle` dict-spreading is copy-pasted six times
**Severity: Medium**

The pattern below appears in at least six places across `app.py`:

```python
wk = watch_map.get(d["player_id"])
d["watch_kyle"]          = wk["watch_kyle"]    if wk else None
d["watch_best_count"]    = wk["best_count"]    if wk else None
d["watch_total_watched"] = wk["total_watched"] if wk else None
d["watch_raw_score"]     = wk["raw_score"]     if wk else None
```

**Action:** Extract a one-liner helper `_attach_watch_kyle(d, wk)`.

### 1.4 `_game_row_to_dict` is a no-op alias
**Severity: Low**

```python
def _game_row_to_dict(row):
    d = dict(row)
    return d
```

This is identical to `_row_to_dict`. Remove it and use `_row_to_dict` (or just `dict(row)`) everywhere.

### 1.5 `ROUND_MAP` and `ROUND_WEIGHTS` are defined independently
**Severity: Low**

`ROUND_WEIGHTS` is defined at the top of `app.py` (values 1/2/4/8) while the SQL inside `_get_watch_kyle_by_player` and `best_player_leaderboard` embeds values 1/2/3/4 inline. The leaderboard endpoint also defines its own `ROUND_KEY` dict. These should all reference a single source-of-truth constant.

---

## 2. Duplicated SQL Queries

### 2.1 Selected-player fetch query is repeated ≥ 5 times
**Severity: High** (DONE)

The following query (with minor field-set variations) is repeated across `get_selected`, `get_all_players`, `cumulative_kyle`, `best3year`, `suggest_game`, and `suggest_game_for_player`:

```sql
SELECT p.id AS player_id, p.name, ps.minutes, ps.usage_rate, ...
FROM selected_players sp
JOIN players p ON p.id = sp.player_id
JOIN player_stats ps ON ps.player_id = sp.player_id AND ps.season_id = sp.season_id
WHERE sp.season_id = ?
```

**Action:** Extract `_fetch_selected_player_dicts(conn, season_id) -> list[dict]` in `db.py` or a repository module.

### 2.2 Co-appearance query is duplicated in two suggest endpoints
**Severity: Medium**

The `ROW_NUMBER() OVER (...)` co-appearance JOIN between `player_game_appearances` rows exists almost identically in both suggest endpoints. Extract into `_find_co_appearance_games(conn, p1_id, p2_id, years) -> list[Row]`.

### 2.3 Watched-game lookup (the `t1_ph`/`t2_ph` dynamic SQL) is duplicated
**Severity: Medium**

The dynamically constructed `IN (?)` watched-game check with team-name variants is duplicated between the two suggest endpoints. Extract into `_game_already_watched(conn, season_year, game_of_round, team1_variants, team2_variants) -> bool`.

---

## 3. Database Layer

### 3.1 Connections are opened and closed manually everywhere
**Severity: High**

Every route handler calls `get_conn()` and then manually calls `conn.close()` (or wraps in `try/finally`). This is error-prone — a `return` before the `finally` block, or a second `conn2 = get_conn()` inside a function, can leave connections open.

**Action:** Use a context manager or Flask's `g` object:

```python
# db.py
from contextlib import contextmanager

@contextmanager
def db_conn():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()
```

Then in routes:
```python
with db_conn() as conn:
    ...
```

Note: `best_player_leaderboard` currently closes the connection and then opens a second one (`conn2`) in the same request — a classic leaked-connection bug.

### 3.2 Schema migrations are run at startup with no versioning
**Severity: Medium**

`init_db()` manually checks for column presence with `PRAGMA table_info(...)` to run `ALTER TABLE` migrations. As the schema grows, this becomes unmanageable and fragile (e.g., column-type changes are impossible via `ALTER TABLE` in SQLite).

**Action:** Adopt a lightweight migration tool such as [Alembic](https://alembic.sqlalchemy.org/) or a simple integer `PRAGMA user_version` migration runner.

### 3.3 Missing indexes
**Severity: Medium**

Queries that filter on `season_id`, `player_id`, `season_year`, and `season_type` are run frequently. The `CREATE TABLE` statements do not define any secondary indexes.

**Suggested indexes to add in `init_db()`:**
```sql
CREATE INDEX IF NOT EXISTS idx_player_stats_season   ON player_stats(season_id);
CREATE INDEX IF NOT EXISTS idx_player_stats_player   ON player_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_selected_season       ON selected_players(season_id);
CREATE INDEX IF NOT EXISTS idx_pga_player_year_type  ON player_game_appearances(player_id, season_year, season_type);
CREATE INDEX IF NOT EXISTS idx_wpg_game_year         ON watched_playoff_games(game_year);
CREATE INDEX IF NOT EXISTS idx_wgp_game_player       ON watched_game_players(game_id, player_id);
```

### 3.4 2026 Regular Season is hard-coded as a seed row
**Severity: Low**

```python
# db.py line ~148
cur.execute("SELECT id FROM seasons WHERE season_year = 2026 AND season_type = 'regular'")
```

This will need manual updating every year and does not belong in generic schema initialization logic. Remove the seed row or make the year configurable via an environment variable.

---

## 4. In-Memory Cache

### 4.1 `_suggest_cache` has no size limit or TTL
**Severity: Medium** (DONE)

The server-side `_suggest_cache` dict grows unboundedly. Each cache entry stores a full list of candidate dicts. Over time (many window/player-set combinations) this could consume significant memory.

**Action:** Use `functools.lru_cache` or a simple `cachetools.TTLCache`/`LRUCache` wrapper with a reasonable max-size.

### 4.2 Process-level caches (`_fetched_seasons`, `_p_mode_cache`) are not thread-safe
**Severity: Medium** (DONE)

Flask can run with multiple threads (default in dev; Gunicorn workers in prod). The module-level `set` and `dict` caches in `scraper.py` are mutated without any locking. Two concurrent requests for the same season could race to fetch from the nba_api and double-insert rows (mitigated by `INSERT OR IGNORE`, but still racy).

**Action:** Protect mutations with a `threading.Lock`, or move to a proper cache layer.

---

## 5. Performance

### 5.1 `cumulative_kyle` and `best3year` re-compute K.Y.L.E. from scratch on every request
**Severity: High**

Both endpoints loop over every selected season, re-fetch all player rows, re-compute K.Y.L.E. ratings, and rebuild the entire dataset on every HTTP request. With many seasons this is slow.

**Action:** Cache computed season ratings (keyed by `(season_id, season_type, watch_log_count)`) so repeated calls within the same "state" are served from memory. The suggest-game endpoint already does this correctly — apply the same pattern to cumulative and best3year.

### 5.2 `best_player_leaderboard` has an O(n²) inner loop
**Severity: Medium**

The per-year `watch_kyle` normalisation inside `best_player_leaderboard` uses a nested `next(... for r in rows if ...)` lookup (O(n) per player-year), making the total complexity O(players × years). Refactor to build a `{(player_id, year): total_watched}` dict before the loop.

### 5.3 `_get_watch_kyle_by_player` is called repeatedly for the same year
**Severity: Medium**

In `best3year` and `suggest_game_for_player`, `_get_watch_kyle_by_player(conn, season_year)` is called in a loop once per playoff season, even though results don't change within a request. The result should be memoized per request (a local dict keyed by year is sufficient).

### 5.4 Appearance-cache checks inside `suggest_game` fire one query per player per year
**Severity: Medium**

The appearance-fetch loop in both suggest endpoints runs `SELECT 1 FROM player_game_appearances WHERE player_id=? AND season_year=? ...` for every (player, year) pair in the overlap. For large overlapping windows with many players this is many round-trips. Batch with a single `WHERE (player_id, season_year) IN (...)` query.

---

## 6. Error Handling & Robustness

### 6.1 `remove_selected` does not check whether the row exists
**Severity: Low**

```python
@app.route("/api/selected/<int:selected_id>", methods=["DELETE"])
def remove_selected(selected_id):
    conn.execute("DELETE FROM selected_players WHERE id = ?", (selected_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})
```

No 404 is returned when `selected_id` doesn't exist. Be consistent with `delete_season` which checks `rowcount`.

### 6.2 `patch_stats` uses f-string interpolation for SQL column names
**Severity: Medium**

```python
set_clause = ", ".join(f"{k} = ?" for k in updates)
conn.execute(f"UPDATE player_stats SET {set_clause} WHERE id = ?", values)
```

While `allowed` whitelist mitigates injection risk, this pattern is fragile. Prefer explicit mapping to literal SQL strings.

### 6.3 `suggest_game` catches all exceptions with a bare `except Exception`
**Severity: Low**

The broad try/except at the bottom of `suggest_game` and `suggest_game_for_player` swallows programming errors (e.g., `AttributeError`, `KeyError`) and makes debugging harder. Log at `ERROR` level (already done via `logger.exception`) and be as specific as possible about which exceptions are expected.

### 6.4 Scraper birthdate fetch silently swallows all exceptions
**Severity: Low**

```python
try:
    bd = scrape_player_birthdate(player_dict["bbref_url"])
    ...
except Exception:
    pass
```

At minimum, log the exception so silent failures are visible.

---

## 7. Testing — There Are No Tests

**Severity: High**

There are zero test files in the project. The following areas are highest priority for coverage:

### 7.1 `kyle.py` — Pure logic, easy to test (DONE)

`kyle.py` has no I/O and contains the core rating algorithm. This should have near-100% coverage.

**Suggested test cases (`tests/test_kyle.py`):**

| Test | Scenario |
|---|---|
| `test_calculate_basic` | 2 players, all fields populated → verify `kyle_rating` is between −N and +N |
| `test_lower_is_better_turnover` | Player with lower `turnover_pct` gets a higher norm |
| `test_special_worst_minutes` | Minutes worst = max/2; player at max gets norm = +1 |
| `test_on_off_asterisk_substitution` | Asterisk'd player gets `on_off_diff_norm` = average of other norms |
| `test_clamp_in_calculate` | `calculate()` clamps norms to [-1, +1] |
| `test_no_clamp_in_calculate_all` | `calculate_all()` allows norms outside [-1, +1] |
| `test_playoffs_excludes_minutes` | `calculate(season_type='playoffs')` sets `minutes_norm = None` |
| `test_watch_kyle_added_to_total` | `watch_kyle` is included in `kyle_rating` sum |
| `test_single_player` | 1 player → all norms = 0, `kyle_rating` = 0 |
| `test_all_none_fields` | All fields None → `kyle_rating = None` |
| `test_least_squares_basic` | Simple win/loss list → scores reflect wins |
| `test_least_squares_empty` | Empty comparisons → `{}` |

### 7.2 `db.py` — Schema and migrations

```
tests/test_db.py
```

- `test_init_db_creates_tables` — verify all expected tables exist after `init_db()`
- `test_init_db_idempotent` — calling `init_db()` twice does not raise or duplicate data
- `test_migrations_add_columns` — columns added by migrations are present
- `test_foreign_keys_on` — `PRAGMA foreign_keys` is ON

Use an in-memory SQLite database (`":memory:"`) via a fixture to avoid touching `nba.db`.

### 7.3 `scraper.py` — Scraping helpers

```
tests/test_scraper.py
```

Mock `requests.get` with `unittest.mock.patch` or `responses`:

- `test_parse_table_basic` — given a minimal HTML table, verify correct list-of-dicts output
- `test_parse_table_skips_header_rows` — rows where `player == "Player"` are excluded
- `test_parse_table_uncomments_tables` — HTML comment-wrapped tables are found
- `test_safe_float` — None, empty string, "—", and valid floats handled correctly
- `test_get_429_retry` — mock 429 responses → verify retry/backoff and `RateLimitError`
- `test_nba_season_str` — `1978 → "1977-78"`, `2000 → "1999-00"`, etc.
- `test_abbr_to_team_name_with_year` — `"CHA", 1999` → `"Charlotte Hornets"`, `"CHA", 2020` → `"Charlotte Hornets"`
- `test_bbref_to_nba_abbr` — "PHO" → "PHX", passthrough for unlisted

### 7.4 `app.py` — API endpoints (DONE)

```
tests/test_app.py
```

Use Flask's built-in `app.test_client()` with an in-memory DB fixture:

- `test_get_seasons_empty` — returns `[]` with empty DB
- `test_create_season_success` — POST creates row, returns 201
- `test_create_season_duplicate` — returns 409
- `test_create_season_bad_type` — returns 400
- `test_delete_season` — removes season and cascades
- `test_get_selected_empty` — returns `[]`
- `test_add_and_remove_selected` — round-trip add/delete
- `test_get_all_players_no_season` — returns 400
- `test_patch_stats_defense` — PATCH updates defense field
- `test_patch_stats_disallowed_field` — disallowed field returns 400
- `test_create_watched_game` — POST creates game, links players
- `test_delete_watched_game_not_found` — returns 404

---

## 8. Documentation

### 8.1 Public functions in `scraper.py` lack docstrings
**Severity: Medium**

`_scrape_advanced`, `_scrape_totals`, `_scrape_pbp`, `_safe_float`, `run_scrape`, and `_compute_pre97_on_off` have no docstrings. `kyle.py` and `db.py` are well-documented by comparison.

**Action:** Add NumPy-style or Google-style docstrings to at least the public-facing functions (`run_scrape`, `abbr_to_team_name`, `abbr_to_team_name_variants`). Internal helpers need at minimum a one-line description.

### 8.2 API endpoints lack OpenAPI / docstring-level documentation
**Severity: Low**

Many route handlers have good inline comments, but there is no machine-readable API contract (OpenAPI/Swagger). For a project of this size, even a simple `docs/api.md` listing all endpoints, parameters, and response shapes would be valuable.

### 8.3 `README.md` does not describe the data model or the K.Y.L.E. formula
**Severity: Low**

New contributors (or future-you) will not understand what fields like `on_off_asterisk`, `watch_kyle`, or `points_per_shot` mean without reading the source. The `kyle.py` module docstring is good — surface it in the README.

---

## 9. Code Style & Minor Cleanups

| Item | File | Suggestion |
|---|---|---|
| `from collections import defaultdict` is imported **inside** two functions | `app.py` | Move to top-level imports |
| `from scraper import scrape_player_birthdate` is imported inside a route handler | `app.py` | Move to top of file |
| `import datetime` is done inside `_record_league_game_log_fetch` | `scraper.py` | `datetime` is already imported at the top |
| `from __future__ import annotations` is in `scraper.py` but nowhere else | — | Apply consistently or remove |
| Magic constant `MIN_GAMES_WATCHED = 5` is defined inside `best3year` | `app.py` | Move to module-level constant with a comment |
| `ZERO_YEAR` dict is re-defined inside two separate loops | `app.py` | Define once at module level |
| `_ABBR_TO_TEAM_NAME_BY_YEAR` has duplicate abbreviations for both bbref and nba_api variants (e.g. `"PHX"` and `"PHO"`, `"GOS"` and `"GSW"`) | `scraper.py` | Add a comment explaining why duplicates exist to avoid future confusion |
| `2099` is used as the end-year sentinel for "still active" teams | `scraper.py` | Define a constant `_CURRENT_ERA = 2099` |

---

## 10. Prioritized Action Plan

| Priority | Item | Effort |
|---|---|---|
| ✅ P1 | ~~Add unit tests for `kyle.py`~~ — **Done.** `tests/test_kyle.py` implements all 12 cases from §7.1. | Low — pure functions, no mocks needed |
| ✅ P1 | ~~Extract `compute_peak_windows()` — eliminate 3× duplication~~ — **Done.** `_compute_peak_windows(conn, window)` added to `app.py`; all three callers (`best3year`, `suggest_game`, `suggest_game_for_player`) now delegate to it. | Medium |
| ✅ P1 | ~~Add context-manager DB connection handling~~ — **Done.** `db_conn()` context manager added to `db.py`; all ~25 route handlers in `app.py` converted from manual `get_conn()`/`conn.close()` to `with db_conn() as conn:`. Leaked-connection bugs in `best3year`, `player_watch_log`, `suggest_game`, and `suggest_game_for_player` fixed in the process. | Low |
| ✅ P1 | ~~Add missing DB indexes~~ — **Done.** Six `CREATE INDEX IF NOT EXISTS` statements added to `init_db()` in `db.py` covering `player_stats`, `selected_players`, `player_game_appearances`, `watched_playoff_games`, and `watched_game_players`. | Low |
| ✅ P2 | ~~Split `app.py` into service modules~~ — **Done.** `services/kyle_service.py`, `watch_log_service.py`, `player_service.py`, and `suggest_service.py` created; `app.py` reduced from 1,984 → 683 lines (routes only). `attach_watch_kyle()` eliminates the 6× copy-paste; `_game_row_to_dict` deleted; `_suggest_cache` moved to `suggest_service`; `from collections import defaultdict` and `from scraper import scrape_player_birthdate` moved to module-level imports in their respective service files. All 13 tests pass. | High |
| ✅ P2 | ~~Extract `_fetch_selected_player_dicts()` helper~~ — **Done.** `fetch_selected_player_dicts()` expanded to include all fields (`selected_id`, `stats_id`, `position`); inline SQL in `get_selected` and `get_all_players` replaced with single call to `kyle_service.fetch_selected_player_dicts()`. All 13 tests pass. | Low |
| ✅ P2 | ~~Add Flask API tests with in-memory DB~~ — **Done.** `tests/test_app.py` implements 16 tests covering seasons (CRUD, duplicate, bad-type), selected players (add/remove round-trip), stats patching (allowed and disallowed fields), and watched games (create with players, delete, 404 handling). All 16 pass. | Medium |
| ✅ P2 | ~~Fix `_suggest_cache` size limit~~ — **Done.** `_suggest_cache` converted from an unbounded `dict` to a 128-entry LRU cache (`OrderedDict`) with `_cache_get`/`_cache_set` helpers that evict the oldest entry when the limit is exceeded. All 29 tests pass. | Low |
| ✅ P2 | ~~Fix thread-safety on module-level caches~~ — **Done.** `import threading` added to `scraper.py`; `_fetched_seasons_lock` and `_fetched_bbref_seasons_lock` (`threading.Lock`) added alongside their respective caches. All reads and writes to `_fetched_seasons`, `_p_mode_cache`, and `_fetched_bbref_seasons` are now wrapped with `with lock:` blocks, eliminating the check-then-act race under concurrent Flask/Gunicorn threads. All 29 tests pass. | Medium |
| 🟡 P3 | Cache cumulative/best3year computed ratings | Medium |
| 🟡 P3 | Add docstrings to `scraper.py` public functions | Low |
| 🟡 P3 | Unify `ROUND_WEIGHTS` / `ROUND_MAP` / `ROUND_KEY` constants | Low |
| 🟡 P3 | Adopt schema migration versioning | Medium |
| 🟢 P4 | Write API docs (`docs/api.md`) | Low |
| 🟢 P4 | Remove 2026 hard-coded seed row from `init_db()` | Low |
| 🟢 P4 | Remove `_game_row_to_dict` no-op | Low |
