# Plan: Peak Opponent Games Section on Player Page

## Overview

Add a new section to the `/player/<id>` page that lists every **playoff game** the
focal player appeared in alongside at least one other selected player who was
**at their peak** during that year. The peak window size is user-selectable. The
list is chronological and can be filtered by a single peak opponent via a
dropdown. Each row shows whether the game was watched and which peak opponents
were present.

---

## Definitions

- **Peak window**: the best consecutive N-year K.Y.L.E. window for a player, as
  computed by the existing `compute_peak_windows(conn, window)` function in
  `services/kyle_service.py`.
- **At peak**: a player is "at peak" in a game if `game_year` falls within their
  `[best_start_year, best_end_year]` inclusive.
- **Game appearance**: sourced from `player_game_appearances`
  (`season_type = 'playoffs'`). A game is uniquely identified by
  `(season_year, game_date, team_abbr)` for the focal player, cross-joined
  against the opponent team (`opp_abbr`).
- **Watched**: a game is "watched" if a matching row exists in
  `watched_playoff_games` where both teams match `{team_abbr, opp_abbr}` and
  `game_year = season_year`. The `watched_playoff_games` row also carries
  `round`, `game_of_round`, `conference`, and `best_player_id`.
- **Peak opponent in game**: another selected player (not the focal player) who
  has a row in `player_game_appearances` for the same
  `(season_year, game_date)` AND whose peak window covers `season_year`.

---

## Data Flow

```
User selects window size (default 3)
        │
        ▼
GET /api/player/<id>/peak-games?window=3
        │
        ├─ compute_peak_windows(conn, window)
        │      → peaks list: {player_id, best_start_year, best_end_year}
        │
        ├─ Query player_game_appearances for focal player (playoffs only)
        │      → list of {season_year, game_date, team_abbr, opp_abbr}
        │
        ├─ For each game, query player_game_appearances for other players
        │  on the same (season_year, game_date), filter to those whose
        │  peak window covers season_year
        │      → peak_opponents per game
        │
        ├─ Keep only games that have ≥ 1 peak opponent
        │
        ├─ Left-join to watched_playoff_games to get watched status + metadata
        │
        └─ Return JSON (see Response Shape below)
```

---

## API Endpoint

### `GET /api/player/<int:player_id>/peak-games`

**Query parameters**

| Param    | Type | Default | Description                        |
|----------|------|---------|------------------------------------|
| `window` | int  | `3`     | Peak window size (1–17 years)      |

**Response shape**

```json
{
  "player_id": 12,
  "window": 3,
  "games": [
    {
      "game_year":      1992,
      "game_date":      "1992-06-03",
      "team_abbr":      "CHI",
      "opp_abbr":       "POR",
      "watched":        true,
      "round":          "NBA Finals",
      "game_of_round":  1,
      "conference":     "NBA Finals",
      "best_player_id": 12,
      "best_player_name": "Michael Jordan",
      "peak_opponents": [
        {
          "player_id":   7,
          "name":        "Clyde Drexler",
          "peak_start":  1990,
          "peak_end":    1992
        }
      ]
    }
  ],
  "all_peak_opponents": [
    { "player_id": 7, "name": "Clyde Drexler" }
  ]
}
```

- `games` is sorted chronologically by `(game_year, game_date)`.
- `all_peak_opponents` is the de-duplicated union of peak opponents across all
  games, sorted alphabetically by name. Used to populate the filter dropdown.
- For unwatched games, `round`, `game_of_round`, `conference`,
  `best_player_id`, and `best_player_name` are `null`.

---

## Backend Changes

### 1. `services/player_service.py` — new function

```python
def get_peak_opponent_games(conn, player_id: int, window: int) -> dict:
    """Return playoff games for player_id that include at least one peak opponent."""
```

Logic:

1. Call `kyle_service.compute_peak_windows(conn, window)` to get
   `{player_id: (best_start_year, best_end_year)}` for all selected players.
   Exclude the focal player from the opponent map.

2. Query `player_game_appearances` for the focal player:
   ```sql
   SELECT season_year, game_date, team_abbr, opp_abbr
   FROM player_game_appearances
   WHERE player_id = ? AND season_type = 'playoffs'
   ORDER BY season_year, game_date
   ```

3. Build a lookup: for each `(season_year, game_date)`, query which other
   selected players appeared in that game:
   ```sql
   SELECT DISTINCT pga.player_id, p.name
   FROM player_game_appearances pga
   JOIN players p ON p.id = pga.player_id
   WHERE pga.season_year = ?
     AND pga.game_date   = ?
     AND pga.season_type = 'playoffs'
     AND pga.player_id  != ?
   ```
   Filter the result to those whose peak window covers `season_year`.

   > **Performance note**: batch this query. Collect all distinct
   > `(season_year, game_date)` pairs up front, then do one query with an `IN`
   > clause (or a temp table) rather than N individual queries.

4. Discard games with zero peak opponents.

5. Left-join to `watched_playoff_games`:
   ```sql
   SELECT id, round, game_of_round, conference, best_player_id, p.name AS best_player_name
   FROM watched_playoff_games w
   LEFT JOIN players p ON p.id = w.best_player_id
   WHERE w.game_year = ?
     AND (
           (w.home_team = ? AND w.away_team = ?)
        OR (w.home_team = ? AND w.away_team = ?)
     )
   ```
   This join is done per-game (or batched by year). A game is watched if a row
   is found.

6. Assemble and return the response dict.

### 2. `app.py` — new route

```python
@app.route("/api/player/<int:player_id>/peak-games")
def player_peak_games(player_id):
    window = request.args.get("window", 3, type=int)
    window = max(1, min(window, 17))
    with db_conn() as conn:
        data = player_service.get_peak_opponent_games(conn, player_id, window)
    return jsonify(data)
```

---

## Frontend Changes

### `templates/player.html`

Add a new `<section>` after the existing `#section-watchlog`:

```html
<!-- Peak Opponent Games section -->
<section class="profile-section hidden" id="section-peak-games">
  <h3 class="section-title">Games vs. Peak Opponents</h3>

  <div class="peak-games-controls">
    <label for="peak-games-window">Peak window:</label>
    <select id="peak-games-window">
      <!-- 1–17 year options, default 3 selected -->
    </select>

    <label for="peak-games-opponent">Opponent:</label>
    <select id="peak-games-opponent">
      <option value="">All</option>
      <!-- populated dynamically -->
    </select>
  </div>

  <p id="peak-games-empty" class="empty-msg hidden">
    No playoff games found against peak opponents for this window.
  </p>

  <div class="table-wrap" id="peak-games-table-wrap">
    <table class="profile-table" id="peak-games-table">
      <thead>
        <tr>
          <th>Year</th>
          <th>Date</th>
          <th>Matchup</th>
          <th>Round</th>
          <th>Game #</th>
          <th>Watched</th>
          <th>Best Player</th>
          <th>Peak Opponents</th>
        </tr>
      </thead>
      <tbody id="peak-games-body"></tbody>
    </table>
  </div>
</section>
```

### `static/player.js`

**New module-level state:**

```javascript
let _peakGamesData   = null;   // full API response, cached per window
let _peakGamesWindow = 3;
```

**New functions:**

- `loadPeakGames(playerId, window)` — fetches
  `/api/player/<id>/peak-games?window=<n>`, stores result in `_peakGamesData`,
  calls `renderPeakGames()`.
- `renderPeakGames(opponentFilter)` — renders the table from `_peakGamesData`,
  filtering rows to those whose `peak_opponents` includes the selected
  `player_id` (or all rows when filter is empty). Populates the opponent
  dropdown on first render.
- Wires up change handlers for both dropdowns:
  - Window dropdown → re-fetches (`loadPeakGames`), resets opponent filter.
  - Opponent dropdown → re-renders only (`renderPeakGames(opponentId)`), no
    new fetch.

**Table row rendering:**

| Column          | Content |
|-----------------|---------|
| Year            | `game_year` |
| Date            | `game_date` formatted `MMM D` |
| Matchup         | `team_abbr` vs `opp_abbr` |
| Round           | round + game # (e.g. "Finals G1"), `—` if unwatched |
| Game #          | `game_of_round` or `—` |
| Watched         | ✅ badge or ⬜ badge |
| Best Player     | name or `—` if unwatched |
| Peak Opponents  | comma-separated names, each linked to `/player/<id>` |

**Integration in `renderProfile`:**

Call `loadPeakGames(playerId, 3)` after the existing section renders so the new
section loads alongside the rest of the profile.

**Window selector options:** 1–17 years (matches the existing suggest popover
pattern).

---

## CSS (`static/style.css`)

Small additions only — no new layout concepts needed:

- `.peak-games-controls` — flexbox row with `gap`, `align-items: center`,
  `margin-bottom` matching other control rows on the page.
- Watched badges can reuse or extend any existing badge/chip class.

---

## Testing

### `tests/test_app.py`

Add tests for the new endpoint:

| Test | What it checks |
|------|----------------|
| `test_peak_games_no_player` | returns 404 for unknown player |
| `test_peak_games_no_data`   | returns empty `games` list when player has no appearances |
| `test_peak_games_basic`     | games with a peak opponent appear; games without do not |
| `test_peak_games_watched`   | `watched: true` when matching watched_playoff_games row exists |
| `test_peak_games_window`    | changing `window` changes which opponents qualify |

### Manual

1. Open any player page, scroll to new section — should load with default
   window=3.
2. Change window size — list should refresh.
3. Select an opponent from dropdown — list should filter to only games
   containing that opponent.
4. Verify watched/unwatched badges match watch log.
5. Verify peak opponent names link correctly to their player pages.

---

## File Change Summary

| File | Change |
|------|--------|
| `services/player_service.py` | Add `get_peak_opponent_games(conn, player_id, window)` |
| `app.py` | Add `GET /api/player/<id>/peak-games` route |
| `templates/player.html` | Add `#section-peak-games` HTML block |
| `static/player.js` | Add `loadPeakGames`, `renderPeakGames`, wire up controls |
| `static/style.css` | Add `.peak-games-controls` styles |
| `tests/test_app.py` | Add endpoint tests |

---

## Open Questions / Future Considerations

- **Performance**: `player_game_appearances` can have many rows. The batched
  game-date lookup described above should be fast enough, but an index on
  `(season_year, game_date, season_type)` could be added as a migration if
  needed.
- **Regular season**: explicitly out of scope per spec. The `season_type =
  'playoffs'` filter handles this.
- **Focal player's own peak**: the feature shows all games the focal player
  appeared in, regardless of whether they were at their own peak. That is
  intentional per the spec — the "peak" qualifier applies only to opponents.
  This could be added as a toggle later.
