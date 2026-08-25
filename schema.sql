-- Expenses tracker — Postgres schema
-- Stocks/flows model. snap_key becomes a real composite unique constraint.

CREATE TABLE IF NOT EXISTS accounts (
  account_id  text PRIMARY KEY,
  name        text NOT NULL,
  currency    text NOT NULL DEFAULT 'SGD',
  type        text NOT NULL CHECK (type IN ('asset','liability')),
  active      boolean NOT NULL DEFAULT true,
  sort_order  int NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
  name             text PRIMARY KEY,
  is_discretionary boolean NOT NULL DEFAULT false,
  sort_order       int NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recurring (
  recur_id     text PRIMARY KEY,
  name         text NOT NULL,
  account_id   text NOT NULL REFERENCES accounts(account_id),
  flow         text NOT NULL DEFAULT 'expense',
  category     text NOT NULL DEFAULT 'Other',
  amount       numeric(14,2) NOT NULL DEFAULT 0,
  currency     text NOT NULL DEFAULT 'SGD',
  day_of_month int NOT NULL,
  active       boolean NOT NULL DEFAULT true,
  start_date   date,
  end_date     date,
  note         text DEFAULT '',
  external_pipeline boolean NOT NULL DEFAULT false
);

-- FLOWS: categorized money movements (append-only ledger)
CREATE TABLE IF NOT EXISTS transactions (
  id          bigserial PRIMARY KEY,
  txn_id      text UNIQUE,
  txn_date    date NOT NULL,
  account_id  text NOT NULL REFERENCES accounts(account_id),
  flow        text NOT NULL DEFAULT 'expense',
  category    text NOT NULL DEFAULT 'Other',
  amount      numeric(14,2) NOT NULL DEFAULT 0,
  currency    text NOT NULL DEFAULT 'SGD',
  source      text NOT NULL DEFAULT 'manual',
  note        text DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Idempotency for the recurring poster: at most one posted row per recurring item per day
CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_recurring_day
  ON transactions (source, txn_date)
  WHERE source LIKE 'recurring:%';

CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions (txn_date);

-- STOCKS: account balance snapshots. One row per account per day (the old snap_key).
CREATE TABLE IF NOT EXISTS balances (
  id          bigserial PRIMARY KEY,
  snap_date   date NOT NULL,
  account_id  text NOT NULL REFERENCES accounts(account_id),
  balance     numeric(14,2) NOT NULL DEFAULT 0,
  currency    text NOT NULL DEFAULT 'SGD',
  source      text NOT NULL DEFAULT 'form',
  note        text DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (snap_date, account_id)
);

-- Latest balance per account (replaces the fragile "max date" sheet formula)
CREATE OR REPLACE VIEW v_latest_balance AS
SELECT DISTINCT ON (account_id)
       account_id, snap_date, balance, currency
FROM balances
ORDER BY account_id, snap_date DESC, id DESC;

-- Net cash by currency (assets minus liabilities, latest snapshot)
CREATE OR REPLACE VIEW v_net_cash AS
SELECT a.currency,
       SUM(CASE WHEN a.type = 'asset' THEN lb.balance ELSE -lb.balance END) AS net
FROM v_latest_balance lb
JOIN accounts a USING (account_id)
GROUP BY a.currency;

-- Config (single-row): FX rate, optional digest recipient
CREATE TABLE IF NOT EXISTS app_config (
  id          int PRIMARY KEY DEFAULT 1,
  myr_to_sgd  numeric(10,4) NOT NULL DEFAULT 0.30,
  CHECK (id = 1)
);
INSERT INTO app_config (id) VALUES (1) ON CONFLICT DO NOTHING;
