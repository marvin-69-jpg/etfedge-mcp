# etfedge-mcp

ETFedge owner-only read-only MCP service. Every HTTP request requires the
single bearer stored in `MCP_OWNER_TOKEN`; this is not a public integration.

## Codex owner setup

Keep the same long random token in server secret storage and the
`MCP_OWNER_TOKEN` environment variable, then add this to `~/.codex/config.toml`:

```toml
[mcp_servers.etfedge]
url = "https://mcp.etfedge.xyz/mcp"
bearer_token_env_var = "MCP_OWNER_TOKEN"
```

There is no OAuth login or public account onboarding. A missing or mismatched
bearer receives `401` before FastMCP handles the request.

## Local run

```bash
pip install -e .
cp .env.example .env
set -a; . ./.env; set +a
python mcp_server.py
```

`MCP_DATABASE_URL` should use the existing read-only PostgreSQL role. Never put
the bearer or database URL in this repository.

The owner retains the existing safe generic tools: allowlisted table queries,
freshness, ETF holdings and deltas, stock history, the legacy market-value view,
the estimated unrealized-P&L helper, and cross-ETF consensus buys. Arbitrary SQL
is not exposed.
