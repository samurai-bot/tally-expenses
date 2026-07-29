# Tally MCP server

Exposes the running Tally app to any MCP-capable agent (Claude Desktop, Claude
Code, your own client) as a set of tools. It's a thin HTTP client over Tally's
`/api/*` endpoints — it authenticates with the **API key**, so it never needs the
browser login cookie, and it doesn't touch the database directly.

## Tools

| Tool | Calls | Purpose |
|---|---|---|
| `get_dashboard` | `GET /api/dashboard` | Current-month snapshot (liquid cash, spend by category, burn, projection) |
| `get_liquid_cash` | `GET /api/liquid-cash` | Combined SGD liquid cash for any date — month-on-month comparisons |
| `list_reference` | `GET /api/reference` | Valid accounts, categories, currencies — read this before writing |
| `get_fx_rates` | `GET /api/fx` | FX rates (`to_sgd`) + supported currencies |
| `list_transactions` | `GET /api/transactions` | Transactions for any date range + `expense_total_sgd` (per-day/-week spend) |
| `parse_expense` | `POST /api/parse` | NL text → structured fields, **no write** |
| `log_transaction` | `POST /api/txn` | Record a transaction (expense/income/transfer) |
| `log_from_text` | parse → txn | One-shot NL logging (`confirm=False` to preview only) |
| `delete_transaction` | `DELETE /api/txn/{id}` | Undo a mistaken log (remove a wrong/duplicate entry) |
| `set_balance` | `POST /api/balance` | Upsert today's balance for one account |

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r tally_mcp/requirements.txt
```

## Configure

| Env var | Default | Notes |
|---|---|---|
| `TALLY_BASE_URL` | `http://localhost:8000` | Where the Tally app is reachable |
| `TALLY_API_KEY` | — | Must equal `API_KEY` in Tally's `.env` (**required**) |
| `TALLY_TIMEOUT` | `15` | Per-request timeout (seconds) |

## Run

```bash
TALLY_BASE_URL=http://app-host:8000 \
TALLY_API_KEY=tally_sk_... \
python -m tally_mcp.server
```

## Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "tally": {
      "command": "python",
      "args": ["-m", "tally_mcp.server"],
      "cwd": "/path/to/expenses-app",
      "env": {
        "TALLY_BASE_URL": "http://app-host:8000",
        "TALLY_API_KEY": "tally_sk_..."
      }
    }
  }
}
```

(Point `command` at the venv's python — e.g. `/path/to/expenses-app/.venv/bin/python` — if `mcp`/`httpx` aren't on the system python.)

## Claude Code

```bash
claude mcp add tally \
  --env TALLY_BASE_URL=http://app-host:8000 \
  --env TALLY_API_KEY=tally_sk_... \
  -- python -m tally_mcp.server
```

See [`../SEMANTIC_LAYER.md`](../SEMANTIC_LAYER.md) for the domain model the agent
should understand (accounts vs balances vs transactions, liquid cash, burn, FX).
