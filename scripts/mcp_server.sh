#!/bin/bash
# MCP server wrapper for Claude Code
cd "$(dirname "$0")/.." || exit 1
exec python3 -m skopaq.mcp_server
