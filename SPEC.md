# Expenses Tracker — Build Spec (for Claude Code)

> **Historical document.** This is the original v1.0 build plan. The app has since
> evolved: login gate was added, FX moved to a multi-currency `fx_rates` table,
> transfers with double-entry were introduced, PAYLAH was consolidated into DBS,
> and the MCP sidecar + `/api/liquid-cash` endpoint were added. For the current
> state see [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`SEMANTIC_LAYER.md`](SEMANTIC_LAYER.md).

A self-hosted personal expenses tracker. Replaces a Google Sheet + n8n webhooks with one
private FastAPI service. The **app runs on `app-host`**; **Postgres runs on `db-host`** and is
reached across the tailnet. Reachable only over Tailscale.

Implement exactly to this spec. `schema.sql` and `seed.sql` (same folder) are authoritative for the
data model and starting data — load them as-is; do not redesign the schema.

---

## 1. Goals & non-goals

- One service on **app-host**: serves the UI (dashboard + two forms), exposes a small JSON API,
  runs scheduled jobs.
- Backed by the user's **existing Postgres on `db-host`**, reached over the tailnet via `DATABASE_URL`
  (host = db-host's tailnet name/IP, e.g. `db-host.<tailnet>.ts.net` or its `100.x` address).
- **Private**: lives entirely inside the tailnet. The app binds a port on app-host and is reached at
  app-host's tailnet address; no public exposure (never Funnel), no login screen. `tailscale serve`
  is optional (only for HTTPS + a clean hostname).
- No Google Sheets, no n8n. This fully replaces them.
- Non-goals: multi-user, mobile-native apps, bank API integration (no SG bank API exists).

## 2. Stack

- Python 3.12, **FastAPI** + Uvicorn.
- Postgres via **psycopg 3** (`psycopg[binary]`) with `psycopg_pool.ConnectionPool` (sync).
- **Jinja2** server-rendered templates (no SPA framework).
- **APScheduler** (`BackgroundScheduler`) for the cron jobs, started in the FastAPI lifespan.
- **httpx** for the LiteLLM call.
- Timezone: **Asia/Singapore** for all date logic (`zoneinfo.ZoneInfo("Asia/Singapore")`), never UTC.
- Money: `numeric(14,2)` in DB; format to 2 dp in UI. Amounts are always positive; direction comes
  from `flow`.

## 3. Repo layout (suggested)

```
expenses-app/
  app/
    main.py          # FastAPI app, routes, lifespan (starts scheduler)
    db.py            # connection pool + query helpers
    queries.py       # all SQL (dashboard metrics, latest balances, etc.)
    jobs.py          # recurring poster, monthly rollover, weekly digest
    llm.py           # LiteLLM parse + regex fallback
    templates/       # dashboard.html, balance.html, log.html, base.html
    static/          # optional css
  schema.sql
  seed.sql
  requirements.txt
  Dockerfile
  docker-compose.yml
  .env.example
  README.md
```

## 4. Data model

Use `schema.sql` verbatim. Summary:

- `accounts(account_id PK, name, currency, type[asset|liability], active, sort_order)`
- `categories(name PK, is_discretionary, sort_order)` — `is_discretionary` drives the burn-rate calc.
- `recurring(recur_id PK, name, account_id FK, flow, category, amount, currency, day_of_month, frequency, month_of_year, active, start_date, end_date, note, external_pipeline)` — `frequency` ∈ {`monthly`, `yearly`, `every30`}; `external_pipeline=true` keeps the charge in projections but delegates ledger logging to the email pipeline (`frequency`/`month_of_year`/`external_pipeline` are added additively at startup if missing)
- `transactions(id, txn_id UNIQUE, txn_date, account_id FK, flow, category, amount, currency, source, note, created_at)` — **append-only flows ledger**. Partial unique index `(source, txn_date) WHERE source LIKE 'recurring:%'` enforces recurring-poster idempotency.
- `balances(id, snap_date, account_id FK, balance, currency, source, note, created_at, UNIQUE(snap_date, account_id))` — **stocks**, one row per account per day (the old `snap_key`).
- `app_config(id=1, myr_to_sgd)` — FX assumption.
- Views: `v_latest_balance` (DISTINCT ON account_id, newest snap_date), `v_net_cash` (assets − liabilities per currency).

### Core model principle
**Stocks and flows stay in sync.** `balances` is "how much money exists"; `transactions` is "where it
went" (categorization). Inserting or deleting a transaction **auto-reflects** into the account's latest
balance snapshot: an expense subtracts from assets (adds to a liability/card), income does the reverse,
and a delete reverses the same adjustment. The reflected row is written as today's snapshot with
`source='auto'`. Manual capture still wins — saving the balance form (`source='form'`) upserts the same
`(snap_date, account_id)` and overwrites the auto row, so a user-entered figure is never clobbered.
(Originally stocks and flows were kept independent; auto-reflection superseded that so the dashboard
tracks spending without waiting for the next manual snapshot.)

## 5. Business rules

### 5.1 Balance capture (upsert)
- POST writes one row per account for **today** (Asia/Singapore).
- Upsert on `(snap_date, account_id)`: `INSERT ... ON CONFLICT (snap_date, account_id) DO UPDATE SET balance=EXCLUDED.balance, currency=EXCLUDED.currency, source='form', note=EXCLUDED.note`.
- Re-saving the same day updates the row (no duplicates). Different day = new row (history kept).
- Date is stamped **server-side**; ignore any client-supplied date.

### 5.2 Transaction logging (append)
- POST inserts one row into `transactions`, `txn_date = today` (server-side), `source='manual'`,
  `txn_id = 'T' + yyyymmddHHMMSS`.
- Never upsert transactions; each is a distinct row.

### 5.3 Recurring poster (daily 05:00)
- For each `recurring` row where `active` AND `external_pipeline=false` AND today is within `[start_date, end_date]` (end NULL = open),
  decide "due today" via `frequency` (shared `is_due()`/`next_due()` helpers in `jobs.py`):
  - **monthly** — due when `today.day == min(day_of_month, days_in_month)` (so day 30/31 fires on
    month-end in short months).
  - **yearly** — same day rule, but only in `month_of_year`.
  - **every30** — due every 30 days counted from `start_date` (the last subscription date); i.e.
    `(today − start_date).days` is positive and a multiple of 30. SaaS/Netflix-style fixed cycle.
  - When due: insert a transaction with `source='recurring:'+recur_id`, `category`, `amount`,
    `currency`, `account_id` from the template, `note='auto'`, `txn_id='T'+yyyymmdd+'-'+recur_id`.
  - Idempotent: rely on the partial unique index; use `ON CONFLICT DO NOTHING`. Re-running the same
    day posts nothing new. The recurring UI shows each template's estimated `next_due`.

### 5.4 Monthly rollover (1st of month 00:05)
- For each account, take its latest balance (`v_latest_balance`) and insert a row dated the **1st of
  the current month**, `source='rollover'`, `note='carry-forward opening'`.
- Upsert on `(snap_date, account_id) DO NOTHING` so it never overwrites a real form snapshot for the 1st
  and never duplicates.

### 5.5 Dashboard metrics (all "current month" = month of today, Asia/Singapore)
- **Net cash (SGD)** = Σ latest balance of SGD assets − Σ latest balance of SGD liabilities.
- **Net cash (MYR)** = same for MYR.
- **Combined (SGD)** = Net SGD + Net MYR × `app_config.myr_to_sgd`.
- **Latest balance per account** from `v_latest_balance` (+ account meta).
- **Spend by category (MTD)** = Σ `transactions.amount` where `flow='expense'`, `currency='SGD'`,
  `txn_date` in current month, grouped by category. (Show MYR column too if any MYR expenses.)
- **Total expenses (MTD, SGD)** = Σ of the above.
- **Days elapsed** = `today.day`.
- **Avg daily burn** = (Σ discretionary categories MTD) / days elapsed, where discretionary =
  categories with `is_discretionary=true` (currently Transport, Food, Other). NOT total/days — fixed
  monthly bills must be excluded or the figure explodes early in the month.
- **Projected month** = Total expenses MTD + (avg daily burn) × (days_in_month − days elapsed).
  i.e. fixed costs already booked + discretionary run-rate over remaining days.

## 6. Pages (Jinja, server-rendered, dark theme to match existing forms)

Reuse the existing visual style: dark background `#0b1220`, cards `#141d2e`, borders `#28344f`,
text `#eef2f8`, muted `#93a0b8`, accent `#3b82f6`. Mobile-first, `max-width:460px` for forms.

### `GET /` — Dashboard
KPI band (Net SGD / Net MYR / Combined), latest-balance table, spend-by-category table (flag
discretionary), Total / Days elapsed / Avg daily burn / Projected month. Links to the two forms.

### `GET /balance` — Balance capture form
- One number input per **active** account, pre-filled with the latest balance per account.
- Live "Net cash SGD | MYR" readout as the user edits (liabilities subtract; `_CC`/type=liability).
- Save → `POST /api/balance` with `{rows:[{account_id, balance, currency}]}` → success state.

### `GET /log` — Transaction logger
- Natural-language input + "Parse" button → `POST /api/parse` → fills editable fields
  (amount, category[select], account[select], note) → confirm → "Log it" → `POST /api/txn`.
- Full category + account dropdowns (all 11 accounts, all 10 categories).
- Parse must fall back to the local regex (client or server) if the LLM is unavailable, so logging
  never blocks on the LLM.

## 7. JSON API

- `POST /api/balance` → body `{rows:[{account_id, balance, currency?}]}` → upsert today's snapshot.
  Returns `{ok:true, rows:n}`.
- `POST /api/txn` → body `{account_id, category, amount, currency?, flow?, note?}` → insert + auto-reflect
  the account's latest balance (§4 core principle). Returns `{ok:true, txn_id}`. `flow` defaults to
  `expense`; `income` (salary/refund/cashback) and `transfer` are also accepted.
- `POST /api/parse` → body `{text}` → returns `{amount, category, account, flow, note}`; detects expense
  vs income and the currency from the text. Server calls LiteLLM; on any error returns the regex-based
  parse (never 5xx).
- `GET /api/dashboard` → JSON of all dashboard metrics (handy for future widgets).

## 8. LLM parse (OpenAI-compatible, OpenRouter)

- Endpoint: `LLM_URL` (default `http://localhost:8000/v1/chat/completions`),
  `Authorization: Bearer ${LLM_KEY}`, model `LLM_MODEL`
  (current choice: `google/gemma-4-26b-a4b-it`).
- Request: `temperature:0`, `response_format:{type:"json_object"}`, system + user messages.
- System prompt: extract one transaction (expense or income) → JSON `{amount(number), category(one of the
  categories), account(one of the account_ids), flow("expense"|"income"|"transfer"), note(short)}`. Rules:
  **Grab/Gojek/Tada/taxi/MRT/bus/ezlink/fuel/parking → Transport, unless the text clearly means food (e.g.
  GrabFood); salary/bonus/refund/cashback → income. Default account CASH_SGD, default flow expense.**
- Robust extraction: the model may wrap JSON in prose/``` fences — extract the first `{...}` block,
  `JSON.parse`, coerce types, default missing fields.
- **Regex fallback** (use when LLM errors or returns junk): number → amount; keyword maps for
  category (`rice|food|kopi|...`→Food, `grab|gojek|tada|taxi|mrt|bus|ezlink|fuel|parking`→Transport
  [after food check], `netflix|icloud|...`→Subscriptions) and account (`dbs cc`→DBS_CC, `mbb cc`→MBB_CC,
  `isaavy`→MBB_ISAAVY, `dbs`→DBS, `ocbc`→OCBC, `paylah`→DBS, `wise`→WISE, `tngo`→TNGO,
  `tunai`→TUNAI, `maybank|myr`→MBB_MYR, else CASH_SGD). Currency derived from the account.

## 9. Config / env (`.env`)

```
# Postgres lives on db-host; reach it over the tailnet (NOT localhost)
DATABASE_URL=postgresql://user:pass@db-host.<tailnet>.ts.net:5432/expenses
TZ=Asia/Singapore
LLM_URL=https://openrouter.ai/api/v1/chat/completions
LLM_KEY=sk-...
LLM_MODEL=google/gemma-4-26b-a4b-it
# Weekly digest email via Resend (skip the job if RESEND_API_KEY unset)
RESEND_API_KEY=re_...
DIGEST_FROM=expenses@your-verified-domain.com   # Resend-verified domain
DIGEST_TO=you@example.com
```

Currency per account comes from the DB, not env. FX rate lives in `app_config.myr_to_sgd` (editable in
DB; optionally expose a tiny settings field later).

## 10. Scheduled jobs (APScheduler, Asia/Singapore)

- `recurring_poster` — cron daily 05:00; skips `external_pipeline` templates.
- `monthly_rollover` — cron day=1 00:05.
- `weekly_digest` — cron Mon 07:00. Compute the dashboard metrics; if `RESEND_API_KEY` set, send an
  HTML summary to `DIGEST_TO` via the **Resend API** (`POST https://api.resend.com/emails`,
  `Authorization: Bearer {RESEND_API_KEY}`, body `{from: DIGEST_FROM, to: [DIGEST_TO], subject, html}`,
  via httpx); else no-op (or log). Reads only. `DIGEST_FROM` must be a Resend-verified sender.

All jobs must be safe to run repeatedly (idempotent) and on an empty/partial month.

## 11. Deployment

Topology: **app on app-host → Postgres on db-host**, both on the tailnet.

Prerequisite (on db-host Postgres): allow connections from app-host — `listen_addresses` includes
the tailscale interface, and a `pg_hba.conf` line permitting app-host's tailnet IP (or the
`100.64.0.0/10` CGNAT range) to the `expenses` DB. Create the DB/role first.

- `docker-compose.yml` runs **only the app** on app-host. It connects to Postgres via `DATABASE_URL`
  pointing at db-host's tailnet host (do NOT use `localhost` / `host.docker.internal` — Postgres is on
  a different machine). The container needs tailnet reachability: simplest is `network_mode: host`
  (app-host is already on the tailnet), or otherwise ensure DNS/routing to `*.ts.net` from the
  container.
- Load schema + seed against db-host (run from app-host):
  `psql "$DATABASE_URL" -f schema.sql && psql "$DATABASE_URL" -f seed.sql`.
- `docker compose up -d` on app-host. Bind the app to `0.0.0.0:8000` so it's reachable on the
  tailnet interface.
- Access: it's already private to the tailnet — open `http://app-host.<tailnet>.ts.net:8000/` (or the
  `100.x:8000` address) from any tailnet device. **Do NOT use `tailscale funnel`.** Optionally run
  `tailscale serve https / http://localhost:8000` for HTTPS + a port-less hostname — nice-to-have, not
  required.
- README must include these steps + how to point a phone at it (open the app-host URL, Add to Home
  Screen).

## 12. Security

- No public exposure (Serve, not Funnel). No auth screen by request — tailnet is the boundary.
- Parameterize ALL SQL (psycopg placeholders) — no string interpolation into queries.
- Validate `account_id` and `category` against the DB on writes; reject unknown values.
- Keep `LLM_KEY` in env only.

## 13. Acceptance criteria

1. `schema.sql` + `seed.sql` load clean into a fresh Postgres; with latest snapshot = 2026-06-03 the
   dashboard shows Net cash (SGD) = **11,320.00**, Net cash (MYR) = **5,150.00**, Combined (SGD) ≈
   **12,865.00** (FX 0.30). (Assets 11,670.00 − liabilities 350.00 = 11,320.00.)
2. Saving the balance form twice in one day yields exactly one row per account for that date (upsert).
3. Logging "grab to office 12" → category **Transport**, account default **CASH_SGD**, amount 12,
   flow **expense**; appended to `transactions` and the CASH_SGD latest balance auto-adjusts down by 12.
4. Running the recurring poster on a day a template is due appends exactly one row; running it again
   the same day appends nothing.
5. Avg daily burn uses only discretionary categories; Projected month = booked MTD + burn × remaining
   days (no fixed-cost explosion).
6. Killing the LiteLLM endpoint still lets `/log` parse via regex and log successfully.
7. App reachable from a tailnet device at app-host's address; not reachable publicly (no Funnel).

## 14. Migration notes

- `seed.sql` is **dummy sample data** (not real finances) covering accounts, categories, recurring
  templates, a short balance history (31 May–3 Jun), and a small transaction ledger — enough to make
  the dashboard, charts, and jobs meaningful for demos and local testing. Replace it with your own.
