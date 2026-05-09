# Feature Plan: "Suggest a Game" on Individual Player Pages + Watch Log Quick-Add

## Overview

Two related features:

1. **Player-page suggest:** A "🎬 Suggest a Game" button on each player's profile page. A small window-size picker appears, then the backend finds the best unwatched playoff game featuring that player against the highest-peak opponent (drawn from currently selected players). A result card with a Skip/Next button is displayed inline. Includes a "📋 Log This Game" quick-add button.

2. **Watch Log quick-add (both pages):** The existing Best 3-Year suggest card and the new player-page suggest card both gain a "📋 Log This Game" button. Clicking it navigates to `/watch_log` with pre-fill query params; `watch_log.js` detects these on boot and auto-opens the modal pre-populated with the game's teams, year, and round.

---

## Decisions

| # | Question | Answer |
|---|---|---|
| 1 | Opponent pool | Currently selected players only (same pool as the Best 3-Year page) |
| 2 | Peak window | User picks **length only**; backend finds the best window of that length for each player independently |
| 3 | Skip/Next | Cycles through next best unwatched game **featuring this focal player** (any opponent, any year) |
| 4 | Year filtering | Only search years where the focal player has actual playoff data (i.e., a `player_stats` row with `season_type = 'playoffs'`) |
| C | Same window for player & opponent? | Yes — one shared window-length picker |
| D | Cache key | `(player_id, window, watch_log_count)` |

---

## Key Technical Notes

### How the existing suggest works (Best 3-Year page)
- **Endpoint:** `GET /api/suggest_game?window=3&skip=0`
- Runs the full peak-window calculation for all **selected** players, builds overlapping pairs sorted by `min(score_A, score_B)` descending, then iterates pairs to find the first (skip-th) unwatched co-appearance game.
- Full candidate list is cached in `_suggest_cache` keyed by `(window, selected_player_ids, watch_log_count)`.
- Co-appearance data comes from `player_game_appearances` (cached from `stats.nba.com` / BBRef).
- Watch-log cross-reference uses `ABBR_TO_TEAM_NAME` → `watched_playoff_games`.

### New endpoint difference
The new endpoint **fixes one player** (the focal player from the URL) and treats all other selected players as potential opponents. Rather than pair-scoring symmetrically, opponents are ranked by their own **peak score** descending (highest-peak opponent first). The focal player's peak window is computed the same way — best consecutive N years — but only over years where they have playoff stats.

---

## Step-by-Step Implementation Plan

---

### Step 1 — Backend: New Endpoint `GET /api/suggest_game_for_player`

**File:** `app.py`

**Query parameters:**
- `player_id` (int, required)
- `window` (int, default 3, clamped 1–20)
- `skip` (int, default 0)

**Cache:** Use the existing `_suggest_cache` dict. New cache key: `("player", player_id, window, watch_log_count)`. This is separate from the Best 3-Year cache so they don't collide.

#### Logic

**Step 1a — Validate focal player**
```python
player_row = conn.execute(
    "SELECT id, name, nba_id, bbref_url FROM players WHERE id = ?", (player_id,)
).fetchone()
```
If not found, return `{"result": "error", "message": "Player not found."}`.

**Step 1b — Find focal player's peak window**

Run the same per-player peak-window logic already used in `suggest_game()`:

1. Query `player_stats` for the focal player, `season_type = 'playoffs'` only (per decision #4 — we only care about years where they have real playoff data).
2. Build `playoff_years: set[int]` — the set of years they appear.
3. Run the full KYLE rating calculation (call `kyle.calculate()`) for each of those season_ids to get a rating per year.
4. Find the best consecutive window of length `window` **only over years that appear in `playoff_years`**. This means gaps (e.g., a player who missed a year) are treated as gaps — don't bridge over them.

```python
sorted_playoff_years = sorted(playoff_years)
best_focal = None
for i in range(len(sorted_playoff_years) - window + 1):
    w_years = sorted_playoff_years[i : i + window]
    # Only treat as consecutive if max - min + 1 == window (no calendar gaps)
    if w_years[-1] - w_years[0] + 1 != window:
        continue
    total = sum(year_ratings.get(y, 0.0) for y in w_years)
    if best_focal is None or total > best_focal["score"]:
        best_focal = {
            "player_id": player_id,
            "name": player_row["name"],
            "best_start_year": w_years[0],
            "best_end_year": w_years[-1],
            "score": round(total, 4),
        }
```

If `best_focal` is `None` (player has fewer playoff seasons than the window), return `{"result": "none", "message": "Focal player has fewer playoff seasons than the selected window."}`.

**Step 1c — Build opponent peaks (all other selected players)**

Reuse the same full peak-window calculation already in `suggest_game()` (compute for all selected players, then exclude the focal player). Each opponent gets:
- `player_id`, `name`, `best_start_year`, `best_end_year`, `best_window_total`, `nba_id`, `bbref_url`

**Step 1d — Filter to overlapping opponents and sort**

For each opponent, check overlap with the focal player's window:
```python
overlap = (
    opp["best_start_year"] <= best_focal["best_end_year"] and
    best_focal["best_start_year"] <= opp["best_end_year"]
)
```
Sort overlapping opponents **descending by `best_window_total`** (highest-peak opponent first).

**Step 1e — Find unwatched co-appearance games**

For each opponent (in score order):
1. Compute `overlap_start = max(focal_start, opp_start)`, `overlap_end = min(focal_end, opp_end)`.
2. Further restrict to years in `playoff_years` (focal player's actual playoff seasons).
3. Fetch appearances for both players in those years via `_get_player_appearances()` (existing function).
4. SQL co-appearance join (identical to existing `suggest_game()` logic):
   ```sql
   SELECT a1.game_date, a1.season_year, a1.team_abbr AS focal_abbr, a2.team_abbr AS opp_abbr
   FROM player_game_appearances a1
   JOIN player_game_appearances a2
     ON a1.game_date = a2.game_date AND a1.season_year = a2.season_year
   WHERE a1.player_id = :focal_id
     AND a2.player_id = :opp_id
     AND a1.season_type = 'playoffs'
     AND a1.season_year BETWEEN :overlap_start AND :overlap_end
   ORDER BY a1.season_year ASC, a1.game_date ASC
   ```
5. Cross-reference each co-appearance game against `watched_playoff_games` using `ABBR_TO_TEAM_NAME` (same as existing endpoint).
6. Collect all unwatched games as candidates.

After iterating all opponents, sort all candidates by **opponent peak score descending**, then by `game_date` ascending as a tiebreaker within the same opponent.

Store the full list in `_suggest_cache[cache_key]`. Serve `candidates[skip]`.

**Step 1f — Response shape**

```json
{
  "result": "found",
  "focal_player": { "id": 5, "name": "Michael Jordan", "peak": "1991–1993", "score": 4.21 },
  "opponent":     { "id": 12, "name": "Magic Johnson",  "peak": "1990–1992", "score": 3.88 },
  "game": {
    "year": 1991,
    "game_date": "1991-05-25",
    "team1": "Chicago Bulls",
    "team2": "Los Angeles Lakers",
    "round": "Unknown"
  }
}
```

Error / edge cases:
```json
{ "result": "none",         "message": "No overlapping opponents found." }
{ "result": "missing_data", "player": "Magic Johnson" }
{ "result": "none",         "message": "All games featuring this player have been watched." }
```

---

### Step 2 — Player Page HTML Changes

**File:** `templates/player.html`

**2a — Add button to header controls:**
```html
<button id="player-suggest-btn" class="btn-nav btn-suggest">🎬 Suggest a Game</button>
```
Place this after the `Watch Log` nav link in the `<div class="controls">`.

**2b — Add window-picker popover and result card below the header:**
```html
<!-- Suggest popover (window picker) -->
<div id="player-suggest-popover" class="suggest-popover hidden">
  <label for="player-suggest-window" style="font-weight:600;">Peak window:</label>
  <select id="player-suggest-window" class="btn-nav" style="padding:4px 8px;">
    <option value="1">1 Year</option>
    <option value="2">2 Years</option>
    <option value="3" selected>3 Years</option>
    <!-- ... up to 17 -->
  </select>
  <button id="player-suggest-go" class="btn-nav btn-suggest">Search</button>
  <button id="player-suggest-popover-close" class="btn-nav">✕</button>
</div>

<!-- Suggest result card -->
<div id="player-suggest-card" class="suggest-card hidden">
  <button id="player-suggest-dismiss" class="suggest-dismiss" title="Dismiss">✕</button>
  <div id="player-suggest-content"></div>
</div>
```

Place the popover inside the `<header>` div (or just below it, before `<main>`), and the result card at the top of `<main>`, matching where the Best 3-Year page puts its `#suggest-card`.

---

### Step 3 — Player Page JS Changes

**File:** `static/player.js`

Add to the top (state):
```javascript
let playerSuggestSkip   = 0;
let playerSuggestWindow = 3;
```

Add to `DOMContentLoaded`:
```javascript
document.getElementById("player-suggest-btn").addEventListener("click", () => {
  const popover = document.getElementById("player-suggest-popover");
  popover.classList.toggle("hidden");
});

document.getElementById("player-suggest-popover-close").addEventListener("click", () => {
  document.getElementById("player-suggest-popover").classList.add("hidden");
});

document.getElementById("player-suggest-go").addEventListener("click", () => {
  playerSuggestWindow = parseInt(document.getElementById("player-suggest-window").value, 10);
  playerSuggestSkip   = 0;
  document.getElementById("player-suggest-popover").classList.add("hidden");
  runPlayerSuggest();
});

document.getElementById("player-suggest-dismiss").addEventListener("click", () => {
  document.getElementById("player-suggest-card").classList.add("hidden");
  playerSuggestSkip = 0;
});
```

**New function `runPlayerSuggest()`** (mirrors `runSuggest()` in `best3year_kyle.js`):
```javascript
async function runPlayerSuggest() {
  const spinner = document.getElementById("spinner");
  const card    = document.getElementById("player-suggest-card");
  const content = document.getElementById("player-suggest-content");
  const pid     = getPlayerIdFromUrl();

  spinner.classList.remove("hidden");
  card.classList.add("hidden");

  try {
    const data = await apiFetch(
      `/api/suggest_game_for_player?player_id=${pid}&window=${playerSuggestWindow}&skip=${playerSuggestSkip}`
    );
    content.innerHTML = renderPlayerSuggestContent(data);

    const nextBtn = document.getElementById("player-suggest-next-btn");
    if (nextBtn) {
      nextBtn.addEventListener("click", () => {
        playerSuggestSkip++;
        runPlayerSuggest();
      });
    }

    const logBtn = document.getElementById("player-suggest-log-btn");
    if (logBtn) {
      logBtn.addEventListener("click", () => {
        const g = data.game;
        const params = new URLSearchParams({ home: g.team1, away: g.team2, year: g.year });
        if (g.round && g.round !== "Unknown") params.set("round", g.round);
        window.location.href = `/watch_log?${params.toString()}`;
      });
    }

    card.classList.remove("hidden");
  } catch (err) {
    content.innerHTML = `<span class="suggest-error">✗ ${err.message}</span>`;
    card.classList.remove("hidden");
  } finally {
    spinner.classList.add("hidden");
  }
}
```

**New function `renderPlayerSuggestContent(data)`:**

```javascript
function renderPlayerSuggestContent(data) {
  if (data.result === "found") {
    const g   = data.game;
    const fp  = data.focal_player;
    const opp = data.opponent;
    const gameInfo = g.team1 && g.team2 ? `${g.team1} vs ${g.team2}` : "Unknown matchup";
    return `
      <div class="suggest-found">
        <div class="suggest-pair">
          <strong>vs <a href="/player/${opp.id}" class="player-link">${opp.name}</a></strong>
          <span class="suggest-peak">${opp.peak} &mdash; ${fmt(opp.score)}</span>
          <span class="suggest-peak suggest-focal-peak">(${fp.name} peak: ${fp.peak} &mdash; ${fmt(fp.score)})</span>
        </div>
        <div class="suggest-game-info">
          <span class="suggest-game-label">Suggested game:</span>
          <strong>${gameInfo}</strong>
          &bull; ${g.year}${g.game_date ? ` &bull; ${g.game_date}` : ""}
        </div>
        <div class="suggest-actions">
          <button id="player-suggest-log-btn" class="btn-nav btn-suggest-log">📋 Log This Game</button>
          <button id="player-suggest-next-btn" class="btn-nav btn-suggest-next">Next ›</button>
        </div>
      </div>`;
  }
  if (data.result === "missing_data") {
    return `<span class="suggest-error">Appearance data missing for <strong>${data.player}</strong>. Run a scrape on their player page first.</span>`;
  }
  if (data.result === "none") {
    return `<span class="suggest-none">${data.message || "No suggestion available."}</span>`;
  }
  return `<span class="suggest-error">${data.message || "An error occurred."}</span>`;
}
```

Note: `fmt()` is the existing number formatter. Confirm it is available in `player.js` scope (it is already defined in that file or a shared util); if not, copy it from `best3year_kyle.js`.

---

### Step 4 — Watch Log Quick-Add (Both Pages)

The "Log This Game" button on both the player-page suggest card and the Best 3-Year suggest card navigates to `/watch_log` with pre-fill query params. The Watch Log page detects these and auto-opens the modal.

**4a — Best 3-Year page (`static/best3year_kyle.js`)**

In `renderSuggestContent()`, replace the existing `"→ Watch Log"` link with a "📋 Log This Game" button, and wire it after rendering (same pattern as the Next button):

```javascript
// In renderSuggestContent(), inside the "found" branch, change suggest-actions to:
`<div class="suggest-actions">
  <button id="suggest-log-btn" class="btn-nav btn-suggest-log">📋 Log This Game</button>
  <button id="suggest-next-btn" class="btn-nav btn-suggest-next">Next ›</button>
</div>`
```

In `runSuggest()`, after wiring `suggest-next-btn`, add:
```javascript
const logBtn = document.getElementById("suggest-log-btn");
if (logBtn) {
  logBtn.addEventListener("click", () => {
    const g = data.game;
    const params = new URLSearchParams({ home: g.team1, away: g.team2, year: g.year });
    if (g.round && g.round !== "Unknown") params.set("round", g.round);
    window.location.href = `/watch_log?${params.toString()}`;
  });
}
```

**4b — Watch Log page (`static/watch_log.js`)**

In `DOMContentLoaded`, after `bindUI()`, add:

```javascript
// Auto-open modal if pre-fill params are present (from suggest card)
const qs = new URLSearchParams(window.location.search);
if (qs.has("home") || qs.has("away") || qs.has("year")) {
  const prefill = {
    home_team:  qs.get("home")  || "",
    away_team:  qs.get("away")  || "",
    game_year:  qs.get("year")  || "",
    round:      qs.get("round") || "",
    conference: qs.get("conf")  || "",
  };
  // Small delay to ensure loadGames() has started
  setTimeout(() => openModal(prefill), 100);
  // Clean the URL so a refresh doesn't re-open the modal
  history.replaceState({}, "", "/watch_log");
}
```

**Update `openModal()` to accept a prefill object** (it currently accepts a full `game` record or `null`). The prefill object won't have `id`, `date_watched`, etc. so `openModal` already handles `null`-safe access with `game?.field`. Verify that these fields map correctly:

| Prefill key | Form field id | `openModal` line |
|---|---|---|
| `home_team` | `f-home` | `game.home_team` |
| `away_team` | `f-away` | `game.away_team` |
| `game_year` | `f-year` | `game.game_year` |
| `round`     | `f-round`| `game.round` |
| `conference`| `f-conf` | `game.conference` |

Since `openModal` already uses `game?.home_team`, `game?.away_team`, etc., and the prefill keys match, no changes to `openModal` are needed — passing the prefill object directly works.

---

### Step 5 — CSS Changes

**File:** `static/style.css`

Add styles for:

1. **`.suggest-popover`** — small inline panel that appears below the "🎬 Suggest a Game" button on the player page. Similar to how a dropdown appears. Should be relatively positioned to the button.

```css
.suggest-popover {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface, #161b22);
  border: 1px solid var(--border, #30363d);
  border-radius: 6px;
  padding: 8px 12px;
  margin-top: 6px;
}
```

2. **`.suggest-focal-peak`** — secondary/muted text showing the focal player's own peak alongside the opponent's. Slightly smaller and muted color.

```css
.suggest-focal-peak {
  font-size: .85rem;
  color: var(--muted, #8b949e);
  margin-left: 8px;
}
```

3. **`.btn-suggest-log`** — style for the "📋 Log This Game" button. Should visually distinguish it from "Next" — e.g., slightly different accent color or border.

```css
.btn-suggest-log {
  border-color: var(--accent-alt, #388bfd);
  color: var(--accent-alt, #388bfd);
}
.btn-suggest-log:hover {
  background: var(--accent-alt, #388bfd);
  color: #fff;
}
```

---

## Files to Touch

| File | Change |
|---|---|
| `app.py` | Add `GET /api/suggest_game_for_player` endpoint; reuse `_suggest_cache`, `ABBR_TO_TEAM_NAME`, `_get_player_appearances` |
| `templates/player.html` | Add suggest button, popover, and result card HTML |
| `static/player.js` | Add state vars, event listeners, `runPlayerSuggest()`, `renderPlayerSuggestContent()` |
| `static/best3year_kyle.js` | Replace `→ Watch Log` link with `📋 Log This Game` button; wire click handler |
| `static/watch_log.js` | Detect pre-fill query params on boot; auto-open modal |
| `static/style.css` | Add `.suggest-popover`, `.suggest-focal-peak`, `.btn-suggest-log` |

---

## Build Order

1. **`app.py`** — Write `/api/suggest_game_for_player`. Test manually with `curl "/api/suggest_game_for_player?player_id=X&window=3&skip=0"` before touching any frontend.
2. **`static/watch_log.js`** — Add pre-fill param detection. Test by navigating to `/watch_log?home=Chicago+Bulls&away=Los+Angeles+Lakers&year=1991` and confirming the modal opens pre-filled.
3. **`static/best3year_kyle.js`** — Swap the Watch Log link for the Log This Game button and wire the click handler.
4. **`templates/player.html`** — Add the button, popover, and card HTML scaffolding.
5. **`static/player.js`** — Wire up all event listeners and add `runPlayerSuggest()` / `renderPlayerSuggestContent()`.
6. **`static/style.css`** — Add the new CSS rules.

---

## Edge Cases & Error Handling

| Condition | Response |
|---|---|
| Focal player has fewer playoff seasons than `window` | `{ "result": "none", "message": "Fewer playoff seasons than the selected window length." }` |
| No selected players overlap with focal player's peak | `{ "result": "none", "message": "No selected players with overlapping peak windows." }` |
| Appearance data missing for an opponent | Skip that opponent silently; if ALL opponents are missing data, return `{ "result": "missing_data", "player": "<name>" }` for the highest-ranked missing one |
| All co-appearance games already watched | `{ "result": "none", "message": "All games featuring this player have been watched. Impressive." }` |
| `skip` exceeds candidate list length | Same "none" message |
| `player_id` not in DB | `{ "result": "error", "message": "Player not found." }` |

---

## Notes for the Implementer

- **Reuse heavily.** The new endpoint is a focused variant of `suggest_game()`. Pull the peak-window calculation into a shared helper function (e.g., `_compute_peaks(conn, window, player_ids)`) so both endpoints call the same code rather than duplicating the 60-line loop.
- **The `fmt()` function** in `player.js`: search the file to confirm it exists. If not, copy it verbatim from `best3year_kyle.js`.
- **Consecutive-year check:** The focal player's peak window must span truly consecutive calendar years (no gaps). The check `w_years[-1] - w_years[0] + 1 == window` enforces this.
- **`openModal` prefill compatibility:** The prefill object passed from the query-param detection code uses the same key names as actual game records (`home_team`, `away_team`, `game_year`, `round`). Double-check against the current `openModal` implementation before shipping to make sure no keys diverged.
- **URL cleanup after pre-fill:** `history.replaceState({}, "", "/watch_log")` removes the query string after the modal opens, so a page refresh doesn't re-trigger the modal.
