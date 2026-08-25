# Tally — Semantic Layer

Context for an AI agent operating Tally, a self-hosted personal **expense and
net-worth tracker**. Read this to understand what the data *means* before reading
or writing it. The machine-readable enumerations (accounts, categories, FX rates)
are live — fetch them from `GET /api/reference` and `GET /api/fx`; this document
explains the concepts behind them.

---

## 1. What Tally tracks

Two kinds of money data, kept in sync automatically:

- **Stocks → balances.** *How much you have right now* in each account. A
  **balance** is a point-in-time snapshot (e.g. "DBS had S$4,200 on 5 Jun").
  History is a series of dated snapshots, one per account per day at most.
- **Flows → transactions.** *Money moving* — a spend, an income, a transfer. A
  **transaction** is an event with a date, amount, category, and account.

When you log or delete a transaction, the latest balance snapshot **automatically
reflects it** (expense subtracts from assets / adds to liabilities; income does
the reverse; delete reverses the adjustment). You can still manually override
a balance snapshot at any time — it becomes the new baseline.

## 2. Conventions

- **Timezone:** Asia/Singapore (SGT). "Today", month boundaries, and IDs use SGT.
- **Base currency:** **SGD.** Every headline figure is shown *SGD-equivalent*,
  converted via stored FX rates. Native amounts are preserved on each row.
- **FX storage:** rates are stored as `to_sgd` — 1 unit of the currency = N SGD
  (e.g. MYR `to_sgd` ≈ 0.30). To value an amount in SGD: `amount * to_sgd`.
- **Money precision:** amounts are 2-decimal; FX rates up to 6-decimal.

## 3. Core entities

### accounts
A place money sits. Fields: `account_id` (uppercase code, e.g. `DBS`, `MBB_MYR`),
`name`, `currency`, `type` (**`asset`** or **`liability`**), `active`, `sort_order`.
A liability (e.g. a credit card) counts *negatively* toward net worth.

### categories
A spending/income bucket. Fields: `name` (e.g. `Food`, `Transport`, `Housing`),
`is_discretionary` (bool). **Discretionary** = controllable/optional spend; it's
what the "burn" metric measures (see §4). Non-discretionary = fixed/committed
(rent, insurance, tax…).

### transactions (flows)
Fields: `txn_id`, `txn_date`, `account_id`, `flow`, `category`, `amount`,
`currency`, `source`, `note`.
- **`flow`** ∈ `expense` | `income` | `transfer`. Dashboard spend = `expense` rows.
- **`source`** records origin: `manual`, `form`, or `recurring:<id>` for
  auto-posted recurring charges.

### balances (stocks)
Fields: `snap_date`, `account_id`, `balance`, `currency`, `source`, `note`.
One row per (account, day). Re-capturing the same day upserts. Carry-forward
logic means a day's net worth uses the latest snapshot on or before that day per
account, so a partial capture still reflects true standing.

### recurring
Templates that auto-post transactions on a schedule. Fields include `flow`,
`category`, `amount`, `currency`, `frequency` (`monthly` | `yearly` | `every30`),
`day_of_month`, `month_of_year` (yearly only), `start_date`/`end_date`, `active`,
and `external_pipeline`. When `external_pipeline` is true, the template remains
in dashboard projections but is not posted by Tally's recurring job; an external
pipeline owns the ledger entry.
For `every30` (SaaS/Netflix-style subs), `start_date` is the last subscription
date and renewals fall every 30 days after it. A daily job posts due ones
idempotently.

### fx_rates
Per-currency `to_sgd`, with `source` and `updated_at`. Refreshed daily from a
public endpoint; falls back to last-known/static rates on failure.

## 4. Derived metrics (what the dashboard means)

All SGD-equivalent, for the **current month** unless noted.

| Metric | Meaning | How it's computed |
|---|---|---|
| **Liquid cash** | Net spendable cash across all accounts | Σ over accounts of `balance * to_sgd`, assets positive, **liabilities negative** |
| **Total expenses (MTD)** | Spend so far this month | Σ of `expense` transactions × `to_sgd`, month-to-date |
| **Spend by category** | Per-category SGD-equivalent totals | grouped expenses; non-SGD shown with native breakdown |
| **Days elapsed / remaining** | Position within the month | `today.day` / `days_in_month − day` |
| **Average daily burn** | Discretionary spend rate | `(discretionary expenses MTD) / days_elapsed` |
| **Projected month** | Estimated month-end spend | `total_expenses + avg_daily_burn × days_remaining` |

Note **burn uses only discretionary categories** — fixed costs are excluded so
the rate reflects controllable spending. "Liquid cash" was formerly labelled
"Net worth"; it's the headline number on the dashboard.

## 5. API surface

Base URL: the running app (e.g. `http://app-host:8000`). All `/api/*` routes
require auth — for an agent, send the API key as either header:

```
Authorization: Bearer <API_KEY>
X-API-Key: <API_KEY>
```

(The browser uses a login cookie instead; same endpoints.) FastAPI also serves
auto-generated docs at `/docs` and the schema at `/openapi.json`.

### Read

| Endpoint | Returns |
|---|---|
| `GET /api/dashboard` | All §4 metrics + latest balances + spend-by-category (month-to-date) |
| `GET /api/liquid-cash` | Combined SGD liquid cash as of any date (`?date=YYYY-MM-DD`; omit for today). Carry-forward, current FX. |
| `GET /api/reference` | **Live** `accounts`, `categories`, `currencies` — read before writing |
| `GET /api/fx` | FX `rates` (`to_sgd`) + supported `currencies` |
| `GET /api/transactions` | Filtered transaction list for **any date range** + `expense_total_sgd`. Use for per-day / per-week spend (dashboard is month-to-date only) |

(Write endpoints below also include `DELETE /api/txn/{txn_id}` to undo a log.)

**`GET /api/transactions`** — query params (all optional): `date_from`, `date_to`
(`YYYY-MM-DD`; pass the same date in both for a single day), `account`, `category`,
`flow`, `q` (note/id substring), `limit` (default 100, max 500).
```
GET /api/transactions?date_from=2026-06-05&date_to=2026-06-05&flow=expense
→ { "count": 3, "returned": 3, "expense_total_sgd": 41.40,
    "transactions": [ { "txn_id": "...", "txn_date": "2026-06-05", "account_id": "DBS",
                        "category": "Food", "amount": 12.50, "currency": "SGD",
                        "amount_sgd": 12.50, "note": "lunch", ... } ] }
```
`expense_total_sgd` sums **all** matching expenses (not just the returned page);
each row carries `amount_sgd`. "Today" = `date_from == date_to == dashboard.today`.

### Write

**`POST /api/txn`** — record one transaction. The latest balance snapshot is
automatically adjusted for the affected account.
```json
{ "account_id": "DBS", "category": "Food", "amount": 12.50,
  "currency": "SGD", "flow": "expense", "note": "lunch" }
→ { "ok": true, "txn_id": "T20260605193000" }
```
`currency` defaults to the account's currency. `account_id` and `category` must
exist (validated server-side → 400 otherwise). `flow` defaults to `expense`;
use `income` for salary, refunds, cashback, etc. — balance adjusts accordingly.

**`DELETE /api/txn/{txn_id}`** — delete one transaction by id. Reverses the
balance adjustment automatically. Use to **undo a mistaken log** (wrong account,
duplicate) rather than leaving a double entry.
```
DELETE /api/txn/T20260607161528
→ { "ok": true, "deleted": "T20260607161528" }   (404 if it doesn't exist)
```

**`POST /api/balance`** — upsert today's snapshot for one or more accounts.
```json
{ "rows": [ { "account_id": "DBS", "balance": 4200.00, "currency": "SGD" } ] }
→ { "ok": true, "rows": 1 }
```

**`POST /api/parse`** — NL text → structured fields, **does not write**. Never
errors (regex fallback if the LLM is down). Detects expense vs income.
```json
{ "text": "grab to airport 24" }
→ { "amount": 24.0, "currency": "SGD", "category": "Transport",
    "account": "CASH_SGD", "flow": "expense", "note": "grab to airport 24" }

{ "text": "received salary 5000" }
→ { "amount": 5000.0, "currency": "SGD", "category": "Other",
    "account": "CASH_SGD", "flow": "income", "note": "received salary 5000" }
```
Note the key is `account` (an `account_id`); pass it as `account_id` to `/api/txn`.

**`POST /api/jobs/{name}`** — ops trigger for `recurring_poster` /
`monthly_rollover` / `weekly_digest` / `fx_update`. Rarely needed by an agent.

## 6. Worked flows

**Log a spend from chat:** `POST /api/parse {text}` → review fields → `POST
/api/txn` with `account_id = parsed.account`. (The MCP `log_from_text` tool does
both; `confirm=false` previews without writing.)

**Answer "how am I doing this month?":** `GET /api/dashboard` → report
`total_expenses_sgd`, `avg_daily_burn`, `projected_month`, and the top
`spend_by_category` entries. All already SGD-equivalent.

**Compare liquid cash month-on-month:** `GET /api/liquid-cash` (today) and
`GET /api/liquid-cash?date=YYYY-MM-DD` (same day last month) → subtract
`combined_sgd` values. Same carry-forward + FX convention as the dashboard.

**Record account balances:** ask the user for current balances → `POST
/api/balance {rows:[…]}`. One snapshot per account per day; re-sending updates.

## 7. Agent guidance / guardrails

- **Validate against live reference.** Don't assume an `account_id` or `category`
  exists — pull `GET /api/reference` and match. Unknown values return HTTP 400.
- **Writes are real.** `/api/txn` and `/api/balance` mutate the user's ledger.
  Prefer parse-then-confirm for ambiguous input rather than logging blind.
- **Currency:** if the user names a currency, pass it explicitly; otherwise let
  it default to the account's currency. All reporting is SGD-equivalent.
- **Don't double-count recurring.** Fixed monthly charges (rent, subscriptions)
are auto-posted by the recurring job unless `external_pipeline` is true. The
latter are logged by the email expense pipeline and must not also be logged
manually.
- **Idempotency:** there's no de-dupe on manual `/api/txn`; re-sending creates a
  second transaction. Track what you've already logged in a session.
- **Undo mistakes, don't leave them.** If you log to the wrong account, wrong
  amount, or create a duplicate, call `DELETE /api/txn/{txn_id}` — it removes the
  row and reverses the balance adjustment automatically. Don't log a correction on
  top of a mistake. The `txn_id` comes back from every `/api/txn` write.
- This is a **single-user, tailnet-private** app. The API key is the only thing
  gating programmatic access — treat it as a secret.
