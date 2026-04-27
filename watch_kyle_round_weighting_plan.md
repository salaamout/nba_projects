# Watch K.Y.L.E. Round-Weighting Plan

## Goal

Improve the Watch K.Y.L.E. formula so that playoff games in later rounds count
more, reflecting that the opposing players and teams are better. This affects
every place playoff Watch K.Y.L.E. is displayed: the main selected-players
page, the cumulative page, the best-3-year page, individual player pages, and
the leaderboard on the Watch Log page.

---

## New Formula

### Step 1 — Round Weights (exponential, doubling per round)

| Round              | Weight |
|--------------------|--------|
| First Round        | 1      |
| Second Round       | 2      |
| Conference Finals  | 4      |
| NBA Finals         | 8      |

Any unrecognised round string falls back to weight **1**.

### Step 2 — Per-Player Raw Score

For each player, within a given playoff year:

```
N = sum of round_weight for every watched game in which the player was
    the Best Player

M = total number of watched games the player appeared in (plain count, unweighted)

raw_score = N / M
```

- `raw_score = 0` if the player was never Best Player (but was in watched games).
- Players with `M = 0` (not in any watched game) are excluded / given `None`.
- `raw_score` can exceed 1 (e.g. a player who was Best Player in every Finals
  game they were watched in gets `raw_score = 8`), which is fine — the
  year-level normalisation in Step 3 handles it.

### Step 3 — Year-Level Normalisation

Within each playoff year, find `max_raw` = the highest `raw_score` among all
players that year.

```
watch_kyle = (raw_score / max_raw) * 2 - 1
```

- Player with `raw_score = 0`       → **−1.000**
- Player with `raw_score = max_raw` → **+1.000**
- Everyone else lands linearly between −1 and +1.

**Edge case:** if `max_raw = 0` for the whole year (nobody was ever marked as
Best Player), set `watch_kyle = 0.0` for all players that year.

---

## Implementation Plan

### 1. `app.py` — `_get_watch_kyle_by_player(conn, season_year)`

This is the central function that all endpoints use. Replace the current
single-pass query+calculation with a two-pass approach:

**Pass 1 — SQL (weighted counts):**

```sql
SELECT
    wgp.player_id,
    COUNT(DISTINCT wgp.game_id) AS total_watched,
    SUM(CASE
        WHEN g.best_player_id = wgp.player_id THEN
            CASE g.round
                WHEN 'First Round'       THEN 1
                WHEN 'Second Round'      THEN 2
                WHEN 'Conference Finals' THEN 4
                WHEN 'NBA Finals'        THEN 8
                ELSE 1
            END
        ELSE 0
    END) AS weighted_best
FROM watched_game_players wgp
JOIN watched_playoff_games g ON g.id = wgp.game_id
WHERE g.game_year = ?
GROUP BY wgp.player_id
```

**Pass 2 — Python (normalise):**

```python
# Compute raw scores
players = []
for r in rows:
    M = r["total_watched"] or 0      # unweighted game count
    N = r["weighted_best"] or 0.0    # sum of round weights for best-player games
    raw = N / M if M > 0 else 0.0
    players.append({
        "player_id":     r["player_id"],
        "total_watched": M,
        "weighted_best": N,
        "raw":           raw,
    })

# Year-level max
max_raw = max((p["raw"] for p in players), default=0.0)

# Normalise
result = {}
for p in players:
    if p["total_watched"] == 0:
        continue
    if max_raw > 0:
        watch_kyle = round((p["raw"] / max_raw) * 2 - 1, 3)
    else:
        watch_kyle = 0.0
    result[p["player_id"]] = {
        "watch_kyle":    watch_kyle,
        "total_watched": p["total_watched"],   # M  (sub-label denominator)
        "weighted_best": p["weighted_best"],   # N  (sub-label numerator)
        "raw_score":     round(p["raw"], 4),
    }
```

> **Sub-label in UI:** show `{weighted_best:.1f} / {total_watched}` — e.g.
> `6.0 / 8` means "6 weighted best-player points across 8 games watched".

### 2. Endpoints that call `_get_watch_kyle_by_player`

No signature change needed. All of the following already call this function and
forward `watch_kyle` into the K.Y.L.E. calculation:

- `GET /api/selected` (main page)
- `GET /api/cumulative_kyle`
- `GET /api/best3year`
- `GET /api/player/<id>` (player history page)
- `GET /api/watched_games/best_player_leaderboard` — **separate logic**, see §3
- `GET /api/player/<id>/watch_log` — **separate logic**, see §4

### 3. `GET /api/watched_games/best_player_leaderboard` (~line 1102)

This endpoint has its own query and computes `watch_kyle = pct * 2 - 1` inline.
It needs the same two-pass treatment (weighted N, unweighted M, normalise by
year max).

### 4. `GET /api/player/<id>/watch_log` (~line 1181)

This endpoint builds `watch_by_year` in Python by iterating over fetched game
rows. It also needs updating:

- Accumulate `weighted_best` (add `round_weight` when player was best) instead
  of a plain count.
- For per-year `watch_kyle`, normalise against the year's max *across all
  players* (requires fetching all players for that year, or reusing
  `_get_watch_kyle_by_player`).
- Simplest approach: call `_get_watch_kyle_by_player` for each year that
  appears in the player's games, then look up this player's entry.

### 5. UI — tooltip / sub-label updates

Currently the sub-label shows `best_count / total_watched`. After the change
this is still valid (unweighted), but the tooltip should mention round
weighting. Consider updating the title attribute to:

```
"Weighted best-player score: {raw_score:.3f}  ({best_count} best / {total} games watched, later rounds weighted more)"
```

---

## What Does NOT Change

- The K.Y.L.E. field component `watch_kyle_norm` fed into `kyle_rating` stays
  the same (`watch_kyle` value is already on the −1…+1 scale).
- The `watch_kyle` field range (−1 to +1) is preserved; only the internal
  calculation changes.
- Regular-season K.Y.L.E. is completely unaffected.

---

## Decisions / Open Questions

1. ~~**Best-count display**~~ **Resolved:** sub-label shows a per-round
   breakdown of best-player counts over games watched, followed by the weighted
   raw score:

   ```
   (R1_best + R2_best + CF_best + F_best) / (R1_games + R2_games + CF_games + F_games) → raw_score
   ```

   Example — player watched in 1 Second Round game (was best) and 3 Finals
   games (was best in 1):

   ```
   (0+1+0+1) / (0+1+0+3) → 2.5
   ```

   The numerator and denominator entries are raw **counts** (human-readable),
   but the raw score is computed as weighted-N / unweighted-M:
   `(0×1 + 1×2 + 0×4 + 1×8) / 4 = 10/4 = 2.5`.
   Zero-count rounds are still shown to make the round structure clear.

2. **Cross-year normalisation is per-year by design** ✅ — confirmed intentional.
   A dominant Finals performance in a lightly-watched year can look better than
   a great first-round year; each year stands on its own.

3. ~~**Leaderboard aggregation**~~ **Resolved:** normalise per-year first (so each
   year produces a −1…+1 value), then sum across years on the leaderboard. This
   is consistent with how regular K.Y.L.E. is accumulated.

4. **Players not watched:** A player in the selected set who has zero watched
   games gets `watch_kyle = None` (no contribution to `kyle_rating`). This is
   unchanged.

5. **Weights are fixed:** The doubling schedule (1-2-4-8) is hard-coded. If you
   want to experiment with different curves later, extract `ROUND_WEIGHTS` to a
   module-level dict (or a config file) to make tuning easy.
