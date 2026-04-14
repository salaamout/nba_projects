Project plan for automating my K.Y.L.E. nba stats spreasheet into a web app

Data Sources:
1. Scraped from basketball-reference.com

Fields:
1. Player Name
2. Minutes
3. Usage Rate
4. Points per shot
  - true shooting percentage * 2
5. Assist Rate
6. Turnover Percentage
7. On Court Rating
8. On/Off Difference
9. BPM
10. Defense
  - This is assigned by Kyle directly based on feel because there isn't really a good defensive stat

K.Y.L.E. Formula
For each field (other than player name), compute the best and worst value. Those are assigned +1 and -1 respectively. Interpolate between those for the values in between best and worst. Then sum together all the fields (other than name) to produce the K.Y.L.E. rating for each player.
Variations:
1. The -1 value for minutes is the max value divided by 2
2. More turnovers are bad, so the "best" value is the lowest value, and "worst" is the highest

Which players?
All players that play minutes in a given season should be in the database, and updated whenever the numbers are updated. However, the K.Y.L.E. best/worst values should come from a limited set. That set will be chosen by the user via a search bar/drop down list

Update
The database should live on this laptop. The web app should have a button for updating, where it re-populates the information from the various websites and recalculates the K.Y.L.E values

Display
There should be a table with the limited set, which displays each field, the value for that field that will be used in the final sum, and the final sum K.Y.L.E. rating. The table should be sortable by any field.

Implementation plan

**Tech Stack**
- Backend: Python with Flask (simple, minimal boilerplate, easy to maintain)
- Database: SQLite (local, no server required, fits well with Flask via SQLAlchemy or direct sqlite3)
- Frontend: Plain HTML + JavaScript with a small amount of CSS (no build toolchain needed)

**Future Milestones (not yet planned in detail)**
- Milestone 2: Add 2026 playoff season data
- Milestone 3: Add prior regular and playoff season data
- Milestone 4: Cumulative multi-season K.Y.L.E. stat and display

---

**Milestone 1: 2026 Regular Season — Scrape, Store, and Display**

Goal: A locally-running web app that scrapes 2026 NBA regular season stats from basketball-reference.com, stores them in a local SQLite database, calculates K.Y.L.E. ratings for a user-selected set of players, and displays results in a sortable table.

**Database Schema**

`seasons` table
- id (integer, primary key)
- label (text) — e.g. "2026 Regular Season"
- season_year (integer) — e.g. 2026
- season_type (text) — "regular" or "playoff"

`players` table
- id (integer, primary key)
- name (text)

`player_stats` table
- id (integer, primary key)
- player_id (foreign key → players)
- season_id (foreign key → seasons)
- minutes (real) — total minutes for the season (MP column from totals table)
- usage_rate (real)
- true_shooting_pct (real) — stored raw; points_per_shot = ts% * 2 is calculated at display time
- assist_rate (real)
- turnover_pct (real)
- on_court_rating (real) — OnCourt column from play-by-play table (+/- per 100 poss)
- on_off_diff (real) — On-Off column from play-by-play table
- bpm (real)
- defense (real, nullable) — manually entered by user; inline-editable in the table
- position (text, nullable) — "Guard", "Forward", or "Center"; manually assigned per player per season; inline-editable in the table

`selected_players` table
- id (integer, primary key)
- player_id (foreign key → players)
- season_id (foreign key → seasons)
- (unique constraint on player_id + season_id)

**Scraping**
- Use Python `requests` + `BeautifulSoup` to scrape basketball-reference.com
- Three league-wide tables, one HTTP request each:
  1. **Advanced stats** (`/leagues/NBA_2026_advanced.html`) — Usage Rate, Assist Rate, Turnover %, BPM, True Shooting %
  2. **Per-game / totals** (`/leagues/NBA_2026_totals.html`) — total Minutes (MP column)
  3. **Play-by-play** (`/leagues/NBA_2026_play-by-play.html`) — OnCourt (+/- per 100 poss) and On-Off columns
- basketball-reference embeds some tables as HTML comments; use BeautifulSoup with `html.parser` and uncomment as needed
- Players are matched across tables by name; bbref player IDs can be used as a tiebreaker if available
- Scraped stats are upserted into `player_stats` for the matching season row (replace existing data for that season on each update)

**Backend (Flask)**
- `GET /api/players?season_id=1` — returns all players with stats for a season
- `GET /api/selected?season_id=1` — returns the selected player set with K.Y.L.E. values calculated
- `POST /api/selected` — add a player to the selected set `{ player_id, season_id }`
- `DELETE /api/selected/<id>` — remove a player from the selected set
- `POST /api/update?season_id=1` — triggers re-scrape and recalculates K.Y.L.E. for the season
- `PATCH /api/stats/<player_stats_id>` — update the defense and/or position value for a player (inline table edit)
- K.Y.L.E. calculation logic lives in a standalone Python module (`kyle.py`) so it can be reused across seasons later

**K.Y.L.E. Calculation (kyle.py)**
- Inputs: list of player_stats rows for the selected set
- For each numeric field, find the best and worst value across the selected set
  - Minutes: best = max, worst = max / 2 (values below worst get -1)
  - Turnover %: best = lowest value, worst = highest value
  - All others: best = highest, worst = lowest
- Normalize each field to [-1, +1] via linear interpolation
- Sum all normalized fields to produce the K.Y.L.E. rating
- Return per-player: each raw value, each normalized value, and the final sum

**Frontend (HTML + JS)**
- Single-page app (`index.html`) served by Flask
- Season selector dropdown (populated from the `seasons` table; only 2026 Regular Season for Milestone 1)
- Player search/dropdown to add players to the selected set (searches `players` table by name)
- Sortable table displaying the selected set:
  - Columns: Player Name, Position, Minutes, Usage Rate, Points Per Shot, Assist Rate, Turnover %, On Court Rating, On/Off Diff, BPM, Defense, K.Y.L.E. Rating
  - Each row shows the normalized value used in the sum, with the raw value shown below it or on hover
  - Defense column is inline-editable (click to edit, sends PATCH to backend)
  - Position column is inline-editable via a dropdown (Guard / Forward / Center), sends PATCH to backend
  - Table header click sorts ascending/descending
- "Update" button triggers `POST /api/update`, shows a loading spinner, then refreshes the table
- "Remove" button on each row removes a player from the selected set

**Project File Structure**
```
nba_projects/
├── app.py              # Flask app and routes
├── kyle.py             # K.Y.L.E. calculation logic
├── scraper.py          # basketball-reference scraping logic
├── db.py               # SQLite connection and schema setup
├── nba.db              # SQLite database (git-ignored)
├── templates/
│   └── index.html      # Main page
├── static/
│   ├── style.css
│   └── app.js          # Frontend logic
├── requirements.txt
└── plan.md
```

**Development Steps (Milestone 1)**
1. Set up the project: create virtualenv, install Flask + requests + BeautifulSoup4
2. Implement `db.py`: define schema, seed the "2026 Regular Season" row in `seasons`
3. Implement `scraper.py`: scrape and parse the relevant basketball-reference tables for 2026
4. Implement `kyle.py`: normalization and K.Y.L.E. sum logic
5. Implement `app.py`: wire up Flask routes
6. Implement `index.html` + `app.js`: player search, table display, sorting, inline defense edit, update button
7. End-to-end test: run update, verify data in DB, verify K.Y.L.E. values in the table