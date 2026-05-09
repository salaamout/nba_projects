# K.Y.L.E. NBA Stats

A locally-running web app that scrapes NBA stats from basketball-reference.com, stores them in a local SQLite database, calculates K.Y.L.E. ratings for a user-selected set of players, and displays results in a sortable table.

## Setup

### 1. Create and activate a virtual environment

```bash
cd nba_projects
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Initialize the database

This creates `nba.db` and seeds the 2026 Regular Season row:

```bash
python db.py
```

## Running the App

```bash
python app.py
```

Then open your browser and navigate to:

```
http://127.0.0.1:5000
```

## Using the App

### Fetch / update stats
Click the **⟳ Update Data** button to scrape the latest 2026 regular season stats from basketball-reference.com. This takes ~10 seconds (rate-limited to be polite to the site). You must do this at least once before any players appear in the search box.

### Add players to your comparison set
Type a player's name into the **Add player…** search box. Matching players will appear in a dropdown — click one (or press Enter) to add them to the table.

### Reading the table
Each stat column shows two values:
- **Large number** — the normalized K.Y.L.E. score for that field (−1 to +1), color-coded green (good) to red (bad)
- **Small number below** — the raw stat value

The **K.Y.L.E.** column is the sum of all normalized fields (range: −9 to +9).

### Sorting
Click any column header to sort by that column. Click again to reverse the sort direction.

### Editing Defense and Position
These two fields are manually assigned:
- **Defense** — click the raw value (or "click to set") to type in a number, then press Enter or click away to save
- **Position** — click the displayed position (or "—") to choose Guard / Forward / Center from a dropdown

Changes save instantly and K.Y.L.E. ratings recalculate automatically.

### Removing a player
Click the **✕** button below a player's name to remove them from the selected set. Their stats remain in the database; they can be re-added at any time.

## K.Y.L.E. Formula

Each of the 9 stat fields is normalized to a −1 to +1 scale based on the best and worst values **within the currently selected set**:

| Field | Best | Worst |
|---|---|---|
| Minutes | max | max ÷ 2 |
| Usage Rate | highest | lowest |
| Points Per Shot (TS% × 2) | highest | lowest |
| Assist Rate | highest | lowest |
| Turnover % | **lowest** | **highest** |
| On-Court Rating | highest | lowest |
| On/Off Diff | highest | lowest |
| BPM | highest | lowest |
| Defense | highest | lowest |

The normalized scores are summed to produce the final K.Y.L.E. rating. Because best/worst are derived from the selected set, ratings change as players are added or removed.

### Special fields

| Field | Description |
|---|---|
| `points_per_shot` | Derived as `true_shooting_pct × 2`. Converts TS% into an approximate points-per-shot estimate on a 0–2 scale. Stored on the fly; not saved to the database. |
| `on_off_asterisk` | A flag (boolean) set to `True` when a player's on/off differential is considered unreliable — e.g., the player shared the court with an unusually dominant or weak supporting cast in the sample. When set, `on_off_diff_norm` is replaced with the average of the player's other normalised scores rather than using the raw on/off number. |
| `watch_kyle` | A bonus/penalty (−1 to +1) derived from the user's **Playoff Watch Log**. Players who appeared in highly-rated games the user watched receive a positive boost; players in unwatched or low-rated games receive a smaller one. Added to the K.Y.L.E. sum as a tenth component. |

### Playoff mode

When `season_type = "playoffs"`, the `minutes` field is **excluded** from the rating sum. Playoff minute totals are much smaller than regular-season totals, so normalising them the same way would unfairly penalise starters who log fewer minutes in a short series.

### `calculate` vs `calculate_all`

| Function | Bounds source | Clamped to ±1? | Use case |
|---|---|---|---|
| `calculate(rows)` | Derived from the passed-in rows | Yes | Selected-player comparison table |
| `calculate_all(all_rows, selected_bounds)` | Passed in from the selected set | No | "All players" view — players outside the selected range can exceed ±1 |

## Data Model

The app stores everything in a local SQLite file (`nba.db`). The key tables are:

### `seasons`

One row per season scraped. Each season is uniquely identified by `(season_year, season_type)`.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `season_year` | INTEGER | The *ending* year of the season (e.g. `2026` for the 2025–26 season) |
| `season_type` | TEXT | `"regular"` or `"playoffs"` |
| `label` | TEXT | Display label (e.g. `"2026 Regular Season"`) |

### `players`

One row per player ever seen. Re-used across seasons.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-increment ID |
| `name` | TEXT | Full player name as scraped from basketball-reference |
| `bbref_url` | TEXT | Relative URL to the player's basketball-reference page (used for birthdate scraping) |

### `player_stats`

One row per (player, season). Contains the raw scraped numbers.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | |
| `player_id` | INTEGER FK | References `players.id` |
| `season_id` | INTEGER FK | References `seasons.id` |
| `minutes` | REAL | Total minutes played |
| `usage_rate` | REAL | Usage percentage (% of team plays used while on court) |
| `true_shooting_pct` | REAL | True Shooting % (accounts for 3-pointers and free throws) |
| `assist_rate` | REAL | Assist rate (% of teammate FGs assisted while on court) |
| `turnover_pct` | REAL | Turnover rate (turnovers per 100 plays) |
| `on_court_rating` | REAL | Net rating while player is on the court |
| `on_off_diff` | REAL | On-court rating minus off-court rating |
| `on_off_asterisk` | INTEGER | `1` if the on/off differential is flagged as unreliable (see above) |
| `bpm` | REAL | Box Plus/Minus |
| `defense` | REAL | Manually assigned defensive rating (−1 to +1 scale) |
| `position` | TEXT | Manually assigned position: `"G"`, `"F"`, or `"C"` |

### `selected_players`

Tracks which players have been added to the comparison table for each season.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | |
| `player_id` | INTEGER FK | References `players.id` |
| `season_id` | INTEGER FK | References `seasons.id` |

### `player_game_appearances`

One row per (player, playoff game). Used by the Suggest Game and Watch Log features.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | |
| `player_id` | INTEGER FK | References `players.id` |
| `season_year` | INTEGER | Year of the playoff series |
| `season_type` | TEXT | Always `"playoffs"` |
| `game_of_round` | TEXT | Round + game identifier (e.g. `"R1G3"`) |
| `team` | TEXT | Team abbreviation the player was on |
| `opponent` | TEXT | Opponent team abbreviation |

### `watched_playoff_games`

Games the user has logged as watched, with a personal rating.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | |
| `game_year` | INTEGER | Playoff year |
| `game_of_round` | TEXT | Round + game identifier |
| `team1` / `team2` | TEXT | The two teams |
| `rating` | REAL | User's personal rating for the game (used to compute `watch_kyle`) |

### `watched_game_players`

Junction table linking watched games to the players who appeared in them.

| Column | Type | Description |
|---|---|---|
| `game_id` | INTEGER FK | References `watched_playoff_games.id` |
| `player_id` | INTEGER FK | References `players.id` |

## Project Structure

```
nba_projects/
├── app.py              # Flask app and API routes
├── kyle.py             # K.Y.L.E. normalization and rating logic
├── scraper.py          # basketball-reference.com scraping
├── db.py               # SQLite schema setup and connection helper
├── nba.db              # SQLite database (created on first run, git-ignored)
├── templates/
│   └── index.html      # Single-page frontend
├── static/
│   ├── style.css
│   └── app.js          # Frontend logic
├── requirements.txt
├── plan.md
└── README.md
```

## Data Sources

All stats are scraped from [basketball-reference.com](https://www.basketball-reference.com):

- `/leagues/NBA_2026_advanced.html` — Usage%, Assist%, Turnover%, BPM, True Shooting%
- `/leagues/NBA_2026_totals.html` — Total Minutes
- `/leagues/NBA_2026_play-by-play.html` — On-Court Rating, On/Off Differential
