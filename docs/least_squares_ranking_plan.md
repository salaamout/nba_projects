# Least Squares Ranking — Planning Document

## Overview

Add a "Least Squares Score" column to the **Best N-Year** page that ranks
players by solving a least-squares system derived from pairwise playoff-game
comparisons, restricted to each player's peak N-year window.

---

## Background: How It Works

### Pairwise signal generation

For each watched playoff game:
1. Collect the players who appeared in that game **and** whose peak N-year
   window includes the game's season year.
2. If fewer than 2 such players remain, skip the game entirely.
3. If the game has a `best_player_id` and that player is in the filtered set:
   - Each pair `(best, non-best)` generates one equation: `s_best - s_other = 1`.
4. If there is no `best_player_id`, or the best player was filtered out: skip
   (a 0/0 game provides no ranking signal).

### Least-squares formulation

Collect all equations into matrix form:

```
A · s = b
```

- `s` — vector of unknown scores, one per player (length P)
- `b` — vector of `1`s, one per pairwise comparison equation (length E)
- `A` — matrix of shape (E × P); each row has `+1` in the winner's column
  and `−1` in the loser's column; all other entries are 0.

Because E >> P and the system is contradictory, solve:

```
minimize  ||A·s − b||²
```

via `numpy.linalg.lstsq`.

### Regularization (fixing the scale)

The system is rank-deficient: adding a constant to every score leaves all
differences unchanged. Fix this by appending one extra equation:

```
sum(s) = 0   →   row of all 1s, b-entry = 0
```

This is the standard "zero-mean" anchor and makes the solution unique.

---

## Implementation Plan

### Step 1 — Backend utility function (`kyle.py` or a new `ranking.py`)

Create `compute_least_squares_scores(comparisons)`:

```python
# comparisons: list of (winner_id, loser_id)
# returns: dict of player_id -> ls_score (float)
```

- Build sorted list of unique player IDs → column index map.
- Build `A` (numpy array) and `b` (numpy array).
- Append the sum-to-zero regularization row.
- Call `numpy.linalg.lstsq(A, b, rcond=None)`.
- Return `{player_id: score}` dict, scores rounded to 4 decimal places.

### Step 2 — Peak-window lookup helper (`app.py`)

Refactor the peak-window loop inside `best3year()` so the
`{player_id: (start_year, end_year)}` dict can be computed independently and
reused by the new endpoint.

Extract a helper:

```python
def _compute_peak_windows(conn, window: int) -> dict[int, dict]:
    """
    Returns {player_id: {"name": ..., "start": int, "end": int}}
    for every player whose best consecutive N-year window can be determined.
    """
```

### Step 3 — New API endpoint `GET /api/least_squares_ranking`

Query params:
- `window` (int, default 3, clamped 1–20) — same semantics as `/api/best3year`

Logic:
1. Call `_compute_peak_windows(conn, window)` to get every player's peak years.
2. Query all `watched_playoff_games` joined with `watched_game_players`
   to get `{game_id, game_year, best_player_id, [player_ids]}`.
3. For each game:
   a. Filter `player_ids` to those whose peak window contains `game_year`.
   b. If `best_player_id` is in the filtered set and len(filtered) >= 2:
      - Emit `(best_player_id, other_id)` for every `other_id` in filtered.
   c. Otherwise skip.
4. Call `compute_least_squares_scores(comparisons)`.
5. Return JSON list sorted by `ls_score` descending:

```json
[
  {
    "player_id": 42,
    "name": "LeBron James",
    "ls_score": 0.8123,
    "peak_start": 2011,
    "peak_end": 2013,
    "comparisons_as_winner": 18,
    "comparisons_as_loser": 4,
    "total_comparisons": 22
  },
  ...
]
```

### Step 4 — Merge into `/api/best3year` response

Rather than a separate round-trip, add `ls_score` directly to each entry
returned by `/api/best3year` by calling `_compute_least_squares_scores`
internally and joining on `player_id`.

Players with no comparisons get `ls_score: null`.

### Step 5 — Frontend (`static/best3year_kyle.js`)

Add a new column definition:

```js
{ key: "ls_score", label: "LS Score", isLS: true }
```

- Render with 4 decimal places (same `fmt()` helper used for other scores).
- Make it sortable (same `handleSort` mechanism already in place).
- Default sort remains `best_window_total`; user can click to sort by `ls_score`.

Add cell rendering in `buildRow`:

```js
} else if (col.isLS) {
  td.className   = "num-cell";
  td.textContent = player.ls_score != null ? fmt(player.ls_score) : "—";
}
```

### Step 6 — Dependencies

`numpy` is almost certainly already available (check `requirements.txt`).
If not, add it.

---

## Data Flow Summary

```
watched_playoff_games  ──┐
watched_game_players   ──┼──► filter by peak window ──► pairwise comparisons
_compute_peak_windows  ──┘

pairwise comparisons ──► compute_least_squares_scores ──► {player_id: ls_score}
                                                               │
/api/best3year ────────────────────────────────────────────── join ──► response JSON
                                                               │
best3year_kyle.js ─────────────────────────────────────────── render LS Score column
```

---

## Edge Cases

| Situation | Handling |
|-----------|----------|
| Player appears in watched games but never as best or non-best in a peak-window game | `ls_score: null`, shown as `—` |
| Only one player from a game is in their peak window | Game skipped entirely |
| All games for a year have no best player set | No signal from those games |
| Window = 1 | Works normally; peak "window" is just one year |
| Two players with identical comparison records | Least squares naturally assigns them equal scores |
| Player peak windows don't overlap at all with watched game years | `ls_score: null` |

---

## Files to Change

| File | Change |
|------|--------|
| `kyle.py` or new `ranking.py` | Add `compute_least_squares_scores(comparisons)` |
| `app.py` | Extract `_compute_peak_windows()`, augment `best3year()` with LS scores |
| `static/best3year_kyle.js` | Add `ls_score` column, cell renderer |
| `requirements.txt` | Add `numpy` if not present |

---

## Out of Scope (for now)

- Weighting comparisons by playoff round (could multiply the `b` entry by the
  round weight instead of always using `1`).
- Cross-window comparisons (comparing players whose peak windows don't overlap
  in year — currently excluded by design).
- Displaying the LS score on the individual player page.
