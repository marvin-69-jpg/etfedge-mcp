"""Owner-only MCP server exposing ETFedge read-only query tools."""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys

import uvicorn
from fastmcp import FastMCP
from sqlalchemy import create_engine, event

import mcp_tools


logging.basicConfig(level=logging.INFO, format="%(message)s")
DATABASE_URL = os.environ.get("MCP_DATABASE_URL", "").strip()
OWNER_TOKEN = os.environ.get("MCP_OWNER_TOKEN", "").strip()
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))

if not DATABASE_URL:
    sys.exit("[mcp_server] MCP_DATABASE_URL is required")
if len(OWNER_TOKEN) < 32:
    sys.exit("[mcp_server] MCP_OWNER_TOKEN must contain at least 32 characters")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=4, max_overflow=4)


@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_conn, _record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("SET statement_timeout = 10000")
    cursor.close()


mcp = FastMCP("stock-research")


class OwnerBearerMiddleware:
    """Fail closed before FastMCP sees any unauthenticated HTTP request."""

    def __init__(self, app, token: str) -> None:
        if len(token) < 32:
            raise ValueError("owner bearer token must contain at least 32 characters")
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"authorization", b"").decode(
            "latin-1", errors="replace"
        )
        if not hmac.compare_digest(supplied, self._expected):
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"cache-control", b"no-store"],
                        [b"www-authenticate", b"Bearer"],
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": body, "more_body": False}
            )
            return
        await self._app(scope, receive, send)


@mcp.tool()
def list_db_tables() -> list[dict]:
    """List safe, allowlisted database tables."""
    with engine.connect() as connection:
        return mcp_tools.list_db_tables(connection)


@mcp.tool()
def describe_table(table: str) -> dict:
    """Describe one allowlisted table."""
    with engine.connect() as connection:
        return mcp_tools.describe_table(connection, table)


@mcp.tool()
def get_table_stats(table: str) -> dict:
    """Return row count and date range for one allowlisted table."""
    with engine.connect() as connection:
        return mcp_tools.get_table_stats(connection, table)


@mcp.tool()
def query_table(
    table: str,
    filters: list[dict] | None = None,
    sort_by: str | None = None,
    sort_dir: str = "asc",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Query one allowlisted table with parameterized filters and pagination."""
    with engine.connect() as connection:
        return mcp_tools.query_table(
            connection, table, filters, sort_by, sort_dir, limit, offset
        )


@mcp.tool()
def get_distinct_values(
    table: str,
    column: str,
    filters: list[dict] | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return distinct values and counts for an allowlisted column."""
    with engine.connect() as connection:
        return mcp_tools.get_distinct_values(
            connection, table, column, filters, limit
        )


@mcp.tool()
def get_data_freshness() -> list[dict]:
    """Return latest dates and counts for allowlisted datasets."""
    with engine.connect() as connection:
        return mcp_tools.get_data_freshness(connection)


@mcp.tool()
def list_etfs() -> list[dict]:
    """List active Taiwan ETFs in the warehouse."""
    with engine.connect() as connection:
        return mcp_tools.list_etfs(connection)


@mcp.tool()
def get_etf_buy_delta(
    etf: str, start_date: str, end_date: str, instrument_type: str = "equity"
) -> dict:
    """Return one ETF's position changes between two dates."""
    with engine.connect() as connection:
        return mcp_tools.get_etf_buy_delta(
            connection, etf, start_date, end_date, instrument_type
        )


@mcp.tool()
def get_etf_holdings(etf: str, instrument_type: str = "equity") -> dict:
    """Return an ETF's latest holdings snapshot."""
    with engine.connect() as connection:
        return mcp_tools.get_etf_holdings(connection, etf, instrument_type)


@mcp.tool()
def get_stock_history(etf: str, stock_code: str, days: int = 30) -> list[dict]:
    """Return an ETF-stock share-count history."""
    with engine.connect() as connection:
        return mcp_tools.get_stock_history(connection, etf, stock_code, days)


@mcp.tool()
def get_stock_pnl(etf: str, stock_code: str) -> dict:
    """Legacy current market-value view; not a P&L calculation."""
    with engine.connect() as connection:
        return mcp_tools.get_stock_pnl(connection, etf, stock_code)


@mcp.tool()
def get_stock_unrealized_pnl_estimate(etf: str, stock_code: str) -> dict:
    """Estimate unrealized P&L from share-count deltas and market closes."""
    with engine.connect() as connection:
        return mcp_tools.get_stock_unrealized_pnl_estimate(
            connection, etf, stock_code
        )


@mcp.tool()
def get_consensus_buys(
    start_date: str, end_date: str, min_etfs: int = 4
) -> list[dict]:
    """Return stocks bought by at least min_etfs ETFs between two dates."""
    with engine.connect() as connection:
        return mcp_tools.get_consensus_buys(
            connection, start_date, end_date, min_etfs
        )


def build_app():
    return OwnerBearerMiddleware(
        mcp.http_app(path="/mcp", transport="http"), OWNER_TOKEN
    )


def main() -> int:
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
