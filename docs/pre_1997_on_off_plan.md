# Plan: Pre-1997 On/Off Approximation

## Background

Play-by-play on/off data on basketball-reference begins in 1997. For seasons 1978–1996
(the first year usage rate and turnover % are available), we need an approximation for
`on_court_rating` and `on_off_diff`. The approach:

- **on_court_rating** ≈ average point margin (team pts − opp pts) across all games the
  player appeared in (minutes > 0), from the player's team's perspective
- **on_off_diff** ≈ on_court_rating minus the average point margin across all games the
  player was on the roster but received 0 minutes
- If a player missed fewer than 3% of their team's games in a season, `on_off_diff` is
  set to 0 and flagged with an asterisk in the UI (too few missed games to be meaningful)
- Multi-team (traded) players are handled per-stint and then combined (see below)

---

## Data Sources

### Option A — Player game log only
`/players/{letter}/{player_id}/gamelog/{year}`

Each row is one game; columns include date, team, opponent, result, margin, and MP.
**Problem:** basketball-reference game logs typically only list games the player was
*active* for. DNP/inactive games are either omitted or listed without stats. We need
to verify this empirically before relying on it alone.

### Option B — Team game log + player game log (recommended)

| Source | URL pattern | What we get |
|--------|-------------|-------------|
| Team game log | `/teams/{TEAM}/{year}_games.html` | Every game the team played, with final score and thus margin |
| Player game log | `/players/{l}/{pid}/gamelog/{year}` | Every game the player appeared in (MP > 0), with date for cross-referencing |

**Why this is better:** The team game log is the authoritative list of all games
(including those the player missed). The player game log tells us which games the player
was in. The difference = missed games. Team game logs are also highly cacheable —
many players share the same team-season page.

**Playoffs:** Use the same pages under the playoff gamelog variant:
- Team: `/teams/{TEAM}/{year}_games.html` has a separate playoffs section, or use
  `/teams/{TEAM}/{year}_games_playoffs.html`
- Player: `/players/{l}/{pid}/gamelog/{year}` (playoffs tab on the same page, or a
  separate URL — verify during implementation)

---

## Scope

- **Seasons:** 1978–1996 (season_year 1978 through 1996), both regular season and
  playoffs (matching whatever seasons are already in the DB)
- **Trigger:** During `run_scrape()`, if `year < 1997`, skip `_scrape_pbp()` and call
  `_compute_pre97_on_off()` instead

---

## New Database Tables

### `team_game_logs`
Caches every team game result. Populated once per team-season; reused across all
players on that team.

```
id            INTEGER PRIMARY KEY AUTOINCREMENT
team_abbr     TEXT NOT NULL          -- e.g. "BOS"
season_year   INTEGER NOT NULL
season_type   TEXT NOT NULL          -- "regular" or "playoffs"
game_date     TEXT NOT NULL          -- "YYYY-MM-DD"
margin        REAL NOT NULL          -- team_pts - opp_pts (from team perspective)
UNIQUE(team_abbr, season_year, season_type, game_date)
```

### `player_game_appearances`
Tracks which games a player appeared in (MP > 0). Populated once per player-season.

```
id            INTEGER PRIMARY KEY AUTOINCREMENT
player_id     INTEGER NOT NULL REFERENCES players(id)
season_year   INTEGER NOT NULL
season_type   TEXT NOT NULL
team_abbr     TEXT NOT NULL          -- team the player was on for this game
game_date     TEXT NOT NULL          -- "YYYY-MM-DD"
UNIQUE(player_id, season_year, season_type, game_date)
```

> **Note:** We do not store margins here — those live in `team_game_logs` and are
> joined at calculation time. This keeps the data normalized and avoids re-scraping
> team pages when player data is refreshed.

---

## Scraper Changes (`scraper.py`)

### New helper: `_scrape_team_game_log(team_abbr, year, season_type)`
- Fetches `/teams/{team_abbr}/{year}_games.html`
- Parses the results table; extracts `game_date` and `margin` (team score − opp score)
- Upserts into `team_game_logs`
- Returns a `dict[date_str, margin]`

### New helper: `_scrape_player_game_log(bbref_url, year, season_type)`
- Fetches the player's game log page
- Parses rows where MP > 0 (active games); extracts `game_date` and `team_abbr`
- Returns a `list[dict]` with `{game_date, team_abbr}`
- Does **not** write to DB (caller handles persistence)

### New helper: `_get_player_team_stints(player_name, year, season_type, advanced_data)`
- Uses the already-scraped advanced/totals data to determine which team(s) a player
  was on and in what order
- For traded players (TOT row in advanced), identify individual team rows from the same
  scrape to get the per-stint team abbreviations
- Returns `list[str]` of team abbreviations in order (e.g. `["MIL", "LAL"]`)

### New main function: `_compute_pre97_on_off(player_name, player_id, year, season_type, conn)`

```
1. Get team stints for this player from the already-scraped advanced data
2. For each stint team:
   a. If team_game_logs not yet populated for (team, year, season_type):
      - Call _scrape_team_game_log() and cache in DB
   b. Call _scrape_player_game_log() to get dates the player appeared
   c. Store appearances in player_game_appearances
3. Compute on_court_rating:
   - Join player_game_appearances with team_game_logs on (team_abbr, game_date)
   - Average the margins across all played games (weighted equally)
4. Compute off_court average:
   - All team games for the player's stints MINUS the played games
   - Average those margins
5. Check 3% threshold:
   - total_team_games = count of all games across all stints
   - missed_games = total_team_games - played_games
   - If missed_games / total_team_games < 0.03:
     on_off_diff = 0, set a flag for the asterisk
   - Else:
     on_off_diff = on_court_rating - off_court_avg
6. Return (on_court_rating, on_off_diff, asterisk_flag)
```

**Multi-team weighting:** Margins from all stints are pooled into a flat list before
averaging. A player who spent 60 games on team A and 20 games on team B will naturally
have their on-court average weighted 3:1 toward team A's results, which is correct.

### Changes to `run_scrape()`

```python
if year < 1997:
    # Skip pbp scrape; compute approximation per player after merging advanced/totals
    pre97_results = {}
    for name in all_names:
        on_court, on_off, asterisk = _compute_pre97_on_off(name, ...)
        pre97_results[name] = {"on_court": on_court, "on_off": on_off, "asterisk": asterisk}
    pbp = pre97_results
else:
    pbp = _scrape_pbp(year, season_type)
```

---

## Database Schema Changes (`db.py`)

1. Add `CREATE TABLE IF NOT EXISTS team_game_logs (...)` and
   `CREATE TABLE IF NOT EXISTS player_game_appearances (...)` to `init_db()`
2. Add migration: `ALTER TABLE player_stats ADD COLUMN on_off_asterisk INTEGER DEFAULT 0`
   — stores 1 if the on_off_diff was set to 0 due to the 3% threshold, else 0

---

## Backend/API Changes (`app.py`)

- The `on_off_asterisk` flag should be returned in all player stats API responses
  alongside `on_off_diff`
- No other logic changes needed; K.Y.L.E. calculation uses `on_off_diff` as before

---

## Frontend Changes

- In the player stats table (both the main season view and any cumulative view),
  if `on_off_asterisk == 1`, display the `on_off_diff` value (which will be 0) with
  an asterisk and a tooltip/title like:
  *"Fewer than 3% of team games missed; on/off diff set to 0"*

---

## Rate Limiting & Caching Strategy

Basketball-reference rate-limits aggressively. This scrape is significantly more
expensive than the current single-season scrape:

| Operation | Pages per season | Seasons | Total pages |
|-----------|-----------------|---------|-------------|
| Team game logs | ~30 teams | 19 | ~570 |
| Player game logs | ~450 players | 19 | ~8,550 |
| **Total** | | | **~9,120** |

Mitigation:
- Team game log pages are fetched **once per team-season** and cached in `team_game_logs`;
  subsequent calls for the same team reuse DB data (no HTTP request)
- Player game log pages are only re-fetched if `player_game_appearances` has no rows
  for `(player_id, year, season_type)` — i.e. incremental/resumable
- Maintain the existing `time.sleep(2)` between requests; consider increasing to 3–4s
  for bulk historical scrapes
- Expose a separate CLI entry point (e.g. `python scraper.py --backfill --start 1978
  --end 1996`) that loops over each season year and calls `run_scrape()` for both
  regular season and playoffs. This does **everything in one pass** — advanced stats,
  totals, BPM, and the on/off approximation — so you do not need a separate step to
  import the other per-season values. After the backfill completes, each subsequent
  season can be added individually via the normal update flow (or the same CLI with a
  single year). The backfill should not be triggered by the live update button in the UI.

---

## Open Questions / Risks

1. **DNP representation in player game logs:** Need to confirm whether
   basketball-reference game logs include rows for games where a player was on the
   roster but didn't play, or whether those rows are simply absent. If absent, the
   team-game-log diff approach handles it correctly. If they're present with MP=0,
   we need to filter them out.

2. **Team abbreviation consistency:** bbref uses different abbreviations across eras
   (e.g. NJN vs BRK, SEA vs OKC). The team stints extracted from advanced data and
   the team game log URLs must use the same abbreviation. May need a mapping table for
   historical franchises.

3. ~~**Roster vs. active:**~~ This is a non-issue. Because we are doing pure set math —
   (all team games) − (games player appeared in with MP > 0) = missed games — it does
   not matter *why* a player missed a game. Injured, suspended, DNP-CD, or simply not
   yet on the team all fall into the "off" bucket correctly. No additional data needed.

4. **Backfill runtime:** The page counts in the caching table above are totals across
   all 19 seasons. Per-season cost is roughly ~30 team pages + ~450 player pages ≈
   480 pages × 3s ≈ **~24 minutes per season**, or **~7.5 hours for the full 1978–1996
   backfill**. After the one-time backfill, incremental per-season scrapes are cheap
   because cached team and player game log rows are skipped. The full backfill should
   be a one-time offline operation, not tied to the live update button.

---

## Implementation Order

1. Add new DB tables and `on_off_asterisk` column migration in `db.py`
2. Implement `_scrape_team_game_log()` and verify against a known season
3. Implement `_scrape_player_game_log()` and verify DNP handling (Open Question #1)
4. Implement `_get_player_team_stints()` using existing advanced data
5. Implement `_compute_pre97_on_off()` and unit-test against a few well-known players
6. Integrate into `run_scrape()` with the `year < 1997` branch
7. Add `--backfill` CLI flag and run historical scrape
8. Update API to return `on_off_asterisk`
9. Update frontend to display asterisk with tooltip
