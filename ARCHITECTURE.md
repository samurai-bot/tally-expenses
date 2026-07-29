# Tally — Architecture

A system-level view of how Tally is put together: components, request flows,
deployment topology, and trust boundaries. For *features & endpoints* see
[`README.md`](README.md); for the *domain model an agent should understand* see
[`SEMANTIC_LAYER.md`](SEMANTIC_LAYER.md); for the original product spec see
[`SPEC.md`](SPEC.md).

---

## 1. Overview

Tally is a single-user, self-hosted personal-finance tracker: one FastAPI web app
backed by a Postgres database, plus an optional MCP sidecar that exposes the same
data to AI agents. Everything lives inside a private Tailscale tailnet — there is
no public exposure. The web app is intentionally small and dependency-light
(server-rendered HTML, no JS framework, charts are hand-rolled SVG).

The central design rule is **stocks vs flows** (see §4): balances capture
what you have; transactions capture where money went. They stay in sync
automatically — logging a transaction adjusts the latest balance, and deleting
one reverses the adjustment. Manual balance snapshots still take precedence.

## 2. Components

```
                         Tailscale tailnet (the security boundary)
  ┌───────────────────────────────────────────────────────────────────────────┐
  │                                                                             │
  │   Browser ──login cookie──┐                                                 │
  │                           ▼                                                 │
  │                  ┌──────────────────┐   asyncpg/psycopg pool   ┌─────────┐  │
  │   AI agent ─────▶│  app (FastAPI)   │─────────────────────────▶│ Postgres │ │
  │   (Hermes)       │  :8000  uvicorn  │                          │ db-host  │ │
  │      │  API key  └──────────────────┘                          └─────────┘  │
  │      │           ▲     │      │                                              │
  │      │ MCP token │     │      └──httpx──▶ FX source (open.er-api.com, keyless)
  │      ▼           │     └─────────httpx──▶ LLM endpoint (OpenAI-compatible)   │
  │  ┌────────────────────┐                                                      │
  │  │  mcp (sidecar)     │  thin HTTP client over the app's /api/*              │
  │  │  :9000 streamable  │  (holds API key; never touches Postgres directly)    │
  │  └────────────────────┘                                                      │
  └───────────────────────────────────────────────────────────────────────────┘
            ▲
            └── weekly digest → Resend email API (outbound, optional)
```

- **app** — the FastAPI service (HTML pages + JSON API + scheduled jobs). The only
  component that talks to Postgres.
- **mcp** — optional sidecar that re-exposes the JSON API as MCP tools for agents.
  It is a *client* of the app, not of the database.
- **Postgres** — runs on a separate host (`db-host`), reached over the tailnet via
  `DATABASE_URL`. Holds all state.
- **External services** (all optional, all outbound, all fail-soft): the keyless FX
  source, an OpenAI-compatible LLM endpoint for NL parsing, and Resend for the
  weekly digest email.

## 3. Code map (`app/`)

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app: routes (pages + `/api/*`), auth middleware, lifespan (opens the pool, starts the scheduler), JSON serialization |
| `auth.py` | Single-user login (HMAC-signed cookie) and API key gate (`Authorization: Bearer` / `X-API-Key`) for `/api/*` |
| `db.py` | psycopg 3 `ConnectionPool` (sync) + `query`/`query_one`/`execute` helpers; DSN normalization |
| `queries.py` | **All SQL** lives here (parameterized), plus dashboard metric computation |
| `jobs.py` | Scheduled jobs: recurring poster, monthly rollover, weekly digest, FX update |
| `fx.py` | Supported-currency list, live FX fetch via open.er-api.com, NL currency detection |
| `llm.py` | NL → structured-expense parse via any OpenAI-compatible endpoint, with a regex fallback (never raises) |
| `charts.py` | Dependency-free server-rendered SVG (line / donut / bar) |
| `templates/`, `static/` | Jinja2 templates (pages + Balance/Log modals) and the dark-theme CSS |

The MCP sidecar lives in [`tally_mcp/`](tally_mcp/) (`server.py` = tools + transport;
its own `requirements.txt`; built by `Dockerfile.mcp`). It shares no code with `app/`.

## 4. Data model & the core invariant

Four tables carry the state (`schema.sql`), plus an additively-created `fx_rates`:

- **`accounts`** — places money sits (`asset` | `liability`, a currency, active flag).
- **`balances`** — *stocks*: dated snapshots of how much is in each account (one row
  per account per day; upserted). Source of truth for liquid cash.
- **`transactions`** — *flows*: an append-only ledger of money moving
  (`expense` | `income` | `transfer`), each with a category and currency.
- **`recurring`** — templates the poster turns into transactions on schedule.
- **`fx_rates`** — per-currency `to_sgd`, refreshed daily (fallbacks on failure).

**Transaction → balance reflection.** When a transaction is inserted, the latest
balance snapshot for its account is automatically adjusted (expense subtracts from
assets / adds to liabilities; income does the reverse). Deleting a transaction
reverses the adjustment. The recurring poster therefore keeps balances up-to-date
as it posts. Manual balance snapshots (via the form or API) still take precedence —
they overwrite the auto-adjusted row for the day.

All reporting is **SGD-equivalent**: amounts are stored in their native currency and
converted at read time via `fx_rates` (`amount * to_sgd`). Timezone is Asia/Singapore
for every date boundary.

## 5. Request flows

**Page render (browser).** `GET /` → auth middleware checks the login cookie →
`queries.dashboard()` computes month metrics → `charts.*` render SVG → Jinja2 returns
HTML. No client-side data fetching.

**Log a spend (NL).** Browser/agent → `POST /api/parse` (`llm.parse`, regex fallback
if the endpoint is down) → caller confirms → `POST /api/txn` validates `account_id`/
`category` against the DB and appends one `transactions` row.

**Agent via MCP.** Hermes → `mcp :9000/mcp` (MCP token checked by an ASGI middleware)
→ the matching tool calls the app's `/api/*` over HTTP with the API key → app → Postgres.
The agent never holds the API key and never reaches Postgres.

**Scheduled jobs (APScheduler, started in the app lifespan).** Daily recurring poster
(idempotent via a partial unique index), 1st-of-month rollover, daily FX update (+ once
on boot), weekly digest email. All run in a background thread inside the app process.

## 6. Deployment topology

Two containers managed by `docker-compose.yml`, both on `network_mode: host` so they
bind directly on the tailnet host (`app-host`):

| Service | Image | Port | Role |
|---|---|---|---|
| `app` | `Dockerfile` | `:8000` | FastAPI web app + scheduler |
| `mcp` | `Dockerfile.mcp` | `:9000` | MCP sidecar (Streamable HTTP) |

Postgres is **external** (on `db-host`), reached via `DATABASE_URL`. Secrets come from
`.env` (gitignored): DB DSN, login credentials + cookie `SECRET_KEY`, `API_KEY`,
`MCP_AUTH_TOKEN`, and the optional LLM / Resend keys. The app image is rebuilt to ship
code changes (`docker compose up -d --build`) — a plain restart only reloads `.env`.

## 7. Trust boundaries & auth layers

Defence in depth, outermost first:

1. **Tailnet** — nothing is publicly exposed (Tailscale Serve, never Funnel). This is
   the primary boundary; both services assume on-tailnet callers are semi-trusted.
2. **Login cookie** — HTML pages require a single-user HMAC-signed session cookie
   (`auth.py`); credentials + `SECRET_KEY` from env.
3. **API key** — `/api/*` requires `API_KEY` (Bearer / `X-API-Key`) for non-browser
   callers. Constant-time compare; blank disables the bypass.
4. **MCP token** — the `mcp` endpoint requires `MCP_AUTH_TOKEN` (Bearer), checked
   before any MCP traffic. Distinct from the API key: agents hold the MCP token; the
   sidecar holds the API key. Rotating either is a one-line `.env` change.

All SQL is parameterized; `account_id`/`category` are validated against the DB on every
write (unknown → 400). External calls (FX, LLM, email) are outbound-only and fail-soft —
none can block startup or a request.

## 8. Design choices & trade-offs

- **Sync psycopg + a small pool** over async — the workload is one user; simplicity wins.
- **Server-rendered HTML + SVG**, no JS framework — fewer moving parts, trivial to host.
- **Additive schema migrations** (`ensure_*_schema` at startup) keep `schema.sql` stable
  while letting `fx_rates` / recurring columns appear without a migration tool.
- **MCP as a sidecar, not in-process** — keeps the web app free of the `mcp` dependency,
  lets the agent surface be deployed/scaled/secured independently, and keeps the API key
  co-located with the app rather than distributed to every agent.
- **Fail-soft externals** — a dead LLM falls back to regex; a failed FX fetch keeps the
  last/fallback rates; an unset Resend key no-ops the digest.
