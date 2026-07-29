"""MCP server for Tally — exposes the expense/net-worth tracker as agent tools.

This is a thin client over Tally's HTTP JSON API (it does NOT touch the database
directly), so it can run anywhere that can reach the app over the tailnet. It
authenticates with the API key, so the app's login cookie is never needed.

Run as a remote network service (recommended when the agent is on another host —
serves Streamable HTTP at http://<host>:<MCP_PORT>/mcp):

    MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=9000 \
    TALLY_BASE_URL=http://localhost:8000 \
    TALLY_API_KEY=tally_sk_... \
    python -m tally_mcp.server

Run as a local stdio server (MCP host spawns it as a subprocess — Claude Desktop):

    TALLY_BASE_URL=http://app-host:8000 \
    TALLY_API_KEY=tally_sk_... \
    python -m tally_mcp.server

Config (env):
    TALLY_BASE_URL   Base URL of the running Tally app (default http://localhost:8000)
    TALLY_API_KEY    The API_KEY value from Tally's .env (required)
    TALLY_TIMEOUT    Per-request timeout seconds (default 15)
    MCP_TRANSPORT    stdio (default) | streamable-http | sse
    MCP_HOST         Bind host for HTTP transports (default 0.0.0.0)
    MCP_PORT         Bind port for HTTP transports (default 9000)
"""
from __future__ import annotations

import hmac
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("TALLY_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("TALLY_API_KEY", "").strip()
TIMEOUT = float(os.environ.get("TALLY_TIMEOUT", "15"))

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))

# Optional shared token guarding the HTTP endpoint itself (defence-in-depth on
# top of the tailnet boundary). Clients send `Authorization: Bearer <token>` or
# `X-MCP-Token: <token>`. Unset → endpoint is open (stdio never needs it).
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

# host/port are only consumed by the HTTP transports; harmless under stdio.
mcp = FastMCP("tally", host=MCP_HOST, port=MCP_PORT)


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    return httpx.Client(base_url=BASE_URL, headers=headers, timeout=TIMEOUT)


def _request(method: str, path: str, **kw) -> dict:
    """Call the Tally API and return parsed JSON, or a structured error dict so
    the agent gets a readable message instead of an exception."""
    try:
        with _client() as c:
            resp = c.request(method, path, **kw)
    except httpx.HTTPError as exc:
        return {"error": f"could not reach Tally at {BASE_URL}: {exc}"}
    if resp.status_code == 401:
        return {"error": "unauthorized — check TALLY_API_KEY matches Tally's API_KEY"}
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        return {"error": f"HTTP {resp.status_code}: {detail}"}
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"error": f"non-JSON response: {resp.text[:200]}"}


# ── read tools ───────────────────────────────────────────────────────────────

@mcp.tool()
def get_dashboard() -> dict:
    """Current-month financial snapshot: liquid cash (SGD-equivalent), spend by
    category, total expenses, days elapsed/remaining, average daily burn, and
    projected month-end spend. All money values are SGD-equivalent."""
    return _request("GET", "/api/dashboard")


@mcp.tool()
def get_liquid_cash(date: str | None = None) -> dict:
    """Combined SGD liquid cash as of a specific date (YYYY-MM-DD). Uses
    carry-forward: the most recent balance snapshot per account on or before
    that date. Current FX rates. Returns {snap_date, net_sgd, net_myr,
    combined_sgd}. Omit date for today. Use this for month-on-month comparisons
    (e.g. 'compare liquid cash today vs same day last month')."""
    params = {}
    if date:
        params["date"] = date
    return _request("GET", "/api/liquid-cash", params=params)


@mcp.tool()
def list_reference() -> dict:
    """Valid values for building a transaction or balance: the live `accounts`
    list (account_id, name, currency, asset/liability type, active), the
    `categories` list (name, is_discretionary), and supported `currencies`.
    Call this first if you don't know which account_id / category to use —
    they are user-editable, so don't hard-code them."""
    return _request("GET", "/api/reference")


@mcp.tool()
def get_fx_rates() -> dict:
    """Current foreign-exchange rates stored as `to_sgd` (1 unit of the currency
    = N SGD) plus the supported-currency list."""
    return _request("GET", "/api/fx")


@mcp.tool()
def list_transactions(
    date_from: str | None = None,
    date_to: str | None = None,
    account: str | None = None,
    category: str | None = None,
    flow: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> dict:
    """List transactions for a date range with an SGD-equivalent expense total —
    use this for "how much did I spend today / yesterday / this week / on date X".
    Dates are YYYY-MM-DD; pass the SAME date in date_from and date_to for a single
    day. Optional filters: account (account_id), category, flow
    (expense/income/transfer), q (substring of note/id). Returns `count`,
    `expense_total_sgd` (sum of ALL matching expenses, not just the page), and
    `transactions[]` where each row includes `amount_sgd`. For "today", read
    get_dashboard().today first if you're unsure of the current SGT date."""
    params = {"limit": limit}
    for k, v in {
        "date_from": date_from, "date_to": date_to, "account": account,
        "category": category, "flow": flow, "q": q,
    }.items():
        if v:
            params[k] = v
    return _request("GET", "/api/transactions", params=params)


# ── parse / write tools ──────────────────────────────────────────────────────

@mcp.tool()
def parse_expense(text: str) -> dict:
    """Parse free-text into structured fields WITHOUT logging it. Returns
    {amount, currency, category, account, flow, note}. Detects expense vs income
    automatically. Useful to preview/confirm before logging. Examples:
    "grab to airport 24" → flow=expense, "received salary 5000" → flow=income."""
    return _request("POST", "/api/parse", json={"text": text})


@mcp.tool()
def log_transaction(
    account_id: str,
    category: str,
    amount: float,
    to_account_id: str | None = None,
    currency: str | None = None,
    flow: str = "expense",
    note: str = "",
) -> dict:
    """Record a transaction (a money flow). `flow` is one of expense/income/transfer.
    `account_id` and `category` must be valid (see list_reference). `currency`
    defaults to the account's currency if omitted.

    For transfers (flow=transfer), pass `to_account_id` to create a double-entry:
    debits `account_id` and credits `to_account_id`. Category defaults to Transfer
    unless specified. Returns {ok, txn_id, from_account, to_account} for transfers
    or {ok, txn_id} for other flows."""
    body = {
        "account_id": account_id,
        "category": category,
        "amount": amount,
        "flow": flow,
        "note": note,
    }
    if to_account_id:
        body["to_account_id"] = to_account_id
    if currency:
        body["currency"] = currency
    return _request("POST", "/api/txn", json=body)


@mcp.tool()
def log_from_text(text: str, confirm: bool = True) -> dict:
    """One-shot natural-language logging: parse `text` then record the resulting
    transaction. Respects the flow detected by the parser (expense or income).
    Returns the parsed fields plus the new txn_id. Set confirm=False to only
    parse and return what WOULD be logged (no write) — use that when you want
    the user to approve first."""
    parsed = _request("POST", "/api/parse", json={"text": text})
    if "error" in parsed:
        return parsed
    if not confirm:
        return {"would_log": parsed, "logged": False}
    result = log_transaction(
        account_id=parsed["account"],
        category=parsed["category"],
        amount=parsed["amount"],
        currency=parsed.get("currency"),
        flow=parsed.get("flow", "expense"),
        note=parsed.get("note", ""),
        to_account_id=parsed.get("to_account"),
    )
    return {"parsed": parsed, "result": result, "logged": "txn_id" in result}


@mcp.tool()
def delete_transaction(txn_id: str) -> dict:
    """Delete a transaction by its txn_id — use this to UNDO a mistaken log (e.g.
    you logged the wrong account or a duplicate) instead of leaving a double
    entry. Get the txn_id from log_transaction's return, log_from_text's result,
    or list_transactions. Returns {ok, deleted} or an error if it doesn't exist.
    Deleting automatically reverses the balance update from the original log."""
    return _request("DELETE", f"/api/txn/{txn_id}")


@mcp.tool()
def set_balance(
    account_id: str,
    balance: float,
    currency: str | None = None,
    note: str = "",
) -> dict:
    """Upsert today's balance snapshot for a single account (a money stock).
    `currency` defaults to the account's currency. Returns {ok, rows}."""
    row: dict = {"account_id": account_id, "balance": balance, "note": note}
    if currency:
        row["currency"] = currency
    return _request("POST", "/api/balance", json={"rows": [row]})


class _TokenAuthMiddleware:
    """ASGI middleware: require a shared bearer token on the MCP HTTP endpoint.
    No-op when the token is empty. Non-http scopes (lifespan/websocket) pass
    straight through so the StreamableHTTP session manager still starts."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            headers = dict(scope.get("headers") or [])
            presented = ""
            raw = headers.get(b"authorization", b"").decode("latin-1")
            if raw.lower().startswith("bearer "):
                presented = raw[7:].strip()
            if not presented:
                presented = headers.get(b"x-mcp-token", b"").decode("latin-1").strip()
            if not (presented and hmac.compare_digest(presented, self.token)):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    if TRANSPORT in ("streamable-http", "sse"):
        import uvicorn

        inner = mcp.sse_app() if TRANSPORT == "sse" else mcp.streamable_http_app()
        uvicorn.run(_TokenAuthMiddleware(inner, MCP_AUTH_TOKEN), host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run(transport=TRANSPORT)  # stdio
