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

The owner retains the existing safe generic tools: allowlisted relation queries,
freshness, ETF holdings and deltas, stock history, the legacy market-value view,
the estimated unrealized-P&L helper, and cross-ETF consensus buys. Arbitrary SQL
is not exposed. This is an owner-only private research interface; it is not a
public data API.

## Private fund research contract

The safe generic tools may list, describe, query, and report freshness for the
allowlisted `fund_metrics` table and the derived `fund_metrics_resolved` and
`fund_flow_daily` PostgreSQL views. These relations must always be queried with
at least an `etf_code` or `as_of_date` filter, and every generic query is capped
at 500 rows. No arbitrary relation, SQL, write, raw bytes, or secret-bearing
column is exposed.

`fund_metrics.as_of_date` is the portfolio date after the pipeline's calibrated
date shift. `created_at` and `updated_at` are ingest metadata, not a source
publication time. `fund_flow_daily.units_delta` and `creation_ntd` are derived
proxies from changes in units outstanding and NAV, not confirmed issuer cash
flows. `prev_as_of_date` identifies the previous resolved observation used for
that proxy. When interpreting a flow, retain its `source` and `source_adapter`.

For example, a bounded owner query can request one ETF's recent observations:

```json
{
  "table": "fund_flow_daily",
  "filters": [
    {"column": "etf_code", "op": "eq", "value": "00981A"},
    {"column": "as_of_date", "op": "gte", "value": "2026-08-01"}
  ],
  "sort_by": "as_of_date",
  "sort_dir": "desc",
  "limit": 30
}
```
