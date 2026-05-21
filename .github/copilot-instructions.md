# GitHub Copilot Instructions

## Python Environment

- **Always use `.venv/bin/python`** for any Python command in this project. Never use bare `python` or `python3`, as those resolve to the system/conda interpreter which is missing project dependencies.
- Correct: `.venv/bin/python -c "..."`, `.venv/bin/python -m pytest ...`
- Wrong: `python -c "..."`, `python3 -m pytest ...`
- If the `.venv` directory is missing for some reason, run `bash start.sh` once to create it, then use `.venv/bin/python`.
- Similarly, use `.venv/bin/pip` for any `pip` commands, not bare `pip`.

## Localhost vs 127.0.0.1

- **Always use `http://127.0.0.1:5000`** for any `curl` or HTTP commands targeting the local Flask server. Never use `http://localhost:5000`.
- On macOS, `localhost` resolves to `::1` (IPv6) first, which hits the macOS AirPlay Receiver on port 5000 and returns a 403 — not Flask.

## Database

- **Before writing any query or DB-touching code, read `docs/database.md`** — it documents every table, all column names and types, row counts, key relationships, and common query patterns.
- The single database file is `nba.db` (SQLite) in the project root.
- Use `db_conn()` (context manager) or `get_conn()` from `db.py`; `conn.row_factory = sqlite3.Row` is always set so columns can be accessed by name.
- `seasons.season_year` is the *ending* year (e.g. `2026` = the 2025-26 season). `season_type` is `'regular'` or `'playoff'`.

## Tool Preferences

- **Never use MCP server tools** (e.g. `mcp_pylance_*`). Always prefer transparent, readable alternatives:
  - Use `run_in_terminal` with plain shell/Python commands instead of `mcp_pylance_mcp_s_pylanceRunCodeSnippet`.
  - Use `get_errors` or `grep_search` instead of `mcp_pylance_mcp_s_pylanceFileSyntaxErrors`.
  - Use `run_in_terminal` with `.venv/bin/python -c "..."` or a script for any Python execution.
- All terminal commands should be visible and readable so the user can verify them before approving.
