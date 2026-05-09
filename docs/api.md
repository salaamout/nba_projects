# NBA Projects — API Reference

All endpoints are served by the Flask app (`app.py`) on `http://localhost:5000` by default.  
All request and response bodies are JSON unless otherwise noted.

---

## Table of Contents

1. [Seasons](#seasons)
2. [Players](#players)
3. [Selected Set](#selected-set)
4. [All-Players Scored View](#all-players-scored-view)
5. [Cumulative K.Y.L.E.](#cumulative-kyle)
6. [Best N-Year K.Y.L.E.](#best-n-year-kyle)
7. [Suggest Game](#suggest-game)
8. [Stats Patch](#stats-patch)
9. [Scrape / Update](#scrape--update)
10. [Player History](#player-history)
11. [Watch Log](#watch-log)

---

## Seasons

### `GET /api/seasons`
Return all seasons ordered by year descending.

**Response `200`**
```json
[
  { "id": 1, "label": "2024 Playoffs", "season_year": 2024, "season_type": "playoffs" },
  ...
]
```

---

### `POST /api/seasons`
Create a new season.

**Request body**
```json
{ "season_year": 2025, "season_type": "regular" }
```
`season_type` must be `"regular"` or `"playoffs"`.

**Responses**
| Status | Meaning |
|--------|---------|
| `201` | Season created; body is the new season object |
| `400` | Missing or invalid fields |
| `409` | Season already exists; body contains `"id"` of the existing row |

---

### `GET /api/seasons/<season_id>/nearest_selected`
Return the player IDs from the season whose selected set is nearest (by year) to `season_id`. Useful for copying a roster across seasons.

**Response `200`**
```json
{ "player_ids": [12, 34, 56], "source_season_id": 3 }
```
Returns `{ "player_ids": [] }` if no other season has a selected set.

**Response `404`** — season not found.

---

### `DELETE /api/seasons/<season_id>`
Delete a season and cascade-delete its `selected_players` and `player_stats` rows.

**Responses**
| Status | Meaning |
|--------|---------|
| `200` | `{ "ok": true }` |
| `404` | Season not found |

---

## Players

### `GET /api/players?season_id=<id>`
Return every player who has stats rows for the given season.

**Query params**
| Param | Type | Required |
|-------|------|----------|
| `season_id` | int | ✅ |

**Response `200`**
```json
[{ "id": 7, "name": "LeBron James" }, ...]
```

**Response `400`** — `season_id` missing.

---

### `GET /api/players_for_year?year=<year>`
Return every player who has stats in any season for the given calendar year.

**Query params**
| Param | Type | Required |
|-------|------|----------|
| `year` | int | ✅ |

**Response `200`**
```json
[{ "id": 7, "name": "LeBron James" }, ...]
```

**Response `400`** — `year` missing.

---

## Selected Set

The *selected set* is the group of players used as the normalisation reference for K.Y.L.E. ratings in a season.

### `GET /api/selected?season_id=<id>`
Return the selected players for a season, fully scored with K.Y.L.E. ratings.  
For playoff seasons, `watch_kyle`, `watch_best_count`, `watch_total_watched`, and `watch_raw_score` fields are also populated.

**Response `200`** — array of scored player objects.  
**Response `400`** — `season_id` missing.

---

### `POST /api/selected`
Add a player to the selected set for a season.

**Request body**
```json
{ "player_id": 7, "season_id": 2 }
```

**Responses**
| Status | Meaning |
|--------|---------|
| `201` | `{ "ok": true }` |
| `400` | Missing fields |

---

### `DELETE /api/selected/<selected_id>`
Remove a single selected-player row by its own `id` (not the player ID).

**Response `200`** — `{ "ok": true }`

---

### `DELETE /api/selected?season_id=<id>`
Clear the entire selected set **and** all `player_stats` rows for a season.

**Response `200`** — `{ "ok": true }`  
**Response `400`** — `season_id` missing.

---

## All-Players Scored View

### `GET /api/all_players?season_id=<id>`
Return every player with stats for a season, scored against the selected-set bounds.  
Unlike `/api/selected`, norms are **not clamped** to `[-1, +1]` (uses `calculate_all`).

**Response `200`** — array of player objects with K.Y.L.E. fields.  
**Response `400`** — `season_id` missing.

---

## Cumulative K.Y.L.E.

### `GET /api/cumulative_kyle`
Return per-player cumulative K.Y.L.E. totals across all selected seasons.

**Response `200`**
```json
[
  { "player_id": 7, "name": "LeBron James", "total_kyle": 14.23, "seasons": 6 },
  ...
]
```

---

## Best N-Year K.Y.L.E.

### `GET /api/best3year?window=<n>`
Return each player's best consecutive N-year K.Y.L.E. window.

**Query params**
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `window` | int | `3` | 1–20 |

**Response `200`**
```json
[
  {
    "player_id": 7,
    "name": "LeBron James",
    "best_window_start": 2011,
    "best_window_end": 2013,
    "window_total": 11.4
  },
  ...
]
```

---

## Suggest Game

### `GET /api/suggest_game?window=<n>&skip=<k>`
Suggest the best unwatched playoff game featuring two players whose peak K.Y.L.E. windows overlap.

**Query params**
| Param | Type | Default | Range |
|-------|------|---------|-------|
| `window` | int | `3` | 1–20 |
| `skip` | int | `0` | ≥ 0 |

Use `skip` to page through ranked candidates (0 = top suggestion, 1 = second-best, etc.).

**Response `200`**
```json
{
  "result": "ok",
  "game": { ... },
  "player1": { ... },
  "player2": { ... }
}
```

**Response `200`** with `"result": "none"` — no unwatched candidates found.  
**Response `500`** — unexpected server error.

---

### `GET /api/suggest_game_for_player?player_id=<id>&window=<n>&skip=<k>`
Suggest the best unwatched playoff game for a focal player against their highest-peak opponent.

**Query params**
| Param | Type | Default | Range | Required |
|-------|------|---------|-------|----------|
| `player_id` | int | — | — | ✅ |
| `window` | int | `3` | 1–20 | |
| `skip` | int | `0` | ≥ 0 | |

**Response `200`** — same shape as `/api/suggest_game`.  
**Response `400`** — `player_id` missing.  
**Response `500`** — unexpected server error.

---

## Stats Patch

### `PATCH /api/stats/<stats_id>`
Update editable fields on a `player_stats` row.

**Patchable fields**
| Field | Type |
|-------|------|
| `defense` | float |
| `position` | string |

**Request body**
```json
{ "defense": 2.5, "position": "SF" }
```

**Responses**
| Status | Meaning |
|--------|---------|
| `200` | `{ "ok": true }` |
| `400` | No patchable fields provided |

---

## Scrape / Update

### `POST /api/update?season_id=<id>`
Trigger a live scrape from Basketball-Reference for the given season and upsert player stats.

**Response `200`**
```json
{ "ok": true, "players_upserted": 15 }
```

**Response `400`** — `season_id` missing.  
**Response `500`** — scrape error; body contains `"error"` string.

---

## Player History

### `GET /api/player/<player_id>`
Return player metadata plus their per-season K.Y.L.E. history across all selected seasons.

**Response `200`**
```json
{
  "id": 7,
  "name": "LeBron James",
  "nba_id": 2544,
  "bbref_url": "https://www.basketball-reference.com/players/j/jamesle01.html",
  "birthdate": "1984-12-30",
  "seasons": [ { "season_year": 2024, "kyle_rating": 2.1, ... }, ... ]
}
```

---

### `GET /api/player/<player_id>/watch_log`
Return watch-log data (watched games and K.Y.L.E. scores) for a single player.

**Response `200`**
```json
{
  "player_id": 7,
  "name": "LeBron James",
  "watch_kyle": 1.4,
  "games": [ { "game_id": 5, "game_year": 2023, ... }, ... ]
}
```

---

## Watch Log

### `GET /api/watched_games`
List all logged watched games. Supports optional filters.

**Query params (all optional)**
| Param | Type | Description |
|-------|------|-------------|
| `year` | int | Filter by `game_year` |
| `round` | string | Filter by round (e.g. `"First Round"`) |
| `conference` | string | Filter by conference (`"East"` / `"West"`) |

**Response `200`**
```json
[
  {
    "id": 1,
    "home_team": "Boston Celtics",
    "away_team": "Miami Heat",
    "winner_team": "Boston Celtics",
    "game_year": 2023,
    "conference": "East",
    "round": "Second Round",
    "game_of_round": 3,
    "date_watched": "2024-01-15",
    "best_player_id": 7,
    "best_player_name": "LeBron James",
    "notes": "",
    "important_players": [{ "player_id": 7, "name": "LeBron James" }]
  },
  ...
]
```

---

### `POST /api/watched_games`
Log a newly watched game.

**Required fields**
| Field | Type |
|-------|------|
| `home_team` | string |
| `away_team` | string |
| `date_watched` | string (ISO 8601) |
| `game_year` | int |
| `conference` | string |
| `round` | string |
| `game_of_round` | int |

**Optional fields**
| Field | Type |
|-------|------|
| `winner_team` | string |
| `best_player_id` | int |
| `notes` | string |
| `player_ids` | int[] — IDs of important players to link |

**Responses**
| Status | Meaning |
|--------|---------|
| `201` | Created; body is the new game object |
| `400` | Missing required fields |

---

### `GET /api/watched_games/<game_id>`
Fetch a single watched game by ID, including its linked important players.

**Response `200`** — full game object (same shape as list items above).  
**Response `404`** — game not found.

---

### `PUT /api/watched_games/<game_id>`
Replace fields on an existing watched game.

**Updatable fields** — any of the fields accepted by `POST`, plus `player_ids` (replaces the existing player links).

**Response `200`** — updated game object.  
**Response `404`** — game not found.

---

### `DELETE /api/watched_games/<game_id>`
Delete a watched game record.

**Responses**
| Status | Meaning |
|--------|---------|
| `200` | `{ "ok": true }` |
| `404` | Game not found |

---

### `GET /api/watched_games/best_player_leaderboard`
Return a ranked leaderboard of players who have been named best player most often across all logged games.

**Response `200`**
```json
[
  {
    "player_id": 7,
    "name": "LeBron James",
    "best_count": 12,
    "total_watched": 30,
    "watch_kyle": 2.8
  },
  ...
]
```
