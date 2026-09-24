# Claude Code Setup

SkopaqTrader integrates directly with Claude Code via a Model Context Protocol (MCP) server. Once connected, all 23 trading tools are available inside your Claude Code session.

## Prerequisites

- Python 3.12+ installed
- SkopaqTrader cloned and dependencies installed
- A valid `.env` file with at least one LLM API key

## Register the MCP Server

Add the SkopaqTrader MCP server to your Claude Code project configuration.

**Option 1 — Project-level config** (recommended):

Create or edit `.claude/.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "skopaq": {
      "command": "python3",
      "args": ["-m", "skopaq.mcp_server"]
    }
  }
}
```

**Option 2 — Global config** (available in all projects):

Edit `~/.claude.json` and add the same block under `"mcpServers"`.

!!! tip "Use absolute Python path"
    If Claude Code cannot find `python3`, use the full path:
    ```json
    "command": "/usr/local/bin/python3"
    ```
    Find yours with `which python3`.

## Verify the Connection

After saving the config, restart Claude Code. You should see the tools loaded in the system prompt. Run a quick test:

```
/status
```

Or ask Claude directly:

> What is the current status of the SkopaqTrader system?

Claude will call `mcp__skopaq__system_status` and show version, trading mode, connected LLMs, and token health.

## How It Works

The MCP server (`skopaq/mcp_server.py`) runs as a child process using **stdio transport**. Claude Code launches it automatically and communicates over stdin/stdout. No network ports are needed.

```
Claude Code  ──stdin/stdout──  python -m skopaq.mcp_server
                                   │
                                   ├── INDstocks / Kite Connect (broker)
                                   ├── LLM providers (Gemini, Claude, Grok)
                                   └── Supabase (memory)
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Tools not appearing | Restart Claude Code after editing `.mcp.json` |
| `ModuleNotFoundError` | Run `pip install -e .` in the project root |
| Broker errors | Ensure `.env` has valid `SKOPAQ_INDSTOCKS_*` or `SKOPAQ_KITE_*` keys |
| LLM failures | Check that at least `SKOPAQ_GOOGLE_API_KEY` is set |
| MCP server crashes | Run `python -m skopaq.mcp_server` standalone to see errors |

!!! warning "Environment Variables"
    The MCP server reads from your shell environment. If you use `direnv` or a `.env` loader, make sure variables are exported before Claude Code launches.

## Auto-Allowed vs Permission-Required Tools

Read-only tools (quotes, positions, status) are auto-allowed. Tools that modify state require explicit user permission in Claude Code:

| Auto-Allowed | Requires Permission |
|---|---|
| `get_quote`, `get_historical` | `place_order` |
| `get_positions`, `get_holdings`, `get_funds` | `place_gtt_order` |
| `get_orders`, `system_status` | `setup_swing_trade` |
| `analyze_stock`, `scan_market`, `check_safety` | `suggest_option_trade` |
| `gather_*` data tools, `recall_agent_memories`, `quick_decision` | `save_trade_reflection` |

## Next Steps

- Browse all [MCP Tools](mcp-tools.md)
- Try the [Custom Skills](skills.md) (slash commands)
- Understand [Dual-Mode Analysis](dual-mode.md)
