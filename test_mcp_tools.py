import mcp_tools
from datetime import date
from decimal import Decimal


class _Conn:
    def execute(self, sql, params=None):
        self.sql = str(sql)
        self.params = params or {}
        return []


def _raises(fn, text):
    try:
        fn()
    except ValueError as e:
        assert text in str(e)
    else:
        raise AssertionError(f"expected ValueError containing {text!r}")


def test_table_safety_helpers():
    _raises(lambda: mcp_tools._table("admin_sessions"), "not allowed")
    _raises(lambda: mcp_tools._where("prices", []), "requires")
    _raises(
        lambda: mcp_tools._where(
            "prices", [{"column": "close", "op": "gt", "value": 100}]
        ),
        "requires",
    )
    _raises(
        lambda: mcp_tools._where(
            "shares", [{"column": "stock_code", "op": "regex", "value": "2330"}]
        ),
        "unsupported",
    )

    where, params = mcp_tools._where(
        "prices", [{"column": "stock_code", "op": "eq", "value": "2330"}]
    )
    assert where == "WHERE stock_code = :v0"
    assert params == {"v0": "2330"}
    assert mcp_tools._limit(999) == 500
    assert mcp_tools._limit(0) == 100


def test_instrument_filter_defaults_to_equity():
    assert mcp_tools._instrument_filter("s", None) == (
        "s.instrument_type = :itype",
        {"itype": "equity"},
    )
    assert mcp_tools._instrument_filter("", "BOND") == (
        "instrument_type = :itype",
        {"itype": "bond"},
    )
    assert mcp_tools._instrument_filter("s", "all") == ("TRUE", {})
    _raises(
        lambda: mcp_tools._instrument_filter("s", "stock_code = '1' OR 1=1"),
        "instrument_type must be one of",
    )


class _Rows(list):
    def first(self):
        return self[0] if self else None


class _RecordingConn:
    """Fake connection: records every statement, returns canned rows."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        sql = str(sql)
        self.statements.append((sql, params or {}))
        if "max(trade_date) AS d FROM shares" in sql:
            return _Rows([(date(2026, 4, 30),)])
        if "SELECT coalesce(" in sql:
            return _Rows([("Test ETF",)])
        return _Rows()

    def stmt(self, needle):
        for sql, params in self.statements:
            if needle in sql:
                return sql, params
        raise AssertionError(f"no statement containing {needle!r}")


def test_position_tools_filter_non_equity_by_default():
    conn = _RecordingConn()
    mcp_tools.get_etf_buy_delta(conn, "00981A", "2026-01-01", "2026-02-01")
    sql, params = conn.stmt("start_snap")
    assert "instrument_type = :itype" in sql
    assert params["itype"] == "equity"

    conn = _RecordingConn()
    mcp_tools.get_consensus_buys(conn, "2026-01-01", "2026-02-01")
    assert "s.instrument_type = 'equity'" in conn.stmt("per_etf_end")[0]

    conn = _RecordingConn()
    result = mcp_tools.get_etf_holdings(conn, "00981A")
    sql, params = conn.stmt("value_yi")
    assert "s.instrument_type = :itype" in sql
    assert params["itype"] == "equity"
    assert result["instrument_type"] == "equity"
    assert result["as_of"] == "2026-04-30"
    # excluded types are reported so a bond ETF's empty equity book is explainable
    assert "instrument_type <> :itype" in conn.stmt("instrument_type <> :itype")[0]

    conn = _RecordingConn()
    mcp_tools.get_etf_holdings(conn, "00980D", instrument_type="all")
    assert "TRUE" in conn.stmt("value_yi")[0]
    assert conn.stmt("value_yi")[1] == {"etf": "00980D", "as_of": date(2026, 4, 30)}


def test_shares_and_holdings_expose_instrument_type_column():
    for table in ("shares", "holdings"):
        assert mcp_tools._column(table, "instrument_type") == "instrument_type"
    where, params = mcp_tools._where(
        "shares", [{"column": "instrument_type", "op": "eq", "value": "equity"}]
    )
    assert where == "WHERE instrument_type = :v0"
    assert params == {"v0": "equity"}


def test_query_table_uses_safe_sql_parts():
    conn = _Conn()
    assert mcp_tools.query_table(
        conn,
        "shares",
        filters=[{"column": "etf_code", "op": "eq", "value": "00981A"}],
        sort_by="trade_date",
        sort_dir="desc",
        limit=999,
    ) == []
    assert "FROM shares WHERE etf_code = :v0 ORDER BY trade_date DESC" in conn.sql
    assert conn.params == {"v0": "00981A", "limit": 500, "offset": 0}


def test_unrealized_pnl_estimate_weighted_average():
    result = mcp_tools._estimate_unrealized_pnl(
        [
            {
                "trade_date": date(2026, 1, 1),
                "share_count": 100_000_000,
                "close": Decimal("10"),
            },
            {
                "trade_date": date(2026, 1, 2),
                "share_count": 150_000_000,
                "close": Decimal("20"),
            },
            {
                "trade_date": date(2026, 1, 3),
                "share_count": 120_000_000,
                "close": Decimal("30"),
            },
        ],
        Decimal("40"),
        date(2026, 1, 4),
    )
    assert result["current_shares"] == 120_000_000
    assert result["estimated_cost_yi"] == 16.0
    assert result["market_value_yi"] == 48.0
    assert result["unrealized_pnl_yi"] == 32.0
    assert result["unrealized_return_pct"] == 200.0
    assert result["estimate_complete"] is True


def test_unrealized_pnl_estimate_uses_latest_non_null_close():
    class Result(list):
        def first(self):
            return self[0] if self else None

    class Row:
        def __init__(self, **values):
            self._mapping = values
            self.__dict__.update(values)

        def __getitem__(self, index):
            return tuple(self._mapping.values())[index]

    class Conn:
        def execute(self, sql, params=None):
            sql = str(sql)
            if "FROM shares s" in sql:
                return Result([
                    Row(
                        trade_date=date(2026, 1, 1),
                        stock_name="Test Stock",
                        share_count=100_000_000,
                        close=Decimal("10"),
                    )
                ])
            if "SELECT close, trade_date" in sql:
                prices = [
                    Row(close=None, trade_date=date(2026, 1, 3)),
                    Row(close=Decimal("20"), trade_date=date(2026, 1, 2)),
                ]
                if "close IS NOT NULL" in sql:
                    prices = [row for row in prices if row.close is not None]
                return Result(prices)
            return Result([Row(name="Test ETF")])

    result = mcp_tools.get_stock_unrealized_pnl_estimate(Conn(), "TEST", "1234")

    assert result["latest_close"] == 20.0
    assert result["latest_close_date"] == "2026-01-02"
    assert result["unrealized_pnl_yi"] == 10.0


if __name__ == "__main__":
    test_table_safety_helpers()
    test_instrument_filter_defaults_to_equity()
    test_position_tools_filter_non_equity_by_default()
    test_shares_and_holdings_expose_instrument_type_column()
    test_query_table_uses_safe_sql_parts()
    test_unrealized_pnl_estimate_weighted_average()
    test_unrealized_pnl_estimate_uses_latest_non_null_close()
