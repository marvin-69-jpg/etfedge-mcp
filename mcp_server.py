"""MCP server exposing read-only PG query tools.

Runs as a third background process inside the stock-cron container
(alongside nginx + cron daemon). Listens on 127.0.0.1:8000 — public
access goes through nginx /mcp reverse proxy with SSE-friendly settings.

Auth: GitHub OAuth via fastmcp's built-in GitHubProvider. claude.ai
performs Dynamic Client Registration → user logs in to GitHub → server
issues its own JWT → claude.ai sends JWT in Authorization header.

DB: connects via MCP_DATABASE_URL (read-only `claude_mcp_ro` user).
A separate URL from the cron's admin DATABASE_URL — defense in depth.
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from sqlalchemy import create_engine, event
from starlette.middleware.cors import CORSMiddleware

# Path tweak so `import mcp_tools` works whether started from /app or /app/scripts.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_tools  # noqa: E402


# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(message)s")
call_log = logging.getLogger("mcp.calls")


# ── Config ──────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("MCP_DATABASE_URL")
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET")
BASE_URL = os.environ.get("MCP_BASE_URL", "https://etfedge.xyz")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))

if not DATABASE_URL:
    sys.exit("[mcp_server] MCP_DATABASE_URL is required")
if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
    sys.exit("[mcp_server] GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET are required")

# Single shared engine. claude_mcp_ro PG side enforces read-only.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=4, max_overflow=4)


# Cap every query at 10 s — reduced from 30 s. Enough for all named tools
# (get_consensus_buys CTE runs ~1-3 s on this dataset) while cutting off
# pathological queries 3× faster than before.
@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("SET statement_timeout = 10000")
    cur.close()


# GitHub OAuth: claude.ai handles DCR + the auth handshake. fastmcp issues
# its own JWT after GitHub auth so per-request validation stays local.
auth = GitHubProvider(
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    # Root-level base_url so OAuth endpoints are advertised at root and
    # match where fastmcp actually mounts them (regardless of mcp.http_app
    # path). Per GitHubProvider docstring: "Use root-level URL to avoid
    # 404s during discovery when mounting under a path."
    base_url=BASE_URL,
    redirect_path="/auth/callback",
)

mcp = FastMCP("stock-research", auth=auth)


# ── Rate limiter + tool call logger (ASGI middleware) ────────────────
_RATE_WINDOW  = 60      # seconds per sliding window
_RATE_LIMIT   = 20      # max calls per window per user
_DAILY_LIMIT  = 100     # max calls per user per UTC day
_MAX_BODY     = 64_000  # bytes — safeguard against oversized POST bodies
_USAGE_PATH   = pathlib.Path(
    os.environ.get("MCP_USAGE_FILE", "./mcp_usage.json")
)
_ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN", "")

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>etfedge MCP · usage</title>
<style>
:root { --paper:#fafaf7; --panel:#fff; --ink:#1a1a1a; --soft:#888; --rule:#dddad2; --up:#b8860b; --down:#b04040; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family: ui-monospace,Menlo,monospace; background:var(--paper); color:var(--ink); padding:24px; max-width:760px; margin:0 auto; }
h1 { font-size:14px; letter-spacing:0.1em; margin-bottom:12px; }
.bar { background:var(--ink); color:#fff; padding:10px 16px; margin:-24px -24px 16px; display:flex; justify-content:space-between; align-items:center; }
.bar .name { font-size:13px; font-weight:700; letter-spacing:0.08em; }
.bar .stat { font-size:10px; color:rgba(255,255,255,0.4); }
.bar .stat strong { color:var(--up); }
.cards { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:18px; }
.card { background:var(--panel); border:1px solid var(--rule); border-top:2px solid var(--ink); padding:12px 14px; }
.card .lbl { font-size:9px; letter-spacing:0.12em; text-transform:uppercase; color:var(--soft); }
.card .val { font-size:24px; font-weight:700; color:var(--up); margin-top:4px; }
table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--rule); }
th { text-align:left; font-size:10px; letter-spacing:0.08em; text-transform:uppercase; color:var(--soft); padding:8px 12px; border-bottom:1px solid var(--rule); font-weight:500; }
td { padding:9px 12px; border-bottom:1px solid var(--rule); font-size:12px; }
tr:last-child td { border-bottom:none; }
td.code { font-weight:600; }
td.right { text-align:right; }
.fill { display:inline-block; height:6px; background:var(--rule); width:80px; vertical-align:middle; margin-right:6px; }
.fill > span { display:block; height:100%; background:var(--up); }
.warn { color:var(--down); font-weight:600; }
.empty { padding:30px; text-align:center; color:var(--soft); }
.foot { margin-top:18px; font-size:10px; color:var(--soft); display:flex; justify-content:space-between; }
</style>
</head>
<body>
<div class="bar">
  <span class="name">ETFEDGE.MCP</span>
  <span class="stat" id="stat">— · — calls today</span>
</div>
<div class="cards" id="cards"></div>
<div id="table-wrap"></div>
<div class="foot"><span id="updated">—</span><span><a href="javascript:load()" style="color:inherit">↻ refresh</a></span></div>
<script>
const TOKEN = "__TOKEN__";
async function load() {
  const r = await fetch("/admin/usage", { headers: { Authorization: "Bearer " + TOKEN } });
  if (!r.ok) { document.body.innerHTML = "fetch failed: " + r.status; return; }
  const d = await r.json();
  document.getElementById("stat").innerHTML = `<strong>${d.total_users}</strong> users · <strong>${d.total_calls_today}</strong> calls · ${d.date}`;
  document.getElementById("cards").innerHTML = `
    <div class="card"><div class="lbl">Date (UTC)</div><div class="val" style="font-size:18px">${d.date}</div></div>
    <div class="card"><div class="lbl">Active users</div><div class="val">${d.total_users}</div></div>
    <div class="card"><div class="lbl">Total calls</div><div class="val">${d.total_calls_today}</div></div>`;
  const wrap = document.getElementById("table-wrap");
  const users = d.users || {};
  const keys = Object.keys(users);
  if (!keys.length) {
    wrap.innerHTML = '<div class="empty">no calls today</div>';
  } else {
    const rows = keys.map(u => {
      const x = users[u];
      const pct = Math.round(x.used / x.limit * 100);
      const cls = x.remaining < 10 ? "warn" : "";
      return `<tr>
        <td class="code">${u}</td>
        <td class="right">${x.used}</td>
        <td class="right"><span class="fill"><span style="width:${pct}%"></span></span><span class="${cls}">${x.remaining} left</span></td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `<table><thead><tr><th>github user</th><th class="right">used</th><th class="right">remaining (limit ${d.limit_per_user})</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
}
load();
setInterval(load, 30000);
</script>
</body>
</html>"""


class _MCPMiddleware:
    """ASGI middleware: sliding window rate limit + daily quota + call logging.

    Rate limit:  20 calls / 60 s per GitHub user (in-process sliding window).
    Daily quota: 100 calls / UTC day per user, persisted to MCP_USAGE_FILE so
                 it survives container restarts.
    Logging: one JSON line per tool call on mcp.calls logger.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._windows: collections.defaultdict[str, collections.deque] = (
            collections.defaultdict(collections.deque)
        )
        # Daily quota — loaded from persistent file, reset at UTC midnight.
        self._usage_date: str = ""
        self._usage: dict[str, int] = {}
        self._load_usage()

    async def __call__(self, scope, receive, send) -> None:
        # Admin observability endpoints (GET only).
        if scope["type"] == "http" and scope.get("method") == "GET":
            path = scope.get("path")
            if path == "/admin/usage":
                await self._handle_admin_usage(scope, send)
                return
            if path == "/admin/dashboard":
                await self._handle_admin_dashboard(scope, send)
                return

        # Only intercept HTTP POST requests (tool calls + some OAuth flows).
        # All other traffic (GET for OAuth, WebSocket, lifespan) passes through.
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        user = self._user_from_scope(scope)

        # Rate-limit + daily quota — only on the MCP tool call endpoint.
        if scope.get("path") == "/mcp":
            allowed, retry_after = self._check_rate(user)
            if not allowed:
                await self._send_429(send, retry_after)
                return
            quota_ok, remaining = self._check_daily_quota(user)
            if not quota_ok:
                await self._send_429_daily(send, user)
                return

        # Buffer the request body so we can (a) peek at the tool name for
        # logging and (b) replay it to the downstream app unchanged.
        body = await self._read_body(receive)
        tool_name = self._extract_tool_name(body)

        # Replay the buffered body as a single chunk, then forward any real
        # client disconnects from the original receive callable. Returning
        # a synthetic http.disconnect here would prematurely abort SSE
        # streaming responses (fastmcp Streamable HTTP) before they finish.
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        start = time.monotonic()
        await self._app(scope, replay_receive, send)
        ms = int((time.monotonic() - start) * 1000)

        if tool_name:
            self._increment_usage(user)
            call_log.info(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "user": user,
                "tool": tool_name,
                "ms": ms,
                "daily_remaining": _DAILY_LIMIT - self._usage.get(user, 0),
            }))

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    async def _read_body(receive) -> bytes:
        body = b""
        more = True
        while more:
            msg = await receive()
            chunk = msg.get("body", b"")
            if len(body) + len(chunk) <= _MAX_BODY:
                body += chunk
            more = msg.get("more_body", False)
        return body

    @staticmethod
    def _extract_tool_name(body: bytes) -> str | None:
        try:
            data = json.loads(body)
            if data.get("method") == "tools/call":
                return data.get("params", {}).get("name")
        except Exception:
            pass
        return None

    @staticmethod
    def _user_from_scope(scope: dict) -> str:
        """Extract GitHub username (or user ID) from the Bearer JWT payload.

        Does NOT verify the JWT signature — fastmcp verifies it before tool
        execution. We only need to identify the user for rate limiting and
        logging; a forged payload would still be rejected by fastmcp.
        """
        headers = {k: v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("ascii", errors="ignore")
        if not auth.startswith("Bearer "):
            return "anonymous"
        token = auth[7:]
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return token[:8]
            payload_b64 = parts[1]
            # Fix base64 padding.
            payload_b64 += "=" * (-len(payload_b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload_b64))
            # Prefer readable GitHub login; fall back to numeric sub.
            return str(data.get("login") or data.get("sub") or token[:8])
        except Exception:
            return token[:8] if token else "anonymous"

    def _check_rate(self, user: str) -> tuple[bool, int]:
        """Sliding window check. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        dq = self._windows[user]
        cutoff = now - _RATE_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            retry_after = max(1, int(_RATE_WINDOW - (now - dq[0])) + 1)
            return False, retry_after
        dq.append(now)
        return True, 0

    def _check_daily_quota(self, user: str) -> tuple[bool, int]:
        """Daily quota check. Returns (allowed, remaining_calls)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._usage_date:
            self._usage_date = today
            self._usage = {}
        count = self._usage.get(user, 0)
        if count >= _DAILY_LIMIT:
            return False, 0
        return True, _DAILY_LIMIT - count

    def _increment_usage(self, user: str) -> None:
        self._usage[user] = self._usage.get(user, 0) + 1
        self._save_usage()

    def _load_usage(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            if _USAGE_PATH.exists():
                data = json.loads(_USAGE_PATH.read_text())
                if data.get("date") == today:
                    self._usage_date = today
                    self._usage = data.get("usage", {})
                    return
        except Exception:
            pass
        self._usage_date = today
        self._usage = {}

    def _save_usage(self) -> None:
        try:
            _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _USAGE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"date": self._usage_date, "usage": self._usage},
                ensure_ascii=False,
            ))
            tmp.replace(_USAGE_PATH)
        except Exception as e:
            logging.getLogger("mcp.usage").warning("usage save failed: %s", e)

    @staticmethod
    async def _send_429(send, retry_after: int) -> None:
        body = json.dumps(
            {"error": "rate_limit_exceeded", "retry_after": retry_after}
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                [b"content-type", b"application/json"],
                [b"retry-after", str(retry_after).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    async def _send_429_daily(send, user: str) -> None:
        body = json.dumps({
            "error": "daily_quota_exceeded",
            "message": f"Daily limit of {_DAILY_LIMIT} calls reached. Resets at UTC midnight.",
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _handle_admin_usage(self, scope, send) -> None:
        """GET /admin/usage — JSON dump of today's per-user quota state.

        Auth: Bearer token in Authorization header must equal ADMIN_TOKEN env var.
        Returns 503 if ADMIN_TOKEN not configured (defense in depth).
        """
        if not _ADMIN_TOKEN:
            await self._send_admin_err(send, 503, "ADMIN_TOKEN not configured")
            return

        # Read Authorization header (case-insensitive in HTTP, but ASGI lowercases keys).
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
        if auth != f"Bearer {_ADMIN_TOKEN}":
            await self._send_admin_err(send, 401, "invalid admin token")
            return

        # Refresh date in case caller crosses UTC midnight.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._usage_date:
            self._usage_date = today
            self._usage = {}

        users = {
            u: {
                "used": n,
                "limit": _DAILY_LIMIT,
                "remaining": max(0, _DAILY_LIMIT - n),
            }
            for u, n in sorted(self._usage.items(), key=lambda kv: -kv[1])
        }
        body = json.dumps({
            "date": self._usage_date,
            "limit_per_user": _DAILY_LIMIT,
            "rate_limit_per_min": _RATE_LIMIT,
            "total_users": len(users),
            "total_calls_today": sum(self._usage.values()),
            "users": users,
        }, indent=2).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _handle_admin_dashboard(self, scope, send) -> None:
        """GET /admin/dashboard?token=XXX — HTML page that polls /admin/usage."""
        if not _ADMIN_TOKEN:
            await self._send_admin_err(send, 503, "ADMIN_TOKEN not configured")
            return

        # Token from query string ?token=XXX
        qs = scope.get("query_string", b"").decode("latin-1", errors="replace")
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        if params.get("token") != _ADMIN_TOKEN:
            await self._send_admin_err(send, 401, "invalid admin token")
            return

        html = _DASHBOARD_HTML.replace("__TOKEN__", _ADMIN_TOKEN).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"text/html; charset=utf-8"]],
        })
        await send({"type": "http.response.body", "body": html, "more_body": False})

    @staticmethod
    async def _send_admin_err(send, status: int, msg: str) -> None:
        body = json.dumps({"error": msg}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})


# ── Tools ───────────────────────────────────────────────────────────
@mcp.tool()
def list_db_tables() -> list[dict]:
    """List public database tables exposed through the safe allowlist.

    This intentionally does not expose arbitrary public tables such as
    admin/session tables. Use describe_table before query_table.
    """
    with engine.connect() as conn:
        return mcp_tools.list_db_tables(conn)


@mcp.tool()
def describe_table(table: str) -> dict:
    """Describe one allowlisted table and its allowed columns.

    Args:
        table: allowlisted table name from list_db_tables.
    """
    with engine.connect() as conn:
        return mcp_tools.describe_table(conn, table)


@mcp.tool()
def get_table_stats(table: str) -> dict:
    """Return row count and min/max date columns for one allowlisted table."""
    with engine.connect() as conn:
        return mcp_tools.get_table_stats(conn, table)


@mcp.tool()
def query_table(
    table: str,
    filters: list[dict] | None = None,
    sort_by: str | None = None,
    sort_dir: str = "asc",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Query rows from one allowlisted table with safe filters and pagination.

    Args:
        table: allowlisted table name.
        filters: optional list like
            [{"column": "etf_code", "op": "eq", "value": "00981A"}].
            Supported ops: eq, ne, lt, lte, gt, gte, like, ilike, in.
            `prices` requires a filter on stock_code or trade_date.
            `shares`/`holdings` mix listed stocks with bonds, index
            futures/options, cash and FX markers — add
            {"column": "instrument_type", "op": "eq", "value": "equity"}
            whenever the question is about stocks.
        sort_by: optional allowed column.
        sort_dir: "asc" or "desc".
        limit: max 500.
        offset: pagination offset.
    """
    with engine.connect() as conn:
        return mcp_tools.query_table(
            conn, table, filters, sort_by, sort_dir, limit, offset
        )


@mcp.tool()
def get_distinct_values(
    table: str,
    column: str,
    filters: list[dict] | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return distinct values and counts for one allowlisted table column.

    `prices` requires a stock_code or trade_date filter here too.
    """
    with engine.connect() as conn:
        return mcp_tools.get_distinct_values(conn, table, column, filters, limit)


@mcp.tool()
def get_data_freshness() -> list[dict]:
    """Return latest available dates/counts for allowlisted data tables."""
    with engine.connect() as conn:
        return mcp_tools.get_data_freshness(conn)


@mcp.tool()
def list_etfs() -> list[dict]:
    """List all 21 active Taiwan ETFs in the warehouse.

    Returns one row per ETF with `code`, `name`, `aum_billion_twd`,
    `etf_type`, `dividend_policy`. AUM in billions TWD.

    Example: returns 21 rows like
        {"code": "00981A", "name": "...", "aum_billion_twd": 198.5,
         "etf_type": "主動式國內股票型", "dividend_policy": "..."}
    """
    with engine.connect() as conn:
        return mcp_tools.list_etfs(conn)


@mcp.tool()
def get_etf_buy_delta(
    etf: str, start_date: str, end_date: str, instrument_type: str = "equity"
) -> dict:
    """ETF holdings change between two dates: which stocks were bought / sold.

    Args:
        etf: ETF code, e.g. "00981A".
        start_date: YYYY-MM-DD (inclusive). If not a trading day, snaps
            backward to the most recent trading day on or before this date.
        end_date: YYYY-MM-DD (inclusive). Same snap-back rule.
        instrument_type: position type to report. Defaults to "equity" so
            only listed stocks are returned. Other values: "bond" (for the
            bond-based ETFs, codes ending in D), "futures", "option",
            "cash", "fund", "fx", or "all".

    Returns:
        {
          "etf": {"code": "00981A", "name": "<official name from meta>"},
          "instrument_type": "equity",
          "deltas": [
            {"stock_code": "2330", "stock_name": "台積電",
             "delta_shares": 1234567, "delta_value_yi": 42.9,
             "as_of": "2026-04-30"},
            ...
          ]
        }
    Up to 50 rows ordered by abs(delta_value_yi) desc.
    delta_shares: + = bought, - = sold (in shares, not lots).
    delta_value_yi: estimated NTD value at end_date close, in 億.

    IMPORTANT: Use the `etf.name` field for the ETF's official name.
    Do not infer the name from training data — names get rebranded.

    By default only listed-stock positions are returned; bonds, index
    futures/options, cash and FX markers are excluded.
    """
    with engine.connect() as conn:
        return mcp_tools.get_etf_buy_delta(
            conn, etf, start_date, end_date, instrument_type
        )


@mcp.tool()
def get_etf_holdings(etf: str, instrument_type: str = "equity") -> dict:
    """Latest current holdings snapshot for one ETF (all stocks, not deltas).

    Use this when the question is "what does ETF X currently hold" rather
    than "what changed between two dates" (which is get_etf_buy_delta).

    Args:
        etf: ETF code, e.g. "00981A".
        instrument_type: position type to return. Defaults to "equity" so
            only listed stocks are reported. Other values: "bond" (bond-based
            ETFs — codes ending in D hold bonds, not stocks), "futures",
            "option", "cash", "fund", "fx", or "all".

    Returns:
        {
          "etf": {"code": "00981A", "name": "<official name>"},
          "instrument_type": "equity",
          "n_holdings": 47,
          "as_of": "2026-04-30",
          "other_instrument_types": [
            {"instrument_type": "futures", "positions": 1, "weight_pct": 5.89}
          ],
          "holdings": [
            {"stock_code": "2330", "stock_name": "台積電",
             "shares": 12345678, "close": 850.0, "value_yi": 105.0,
             "weight_pct": 28.5, "as_of": "2026-04-30"},
            ...   # ordered by weight desc; entire equity book (not just top N)
          ]
        }

    weight_pct is the issuer-published portfolio weight, so the returned rows
    sum to less than 100% whenever the fund also holds the position types
    listed in `other_instrument_types` (plus uninvested cash). If
    `holdings` is empty but `other_instrument_types` is not, the fund is not
    stock-based — re-call with that instrument_type.

    IMPORTANT: Use `etf.name` from response. Don't guess names.
    """
    with engine.connect() as conn:
        return mcp_tools.get_etf_holdings(conn, etf, instrument_type)


@mcp.tool()
def get_stock_history(
    etf: str, stock_code: str, days: int = 30
) -> list[dict]:
    """Time series of (date, share_count, close) for ETF×stock over N days.

    Args:
        etf: ETF code, e.g. "00981A".
        stock_code: stock code, e.g. "2330".
        days: how many most-recent trading days with shares data to return
            (default 30, capped at 365).

    Returns rows ordered by trade_date asc:
        [{"trade_date": "2026-04-01", "share_count": 12345678,
          "close": 1100.0}, ...]

    Looks up exactly the code you pass, with no instrument-type filtering —
    get the code from get_etf_holdings rather than guessing it.
    """
    with engine.connect() as conn:
        return mcp_tools.get_stock_history(conn, etf, stock_code, days)


@mcp.tool()
def get_stock_pnl(etf: str, stock_code: str) -> dict:
    """Legacy valuation tool: current market value, not actual P&L.

    Args:
        etf: ETF code, e.g. "00981A".
        stock_code: stock code, e.g. "2330".

    Returns:
        {"share_count": <latest>, "close": <latest>,
         "market_value_yi": share_count * close / 1e8 (億 NTD),
         "close_as_of": YYYY-MM-DD, "shares_as_of": YYYY-MM-DD,
         "instrument_type": "equity", "is_pnl": false}

    Check `instrument_type`: anything other than "equity" (bond, futures,
    option, cash, fund, fx) is not a listed stock and the valuation does not
    read as a stock position — `note` says so too.

    Use get_stock_unrealized_pnl_estimate when the user asks "賺多少".
    """
    with engine.connect() as conn:
        return mcp_tools.get_stock_pnl(conn, etf, stock_code)


@mcp.tool()
def get_stock_unrealized_pnl_estimate(etf: str, stock_code: str) -> dict:
    """Estimate an ETF's unrealized P&L for one stock holding.

    Args:
        etf: ETF code, e.g. "00981A".
        stock_code: stock code, e.g. "2327".

    Returns estimated current shares, latest close, estimated remaining cost,
    market value, unrealized P&L, return %, and `instrument_type`. This is an
    estimate only: it derives buys/sells from daily share-count deltas and
    market closes, not real broker lots, fees, taxes, dividends, or issuer
    trade prices.

    The model only applies to listed stocks. If `instrument_type` comes back
    as anything other than "equity", report the caveat in `note` instead of
    presenting the numbers as a stock P&L.
    """
    with engine.connect() as conn:
        return mcp_tools.get_stock_unrealized_pnl_estimate(conn, etf, stock_code)


@mcp.tool()
def get_consensus_buys(
    start_date: str, end_date: str, min_etfs: int = 4
) -> list[dict]:
    """Stocks bought by ≥ min_etfs ETFs simultaneously between two dates.

    "Consensus buy" — useful for daily/weekly Threads posts about
    cross-ETF manager flow.

    Args:
        start_date: YYYY-MM-DD (inclusive, snaps backward).
        end_date: YYYY-MM-DD (inclusive, snaps backward).
        min_etfs: minimum number of ETFs that bought (default 4).

    Returns up to 30 rows ordered by total_delta_value_yi desc:
        [{"stock_code": "2330", "stock_name": "台積電",
          "etfs_buying": 5, "total_delta_value_yi": 120.5}, ...]

    Listed stocks only — bonds, index futures/options, cash and FX markers
    are excluded, since those never overlap meaningfully across issuers.
    """
    with engine.connect() as conn:
        return mcp_tools.get_consensus_buys(
            conn, start_date, end_date, min_etfs
        )


# ── Entry ───────────────────────────────────────────────────────────
def main() -> int:
    # Mount at /mcp to match the public path nginx exposes — keeps proxy
    # config trivial (no path rewrite needed). GitHub OAuth endpoints
    # (/.well-known/..., /authorize, /token, /register, /auth/callback)
    # all live under /mcp/* via the same mount, served by GitHubProvider.
    app = mcp.http_app(path="/mcp", transport="http")
    app = _MCPMiddleware(app)
    # CORS: claude.ai loads MCP from a browser context so preflight OPTIONS
    # must be answered with permissive headers, otherwise the browser blocks
    # the actual POST and the connector page shows an opaque "auth failed".
    app = CORSMiddleware(
        app,
        allow_origins=["https://claude.ai", "https://*.claude.ai"],
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["*"],
        allow_credentials=True,
        expose_headers=["mcp-session-id", "mcp-protocol-version"],
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
