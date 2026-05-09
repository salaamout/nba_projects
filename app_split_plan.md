# Plan: Split `app.py` into Service Modules

**Status:** Ready for review  
**Effort:** High  
**Source:** Tech Debt Review §1.1 (P2)

---

## Goal

Break `app.py` (currently ~1,984 lines) into focused modules so that each file has a single responsibility. After the split, `app.py` should contain **only Flask route definitions**, each handler ≤ ~20 lines.

---

## Target File Structure

```
nba_projects/
├── app.py                        # Flask routes only (~300 lines)
├── services/
│   ├── __init__.py
│   ├── kyle_service.py           # K.Y.L.E. computation & orchestration
│   ├── watch_log_service.py      # Watch-kyle scoring & leaderboard
│   ├── suggest_service.py        # Suggest-game candidate logic
│   └── player_service.py         # Player history, birthdate scrape-on-demand
├── db.py                         # (unchanged — connection + schema)
├── kyle.py                       # (unchanged — pure rating math)
└── scraper.py                    # (unchanged — nba_api / bbref fetching)
```

---

## What Moves Where

### `services/kyle_service.py`

Pulled from `app.py` lines ~572–821:

| Symbol | Current location | Notes |
|---|---|---|
| `_fetch_selected_player_dicts(conn, season_id)` | inline inside multiple routes | New helper; extract while moving |
| `_compute_kyle_for_season(conn, season_id, season_type, season_year)` | inline inside `cumulative_kyle` & `best3year` | New helper encapsulating per-season fetch + `kyle.calculate` |
| `_compute_peak_windows(conn, window)` | already extracted, in `app.py` | Move here |
| `cumulative_kyle()` business logic | `@app.route("/api/cumulative_kyle")` handler body | Route stays; logic moves to `kyle_service.compute_cumulative(conn)` |
| `best3year()` business logic | `@app.route("/api/best3year")` handler body | Route stays; logic moves to `kyle_service.compute_best3year(conn, window)` |
| `compute_bounds` (re-export or thin wrapper) | already in `kyle.py` | Import directly from `kyle` in callers |

**Public API:**
```python
# services/kyle_service.py
def fetch_selected_player_dicts(conn, season_id: int) -> list[dict]: ...
def compute_cumulative(conn) -> list[dict]: ...
def compute_best3year(conn, window: int = 3) -> list[dict]: ...
def compute_peak_windows(conn, window: int) -> list[dict]: ...
```

---

### `services/watch_log_service.py`

Pulled from `app.py` lines ~42–239 and ~1,749–1,870:

| Symbol | Current location | Notes |
|---|---|---|
| `ROUND_WEIGHTS` | module-level constant in `app.py` | Move here; import back into `app.py` for the few places it's needed |
| `ROUND_MAP` | module-level constant in `app.py` | Move here; unify with `ROUND_WEIGHTS` (see §1.5 of tech debt review) |
| `_get_watch_kyle_by_player(conn, season_year)` | helper in `app.py` | Move here; becomes `get_watch_kyle_by_player(...)` (drop leading underscore) |
| `best_player_leaderboard()` business logic | `@app.route("/api/watched_games/best_player_leaderboard")` handler body | Route stays; logic moves to `watch_log_service.compute_leaderboard(conn)` |
| `_attach_watch_kyle(d, wk)` | new helper (see tech debt §1.3) | Create here; fixes 6× copy-paste pattern |

**Public API:**
```python
# services/watch_log_service.py
ROUND_WEIGHTS: dict[str, int]
ROUND_MAP: dict[str, int]

def get_watch_kyle_by_player(conn, season_year: int) -> dict[int, dict]: ...
def attach_watch_kyle(d: dict, wk: dict | None) -> None: ...
def compute_leaderboard(conn) -> list[dict]: ...
```

---

### `services/suggest_service.py`

Pulled from `app.py` lines ~823–1,307:

| Symbol | Current location | Notes |
|---|---|---|
| `_suggest_cache` | module-level dict in `app.py` | Move here; wrap with `cachetools.LRUCache` (size ~128) as a separate P2 item |
| `suggest_game()` business logic | `@app.route("/api/suggest_game")` handler body | Route stays; logic moves to `suggest_service.get_suggestions(conn, window, watch_log_count)` |
| `suggest_game_for_player()` business logic | `@app.route("/api/suggest_game_for_player")` handler body | Route stays; logic moves to `suggest_service.get_suggestions_for_player(conn, player_id, window)` |
| `_find_co_appearance_games(conn, p1_id, p2_id, years)` | inline in both suggest handlers | Extract as private helper in this module |
| `_game_already_watched(conn, ...)` | inline in both suggest handlers | Extract as private helper in this module |

**Public API:**
```python
# services/suggest_service.py
def get_suggestions(conn, window: int, watch_log_count: int) -> list[dict]: ...
def get_suggestions_for_player(conn, player_id: int, window: int) -> list[dict]: ...
```

---

### `services/player_service.py`

Pulled from `app.py` lines ~1,365–1,567:

| Symbol | Current location | Notes |
|---|---|---|
| `player_history()` business logic | `@app.route("/api/player/<int:player_id>")` handler body | Route stays; logic moves to `player_service.get_player_history(conn, player_id)` |
| Birthdate scrape-on-demand block | inside `player_history()` | Move to `player_service.ensure_birthdate(conn, player_dict)` |
| `player_watch_log()` business logic | `@app.route("/api/player/<int:player_id>/watch_log")` handler body | Route stays; logic moves to `player_service.get_player_watch_log(conn, player_id)` |

**Public API:**
```python
# services/player_service.py
def get_player_history(conn, player_id: int) -> dict: ...
def ensure_birthdate(conn, player_dict: dict) -> None: ...
def get_player_watch_log(conn, player_id: int) -> dict: ...
```

---

## What Stays in `app.py`

After the split, `app.py` retains:

- Flask `app` creation and `logging` setup
- `with app.app_context(): init_db()`
- All `@app.route(...)` definitions — each handler calls one service function and returns `jsonify(result)`
- `_row_to_dict` (small utility, widely used by routes)
- Removal of `_game_row_to_dict` (it's a no-op alias for `_row_to_dict` — delete it at the same time, §1.4 of tech debt)

---

## Step-by-Step Execution Order

Tackle one service module at a time. Each step is independently committable and testable.

### Step 1 — Create `services/` package skeleton
- Create `services/__init__.py` (empty)
- No logic moved yet — just validates the import path works

### Step 2 — Extract `watch_log_service.py`
This is the smallest and most self-contained chunk.

1. Move `ROUND_WEIGHTS` and `ROUND_MAP` into `watch_log_service.py`; unify them into a single dict with both the weight (for `suggest` / `cumulative`) and the display rank (for SQL).
2. Move `_get_watch_kyle_by_player` → `get_watch_kyle_by_player`.
3. Add `attach_watch_kyle(d, wk)` helper to eliminate the 6× copy-paste.
4. Move `best_player_leaderboard` body → `compute_leaderboard(conn)`.
5. In `app.py`: import and delegate. Replace the 6 copy-paste blocks with `attach_watch_kyle`.
6. Run `tests/test_kyle.py` to confirm nothing broken; spot-test the leaderboard endpoint.

### Step 3 — Extract `kyle_service.py`
1. Create `fetch_selected_player_dicts(conn, season_id)` (pulled from the repeated `SELECT … FROM selected_players` inline query).
2. Create `compute_cumulative(conn)` by lifting the body of `cumulative_kyle()`.
3. Move `_compute_peak_windows` → `compute_peak_windows` (already extracted; just relocate).
4. Create `compute_best3year(conn, window)` by lifting the body of `best3year()`.
5. In `app.py`: import and delegate.
6. Run the existing test suite; manually test `/api/cumulative_kyle` and `/api/best3year`.

### Step 4 — Extract `player_service.py`
1. Create `ensure_birthdate(conn, player_dict)` with the scrape-on-demand block.
2. Create `get_player_history(conn, player_id)` from the `player_history()` handler body.
3. Create `get_player_watch_log(conn, player_id)` from the `player_watch_log()` handler body.
4. In `app.py`: import and delegate. Move `from scraper import scrape_player_birthdate` to the top of `player_service.py` (fixes tech debt §9 style item).
5. Manually test `/api/player/<id>` and `/api/player/<id>/watch_log`.

### Step 5 — Extract `suggest_service.py`
This is the largest and most complex step — save it for last.

1. Move `_suggest_cache` into `suggest_service.py`.
2. Extract `_find_co_appearance_games(conn, p1_id, p2_id, years)` as a shared private helper.
3. Extract `_game_already_watched(conn, ...)` as a shared private helper.
4. Create `get_suggestions(conn, window, watch_log_count)` from the `suggest_game()` body.
5. Create `get_suggestions_for_player(conn, player_id, window)` from the `suggest_game_for_player()` body.
6. In `app.py`: import and delegate.
7. Manually test both suggest endpoints end-to-end.

### Step 6 — Final cleanup in `app.py`
- Delete `_game_row_to_dict` (no-op alias — replace its ~3 call sites with `_row_to_dict`).
- Move `from collections import defaultdict` to the top if it's still present.
- Verify `app.py` is ≤ ~350 lines.
- Run the full test suite.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Circular imports (`app.py` → `service` → `app.py`) | Services must **not** import from `app`. They only import from `db`, `kyle`, and `scraper`. |
| Shared state (`_suggest_cache`) broken by move | Move the cache dict at the same time as the logic that writes/reads it — do not split across steps. |
| Flask `g` / `request` context used inside service functions | Services should receive `conn` as a parameter and must not call `request` or `g` directly. Route handlers read request args and pass plain values. |
| Regression in suggest endpoints (most complex logic) | Extract suggest last (Step 5) after the simpler services are proven stable. |

---

## Definition of Done

- [ ] `app.py` ≤ 350 lines; every handler body ≤ ~20 lines
- [ ] `services/kyle_service.py`, `watch_log_service.py`, `suggest_service.py`, `player_service.py` all exist with documented public functions
- [ ] The 6× `watch_kyle` copy-paste replaced by `attach_watch_kyle()`
- [ ] `_game_row_to_dict` deleted
- [ ] `from collections import defaultdict` and `from scraper import scrape_player_birthdate` moved to module-level imports
- [ ] All existing tests pass
- [ ] All endpoints manually smoke-tested (seasons, cumulative, best3year, both suggest, player history, watch log, leaderboard)
- [ ] `tech_debt_review.md` P2 item "Split `app.py` into service modules" marked ✅
