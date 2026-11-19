#!/usr/bin/env bash
# Read-only Postgres MCP server.
# Requires POSTGRES_MCP_PASSWORD in the environment; never hardcode it.
set -euo pipefail

: "${POSTGRES_MCP_PASSWORD:?set POSTGRES_MCP_PASSWORD}"

exec npx -y @modelcontextprotocol/server-postgres \
  "postgresql://mcp_ro:${POSTGRES_MCP_PASSWORD}@localhost:5432/vani"
