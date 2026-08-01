# etfedge-mcp

MCP server for [etfedge.xyz](https://etfedge.xyz) — 台灣主動 ETF 研究資料庫的 read-only 查詢介面。

Public MCP endpoint:

```text
https://mcp.etfedge.xyz/mcp
```

## 在 Claude 裡使用

### claude.ai（網頁版）

1. 開啟 [claude.ai](https://claude.ai) → 右上角頭像 → **Settings**
2. 側邊欄選 **Integrations**
3. 點 **Add integration** → 貼上 URL：
   ```
   https://mcp.etfedge.xyz/mcp
   ```
4. 儲存後，Claude 會引導你用 **GitHub 帳號登入**授權

### Codex CLI

Codex 0.130+ 支援 streamable HTTP MCP server：

```bash
codex mcp add etfedge --url https://mcp.etfedge.xyz/mcp
codex mcp login etfedge
codex mcp list
```

或手動加入 `~/.codex/config.toml`：

```toml
[mcp_servers.etfedge]
url = "https://mcp.etfedge.xyz/mcp"
```

完成 OAuth 後，`codex mcp list` 應顯示 `etfedge` 為 `enabled` 且 `Auth` 為 `OAuth`。已開啟的 Codex session 不會熱載入新 MCP；請重開 session 後使用。

### Claude Code（CLI）

在專案根目錄的 `.claude/settings.json`（或全域 `~/.claude/settings.json`）加入：

```json
{
  "mcpServers": {
    "etfedge": {
      "type": "http",
      "url": "https://mcp.etfedge.xyz/mcp"
    }
  }
}
```

重啟 Claude Code 後輸入 `/mcp` 確認連線。

### Claude Desktop

Claude Desktop 目前只支援本地 stdio 伺服器，需透過 `mcp-remote` 代理：

```json
{
  "mcpServers": {
    "etfedge": {
      "command": "npx",
      "args": ["mcp-remote", "https://mcp.etfedge.xyz/mcp"]
    }
  }
}
```

Config 路徑：
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

---

## Tools

| Tool | Description |
|------|-------------|
| `list_db_tables` | 列出可瀏覽的安全 allowlist 資料表 |
| `describe_table` | 查看 allowlist table 欄位與限制 |
| `get_table_stats` | 查看資料表 row count 與日期範圍 |
| `query_table` | 用參數化 filter/sort/pagination 查詢 allowlist table（limit max 500） |
| `get_distinct_values` | 查詢某欄位 distinct values 與筆數 |
| `get_data_freshness` | 查看各資料集最新日期與筆數 |
| `list_etfs` | 列出所有主動 ETF（代號、名稱、AUM、最新持股數） |
| `get_etf_buy_delta` | 取得某 ETF 今日加減碼股票（張數變化 + 市值） |
| `get_etf_holdings` | 取得某 ETF 最新完整持股明細（上市股票） |
| `get_stock_history` | 取得某 ETF 某股票的歷史持股張數 |
| `get_stock_pnl` | Legacy：取得某 ETF 某股票的目前市值，不是真實損益 |
| `get_stock_unrealized_pnl_estimate` | 用每日股數變化與收盤價估算某 ETF 某股票的未實現損益 |
| `get_consensus_buys` | 跨 ETF 共識加碼股票（≥N 家 ETF 同時加碼） |

`query_table` 不提供 raw SQL。`prices` 必須帶 `stock_code` 或 `trade_date`
filter，所有 table 查詢都限制在 allowlist 欄位且 `limit` 最高 500。

持股類 tool（`get_etf_holdings` / `get_etf_buy_delta`）預設只回上市股票
（`instrument_type = 'equity'`），債券、期貨、選擇權、現金與外匯部位不會混入；
債券型 ETF（代號結尾 `D`）請帶 `instrument_type="bond"`，要看完整投資組合帶
`instrument_type="all"`。`get_consensus_buys` 固定只算股票。

## Auth

GitHub OAuth via [fastmcp](https://github.com/jlowin/fastmcp)。連線時會引導 GitHub 登入，每個 GitHub 帳號 20 calls / 60 秒。

---

## 自架（Self-hosting）

```bash
pip install -e .
cp .env.example .env
# 填入 .env 後：
python mcp_server.py
```

See `.env.example` for required environment variables.
