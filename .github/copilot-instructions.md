# GitHub Copilot Instructions

## Tool Preferences

- **Never use MCP server tools** (e.g. `mcp_pylance_*`). Always prefer transparent, readable alternatives:
  - Use `run_in_terminal` with plain shell/Python commands instead of `mcp_pylance_mcp_s_pylanceRunCodeSnippet`.
  - Use `get_errors` or `grep_search` instead of `mcp_pylance_mcp_s_pylanceFileSyntaxErrors`.
  - Use `run_in_terminal` with `python -c "..."` or a script for any Python execution.
- All terminal commands should be visible and readable so the user can verify them before approving.
