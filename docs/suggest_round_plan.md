# Plan: Add Playoff Round to Suggest-a-Game Feature

## Current State

The suggest-game response already has placeholder fields for round info, but they are always empty:

```json
"game": {
  "year": 1991,
  "game_date": "1991-05-25",
  "team1": "Chicago Bulls",
  "team2": "Los Angeles Lakers",
  "round": null,
  "round_known": false
}
```

The `player_game_appearances` table has no `round` column. Round information is never stored during scraping.

---

## Where Round Info Comes From

There are two fetch paths, and each has a different source of truth for round data.

### Path 1 — BBRef (pre-2000 players)

`_fetch_bbref_playoff_gamelog` parses the `pgl_basic_playoffs` HTML table from Basketball Reference. The round is **not a column in every row**; instead it appears as **group header rows** (`<tr class="thead">`) inside the `<tbody>`, with text like "First Round", "Conference Semifinals", "Conference Finals", or "NBA Finals".

The `_parse_table` helper currently skips these header rows. We need to pass through them to tag each subsequent game row with the round it falls under.

### Path 2 — nba_api (2000+ players)

`_fetch_league_game_log_nba` uses `LeagueGameLog`, which **does not include round information**. Round info is available separately via:

- `nba_api.stats.endpoints.SeriesStandings` — returns one row per playoff series per season, including `ROUND_NUM` (1–4) and both team abbreviations. This is a cheap single call per season.

---

## Proposed Round Label Mapping

Both paths need to produce a consistent round label matching what `watched_playoff_games.round` stores (e.g., `"NBA Finals"`, `"Conference Finals"`).

**For post-2000 (nba_api):** `ROUND_NUM` maps to:
| `ROUND_NUM` | Label |
|---|---|
| 1 | `"First Round"` |
| 2 | `"Conference Semifinals"` |
| 3 | `"Conference Finals"` |
| 4 | `"NBA Finals"` |

**For pre-2000 (BBRef):** Parse the group header text directly. The header text is already in the right format (e.g., "NBA Finals") and just needs to be normalized (strip whitespace/asterisks).

---

## Implementation Plan

### Step 1 — DB Schema Migration

**File:** `db.py`

Add a `round` column to `player_game_appearances`:

```python
existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(player_game_appearances)").fetchall()]
if "round" not in existing_cols:
    cur.execute("ALTER TABLE player_game_appearances ADD COLUMN round TEXT")
```

This is safe — all existing rows get `NULL` and can be backfilled later (Step 4).

---

### Step 2 — BBRef Path: Parse Round from Group Headers

**File:** `scraper.py`  
**Function:** `_fetch_bbref_playoff_gamelog`

The `_parse_table` helper currently discards `<tr class="thead">` rows inside `<tbody>`. We need to detect these as round-separator rows and track the current round in a local variable.

**Approach:** Instead of calling `_parse_table` as a black box, inline the table parsing within `_fetch_bbref_playoff_gamelog` (or add an optional `include_group_headers=True` parameter to `_parse_table` that yields a sentinel dict like `{"__round_header__": "Conference Finals"}`).

When inserting each game row, include the current `round` value:

```python
cur.execute(
    "INSERT OR IGNORE INTO player_game_appearances "
    "(player_id, season_year, season_type, team_abbr, opp_abbr, game_date, round) VALUES (?,?,?,?,?,?,?)",
    (player_id, season_year, "playoffs", team_abbr, opp_abbr, game_date, current_round),
)
```

**Round header detection:** A group header row is typically a `<tr>` with class `"thead"` (or no `<td>` cells, only `<th>`) inside `<tbody>`. Its text content will be the round name. Clean it before storing:

```python
raw = header_row.get_text(strip=True)
# Normalize: strip footnote markers, extra spaces
current_round = raw.replace("*", "").strip()
```

**Known BBRef round names to handle:**
- `"First Round"` (modern era), `"Quarterfinals"` / `"Division Semifinals"` (older eras)
- `"Conference Semifinals"` / `"Division Finals"` (older eras)
- `"Conference Finals"` / `"Division Finals"` (context-dependent)
- `"NBA Finals"` / `"Finals"`
- An optional **normalization dict** should map older era names to their modern equivalents for consistent display.

---

### Step 3 — nba_api Path: Fetch Series Standings

**File:** `scraper.py`

Add a new helper:

```python
def _fetch_series_round_map(season_year: int, conn) -> dict[tuple[str, str], str]:
    """
    Return a dict mapping frozenset({team1_abbr, team2_abbr}) → round_label
    for a given season's playoffs using nba_api SeriesStandings.
    
    Results are cached in a new DB table `playoff_series_rounds`.
    """
```

**New DB table** (in `db.py`):

```sql
CREATE TABLE IF NOT EXISTS playoff_series_rounds (
    season_year   INTEGER NOT NULL,
    team1_abbr    TEXT    NOT NULL,
    team2_abbr    TEXT    NOT NULL,
    round         TEXT    NOT NULL,
    PRIMARY KEY (season_year, team1_abbr, team2_abbr)
);
```

**Logic:**
1. Check if rows already exist for `season_year` → return from DB.
2. Call `SeriesStandings(season=..., season_type="Playoffs").series_standings.get_data_frame()`.
3. Map `ROUND_NUM` to a label string.
4. Upsert both orderings of the team pair (team1/team2 and team2/team1) so lookups work either way.

After a successful `_fetch_league_game_log_nba` for playoffs, call `_fetch_series_round_map` and **update `player_game_appearances` with the round** for all rows just inserted for that season:

```sql
UPDATE player_game_appearances
SET round = (
    SELECT psr.round FROM playoff_series_rounds psr
    WHERE psr.season_year = player_game_appearances.season_year
      AND (
        (psr.team1_abbr = player_game_appearances.team_abbr AND psr.team2_abbr = player_game_appearances.opp_abbr)
        OR (psr.team2_abbr = player_game_appearances.team_abbr AND psr.team1_abbr = player_game_appearances.opp_abbr)
      )
)
WHERE season_year = ?
  AND season_type = 'playoffs'
  AND round IS NULL
  AND opp_abbr IS NOT NULL
```

---

### Step 4 — Backfill Existing Cached Rows

Many rows in `player_game_appearances` already exist with `round = NULL`. These need to be backfilled retroactively.

**BBRef rows (pre-2000):** These must be re-fetched from BBRef since the round was never parsed. The `bbref_playoff_fetch_log` tracks fetch status. To force re-fetch, either:
- Delete the `bbref_playoff_fetch_log` rows for affected players (forces re-fetch on next demand), or
- Add a one-time backfill script `scripts/backfill_rounds.py` that iterates players with NULL rounds and re-fetches their BBRef gamelogs.

**nba_api rows (2000+):** The `playoff_series_rounds` table approach (Step 3) can be run retroactively with a simple script. Since `SeriesStandings` is a cheap call (no per-player rate limits), this can be done for all seasons in the DB:

```python
# scripts/backfill_rounds.py (nba_api section)
for year in conn.execute(
    "SELECT DISTINCT season_year FROM player_game_appearances WHERE season_type='playoffs' AND round IS NULL AND opp_abbr IS NOT NULL"
).fetchall():
    _fetch_series_round_map(year["season_year"], conn)
    # Then run the UPDATE query from Step 3
```

**Recommendation:** Include both backfill strategies in `scripts/backfill_rounds.py`. BBRef backfill should be opt-in (rate limit sensitive); nba_api backfill can run automatically.

---

### Step 5 — Update `suggest_service.py` to Include Round

**File:** `services/suggest_service.py`  
**Function:** `_find_co_appearance_games` and `_find_co_appearance_games_in_years`

Add `round` to the SELECT:

```sql
SELECT a1.game_date, a1.season_year,
       a1.team_abbr AS team1_abbr, a2.team_abbr AS team2_abbr,
       a1.round AS round,          -- NEW
       ROW_NUMBER() OVER (
           PARTITION BY a1.season_year, a1.team_abbr, a1.opp_abbr
           ORDER BY a1.game_date
       ) AS game_of_round
FROM ...
```

In `get_suggestions` and `get_suggestions_for_player`, update the candidate dict:

```python
"game": {
    "year":        season_year,
    "game_date":   game["game_date"],
    "team1":       team1_variants[0] if team1_variants else game["team1_abbr"],
    "team2":       team2_variants[0] if team2_variants else game["team2_abbr"],
    "round":       game["round"],               # was: None
    "round_known": game["round"] is not None,   # was: False
},
```

---

### Step 6 — Frontend Display

**Files:** `static/best3year_kyle.js`, `static/watch_log.js` (if suggest card appears there too)

Currently the suggest card likely shows "Round: Unknown" or nothing. Update the render logic:

```javascript
const roundText = game.round_known ? game.round : "Round unknown";
```

Display format for the card:
> **1991 NBA Finals** · Game 3 · May 25, 1991  
> Chicago Bulls vs. Los Angeles Lakers

Or for unknown:
> **1991 Playoffs** · Game 3 · May 25, 1991  
> Chicago Bulls vs. Los Angeles Lakers

---

## Files to Touch

| File | Change |
|---|---|
| `db.py` | Add `round` column migration to `player_game_appearances`; add `playoff_series_rounds` table |
| `scraper.py` | Modify `_parse_table` or `_fetch_bbref_playoff_gamelog` to parse round headers; add `_fetch_series_round_map`; call it after nba_api fetch |
| `services/suggest_service.py` | Add `round` to co-appearance queries; populate `round_known` field |
| `static/best3year_kyle.js` | Update suggest card render to show round |
| `scripts/backfill_rounds.py` | New script to retroactively fill round for existing rows |

---

## Build Order

1. **Schema** — add the `round` column and `playoff_series_rounds` table in `db.py`.
2. **nba_api round map** — implement `_fetch_series_round_map` in `scraper.py` + retroactive UPDATE query.
3. **Backfill script (nba_api section)** — verify round data populates correctly for a test season.
4. **BBRef round parsing** — modify `_fetch_bbref_playoff_gamelog` to track and store round from group headers; reset relevant `bbref_playoff_fetch_log` entries to trigger re-fetch.
5. **Backfill script (BBRef section)** — verify round data for a pre-2000 player.
6. **suggest_service update** — add `round` to queries, update candidate dict.
7. **Frontend update** — update card render with round display.
8. **Tests** — add tests for round parsing in `test_scraper.py`; update suggest fixture in `test_app.py`.

---

## Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| BBRef group header rows have inconsistent text across eras | Build a normalization dict mapping historical names (e.g., "Division Finals") to modern equivalents; log any unrecognized header text |
| `SeriesStandings` nba_api endpoint unavailable or returns no data for a season | Gracefully leave `round = NULL`; `round_known` remains `False` in the UI |
| Existing BBRef-cached rows can't be updated without re-fetching | Backfill script resets `bbref_playoff_fetch_log` status to trigger re-fetch on demand; only needed for pre-2000 players already in the DB |
| Re-fetching BBRef triggers rate limits | Backfill script adds a 3-second delay between players; makes it opt-in |
| `watch_log` round values use different strings than computed values | Normalizing both sides to the same labels (via the mapping dict) ensures `round` from `player_game_appearances` matches what the user would enter in the watch log |

---

## Open Questions

1. **Should older era round names be shown as-is or normalized?** (e.g., show "Division Semifinals" or map to "Conference Semifinals") — recommend normalizing for consistency with the watch log, but display the normalized name.
2. **Should the round affect scoring/ranking of suggestions?** (e.g., prefer Finals games over First Round games) — currently suggestions are ranked by player peak score only. This could be a future enhancement.
3. **Should `get_suggestions_for_player` also surface round?** — yes, same change applies there.
