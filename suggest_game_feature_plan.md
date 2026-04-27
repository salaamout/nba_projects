# Feature Plan: "Suggest Next Game to Watch" on the Best Peaks Page

## Overview

A **"Suggest a Game"** button on the Best 3-Year page that finds the best unwatched playoff game featuring multiple players whose peak windows overlap, then surfaces it to the user.

---

## Decisions (Answered)

1. **Window size:** Always use the currently selected window on the Best 3-Year page.
2. **Pair scoring:** Rank pairs by the *weaker* player's peak score — surface pairs where even the lesser player was elite.
3. **Game appearance data:** Live-fetch from `stats.nba.com` via `nba_api` on demand, then cache into the existing `player_game_appearances` table. This is already wired up in `scraper.py` via `_get_player_appearances` / `_fetch_league_game_log_nba`. **This table is completely separate from `watched_playoff_games` — caching here cannot corrupt the watch log.**
4. **Player scope:** Only players currently visible on the Best 3-Year table (selected players).

---

## Key Technical Notes (from reading the codebase)

### Appearance data source
`scraper.py` already has a full pipeline for this:
- `_get_player_appearances(player_name, player_id, nba_id, season_year, 'playoffs', ...)` — checks `player_game_appearances` DB cache first; if empty, calls `_fetch_league_game_log_nba` which hits `stats.nba.com` with a built-in 2-second rate-limit delay and caches the results.
- The new endpoint should call this existing function directly — no new scraping code needed for the happy path.

### Pre-nba_api era (pre-~1996)
`nba_api` / `stats.nba.com` does not have complete game log data for older seasons. The scraper already notes this with a warning when `nba_api` is unavailable. For players whose peak falls before ~1996, a **Basketball Reference gamelog fallback** is needed. BBRef playoff gamelogs are at:
```
/players/{first_letter}/{bbref_id}/gamelog/{year}
```
The table ID is `pgl_basic_playoffs`. Players already have `bbref_url` stored in the DB (e.g., `/players/j/jordami01.html`), so the BBRef ID is easily derived. The existing `_get()` and `_parse_table()` helpers in `scraper.py` can handle this with no new dependencies.

### Watch log cross-reference challenge
`watched_playoff_games` stores `home_team` / `away_team` as free-text names (e.g., "Chicago Bulls") and `game_year` + `round` + `game_of_round`, but **no original game date**. Meanwhile, `player_game_appearances` stores `game_date` + `team_abbr`. Matching these requires a **team abbreviation → full name mapping**.

The scraper already has a `_BBREF_TO_NBA_ABBR` mapping (~30 teams). A reverse mapping (abbr → display name) needs to be added — it's a one-time static dict. For franchises that relocated or renamed (e.g., SEA → OKC, NJN → BKN), the mapping should use the name that was correct at the time based on `season_year`.

---

## Step-by-Step Implementation Plan

### Step 1 — Compute Overlapping Peak Pairs (Backend)

**File:** `app.py`  
**New endpoint:** `GET /api/suggest_game?window=3`

**Logic:**

1. Run the same peak-window query already used by `/api/best3year` (with the passed `window` param) to get each *selected* player's `player_id`, `name`, `best_start_year`, `best_end_year`, `best_window_total`, and `nba_id` / `bbref_url`.
2. Find all **pairs** of players whose year ranges overlap:
   ```
   overlap if:  A.best_start_year <= B.best_end_year
            AND B.best_start_year <= A.best_end_year
   ```
3. For each pair, compute the **pair score** = `min(A.best_window_total, B.best_window_total)`.
4. Sort pairs **descending** by pair score.
5. Determine each pair's **overlapping year range**: `max(start_A, start_B)` → `min(end_A, end_B)`.

---

### Step 2 — Fetch Appearances & Find an Unwatched Game

**Still in** `app.py`, iterating pairs in order.

For each pair:

#### 2a. Get appearance data for both players
For each year in the overlapping range, call `_get_player_appearances(...)` from `scraper.py` for each player. This will:
- Return immediately from DB cache if already populated.
- Otherwise call `stats.nba.com` and cache into `player_game_appearances` (safe, no watch log impact).
- If `nba_id` is `None` (older player) **and** `bbref_url` is present → fall back to scraping `pgl_basic_playoffs` table from BBRef for each needed year.

#### 2b. Find co-appearance games
```sql
SELECT a1.game_date, a1.season_year, a1.team_abbr AS team1_abbr, a2.team_abbr AS team2_abbr
FROM player_game_appearances a1
JOIN player_game_appearances a2
  ON a1.game_date = a2.game_date
 AND a1.season_year = a2.season_year
WHERE a1.player_id = :p1_id
  AND a2.player_id = :p2_id
  AND a1.season_type = 'playoffs'
  AND a1.season_year BETWEEN :overlap_start AND :overlap_end
ORDER BY a1.season_year DESC, a1.game_date DESC
```

#### 2c. Cross-reference against the watch log
For each co-appearance game, convert `team1_abbr` and `team2_abbr` to full names using a new static dict `ABBR_TO_TEAM_NAME` (see note above). Then check:
```sql
SELECT id FROM watched_playoff_games
WHERE game_year = :season_year
  AND (
    (home_team = :team1_name AND away_team = :team2_name)
    OR (home_team = :team2_name AND away_team = :team1_name)
  )
```
If **no match** → this is an unwatched game. Collect it as a candidate.

#### 2d. Rank candidates and return the best
Among unwatched co-appearance games for this pair, pick the **earliest** by `game_date` (then `season_year` as a tiebreaker). The idea is to suggest starting from the beginning of an era rather than cherry-picking the climax.

If at least one unwatched candidate is found → **return it and stop iterating pairs**.  
If none → move to next pair.

---

### Step 3 — Handle Edge Cases & No Results

| Condition | Response |
|---|---|
| Player has no `nba_id` and no `bbref_url` | `{ "result": "missing_data", "player": "..." }` — UI prompts to run a scrape |
| All pairs exhausted, no unwatched games found | `{ "result": "none", "message": "No unwatched games found for any overlapping pair." }` |
| No overlapping pairs at all | `{ "result": "none", "message": "No players with overlapping peak windows." }` |

**Successful response shape:**
```json
{
  "result": "found",
  "player1": { "name": "Michael Jordan", "id": 1, "peak": "1991–1993", "score": 4.21 },
  "player2": { "name": "Magic Johnson",  "id": 2, "peak": "1990–1992", "score": 3.88 },
  "pair_score": 3.88,
  "game": {
    "year": 1991,
    "game_date": "1991-05-25",
    "team1": "Chicago Bulls",
    "team2": "Los Angeles Lakers",
    "round": "NBA Finals",
    "round_known": true
  }
}
```

---

### Step 4 — UI on the Best 3-Year Page

**Files:** `templates/best3year_kyle.html`, `static/best3year_kyle.js`

1. Add a **"🎬 Suggest a Game"** button in the header controls area (next to the window selector).
2. On click:
   - Show the existing `#spinner`.
   - Call `GET /api/suggest_game?window=<currentWindowSize>`.
3. Display the result in an **inline card** (not a modal, to keep context visible) that appears between the header and the table:
   - **Found:** Show the two players (linked to their player pages), their overlapping peak years, pair score, and game details. Include a **"→ Watch Log"** link that navigates to `/watch_log` — no auto-pre-fill since the user may want to watch it first.
   - **Missing data:** "Appearance data missing for [Player X]. Run a scrape on their page first."
   - **None:** "All overlapping peak games have been watched. Impressive."
4. A **✕ dismiss** button closes the card.
5. Re-clicking the button while a card is showing re-runs the query (in case the watch log has been updated).

---

### Step 5 — Schema Migration (if adding `round` to `player_game_appearances`)

In `db.py` `init_db()`, add the migration:
```python
existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(player_game_appearances)").fetchall()]
if "round" not in existing_cols:
    cur.execute("ALTER TABLE player_game_appearances ADD COLUMN round TEXT")
```
This is optional but improves suggestion quality. Without it, round is displayed as "Unknown" in the result card.

---

## Files to Touch

| File | Change |
|---|---|
| `app.py` | Add `GET /api/suggest_game` endpoint; import `_get_player_appearances` from `scraper` |
| `scraper.py` | Add BBRef playoff gamelog fallback function; add `ABBR_TO_TEAM_NAME` dict |
| `templates/best3year_kyle.html` | Add button + result card HTML |
| `static/best3year_kyle.js` | Add click handler, API call, render logic |
| `static/style.css` | Styles for the result card |

---

## Build Order

1. Add `ABBR_TO_TEAM_NAME` dict to `scraper.py` (covers all historical franchises by year if needed).
2. Write the BBRef playoff gamelog fallback in `scraper.py`.
3. Write the `/api/suggest_game` endpoint in `app.py`.
4. Test the endpoint manually with `curl` before touching the frontend.
5. Add the button + card to `best3year_kyle.html` and wire up the JS.
6. Style the card in `style.css`.

---

## Open Question (Resolved)

- **Round detection for nba_api data:** Going with option (a) — display round as "Unknown" and show the date + teams instead. Revisit later if needed. This also means the `round` column migration to `player_game_appearances` (Step 5) is **not needed** for the initial build and can be skipped.
