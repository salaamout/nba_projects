import sqlite3
conn = sqlite3.connect('nba.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM watched_playoff_games WHERE (home_team='SAS' OR away_team='SAS') ORDER BY date_watched DESC LIMIT 20").fetchall()
for r in rows:
    print(dict(r))
