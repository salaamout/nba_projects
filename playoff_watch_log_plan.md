# Playoff Game Watch Log — Feature Plan

## Overview

Add the ability to log every playoff game you watch, track which players were involved and who was the best player, and surface that data in a leaderboard and on individual player pages.

---

## 1. Data Model (SQLite)

### New Table: `watched_playoff_games`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `home_team` | TEXT | Full team name (e.g. "Lakers") |
| `away_team` | TEXT | Full team name |
| `winner_team` | TEXT | Winning team's full name, nullable (not in CSV — enter manually on new games) |
| `date_watched` | TEXT | ISO date — when *you* watched it |
| `game_year` | INTEGER | Year the actual game was played |
| `conference` | TEXT | "East", "West", or "Finals" |
| `round` | TEXT | "First Round", "Second Round", "Conference Finals", "NBA Finals" |
| `game_of_round` | INTEGER | Game 1–7 |
| `best_player_id` | INTEGER | FK → `players.id`, nullable |
| `notes` | TEXT | Optional free text |

### New Table: `watched_game_players`

Links games to the important players involved (many-to-many).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `game_id` | INTEGER | FK → `watched_playoff_games.id` |
| `player_id` | INTEGER | FK → `players.id` |

---

## 2. CSV Structure (Resolved)

The CSV (`data_for_import/Playoff Game Watch Log - Sheet1.csv`) has these columns:

| Column | Header | Notes |
|---|---|---|
| 1 | `Watched` | Date you watched (M/D/YYYY) |
| 2 | `Year` | Year the game was played |
| 3 | `Round` | **Numeric 1–4** (see mapping below) |
| 4 | `Conference` | "East", "West", or "Finals" |
| 5 | `Game` | Game number in the series (1–7) |
| 6 | *(empty header)* | **Best player — last name only** (e.g. "Abdul-Jabbar") |
| 7 | `Home` | Home team full name (e.g. "Lakers") |
| 8 | `Players` | Home team important players, **semicolon-separated with annotations** (e.g. `Bird (MVP); McHale (6MOY);`) |
| 9 | `Away` | Away team full name |
| 10 | `Important Players` | Away team important players, same format as column 8 |
| 11 | `Notes` | Free-text game notes |

**Round number mapping:**
- 1 → First Round
- 2 → Second Round
- 3 → Conference Finals
- 4 → NBA Finals

⚠️ **No winner column exists in the CSV.** The winner field will be left blank/null on import and can be filled in later, or we can drop the winner field from the schema entirely. See decision #2 below.

## 3. CSV Import

We'll write a one-time import script (`import_watch_log.py`) that:

1. Reads the CSV.
2. **Strips annotations** from player name cells — everything from ` (` onwards — to get bare names (e.g. `Bird (MVP)` → `Bird`).
3. **Merges** home players (column 8) and away players (column 10) into a single list of important players per game.
4. **Matches best player** (last name only, column 6) and important players against the existing `players` table by last name. Since many players share last names (e.g. both "Jones" rows in the same game), the match should be best-effort and **flagged for review**.
5. Inserts rows into `watched_playoff_games` and `watched_game_players`.
6. Prints a report of any player names it couldn't confidently match so you can resolve them manually.

**Importer edge cases to handle:**
- Team names in the CSV are full names ("Lakers"), not abbreviations — store as-is in the `home_team`/`away_team` columns since there's no teams table.
- Some player name cells have trailing semicolons and whitespace that need trimming.
- Rows where the best player cell is empty should leave `best_player_id` as NULL.

---

## 3. Backend API Routes (Flask / `app.py`)

### Game CRUD

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/watched_games` | List all logged games (with filters: `year`, `round`, `conference`) |
| `POST` | `/api/watched_games` | Create a new game log entry |
| `GET` | `/api/watched_games/<id>` | Get a single game with players |
| `PUT` | `/api/watched_games/<id>` | Update a game |
| `DELETE` | `/api/watched_games/<id>` | Delete a game |

### Player Leaderboard

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/watched_games/best_player_leaderboard` | Ranked list of players by # of times chosen as best player |

### Helper

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/players_for_year?year=<year>` | Return all players who have stats in `game_year` — used to populate the "important players" and "best player" dropdowns in the UI |

### Player page integration

Add `best_player_count` and the list of games where a player was best player to the existing `/api/player/<id>` response (or add a sub-route like `/api/player/<id>/watch_log`).

---

## 4. Frontend

### New Page: `/watch_log` — Game Entry & Leaderboard

**Top half — Leaderboard table:**
- Rank, Player Name, # Times Best Player, link to player page.

**Game log table:**
- All watched games in reverse chronological order.
- Columns: Date Watched, Year, Home, Away, **Winner** (inline-editable — clicking the cell reveals a dropdown of the two teams or blank; saves immediately on change via `PUT /api/watched_games/<id>`), Conference, Round, Game #, Best Player, Important Players, Notes.
- Each row has an Edit and Delete button.

**"Log a Game" form / modal:**
- Home team (text or dropdown of teams)
- Away team
- Winner (radio: Home / Away)
- Date Watched (date picker, defaults to today)
- Year of Game (number)
- Conference (dropdown: East / West / Finals)
- Round (dropdown)
- Game of Round (1–7)
- Important Players (multi-select, populated from `/api/players_for_year?year=<year>` when year is filled in)
- Best Player (single-select from the same list, or from the Important Players selection)

### Player Page Integration

Add a new section to `player.html` / `player.js`:
- "Playoff Watch Log" section.
- Shows `Best Player` count prominently (e.g. "Best Player in X watched games").
- Table of games where they were the best player: Year, Matchup, Round, Game #, Date Watched.
- Optionally: games where they were listed as an important player but not the best.

---

## 5. Files to Create / Modify

| File | Action | Summary |
|---|---|---|
| `db.py` | Modify | Add `watched_playoff_games` and `watched_game_players` table creation + migration |
| `app.py` | Modify | Add all new API routes |
| `import_watch_log.py` | Create | One-time CSV import script |
| `templates/watch_log.html` | Create | New page template |
| `static/watch_log.js` | Create | JS for leaderboard, game log table, and entry form |
| `templates/player.html` | Modify | Add watch log section |
| `static/player.js` | Modify | Fetch and render watch log data on player page |
| `static/style.css` | Modify | Any new styles needed |

---

## 6. Decisions (Resolved)

1. **Winner field** — Keep as a nullable TEXT field. Historical imports leave it NULL; new games entered via the form can include the winner. The watch log table should support **inline editing of the winner cell** (click-to-edit dropdown showing the home team, away team, or blank) so you can work through the historical rows and fill them in without opening the full edit form each time.

2. **Ambiguous last-name matches** — Importer will first try to narrow by picking the player who has stats for that `game_year`. If still ambiguous after that, flag for manual review and skip the link (don't guess).

3. **Players not in DB** — Create a stub row in `players` (name only, no stats) so the link is preserved. The stub can be enriched later if stats are ever scraped for that era.

4. **Notes field** — Include in the schema, shown on the watch log page, and available as a textarea in the new game entry form.

---

## 7. Suggested Build Order

1. **DB schema** — Add tables in `db.py`, run migration.
2. **Import script** — Get historical data in immediately after schema is ready.
3. **API routes** — Build and test with curl/Postman.
4. **Leaderboard & log page** — New HTML/JS page.
5. **Player page integration** — Add the watch log section last since it depends on all the above.
