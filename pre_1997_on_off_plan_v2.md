# Plan: Pre-1997 On/Off Approximation — v2 (nba_api replacement)

## Why v1 Failed

The original plan relied on scraping basketball-reference player game log pages
(`/players/{l}/{pid}/gamelog/{year}`) to determine which games a player appeared in.
Basketball-reference has moved these pages to JavaScript rendering, making them
inaccessible via simple HTTP requests. The entire data-collection approach needs to
be replaced.

---

## New Data Source: `nba_api` / `stats.nba.com`

The [`nba_api`](https://github.com/swar/nba_api) Python package wraps the official
`stats.nba.com` JSON endpoints. Two endpoints together provide everything we need:

| Endpoint | `PlayerOrTeam` | What we get |
|----------|---------------|-------------|
| `LeagueGameLog` | `T` (team) | Every team's game results for a season: `GAME_ID`, `GAME_DATE`, `TEAM_ABBREVIATION`, `PTS`, `PLUS_MINUS` (= margin from that team's perspective) |
| `LeagueGameLog` | `P` (player) | Every player game appearance for a season: `PLAYER_ID`, `GAME_ID`, `GAME_DATE`, `TEAM_ABBREVIATION`, `MIN` |

Key advantages over the basketball-reference approach:
- **2 API calls per season** (one T, one P) instead of ~480 page fetches
- No HTML parsing — clean JSON responses
- `PLUS_MINUS` is already computed and available directly
- Covers NBA history back to at least 1946-47 (well before our 1978 start)
- Supports `SeasonType` = `Regular Season` or `Playoffs` natively
- Highly cacheable: one league-wide pull per (season_year, season_type) covers all
  players and all teams for that season

The full 1978–1996 backfill drops from ~9,120 HTTP requests to **38 API calls**
(19 seasons × 2 season types × 2 PlayerOrTeam modes), completing in minutes rather
than hours.

---

## Installation

```bash
pip install nba_api
```

Add `nba_api` to `requirements.txt`.

---

## Database Schema Changes (`db.py`)

Same tables as v1 — no changes to the schema itself, only to how they are populated.

### `team_game_logs` (unchanged)
```
id            INTEGER PRIMARY KEY AUTOINCREMENT
team_abbr     TEXT NOT NULL
season_year   INTEGER NOT NULL
season_type   TEXT NOT NULL          -- "regular" or "playoffs"
game_date     TEXT NOT NULL          -- "YYYY-MM-DD"
margin        REAL NOT NULL          -- PLUS_MINUS from LeagueGameLog (team perspective)
UNIQUE(team_abbr, season_year, season_type, game_date)
```

### `player_game_appearances` (unchanged)
```
id            INTEGER PRIMARY KEY AUTOINCREMENT
player_id     INTEGER NOT NULL REFERENCES players(id)
season_year   INTEGER NOT NULL
season_type   TEXT NOT NULL
team_abbr     TEXT NOT NULL
game_date     TEXT NOT NULL          -- "YYYY-MM-DD"
UNIQUE(player_id, season_year, season_type, game_date)
```

### Migration: `on_off_asterisk` column (unchanged)
```sql
ALTER TABLE player_stats ADD COLUMN on_off_asterisk INTEGER DEFAULT 0
```

---

## Scraper Changes (`scraper.py`)

### Season string helper
`stats.nba.com` uses the format `"1977-78"` for the 1978 season year. Add a helper:

```python
def _nba_season_str(season_year: int) -> str:
    """Convert DB season_year (ending year) to nba_api season string, e.g. 1978 → '1977-78'."""
    return f"{season_year - 1}-{str(season_year)[-2:]}"
```

### Season type string helper
```python
def _nba_season_type(season_type: str) -> str:
    """Convert internal 'regular'/'playoffs' to nba_api string."""
    return "Regular Season" if season_type == "regular" else "Playoffs"
```

### New helper: `_fetch_league_game_log(season_year, season_type, player_or_team, conn)`

Fetches one full season's worth of game log data from `stats.nba.com` and caches it.

```
Args:
    season_year   int    e.g. 1985
    season_type   str    "regular" or "playoffs"
    player_or_team str   "T" or "P"
    conn          sqlite3.Connection

Returns for "T": dict[(team_abbr, game_date)] -> margin
Returns for "P": list of {player_id, game_date, team_abbr, min_str}
```

Implementation outline:
```python
from nba_api.stats.endpoints import leaguegamelog

def _fetch_league_game_log(season_year, season_type, player_or_team, conn):
    time.sleep(2)  # rate limit
    log = leaguegamelog.LeagueGameLog(
        season=_nba_season_str(season_year),
        season_type_all_star=_nba_season_type(season_type),
        player_or_team_abbreviation=player_or_team,
        league_id="00",
    )
    df = log.league_game_log.get_data_frame()
    return df
```

**Caching strategy:**
- For `PlayerOrTeam=T`: before calling the API, check if `team_game_logs` already
  has any rows for `(season_year, season_type)`. If yes, skip the HTTP call and
  reconstruct the dict from DB. If no, fetch, upsert all rows, return.
- For `PlayerOrTeam=P`: similarly check `player_game_appearances` for the given
  `(player_id, season_year, season_type)`. In practice, since the P-mode call covers
  all players, it makes sense to fetch once and upsert everything, then subsequent
  players in the same season reuse DB data without additional API calls.
  Use a module-level set `_fetched_seasons` to track which (season_year, season_type)
  pairs have already been fetched in the current run.

### Replace `_scrape_team_game_log()` → `_get_team_margins(team_abbr, season_year, season_type, conn)`

```
1. Check team_game_logs for (team_abbr, season_year, season_type) — any rows?
   - If yes: query DB and return dict[game_date -> margin]
2. Else: call _fetch_league_game_log(..., "T", conn)
   - Parse df: GAME_DATE → YYYY-MM-DD, margin = PLUS_MINUS
   - Upsert all teams' rows into team_game_logs (one call covers all teams)
   - Return the subset for team_abbr
```

### Replace `_scrape_player_game_log()` → `_get_player_appearances(bbref_player_id, nba_player_id, season_year, season_type, team_abbr, conn)`

```
1. Check player_game_appearances for (player_id, season_year, season_type) — any rows?
   - If yes: query DB and return list of game_dates for team_abbr
2. Else: call _fetch_league_game_log(..., "P", conn)
   - Filter df for rows where MIN is not None/zero (active appearances only)
   - Upsert all players' appearances into player_game_appearances
   - Return the subset for (player_id, team_abbr)
```

**Player ID mapping:** `stats.nba.com` uses its own numeric `PLAYER_ID`. Our DB
currently uses basketball-reference player IDs. We need to either:
- (a) Store the nba.com player ID in the `players` table alongside the bbref ID, or
- (b) Match players by name + team + season when linking records.

Option (a) is cleaner. Add an `nba_id` column to `players`:
```sql
ALTER TABLE players ADD COLUMN nba_id INTEGER;
```
Populate it during the backfill by matching names from the `LeagueGameLog` P-mode
response to existing player rows (exact name match first, fuzzy fallback for
name variations). The `nba_api` static data (`nba_api.stats.static.players`) can
also help cross-reference names to `nba_id` values without an HTTP call.

### `_get_player_team_stints()` — unchanged in logic

Still uses already-scraped advanced data from basketball-reference to determine which
team(s) a player was on. No change needed here — this data is already in the DB from
the normal `run_scrape()` flow.

### `_compute_pre97_on_off()` — same algorithm, different data sources

The computation logic from v1 is unchanged:

```
1. Get team stints from advanced data already in DB
2. For each stint team:
   a. Call _get_team_margins(team_abbr, year, season_type, conn)
      → all game dates + margins for that team
   b. Call _get_player_appearances(player_id, nba_id, year, season_type, team_abbr, conn)
      → game dates where this player appeared (MIN > 0)
3. on_court games  = intersection of (b) across all stints
4. off_court games = all team games from (a) across all stints MINUS on_court games
5. on_court_rating = mean(margins for on_court games)
6. off_court_avg   = mean(margins for off_court games)
7. missed_pct = len(off_court games) / len(all team games)
   If missed_pct < 0.03:
       on_off_diff = 0, asterisk = 1
   Else:
       on_off_diff = on_court_rating - off_court_avg, asterisk = 0
8. Return (on_court_rating, on_off_diff, asterisk)
```

### Changes to `run_scrape()` — same structure as v1

```python
if year < 1997:
    pre97_results = {}
    for name in all_names:
        on_court, on_off, asterisk = _compute_pre97_on_off(name, player_id, year, season_type, conn)
        pre97_results[name] = {"on_court": on_court, "on_off": on_off, "asterisk": asterisk}
    pbp = pre97_results
else:
    pbp = _scrape_pbp(year, season_type)
```

---

## Rate Limiting & Caching Strategy

`stats.nba.com` has its own rate limits, but since we are making only 2 calls per
season (instead of ~480), this is far less of a concern:

| Operation | API calls per season | Seasons | Total calls |
|-----------|---------------------|---------|-------------|
| Team game log (`T`) | 1 | 19 | 19 |
| Player game log (`P`) | 1 | 19 | 19 |
| **Total** | | | **38** |

- Keep `time.sleep(2)` between calls as a courtesy.
- The module-level `_fetched_seasons` set ensures each (season_year, season_type)
  pair is fetched at most once per run, even if `_compute_pre97_on_off()` is called
  for hundreds of players in that season.
- The DB caching from v1 still applies for subsequent runs / restarts.

---

## nba_api Considerations

- **`nba_api` headers:** `stats.nba.com` sometimes requires specific headers (User-Agent,
  Referer, etc.). `nba_api` handles this automatically, but if requests fail, pass
  custom headers via the `headers` parameter to `LeagueGameLog`.
- **Historical coverage:** Verify that `LeagueGameLog` returns data for `1977-78`
  through `1995-96` before running the full backfill. Run a quick test query for
  season `"1977-78"` and confirm row counts look plausible (~82 games × 2 teams × 
  ~22 teams ≈ 3,608 team-game rows per season).
- **`MIN` field in P-mode:** Confirm that games with 0 minutes (DNP / inactive) are
  either absent from the response or have `MIN = '0:00'` / `None`, so that filtering
  `MIN > 0` correctly excludes them. This resolves Open Question #1 from v1.
- **Playoffs availability:** `SeasonType = "Playoffs"` should work the same way; verify
  with a known playoff season before the full backfill.

---

## Open Questions from v1 — Updated Status

1. ~~**DNP representation in player game logs:**~~ Resolved by design. The
   `LeagueGameLog` P-mode only returns rows where the player actually played (MIN > 0).
   Inactive/DNP players are not listed. If they are listed with `MIN = '0:00'`, filter
   them out. The team-game-log minus player-appearance set math still works correctly.

2. **Team abbreviation consistency:** Still relevant. `stats.nba.com` uses its own
   abbreviations (e.g. `NJN`, `SEA`, `WSB`) which may differ from basketball-reference.
   The team stints from bbref advanced data use bbref abbreviations. Add a
   `BBREF_TO_NBA_ABBR` mapping dict for historical franchises, or use `TEAM_ID` from
   the API as the join key instead of abbreviation (preferred — team IDs are stable
   across eras). Store `nba_team_id` in `team_game_logs` and join on that.

3. ~~**Roster vs. active:**~~ Non-issue, same as v1.

4. **Backfill runtime:** With 38 API calls at 2s each, the full backfill network time
   is under 2 minutes. Total runtime including DB writes and computation will be
   under 5 minutes — a dramatic improvement over the ~7.5 hours estimated in v1.

---

## Implementation Order

1. `pip install nba_api`, add to `requirements.txt`
2. **Verify coverage:** run a quick test — `LeagueGameLog(season="1977-78", ...)` —
   confirm data exists and `PLUS_MINUS` is populated
3. Add `nba_id` column to `players` table migration in `db.py`
4. Add `team_game_logs` and `player_game_appearances` tables to `db.py` (same as v1)
5. Add `on_off_asterisk` column migration to `player_stats` in `db.py`
6. Implement `_nba_season_str()`, `_nba_season_type()` helpers
7. Implement `_fetch_league_game_log()` with caching via `_fetched_seasons` set
8. Implement `_get_team_margins()` (DB-first, API fallback)
9. Implement `_get_player_appearances()` (DB-first, API fallback, MIN filter)
10. Implement `_get_player_team_stints()` — reuse from v1 plan (uses existing DB data)
11. Implement `_compute_pre97_on_off()` with the same algorithm as v1
12. Integrate into `run_scrape()` with `year < 1997` branch
13. Add `--backfill` CLI flag (same as v1)
14. Update API (`app.py`) to return `on_off_asterisk`
15. Update frontend to display asterisk with tooltip

---

## What Stays the Same from v1

- All DB table schemas (`team_game_logs`, `player_game_appearances`, `on_off_asterisk`)
- The on/off calculation algorithm (set math on game dates, 3% threshold, asterisk flag)
- The `run_scrape()` branch structure (`year < 1997` → `_compute_pre97_on_off()`)
- Multi-team weighting approach
- The CLI `--backfill` entry point
- Backend API and frontend changes
