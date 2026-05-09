# GitHub Copilot Instructions

## Python Environment

- **Always use `.venv/bin/python`** for any Python command in this project. Never use bare `python` or `python3`, as those resolve to the system/conda interpreter which is missing project dependencies.
- Correct: `.venv/bin/python -c "..."`, `.venv/bin/python -m pytest ...`
- Wrong: `python -c "..."`, `python3 -m pytest ...`
- If the `.venv` directory is missing for some reason, run `bash start.sh` once to create it, then use `.venv/bin/python`.
- Similarly, use `.venv/bin/pip` for any `pip` commands, not bare `pip`.

## Tool Preferences

- **Never use MCP server tools** (e.g. `mcp_pylance_*`). Always prefer transparent, readable alternatives:
  - Use `run_in_terminal` with plain shell/Python commands instead of `mcp_pylance_mcp_s_pylanceRunCodeSnippet`.
  - Use `get_errors` or `grep_search` instead of `mcp_pylance_mcp_s_pylanceFileSyntaxErrors`.
  - Use `run_in_terminal` with `.venv/bin/python -c "..."` or a script for any Python execution.
- All terminal commands should be visible and readable so the user can verify them before approving.
