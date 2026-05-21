# Database Reference

All data lives in a single SQLite file: **`nba.db`** at the project root.
Connection helpers are in `db.py` (`get_conn()` and `db_conn()` context manager).
`conn.row_factory = sqlite3.Row` is always set, so rows can be accessed by column name.

---

## Table Overview

| Table | Rows (approx) | Purpose |
|---|---|---|
| `seasons` | 98 | One row per season + season-type combination |
| `players` | 3 938 | Player identity (name, bbref URL, birthdate, NBA ID) |
| `selected_players` | 3 948 | Which players are included in analysis for a given season |
| `player_stats` | 30 436 | Per-player, per-season advanced stats |
| `player_game_appearances` | 415 000 | Every game a player appeared in (regular + playoffs) |
| `team_game_logs` | 40 801 | Team-level game results (score margin) |
| `playoff_series_rounds` | 804 | Maps team matchups → playoff round label |
| `bbref_playoff_fetch_log` | 3 593 | Tracks which bbref playoff pages have been fetched |
| `league_game_log_fetch_log` | 28 | Tracks which NBA.com league game-log pages have been fetched |
| `watched_playoff_games` | 463 | Kyle's personal playoff game watch log |
| `watched_game_players` | 1 937 | Players tagged to a watched game |

---

## Table Schemas

### `seasons`
One row per (season_year, season_type) pair. Used as a foreign key by `player_stats` and `selected_players`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Auto-increment |
| `label` | TEXT | Human-readable, e.g. `"2026 Regular Season"` |
| `season_year` | INTEGER | The *ending* year of the season, e.g. `2026` for the 2025-26 season |
| `season_type` | TEXT | `'regular'` or `'playoff'` |

### `players`
Master player table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT | Full name, unique |
| `bbref_url` | TEXT | Basketball-Reference URL slug |
| `birthdate` | TEXT | ISO date string (`YYYY-MM-DD`) |
| `nba_id` | INTEGER | NBA.com player ID |

### `selected_players`
Controls which (player, season) pairs are included in analysis views.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `player_id` | INTEGER | FK → `players.id` |
| `season_id` | INTEGER | FK → `seasons.id` |

### `player_stats`
Advanced stats per player per season. Join to `seasons` for year/type, join to `players` for name.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `player_id` | INTEGER | FK → `players.id` |
| `season_id` | INTEGER | FK → `seasons.id` |
| `minutes` | REAL | Total minutes played |
| `usage_rate` | REAL | Usage % |
| `true_shooting_pct` | REAL | TS% |
| `assist_rate` | REAL | Assist % |
| `turnover_pct` | REAL | Turnover % |
| `on_court_rating` | REAL | Net rating while on court |
| `on_off_diff` | REAL | On/off differential |
| `bpm` | REAL | Box Plus/Minus |
| `defense` | REAL | Defensive BPM or rating |
| `position` | TEXT | Position string |
| `playoff_games` | INTEGER | Number of playoff games played that season |
| `on_off_asterisk` | INTEGER | `1` = on/off data unreliable (flag) |

### `player_game_appearances`
Every game a player appeared in. Used for on/off calculations and game-level filtering.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `player_id` | INTEGER | FK → `players.id` |
| `season_year` | INTEGER | Ending year of season |
| `season_type` | TEXT | `'regular'` or `'playoff'` |
| `team_abbr` | TEXT | 3-letter team abbreviation the player was on |
| `game_date` | TEXT | ISO date (`YYYY-MM-DD`) |
| `opp_abbr` | TEXT | Opponent 3-letter abbreviation |
| `round` | TEXT | Playoff round label (NULL for regular season); matches `playoff_series_rounds.round` |

### `team_game_logs`
One row per team per game. Used to compute team win/loss and score margin context.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `team_abbr` | TEXT | 3-letter team abbreviation |
| `season_year` | INTEGER | Ending year of season |
| `season_type` | TEXT | `'regular'` or `'playoff'` |
| `game_date` | TEXT | ISO date |
| `margin` | REAL | Final score margin (positive = win) |

### `playoff_series_rounds`
Maps team-vs-team matchups to a round label. Note: both orderings of (team1, team2) are stored.

| Column | Type | Notes |
|---|---|---|
| `season_year` | INTEGER PK | |
| `team1_abbr` | TEXT PK | |
| `team2_abbr` | TEXT PK | |
| `round` | TEXT | E.g. `'First Round'`, `'Conference Semifinals'`, `'Conference Finals'`, `'NBA Finals'` |

### `bbref_playoff_fetch_log`
Tracks scraping progress for Basketball-Reference playoff pages.

| Column | Type | Notes |
|---|---|---|
| `player_id` | INTEGER PK | FK → `players.id` |
| `season_year` | INTEGER PK | |
| `fetch_status` | TEXT | `'success'`, `'not_found'`, `'error'`, etc. |
| `fetched_at` | TEXT | ISO datetime |

### `league_game_log_fetch_log`
Tracks which NBA.com league game-log pages have been fetched.

| Column | Type | Notes |
|---|---|---|
| `season_year` | INTEGER PK | |
| `season_type` | TEXT PK | `'regular'` or `'playoff'` |
| `player_or_team` | TEXT PK | `'player'` or `'team'` |
| `fetched_at` | TEXT | ISO datetime |

### `watched_playoff_games`
Kyle's personal log of playoff games he has watched.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `home_team` | TEXT | Full team name (e.g. `'Lakers'`) |
| `away_team` | TEXT | Full team name |
| `winner_team` | TEXT | Full team name of winner |
| `date_watched` | TEXT | ISO date Kyle watched the game |
| `game_year` | INTEGER | The NBA season ending year the game was played in |
| `conference` | TEXT | `'East'`, `'West'`, or `'Finals'` |
| `round` | TEXT | `'First Round'`, `'Conference Semifinals'`, `'Conference Finals'`, `'NBA Finals'` |
| `game_of_round` | INTEGER | Game number within the series (1–7) |
| `best_player_id` | INTEGER | FK → `players.id` — best player Kyle designated |
| `notes` | TEXT | Kyle's free-text notes on the game |

### `watched_game_players`
Many-to-many: players featured/tagged in a watched game.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `game_id` | INTEGER | FK → `watched_playoff_games.id` |
| `player_id` | INTEGER | FK → `players.id` |

---

## Key Relationships

```
seasons ──────────────┐
                       ├── player_stats (season_id)
                       └── selected_players (season_id)

players ───────────────┬── player_stats (player_id)
                       ├── selected_players (player_id)
                       ├── player_game_appearances (player_id)
                       ├── bbref_playoff_fetch_log (player_id)
                       ├── watched_playoff_games (best_player_id)
                       └── watched_game_players (player_id)

watched_playoff_games ─── watched_game_players (game_id)

player_game_appearances.round  ←→  playoff_series_rounds.round
player_game_appearances.season_year ←→ seasons.season_year
```

## Common Query Patterns

```sql
-- Get player stats with name and season label
SELECT p.name, s.season_year, s.season_type, ps.*
FROM player_stats ps
JOIN players p ON p.id = ps.player_id
JOIN seasons s ON s.id = ps.season_id;

-- Get all playoff appearances for a player in a given year
SELECT * FROM player_game_appearances
WHERE player_id = ? AND season_year = ? AND season_type = 'playoff';

-- Map a game appearance to its playoff round
SELECT pga.*, psr.round
FROM player_game_appearances pga
JOIN playoff_series_rounds psr
  ON psr.season_year = pga.season_year
 AND psr.team1_abbr = pga.team_abbr
 AND psr.team2_abbr = pga.opp_abbr;
```
