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
