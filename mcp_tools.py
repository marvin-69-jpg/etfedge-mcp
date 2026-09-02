"""SQL query implementations for the MCP tools.

Each function takes a SQLAlchemy Connection and returns plain
list[dict] / dict — JSON-serialisable for MCP transport.

Kept separate from `mcp_server.py` so the SQL is unit-testable
without needing fastmcp / FastAPI / SSE in the test harness.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


# Legacy stock_code pattern guard, kept as defence-in-depth behind the
# instrument_type filter below (covers rows written before the column existed):
# C_NTD/M_NTD/PFUR_NTD/RDI_NTD = cash-style markers
# DA_* = multi-currency cash deductions (00998A)
# ^[0-9]{6}F = futures contracts
_EXCLUDE_NON_STOCK = (
    "stock_code !~ '^(C|M|PFUR|RDI)_NTD$' "
    "AND stock_code NOT LIKE 'DA\\_%' "
    "AND stock_code !~ '^[0-9]{6}F'"
)

# `shares` / `holdings` classify every row: listed equities plus bonds (ISIN
# codes), index futures/options (TX*), cash and FX markers, and fund units.
# Position tools default to equities so non-stock codes never surface as
# "holdings"; pass instrument_type explicitly for bond-based ETFs.
_INSTRUMENT_TYPES = ("equity", "bond", "cash", "futures", "fund", "option", "fx")


def _instrument_filter(alias: str, instrument_type: str | None) -> tuple[str, dict]:
    """Build the instrument_type predicate for a shares/holdings query.

    Returns (sql_predicate, bind_params). ``"all"`` disables the filter
    (the legacy stock_code guard still applies).
    """
    value = str(instrument_type or "equity").strip().lower()
    if value == "all":
        return "TRUE", {}
    if value not in _INSTRUMENT_TYPES:
        raise ValueError(
            "instrument_type must be one of: "
            + ", ".join((*_INSTRUMENT_TYPES, "all"))
        )
    prefix = f"{alias}." if alias else ""
    return f"{prefix}instrument_type = :itype", {"itype": value}

_MAX_LIMIT = 500
_OPS = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "like": "LIKE",
    "ilike": "ILIKE",
}

_DB_TABLES: dict[str, dict[str, Any]] = {
    "etf": {
        "description": "ETF master records.",
        "columns": ["code", "name", "issuer"],
        "date_columns": [],
    },
    "meta": {
        "description": "ETF yearly metadata, AUM, type, fees, listing date.",
        "columns": [
            "etf_code", "snapshot_year", "stock_name", "tracked_index",
            "tracking_method", "etf_type", "leverage_inverse",
            "aum_billion_twd", "asset_class", "units_outstanding_thousand",
            "issuer", "listing_date", "management_fee_text",
            "custody_fee_text", "total_fee_text", "dividend_policy",
            "currency",
        ],
        "date_columns": ["listing_date"],
    },
    "holdings": {
        "description": (
            "ETF holdings weights by date. Rows cover every position type, "
            "not just listed stocks — filter instrument_type = 'equity' for "
            "stock-level analysis."
        ),
        "columns": [
            "etf_code", "trade_date", "stock_code", "stock_name",
            "weight_pct", "instrument_type",
        ],
        "date_columns": ["trade_date"],
    },
    "shares": {
        "description": (
            "ETF holdings shares by date. Rows cover every position type, "
            "not just listed stocks — filter instrument_type = 'equity' for "
            "stock-level analysis."
        ),
        "columns": [
            "etf_code", "trade_date", "stock_code", "stock_name",
            "weight_pct", "share_count", "unit", "instrument_type",
        ],
        "date_columns": ["trade_date"],
    },
    "fund_metrics": {
        "description": (
            "Owner-only ETF unit, NAV, and total-net-asset observations by "
            "portfolio as_of_date and source. as_of_date is the shifted "
            "portfolio date; created_at and updated_at are ingest metadata, "
            "not publication timestamps."
        ),
        "columns": [
            "etf_code", "as_of_date", "source", "source_adapter",
            "units_outstanding", "nav", "total_nav_twd", "created_at", "updated_at",
        ],
        "date_columns": ["as_of_date", "created_at", "updated_at"],
        "requires_filter": True,
        "required_filter_columns": {"etf_code", "as_of_date"},
    },
    "fund_metrics_resolved": {
        "description": (
            "Derived ETF metric series that prefers official observations and "
            "falls back to cmoney. Requires an ETF code or portfolio date filter."
        ),
        "columns": [
            "etf_code", "as_of_date", "source", "units_outstanding", "nav",
            "total_nav_twd", "source_adapter",
        ],
        "date_columns": ["as_of_date"],
        "requires_filter": True,
        "required_filter_columns": {"etf_code", "as_of_date"},
    },
    "fund_flow_daily": {
        "description": (
            "Derived daily units and creation/redemption proxy from the resolved "
            "metric series. units_delta and creation_ntd are not confirmed issuer "
            "cash flows."
        ),
        "columns": [
            "etf_code", "as_of_date", "source", "units_outstanding", "nav",
            "total_nav_twd", "prev_as_of_date", "units_delta", "creation_ntd",
            "source_adapter",
        ],
        "date_columns": ["as_of_date"],
        "requires_filter": True,
        "required_filter_columns": {"etf_code", "as_of_date"},
    },
    "dividend": {
        "description": "ETF dividend records.",
        "columns": [
            "etf_code", "year_quarter", "cash_dividend_twd",
            "dividend_yield_pct", "ex_date", "pay_date",
        ],
        "date_columns": ["ex_date", "pay_date"],
    },
    "prices": {
        "description": "TWSE/TPEx daily OHLCV by stock_code. Requires filters.",
        "columns": [
            "stock_code", "trade_date", "open", "high", "low", "close",
            "volume_shares", "volume_ntd", "transactions", "adj_factor",
        ],
        "date_columns": ["trade_date"],
        "requires_filter": True,
        "required_filter_columns": {"stock_code", "trade_date"},
    },
    "price_adjustments": {
        "description": "Raw price adjustment events.",
        "columns": ["stock_code", "event_date", "event_type", "ratio"],
        "date_columns": ["event_date"],
    },
    "company_shares": {
        "description": "Company issued shares snapshots.",
        "columns": [
            "stock_code", "snapshot_date", "market", "company_name",
            "issue_shares", "paid_in_capital_twd", "source_url", "fetched_at",
        ],
        "date_columns": ["snapshot_date", "fetched_at"],
    },
    "company_industry_chains": {
        "description": "Official company industry chain taxonomy.",
        "columns": [
            "stock_code", "snapshot_date", "ic_code", "chain_code",
            "subchain_code", "company_name", "ic_name", "chain_name",
            "subchain_name", "market_bucket", "source_url", "fetched_at",
        ],
        "date_columns": ["snapshot_date", "fetched_at"],
    },
    "company_industry_blocks": {
        "description": "ETFedge normalized company industry blocks.",
        "columns": [
            "stock_code", "snapshot_date", "company_name", "block_code",
            "block_name", "rule_version", "rule_priority", "source_ic_code",
            "source_ic_name", "source_chain_code", "source_chain_name",
            "source_subchain_code", "source_subchain_name", "source_url",
            "fetched_at",
        ],
        "date_columns": ["snapshot_date", "fetched_at"],
    },
    "job_runs": {
        "description": "Cron/update job status telemetry.",
        "columns": [
            "id", "job_name", "status", "started_at", "updated_at",
            "finished_at", "upstream_date", "deployment_id", "hostname",
            "pid", "detail", "events", "error",
        ],
        "date_columns": ["started_at", "updated_at", "finished_at"],
    },
}


def _row_to_dict(row: Any) -> dict:
    """SQLAlchemy Row -> plain dict (handles Decimal/date -> str)."""
    out = {}
    for k, v in row._mapping.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        elif hasattr(v, "is_finite"):  # Decimal
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _table(name: str) -> dict[str, Any]:
    try:
        return _DB_TABLES[name]
    except KeyError as exc:
        raise ValueError(f"table is not allowed: {name}") from exc


def _column(table: str, column: str) -> str:
    if column not in _table(table)["columns"]:
        raise ValueError(f"column is not allowed for {table}: {column}")
    return column


def _limit(value: int) -> int:
    limit = int(value or 100)
    if limit > _MAX_LIMIT:
        raise ValueError(f"limit must not exceed {_MAX_LIMIT}")
    return max(1, limit)


def _where(table: str, filters: list[dict] | None) -> tuple[str, dict[str, Any]]:
    spec = _table(table)
    filters = filters or []
    if spec.get("requires_filter"):
        required = spec.get("required_filter_columns", set())
        seen = {f.get("column") for f in filters if isinstance(f, dict)}
        if not filters or not (seen & required):
            raise ValueError(
                f"{table} requires at least one filter on: {', '.join(sorted(required))}"
            )

    parts: list[str] = []
    params: dict[str, Any] = {}
    for i, f in enumerate(filters):
        if not isinstance(f, dict):
            raise ValueError("filters must be objects")
        col = _column(table, str(f.get("column", "")))
        op = str(f.get("op", "eq")).lower()
        key = f"v{i}"
        if op == "in":
            value = f.get("value") or []
            if not isinstance(value, list) or not value:
                raise ValueError("in filter requires a non-empty list value")
            if len(value) > _MAX_LIMIT:
                raise ValueError(f"in filter accepts at most {_MAX_LIMIT} values")
            parts.append(f"{col} = ANY(:{key})")
            params[key] = value
        elif op in _OPS:
            parts.append(f"{col} {_OPS[op]} :{key}")
            params[key] = f.get("value")
        else:
            raise ValueError(f"unsupported filter op: {op}")

    return ("WHERE " + " AND ".join(parts) if parts else ""), params


def list_db_tables(conn: Connection) -> list[dict]:
    existing = {
        r[0]: r[1]
        for r in conn.execute(text(
            """
            SELECT table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type IN ('BASE TABLE', 'VIEW')
            """
        ))
    }
    return [
        {
            "table": name,
            "description": spec["description"],
            "columns": len(spec["columns"]),
            "requires_filter": bool(spec.get("requires_filter")),
            "relation_type": existing[name].lower(),
        }
        for name, spec in _DB_TABLES.items()
        if name in existing
    ]


def describe_table(conn: Connection, table: str) -> dict:
    spec = _table(table)
    rows = conn.execute(
        text(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table
            ORDER BY ordinal_position
            """
        ),
        {"table": table},
    )
    columns = [_row_to_dict(r) for r in rows if r.column_name in spec["columns"]]
    return {
        "table": table,
        "description": spec["description"],
        "requires_filter": bool(spec.get("requires_filter")),
        "allowed_columns": spec["columns"],
        "columns": columns,
    }


def get_table_stats(conn: Connection, table: str) -> dict:
    spec = _table(table)
    selects = ["count(*)::bigint AS row_count"]
    for col in spec["date_columns"]:
        selects.append(f"min({col}) AS min_{col}")
        selects.append(f"max({col}) AS max_{col}")
    row = conn.execute(text(f"SELECT {', '.join(selects)} FROM {table}")).first()
    return {"table": table, **(_row_to_dict(row) if row else {})}


def query_table(
    conn: Connection,
    table: str,
    filters: list[dict] | None = None,
    sort_by: str | None = None,
    sort_dir: str = "asc",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    spec = _table(table)
    where, params = _where(table, filters)
    order = ""
    if sort_by:
        sort_col = _column(table, sort_by)
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        order = f"ORDER BY {sort_col} {direction}"
    cols = ", ".join(spec["columns"])
    params.update({"limit": _limit(limit), "offset": max(0, int(offset or 0))})
    sql = text(f"SELECT {cols} FROM {table} {where} {order} LIMIT :limit OFFSET :offset")
    return [_row_to_dict(r) for r in conn.execute(sql, params)]


def get_distinct_values(
    conn: Connection,
    table: str,
    column: str,
    filters: list[dict] | None = None,
    limit: int = 100,
) -> list[dict]:
    col = _column(table, column)
    where, params = _where(table, filters)
    params["limit"] = _limit(limit)
    sql = text(
        f"""
        SELECT {col} AS value, count(*)::bigint AS row_count
        FROM {table}
        {where}
        GROUP BY {col}
        ORDER BY row_count DESC NULLS LAST, {col}
        LIMIT :limit
        """
    )
    return [_row_to_dict(r) for r in conn.execute(sql, params)]


def get_data_freshness(conn: Connection) -> list[dict]:
    rows: list[dict] = []
    for table, spec in _DB_TABLES.items():
        date_cols = spec["date_columns"]
        if not date_cols:
            continue
        col = date_cols[0]
        try:
            row = conn.execute(
                text(f"SELECT max({col}) AS latest, count(*)::bigint AS row_count FROM {table}")
            ).first()
        except Exception:
            continue
        item = _row_to_dict(row)
        rows.append({"table": table, "freshness_column": col, **item})
    return rows


def list_etfs(conn: Connection) -> list[dict]:
    sql = text(
        """
        WITH latest_meta AS (
          SELECT etf_code, max(snapshot_year) AS y FROM meta GROUP BY etf_code
        )
        SELECT
          e.code,
          coalesce(m.stock_name, e.name) AS name,
          m.aum_billion_twd,
          m.etf_type,
          m.dividend_policy
        FROM etf e
        LEFT JOIN latest_meta lm ON lm.etf_code = e.code
        LEFT JOIN meta m ON m.etf_code = lm.etf_code AND m.snapshot_year = lm.y
        ORDER BY m.aum_billion_twd DESC NULLS LAST, e.code
        """
    )
    return [_row_to_dict(r) for r in conn.execute(sql)]


def _etf_label(conn: Connection, etf: str) -> dict:
    """Resolve ETF code → {code, name}.

    Prefer meta.stock_name (richer / official) but fall back to etf.name
    when the ETF is too new to have a meta snapshot yet.
    """
    sql = text(
        """
        SELECT coalesce(
          (SELECT m.stock_name FROM meta m
           WHERE m.etf_code = :etf
           ORDER BY m.snapshot_year DESC LIMIT 1),
          (SELECT e.name FROM etf e WHERE e.code = :etf)
        ) AS name
        """
    )
    row = conn.execute(sql, {"etf": etf}).first()
    return {"code": etf, "name": row[0] if row else None}


def get_etf_buy_delta(
    conn: Connection,
    etf: str,
    start_date: str,
    end_date: str,
    instrument_type: str = "equity",
) -> dict:
    where_instrument, iparams = _instrument_filter("", instrument_type)
    sql = text(
        f"""
        WITH start_snap AS (
          SELECT stock_code, share_count
          FROM shares
          WHERE etf_code = :etf
            AND trade_date = (
              SELECT max(trade_date) FROM shares
              WHERE etf_code = :etf AND trade_date <= :start_date
            )
        ),
        end_snap AS (
          SELECT stock_code, stock_name, share_count, trade_date
          FROM shares
          WHERE etf_code = :etf
            AND {where_instrument}
            AND trade_date = (
              SELECT max(trade_date) FROM shares
              WHERE etf_code = :etf AND trade_date <= :end_date
            )
        ),
        end_close AS (
          SELECT stock_code, close
          FROM prices
          WHERE trade_date = (
            SELECT max(trade_date) FROM prices WHERE trade_date <= :end_date
          )
        )
        SELECT
          e.stock_code,
          e.stock_name,
          (e.share_count - coalesce(s.share_count, 0))::bigint AS delta_shares,
          round(
            (e.share_count - coalesce(s.share_count, 0)) * c.close / 1e8,
            2
          ) AS delta_value_yi,
          e.trade_date AS as_of
        FROM end_snap e
        LEFT JOIN start_snap s USING (stock_code)
        LEFT JOIN end_close c USING (stock_code)
        WHERE {_EXCLUDE_NON_STOCK.replace('stock_code', 'e.stock_code')}
          AND (e.share_count - coalesce(s.share_count, 0)) <> 0
        ORDER BY abs(
          (e.share_count - coalesce(s.share_count, 0)) * coalesce(c.close, 0)
        ) DESC NULLS LAST
        LIMIT 50
        """
    )
    params = {
        "etf": etf,
        "start_date": start_date,
        "end_date": end_date,
        **iparams,
    }
    rows = [_row_to_dict(r) for r in conn.execute(sql, params)]
    return {
        "etf": _etf_label(conn, etf),
        "instrument_type": str(instrument_type or "equity").lower(),
        "deltas": rows,
    }


def _latest_shares_date(conn: Connection, etf: str) -> Any:
    row = conn.execute(
        text("SELECT max(trade_date) AS d FROM shares WHERE etf_code = :etf"),
        {"etf": etf},
    ).first()
    return row[0] if row else None


def _other_instrument_types(
    conn: Connection, etf: str, as_of: Any, selected: str
) -> list[dict]:
    """Position types present in the snapshot but excluded from the result."""
    if selected == "all" or as_of is None:
        return []
    rows = conn.execute(
        text(
            """
            SELECT
              instrument_type,
              count(*)::int AS positions,
              round(sum(weight_pct)::numeric, 2) AS weight_pct
            FROM shares
            WHERE etf_code = :etf AND trade_date = :as_of
              AND instrument_type <> :itype
            GROUP BY instrument_type
            ORDER BY positions DESC
            """
        ),
        {"etf": etf, "as_of": as_of, "itype": selected},
    )
    return [_row_to_dict(r) for r in rows]


def get_etf_holdings(
    conn: Connection, etf: str, instrument_type: str = "equity"
) -> dict:
    """Latest holdings snapshot — uses the issuer-published weight_pct from shares."""
    where_instrument, iparams = _instrument_filter("s", instrument_type)
    selected = str(instrument_type or "equity").strip().lower()
    as_of = _latest_shares_date(conn, etf)
    sql = text(
        f"""
        SELECT
          s.stock_code,
          s.stock_name,
          s.share_count::bigint AS shares,
          p.close,
          round((s.share_count * p.close / 1e8)::numeric, 4) AS value_yi,
          s.weight_pct,
          s.trade_date AS as_of
        FROM shares s
        LEFT JOIN prices p USING (stock_code, trade_date)
        WHERE s.etf_code = :etf
          AND s.trade_date = :as_of
          AND {where_instrument}
          AND {_EXCLUDE_NON_STOCK.replace('stock_code', 's.stock_code')}
        ORDER BY s.weight_pct DESC NULLS LAST
        """
    )
    rows: list[dict] = []
    if as_of is not None:
        rows = [
            _row_to_dict(r)
            for r in conn.execute(sql, {"etf": etf, "as_of": as_of, **iparams})
        ]
    return {
        "etf": _etf_label(conn, etf),
        "instrument_type": selected,
        "n_holdings": len(rows),
        "as_of": as_of.isoformat() if as_of is not None else None,
        "other_instrument_types": _other_instrument_types(
            conn, etf, as_of, selected
        ),
        "holdings": rows,
    }


def get_stock_history(
    conn: Connection, etf: str, stock_code: str, days: int = 30
) -> list[dict]:
    days = max(1, min(days, 365))  # cap defensively
    sql = text(
        """
        WITH date_set AS (
          SELECT DISTINCT trade_date
          FROM shares
          WHERE etf_code = :etf AND stock_code = :stock_code
          ORDER BY trade_date DESC
          LIMIT :days
        )
        SELECT
          s.trade_date,
          s.share_count::bigint,
          p.close
        FROM shares s
        LEFT JOIN prices p USING (stock_code, trade_date)
        WHERE s.etf_code = :etf
          AND s.stock_code = :stock_code
          AND s.trade_date IN (SELECT trade_date FROM date_set)
        ORDER BY s.trade_date ASC
        """
    )
    return [
        _row_to_dict(r)
        for r in conn.execute(
            sql, {"etf": etf, "stock_code": stock_code, "days": days}
        )
    ]


def get_stock_pnl(conn: Connection, etf: str, stock_code: str) -> dict:
    sql = text(
        """
        WITH latest_share AS (
          SELECT share_count, trade_date, instrument_type
          FROM shares
          WHERE etf_code = :etf AND stock_code = :stock_code
          ORDER BY trade_date DESC LIMIT 1
        ),
        latest_close AS (
          SELECT close, trade_date
          FROM prices
          WHERE stock_code = :stock_code
          ORDER BY trade_date DESC LIMIT 1
        )
        SELECT
          ls.share_count::bigint,
          lc.close,
          round(ls.share_count * lc.close / 1e8, 2) AS market_value_yi,
          lc.trade_date AS close_as_of,
          ls.trade_date AS shares_as_of,
          ls.instrument_type
        FROM latest_share ls, latest_close lc
        """
    )
    row = conn.execute(sql, {"etf": etf, "stock_code": stock_code}).first()
    if row is None:
        return {
            "share_count": 0,
            "close": None,
            "market_value_yi": 0,
            "close_as_of": None,
            "shares_as_of": None,
            "instrument_type": None,
            "is_pnl": False,
            "note": "no data found for this etf+stock pair",
        }
    result = _row_to_dict(row)
    result["is_pnl"] = False
    result["note"] = (
        "legacy valuation tool: returns current market value only. "
        "Use get_stock_unrealized_pnl_estimate for estimated P&L."
    )
    if result.get("instrument_type") != "equity":
        result["note"] += (
            f" This position is instrument_type="
            f"{result.get('instrument_type')!r}, not a listed stock — "
            "share_count and close are not directly comparable to equities."
        )
    return result


# ponytail: weighted-average estimate from daily close; replace with broker lots if real cost basis lands.
def _estimate_unrealized_pnl(
    rows: list[dict], latest_close: Decimal, latest_close_date: date
) -> dict:
    shares = Decimal(0)
    cost = Decimal(0)
    bought = Decimal(0)
    sold = Decimal(0)
    missing_price_dates: list[str] = []

    for row in rows:
        next_shares = Decimal(row["share_count"] or 0)
        delta = next_shares - shares
        close = row.get("close")
        if close is None:
            if delta > 0:
                missing_price_dates.append(row["trade_date"].isoformat())
            shares = next_shares
            continue

        close = Decimal(close)
        if delta > 0:
            bought += delta
            cost += delta * close
        elif delta < 0 and shares > 0:
            sell_qty = min(-delta, shares)
            sold += sell_qty
            cost -= cost * (sell_qty / shares)
        shares = next_shares
        if shares <= 0:
            shares = Decimal(0)
            cost = Decimal(0)

    market_value = shares * latest_close
    unrealized = market_value - cost
    return {
        "current_shares": int(shares),
        "latest_close": float(latest_close),
        "latest_close_date": latest_close_date.isoformat(),
        "estimated_cost_yi": round(float(cost / Decimal("1e8")), 2),
        "market_value_yi": round(float(market_value / Decimal("1e8")), 2),
        "unrealized_pnl_yi": round(float(unrealized / Decimal("1e8")), 2),
        "unrealized_return_pct": (
            round(float(unrealized / cost * 100), 2) if cost else None
        ),
        "total_bought_shares": int(bought),
        "total_sold_shares": int(sold),
        "missing_price_dates": missing_price_dates[:20],
        "estimate_complete": not missing_price_dates,
        "method": "weighted_average_cost_from_daily_share_delta_and_close",
    }


def get_stock_unrealized_pnl_estimate(
    conn: Connection, etf: str, stock_code: str
) -> dict:
    series_sql = text(
        """
        SELECT
          s.trade_date,
          s.stock_name,
          s.instrument_type,
          s.share_count::numeric AS share_count,
          px.close
        FROM shares s
        LEFT JOIN LATERAL (
          SELECT p.close
          FROM prices p
          WHERE p.stock_code = s.stock_code AND p.trade_date <= s.trade_date
          ORDER BY p.trade_date DESC
          LIMIT 1
        ) px ON true
        WHERE s.etf_code = :etf AND s.stock_code = :stock_code
        ORDER BY s.trade_date ASC
        """
    )
    close_sql = text(
        """
        SELECT close, trade_date
        FROM prices
        WHERE stock_code = :stock_code AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 1
        """
    )
    params = {"etf": etf, "stock_code": stock_code}
    rows = [dict(r._mapping) for r in conn.execute(series_sql, params)]
    latest = conn.execute(close_sql, {"stock_code": stock_code}).first()
    if not rows or latest is None:
        return {
            "etf": _etf_label(conn, etf),
            "stock_code": stock_code,
            "stock_name": rows[-1]["stock_name"] if rows else None,
            "instrument_type": rows[-1].get("instrument_type") if rows else None,
            "note": "no data found for this etf+stock pair",
        }

    instrument_type = rows[-1].get("instrument_type")
    note = (
        "Estimate only: uses daily share-count deltas and market closes, "
        "not broker lots, fees, taxes, dividends, or issuer trade prices."
    )
    if instrument_type != "equity":
        note += (
            f" This position is instrument_type={instrument_type!r}, not a "
            "listed stock — the cost/P&L model assumes equity share counts "
            "and does not apply."
        )
    result = _estimate_unrealized_pnl(rows, latest.close, latest.trade_date)
    result.update({
        "etf": _etf_label(conn, etf),
        "stock_code": stock_code,
        "stock_name": rows[-1]["stock_name"],
        "instrument_type": instrument_type,
        "shares_as_of": rows[-1]["trade_date"].isoformat(),
        "first_seen": rows[0]["trade_date"].isoformat(),
        "note": note,
    })
    return result


def get_consensus_buys(
    conn: Connection,
    start_date: str,
    end_date: str,
    min_etfs: int = 4,
) -> list[dict]:
    sql = text(
        f"""
        WITH per_etf_start AS (
          SELECT s.etf_code, s.stock_code, s.share_count
          FROM shares s
          WHERE s.trade_date = (
            SELECT max(trade_date) FROM shares s2
            WHERE s2.etf_code = s.etf_code AND s2.trade_date <= :start_date
          )
        ),
        per_etf_end AS (
          -- Cross-ETF consensus is a listed-stock concept: bonds/futures/
          -- options/cash never overlap meaningfully across issuers.
          SELECT s.etf_code, s.stock_code, s.stock_name, s.share_count
          FROM shares s
          WHERE s.instrument_type = 'equity'
            AND s.trade_date = (
              SELECT max(trade_date) FROM shares s2
              WHERE s2.etf_code = s.etf_code AND s2.trade_date <= :end_date
            )
        ),
        deltas AS (
          SELECT
            e.etf_code, e.stock_code, e.stock_name,
            e.share_count - coalesce(s.share_count, 0) AS delta_shares
          FROM per_etf_end e
          LEFT JOIN per_etf_start s USING (etf_code, stock_code)
          WHERE {_EXCLUDE_NON_STOCK.replace('stock_code', 'e.stock_code')}
        ),
        end_close AS (
          SELECT stock_code, close
          FROM prices
          WHERE trade_date = (
            SELECT max(trade_date) FROM prices WHERE trade_date <= :end_date
          )
        ),
        canonical AS (
          -- Pick the most-common stock_name per code to avoid grouping the same
          -- stock into multiple rows when ETF data sources spell names differently.
          SELECT stock_code,
                 mode() WITHIN GROUP (ORDER BY stock_name) AS stock_name
          FROM per_etf_end
          GROUP BY stock_code
        )
        SELECT
          d.stock_code,
          n.stock_name,
          count(*) FILTER (WHERE d.delta_shares > 0)::int AS etfs_buying,
          round(
            sum(d.delta_shares * coalesce(c.close, 0)) / 1e8,
            2
          ) AS total_delta_value_yi
        FROM deltas d
        LEFT JOIN end_close c USING (stock_code)
        LEFT JOIN canonical n USING (stock_code)
        GROUP BY d.stock_code, n.stock_name
        HAVING count(*) FILTER (WHERE d.delta_shares > 0) >= :min_etfs
        ORDER BY total_delta_value_yi DESC NULLS LAST
        LIMIT 30
        """
    )
    return [
        _row_to_dict(r)
        for r in conn.execute(
            sql,
            {
                "start_date": start_date,
                "end_date": end_date,
                "min_etfs": min_etfs,
            },
        )
    ]
