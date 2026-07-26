"""All SQL lives here. Every statement is parameterized (psycopg placeholders)."""
from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal

from . import db
from .db import SGT

TWO = Decimal("0.01")


def _today() -> date:
    return datetime.now(SGT).date()


def d2(value) -> Decimal:
    """Quantize to 2dp."""
    return Decimal(str(value or 0)).quantize(TWO)


# ── reference / validation ──────────────────────────────────────────────────

def accounts(active_only: bool = False) -> list[dict]:
    sql = """
        SELECT account_id, name, currency, type, active, sort_order
        FROM accounts
        {where}
        ORDER BY sort_order, account_id
    """.format(where="WHERE active" if active_only else "")
    return db.query(sql)


def categories() -> list[dict]:
    return db.query(
        "SELECT name, is_discretionary, sort_order FROM categories ORDER BY sort_order, name"
    )


def account_map() -> dict[str, dict]:
    return {a["account_id"]: a for a in accounts()}


def category_names() -> set[str]:
    return {c["name"] for c in categories()}


# ── account management (CRUD) ───────────────────────────────────────────────

def get_account(account_id: str) -> dict | None:
    return db.query_one(
        "SELECT account_id, name, currency, type, active, sort_order "
        "FROM accounts WHERE account_id = %(id)s",
        {"id": account_id},
    )


def next_account_sort() -> int:
    row = db.query_one("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM accounts")
    return int(row["n"]) if row else 1


def create_account(data: dict) -> None:
    db.execute(
        """
        INSERT INTO accounts (account_id, name, currency, type, active, sort_order)
        VALUES (%(account_id)s, %(name)s, %(currency)s, %(type)s, %(active)s, %(sort_order)s)
        """,
        data,
    )


def update_account(account_id: str, data: dict) -> int:
    return db.execute(
        """
        UPDATE accounts SET
            name = %(name)s, currency = %(currency)s, type = %(type)s,
            active = %(active)s, sort_order = %(sort_order)s
        WHERE account_id = %(account_id)s
        """,
        {"account_id": account_id, **data},
    )


def toggle_account(account_id: str) -> int:
    return db.execute(
        "UPDATE accounts SET active = NOT active WHERE account_id = %(id)s",
        {"id": account_id},
    )


def account_reference_counts(account_id: str) -> dict:
    return db.query_one(
        """
        SELECT
            (SELECT count(*) FROM transactions WHERE account_id = %(id)s) AS txns,
            (SELECT count(*) FROM balances     WHERE account_id = %(id)s) AS bals,
            (SELECT count(*) FROM recurring    WHERE account_id = %(id)s) AS recs
        """,
        {"id": account_id},
    )


def delete_account(account_id: str) -> int:
    return db.execute(
        "DELETE FROM accounts WHERE account_id = %(id)s", {"id": account_id}
    )


def fx_rate() -> Decimal:
    """MYR→SGD rate (used for the hero label); live value if present."""
    rates = get_fx_rates()
    if "MYR" in rates:
        return rates["MYR"].quantize(Decimal("0.0001"))
    row = db.query_one("SELECT myr_to_sgd FROM app_config WHERE id = 1")
    return d2(row["myr_to_sgd"]) if row else Decimal("0.30")


# ── FX rates (multi-currency, stored as to_sgd) ─────────────────────────────

def ensure_fx_schema() -> None:
    """Create + seed the fx_rates table (additive; keeps schema.sql untouched)."""
    from . import fx

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS fx_rates (
            currency   text PRIMARY KEY,
            to_sgd     numeric(14,6) NOT NULL,
            source     text NOT NULL DEFAULT 'fallback',
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    for code, rate in fx.FALLBACK_RATES.items():
        db.execute(
            """
            INSERT INTO fx_rates (currency, to_sgd, source)
            VALUES (%(c)s, %(r)s, 'fallback')
            ON CONFLICT (currency) DO NOTHING
            """,
            {"c": code, "r": rate},
        )


def get_fx_rates() -> dict[str, Decimal]:
    """All to_sgd rates as a dict. Falls back to static rates if table is absent."""
    try:
        rows = db.query("SELECT currency, to_sgd FROM fx_rates")
    except Exception:
        from . import fx
        return dict(fx.FALLBACK_RATES)
    rates = {r["currency"]: Decimal(str(r["to_sgd"])) for r in rows}
    rates.setdefault("SGD", Decimal("1"))
    return rates


def fx_rates_detail() -> list[dict]:
    return db.query(
        "SELECT currency, to_sgd, source, updated_at FROM fx_rates ORDER BY currency"
    )


def upsert_fx_rates(rates: dict[str, Decimal], source: str) -> int:
    n = 0
    for code, rate in rates.items():
        n += db.execute(
            """
            INSERT INTO fx_rates (currency, to_sgd, source, updated_at)
            VALUES (%(c)s, %(r)s, %(s)s, now())
            ON CONFLICT (currency)
            DO UPDATE SET to_sgd = EXCLUDED.to_sgd,
                          source = EXCLUDED.source,
                          updated_at = now()
            """,
            {"c": code, "r": rate, "s": source},
        )
    return n


def to_sgd(amount, currency: str, rates: dict[str, Decimal] | None = None) -> Decimal:
    """Convert an amount in ``currency`` to SGD using stored rates."""
    rates = rates if rates is not None else get_fx_rates()
    rate = rates.get(currency, Decimal("1"))
    return (Decimal(str(amount or 0)) * rate).quantize(TWO)


# ── balances (stocks) ───────────────────────────────────────────────────────

def latest_balances() -> list[dict]:
    """Latest balance per account joined with account meta (sorted for UI)."""
    return db.query(
        """
        SELECT a.account_id, a.name, a.currency, a.type, a.active, a.sort_order,
               lb.balance, lb.snap_date
        FROM accounts a
        LEFT JOIN v_latest_balance lb USING (account_id)
        ORDER BY a.sort_order, a.account_id
        """
    )


def latest_balance_map() -> dict[str, Decimal]:
    return {
        r["account_id"]: d2(r["balance"])
        for r in db.query("SELECT account_id, balance FROM v_latest_balance")
    }


def net_cash() -> dict[str, Decimal]:
    rows = db.query("SELECT currency, net FROM v_net_cash")
    return {r["currency"]: d2(r["net"]) for r in rows}


def upsert_balance(snap_date: date, account_id: str, balance, currency: str, note: str = "", source: str = "form") -> None:
    db.execute(
        """
        INSERT INTO balances (snap_date, account_id, balance, currency, source, note)
        VALUES (%(snap_date)s, %(account_id)s, %(balance)s, %(currency)s, %(source)s, %(note)s)
        ON CONFLICT (snap_date, account_id)
        DO UPDATE SET balance  = EXCLUDED.balance,
                      currency = EXCLUDED.currency,
                      source   = EXCLUDED.source,
                      note     = EXCLUDED.note
        """,
        {
            "snap_date": snap_date,
            "account_id": account_id,
            "balance": balance,
            "currency": currency,
            "source": source,
            "note": note,
        },
    )


# ── transactions (flows) ────────────────────────────────────────────────────

def _get_account_type(account_id: str) -> str | None:
    row = db.query_one(
        "SELECT type FROM accounts WHERE account_id = %(id)s", {"id": account_id}
    )
    return row["type"] if row else None


def _txn_balance_delta(flow: str, account_type: str, amount: Decimal) -> Decimal:
    """How much a transaction changes the account balance (signed)."""
    if account_type == "asset":
        # expense/transfer: money leaves  → balance down
        # income:           money arrives → balance up
        return amount if flow == "income" else -amount
    else:  # liability (credit card)
        # expense:          spend on card → debt up
        # income/transfer:  payment       → debt down
        return amount if flow == "expense" else -amount


def _reflect_balance(account_id: str, flow: str, amount, currency: str, sign: int) -> None:
    """Adjust the latest balance for ``account_id`` by the effect of a
    transaction (``sign`` +1 for insert, -1 for delete). Always computes
    from the most recent snapshot, whether manual or auto.
    Converts ``amount`` from ``currency`` to the account's currency if they differ."""
    account_type = _get_account_type(account_id)
    if account_type is None:
        return

    latest = db.query_one(
        "SELECT balance FROM v_latest_balance WHERE account_id = %(id)s",
        {"id": account_id},
    )
    if latest is None:
        return  # no baseline to adjust from

    acct = db.query_one(
        "SELECT currency FROM accounts WHERE account_id = %(id)s", {"id": account_id}
    )
    if acct is None:
        return

    # Convert to account currency if txn currency differs
    acct_currency = acct["currency"]
    if currency != acct_currency:
        rates = get_fx_rates()
        to_sgd = rates.get(currency)
        if to_sgd is None:
            return  # unknown currency — can't convert
        amount_sgd = d2(amount) * to_sgd
        if acct_currency == "SGD":
            amount = amount_sgd
        else:
            # Cross-rate: via SGD
            to_acct = rates.get(acct_currency)
            if to_acct is None:
                return
            amount = amount_sgd / to_acct

    delta = _txn_balance_delta(flow, account_type, d2(amount))
    new_balance = (d2(latest["balance"]) + delta * sign).quantize(TWO)

    upsert_balance(
        _today(), account_id, new_balance, acct_currency,
        note="auto: transaction", source="auto",
    )


def insert_transaction(
    txn_id: str,
    txn_date: date,
    account_id: str,
    flow: str,
    category: str,
    amount,
    currency: str,
    source: str,
    note: str,
    on_conflict_nothing: bool = False,
) -> int:
    """Append a flow row and reflect it in the latest balance snapshot.
    Returns rowcount (0 if a conflict was ignored)."""
    conflict = "ON CONFLICT DO NOTHING" if on_conflict_nothing else ""
    rowcount = db.execute(
        f"""
        INSERT INTO transactions
            (txn_id, txn_date, account_id, flow, category, amount, currency, source, note)
        VALUES
            (%(txn_id)s, %(txn_date)s, %(account_id)s, %(flow)s, %(category)s,
             %(amount)s, %(currency)s, %(source)s, %(note)s)
        {conflict}
        """,
        {
            "txn_id": txn_id,
            "txn_date": txn_date,
            "account_id": account_id,
            "flow": flow,
            "category": category,
            "amount": amount,
            "currency": currency,
            "source": source,
            "note": note,
        },
    )
    if rowcount:
        _reflect_balance(account_id, flow, amount, currency, sign=1)
    return rowcount


def get_transaction(txn_id: str) -> dict | None:
    """A single transaction by id (with the account name), or None."""
    return db.query_one(
        """
        SELECT t.txn_id, t.txn_date, t.account_id, a.name AS account_name,
               t.flow, t.category, t.amount, t.currency, t.source, t.note
        FROM transactions t
        JOIN accounts a USING (account_id)
        WHERE t.txn_id = %(id)s
        """,
        {"id": txn_id},
    )


def delete_transaction(txn_id: str) -> int:
    """Hard-delete a transaction and reverse its effect on the latest balance
    snapshot. Returns rowcount (0 if not found)."""
    txn = db.query_one(
        "SELECT account_id, flow, amount, currency FROM transactions WHERE txn_id = %(id)s",
        {"id": txn_id},
    )
    if txn is None:
        return 0
    rowcount = db.execute(
        "DELETE FROM transactions WHERE txn_id = %(id)s", {"id": txn_id}
    )
    if rowcount:
        _reflect_balance(txn["account_id"], txn["flow"], txn["amount"], txn["currency"], sign=-1)
    return rowcount


# ── dashboard metrics ───────────────────────────────────────────────────────

EVERY_N_DAYS = 30  # cadence for every30 recurring frequency


def _month_end_day(day_of_month: int, year: int, month: int) -> int:
    """Clamp a day to the last day of short months (e.g. 31 → 30/28)."""
    return min(day_of_month, calendar.monthrange(year, month)[1])


def _recurring_is_due(r: dict, target: date) -> bool:
    """Whether a recurring template should post on ``target`` (mirrors jobs.is_due)."""
    freq = r.get("frequency") or "monthly"
    if freq == "every30":
        anchor = r.get("start_date")
        if not anchor:
            return False
        diff = (target - anchor).days
        return diff > 0 and diff % EVERY_N_DAYS == 0
    if freq == "yearly" and target.month != r.get("month_of_year"):
        return False
    return target.day == _month_end_day(r["day_of_month"], target.year, target.month)


def upcoming_recurring_sgd(today: date, month_end: date,
                           rates: dict[str, Decimal]) -> Decimal:
    """Sum of recurring expenses (SGD-equivalent) due between today+1 and
    month_end inclusive. Walks each day and checks every active recurring
    whose window overlaps the future period."""
    from datetime import timedelta

    rows = db.query(
        """
        SELECT recur_id, account_id, flow, category, amount, currency,
               day_of_month, frequency, month_of_year, start_date, end_date
        FROM recurring
        WHERE active
          AND flow = 'expense'
          AND (end_date   IS NULL OR end_date   >  %(today)s)
          AND (start_date IS NULL OR start_date <= %(month_end)s)
        ORDER BY recur_id
        """,
        {"today": today, "month_end": month_end},
    )

    total = Decimal("0.00")
    d = today + timedelta(days=1)
    while d <= month_end:
        for r in rows:
            if _recurring_is_due(r, d):
                total += (d2(r["amount"]) *
                          rates.get(r["currency"], Decimal("1")))
        d += timedelta(days=1)

    return total.quantize(TWO)


def category_spend_rows(month_start: date, month_end: date) -> list[dict]:
    """Expense totals per (category, currency) for the month."""
    return db.query(
        """
        SELECT t.category, c.is_discretionary, c.sort_order, t.currency,
               COALESCE(SUM(t.amount), 0) AS amount
        FROM transactions t
        JOIN categories c ON c.name = t.category
        WHERE t.flow = 'expense'
          AND t.txn_date >= %(start)s
          AND t.txn_date <= %(end)s
        GROUP BY t.category, c.is_discretionary, c.sort_order, t.currency
        ORDER BY c.sort_order, t.category
        """,
        {"start": month_start, "end": month_end},
    )


def daily_spend(month_start: date, month_end: date,
                discretionary_only: bool = False) -> dict[date, Decimal]:
    """SGD-equivalent expense total per day (all currencies, via fx_rates).
    When ``discretionary_only``, only categories with is_discretionary=true
    are counted — excludes mortgage, family transfers, bills etc."""
    disc_join = (
        "JOIN categories c ON c.name = t.category AND c.is_discretionary"
        if discretionary_only else ""
    )
    rows = db.query(
        f"""
        SELECT t.txn_date,
               COALESCE(SUM(t.amount * COALESCE(f.to_sgd, 1)), 0) AS sgd
        FROM transactions t
        {disc_join}
        LEFT JOIN fx_rates f ON f.currency = t.currency
        WHERE t.flow = 'expense'
          AND t.txn_date >= %(start)s AND t.txn_date <= %(end)s
        GROUP BY t.txn_date
        ORDER BY t.txn_date
        """,
        {"start": month_start, "end": month_end},
    )
    return {r["txn_date"]: d2(r["sgd"]) for r in rows}


def dashboard(today: date) -> dict:
    """Compute every dashboard metric for the month containing ``today``."""
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_start = today.replace(day=1)
    month_end = today.replace(day=days_in_month)
    days_elapsed = today.day
    days_remaining = days_in_month - days_elapsed

    rates = get_fx_rates()
    net = net_cash()
    net_sgd = net.get("SGD", Decimal("0.00"))
    net_myr = net.get("MYR", Decimal("0.00"))
    # Combined liquid cash = sum over every currency present, valued in SGD.
    combined = sum(
        (d2(bal) * rates.get(cur, Decimal("1")) for cur, bal in net.items()),
        Decimal("0.00"),
    ).quantize(TWO)

    # Aggregate category spend to SGD-equivalent (so non-SGD spend is counted).
    rows = category_spend_rows(month_start, month_end)
    by_cat: dict[str, dict] = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {
            "category": r["category"],
            "is_discretionary": r["is_discretionary"],
            "sgd_eq": Decimal("0.00"),
            "native": [],
        })
        amt = d2(r["amount"])
        c["sgd_eq"] += (amt * rates.get(r["currency"], Decimal("1"))).quantize(TWO)
        if amt:
            c["native"].append({"currency": r["currency"], "amount": amt})
    cats = list(by_cat.values())
    # Exclude non-spend categories from the dashboard (transfers, CC payments — they
    # move money between accounts rather than consuming it).
    _non_spend_categories = {"Transfer", "Credit Card Payment"}
    cats = [c for c in cats if c["category"] not in _non_spend_categories]
    for c in cats:
        c["sgd_eq"] = c["sgd_eq"].quantize(TWO)
        c["multi_currency"] = any(n["currency"] != "SGD" for n in c["native"])

    total_sgd = sum((c["sgd_eq"] for c in cats), Decimal("0.00")).quantize(TWO)
    has_nonsgd = any(c["multi_currency"] for c in cats)

    # Burn = discretionary categories only (SGD-equivalent), over days elapsed.
    disc_sgd = sum(
        (c["sgd_eq"] for c in cats if c["is_discretionary"]), Decimal("0.00")
    )
    avg_daily_burn = (
        (disc_sgd / days_elapsed).quantize(TWO) if days_elapsed else Decimal("0.00")
    )
    upcoming = upcoming_recurring_sgd(today, month_end, rates)
    burn_projection = (avg_daily_burn * days_remaining).quantize(TWO)
    projected_month = (total_sgd + upcoming + burn_projection).quantize(TWO)

    return {
        "today": today.isoformat(),
        "fx_rate": fx_rate(),
        "net_cash": {"SGD": net_sgd, "MYR": net_myr, "combined_sgd": combined},
        "latest_balances": latest_balances(),
        "spend_by_category": cats,
        "has_nonsgd_expenses": has_nonsgd,
        "total_expenses_sgd": total_sgd,
        "projected_upcoming": upcoming,
        "projected_burn": burn_projection,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "days_remaining": days_remaining,
        "avg_daily_burn": avg_daily_burn,
        "projected_month": projected_month,
    }


# ── history (browse) ────────────────────────────────────────────────────────

def recent_transactions(limit: int = 200) -> list[dict]:
    """Most recent flows, newest first, with account display name."""
    return db.query(
        """
        SELECT t.txn_id, t.txn_date, t.account_id, a.name AS account_name,
               t.flow, t.category, t.amount, t.currency, t.source, t.note,
               t.created_at
        FROM transactions t
        JOIN accounts a USING (account_id)
        ORDER BY t.txn_date DESC, t.id DESC
        LIMIT %(limit)s
        """,
        {"limit": limit},
    )


def transaction_count() -> int:
    row = db.query_one("SELECT count(*) AS c FROM transactions")
    return int(row["c"]) if row else 0


def _txn_where(f: dict) -> tuple[str, dict]:
    """Build a parameterized WHERE clause from a filter dict. Only fixed column
    fragments are interpolated; every user value flows through bound params."""
    conds: list[str] = []
    params: dict = {}
    if f.get("account"):
        conds.append("t.account_id = %(account)s")
        params["account"] = f["account"]
    if f.get("category"):
        conds.append("t.category = %(category)s")
        params["category"] = f["category"]
    if f.get("flow"):
        conds.append("t.flow = %(flow)s")
        params["flow"] = f["flow"]
    if f.get("date_from"):
        conds.append("t.txn_date >= %(date_from)s")
        params["date_from"] = f["date_from"]
    if f.get("date_to"):
        conds.append("t.txn_date <= %(date_to)s")
        params["date_to"] = f["date_to"]
    if f.get("q"):
        conds.append("(t.note ILIKE %(q)s OR t.txn_id ILIKE %(q)s)")
        params["q"] = f"%{f['q']}%"
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def filtered_transactions(f: dict, limit: int, offset: int) -> list[dict]:
    where, params = _txn_where(f)
    params["limit"] = limit
    params["offset"] = offset
    return db.query(
        f"""
        SELECT t.txn_id, t.txn_date, t.account_id, a.name AS account_name,
               t.flow, t.category, t.amount, t.currency, t.source, t.note
        FROM transactions t
        JOIN accounts a USING (account_id)
        {where}
        ORDER BY t.txn_date DESC, t.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    )


def count_filtered_transactions(f: dict) -> int:
    where, params = _txn_where(f)
    row = db.query_one(
        f"SELECT count(*) AS c FROM transactions t {where}", params
    )
    return int(row["c"]) if row else 0


def filtered_expense_total_sgd(f: dict) -> Decimal:
    """SGD-equivalent sum of the *expense* rows matching the filter — over the
    whole match, not just one page. Other flows (income/transfer) are excluded
    so the figure is comparable to the dashboard's spend totals."""
    where, params = _txn_where(f)
    clause = (where + " AND t.flow = 'expense'") if where else "WHERE t.flow = 'expense'"
    row = db.query_one(
        f"""
        SELECT COALESCE(SUM(t.amount * COALESCE(fx.to_sgd, 1)), 0) AS total
        FROM transactions t
        LEFT JOIN fx_rates fx ON fx.currency = t.currency
        {clause}
        """,
        params,
    )
    return d2(row["total"]) if row else Decimal("0.00")


def balance_history() -> list[dict]:
    """Liquid cash as of each snapshot date (carry-forward latest balance per
    account on or before that date), so a day with a partial capture still
    reflects the true standing rather than a smaller same-day sum."""
    return db.query(
        """
        WITH dates AS (SELECT DISTINCT snap_date AS d FROM balances),
        asof AS (
          SELECT d.d AS snap_date, a.type, a.currency,
                 (SELECT b.balance FROM balances b
                  WHERE b.account_id = a.account_id AND b.snap_date <= d.d
                  ORDER BY b.snap_date DESC, b.id DESC
                  LIMIT 1) AS balance
          FROM dates d CROSS JOIN accounts a
        )
        SELECT snap_date,
               COALESCE(SUM(CASE WHEN currency = 'SGD'
                    THEN CASE WHEN type = 'asset' THEN balance ELSE -balance END END), 0) AS net_sgd,
               COALESCE(SUM(CASE WHEN currency = 'MYR'
                    THEN CASE WHEN type = 'asset' THEN balance ELSE -balance END END), 0) AS net_myr
        FROM asof
        WHERE balance IS NOT NULL
        GROUP BY snap_date
        ORDER BY snap_date DESC
        """
    )


def balance_history_window(days: int) -> list[dict]:
    """Last *days* of liquid-cash snapshots (carry-forward per account).
    Returns rows in ascending date order (oldest first) for charting."""
    return db.query(
        """
        WITH dates AS (
            SELECT DISTINCT snap_date AS d FROM balances
            WHERE snap_date >= (SELECT MAX(snap_date) FROM balances) - %(days)s::int + 1
        ),
        asof AS (
          SELECT d.d AS snap_date, a.type, a.currency,
                 (SELECT b.balance FROM balances b
                  WHERE b.account_id = a.account_id AND b.snap_date <= d.d
                  ORDER BY b.snap_date DESC, b.id DESC
                  LIMIT 1) AS balance
          FROM dates d CROSS JOIN accounts a
        )
        SELECT snap_date,
               COALESCE(SUM(CASE WHEN currency = 'SGD'
                    THEN CASE WHEN type = 'asset' THEN balance ELSE -balance END END), 0) AS net_sgd,
               COALESCE(SUM(CASE WHEN currency = 'MYR'
                    THEN CASE WHEN type = 'asset' THEN balance ELSE -balance END END), 0) AS net_myr
        FROM asof
        WHERE balance IS NOT NULL
        GROUP BY snap_date
        ORDER BY snap_date ASC
        """,
        {"days": days},
    )


def balance_history_calendar_month(year: int, month: int) -> list[dict]:
    """Daily combined SGD liquid cash for a calendar month (every day, carry-forward).

    Uses generate_series to produce a row for every calendar day. The carry-forward
    subquery pulls the most recent balance snapshot per account on or before each day.
    Days where no account has any snapshot yet are excluded (HAVING).
    """
    import calendar as cal_mod
    from datetime import timedelta

    first = date(year, month, 1)
    last_day_num = cal_mod.monthrange(year, month)[1]
    last = date(year, month, last_day_num)

    # Extend start back 31 days so day 1 can carry-forward from prior month
    ctx_start = first - timedelta(days=31)

    return db.query(
        """
        WITH day_balances AS (
            SELECT d::date AS d, a.account_id, a.type, a.currency,
                   (SELECT b.balance FROM balances b
                    WHERE b.account_id = a.account_id AND b.snap_date <= d::date
                    ORDER BY b.snap_date DESC, b.id DESC
                    LIMIT 1) AS balance
            FROM generate_series(%(ctx_start)s::date, %(end)s::date, '1 day'::interval) AS d
            CROSS JOIN accounts a
            WHERE a.active
        )
        SELECT d AS snap_date,
               COALESCE(SUM(CASE WHEN currency = 'SGD' AND balance IS NOT NULL
                    THEN CASE WHEN type = 'asset' THEN balance ELSE -balance END END), 0) AS net_sgd,
               COALESCE(SUM(CASE WHEN currency = 'MYR' AND balance IS NOT NULL
                    THEN CASE WHEN type = 'asset' THEN balance ELSE -balance END END), 0) AS net_myr
        FROM day_balances
        GROUP BY d
        HAVING COUNT(balance) > 0
        ORDER BY d ASC
        """,
        {"ctx_start": ctx_start, "end": last},
    )


# ── recurring / rollover (jobs) ─────────────────────────────────────────────

def active_recurring(today: date) -> list[dict]:
    return db.query(
        """
        SELECT recur_id, name, account_id, flow, category, amount, currency,
               day_of_month, frequency, month_of_year, start_date, end_date
        FROM recurring
        WHERE active
          AND (start_date IS NULL OR start_date <= %(today)s)
          AND (end_date   IS NULL OR end_date   >= %(today)s)
        ORDER BY recur_id
        """,
        {"today": today},
    )


# ── recurring management (CRUD) ─────────────────────────────────────────────

def ensure_recurring_schema() -> None:
    """Additively extend `recurring` with frequency + month_of_year (keeps
    schema.sql untouched). Existing rows default to monthly."""
    db.execute(
        "ALTER TABLE recurring ADD COLUMN IF NOT EXISTS "
        "frequency text NOT NULL DEFAULT 'monthly'"
    )
    db.execute(
        "ALTER TABLE recurring ADD COLUMN IF NOT EXISTS month_of_year int"
    )


def list_recurring() -> list[dict]:
    return db.query(
        """
        SELECT recur_id, name, account_id, flow, category, amount, currency,
               day_of_month, frequency, month_of_year, active, start_date,
               end_date, note
        FROM recurring
        ORDER BY active DESC, frequency, recur_id
        """
    )


def get_recurring(recur_id: str) -> dict | None:
    return db.query_one(
        """
        SELECT recur_id, name, account_id, flow, category, amount, currency,
               day_of_month, frequency, month_of_year, active, start_date,
               end_date, note
        FROM recurring WHERE recur_id = %(id)s
        """,
        {"id": recur_id},
    )


def next_recur_id() -> str:
    rows = db.query("SELECT recur_id FROM recurring WHERE recur_id ~ '^R[0-9]+$'")
    nums = [int(r["recur_id"][1:]) for r in rows]
    return f"R{(max(nums) + 1) if nums else 1:02d}"


def create_recurring(data: dict) -> str:
    recur_id = next_recur_id()
    db.execute(
        """
        INSERT INTO recurring
            (recur_id, name, account_id, flow, category, amount, currency,
             day_of_month, frequency, month_of_year, active, start_date,
             end_date, note)
        VALUES
            (%(recur_id)s, %(name)s, %(account_id)s, %(flow)s, %(category)s,
             %(amount)s, %(currency)s, %(day_of_month)s, %(frequency)s,
             %(month_of_year)s, %(active)s, %(start_date)s, %(end_date)s, %(note)s)
        """,
        {"recur_id": recur_id, **data},
    )
    return recur_id


def update_recurring(recur_id: str, data: dict) -> int:
    return db.execute(
        """
        UPDATE recurring SET
            name = %(name)s, account_id = %(account_id)s, flow = %(flow)s,
            category = %(category)s, amount = %(amount)s, currency = %(currency)s,
            day_of_month = %(day_of_month)s, frequency = %(frequency)s,
            month_of_year = %(month_of_year)s, active = %(active)s,
            start_date = %(start_date)s, end_date = %(end_date)s, note = %(note)s
        WHERE recur_id = %(recur_id)s
        """,
        {"recur_id": recur_id, **data},
    )


def delete_recurring(recur_id: str) -> int:
    return db.execute(
        "DELETE FROM recurring WHERE recur_id = %(id)s", {"id": recur_id}
    )


def toggle_recurring(recur_id: str) -> int:
    return db.execute(
        "UPDATE recurring SET active = NOT active WHERE recur_id = %(id)s",
        {"id": recur_id},
    )


def latest_balance_rows() -> list[dict]:
    return db.query(
        """
        SELECT lb.account_id, lb.balance, lb.currency
        FROM v_latest_balance lb
        JOIN accounts a USING (account_id)
        ORDER BY lb.account_id
        """
    )


def rollover_balance(snap_date: date, account_id: str, balance, currency: str) -> int:
    return db.execute(
        """
        INSERT INTO balances (snap_date, account_id, balance, currency, source, note)
        VALUES (%(snap_date)s, %(account_id)s, %(balance)s, %(currency)s,
                'rollover', 'carry-forward opening')
        ON CONFLICT (snap_date, account_id) DO NOTHING
        """,
        {
            "snap_date": snap_date,
            "account_id": account_id,
            "balance": balance,
            "currency": currency,
        },
    )


def update_transaction(txn_id: str, *, category: str | None = None, note: str | None = None) -> int:
    """Update category and/or note of a transaction by txn_id.
    Returns rowcount (should be 1 on success, 0 if not found).
    """
    sets = []
    params = {"txn_id": txn_id}
    if category is not None:
        sets.append("category = %(category)s")
        params["category"] = category
    if note is not None:
        sets.append("note = %(note)s")
        params["note"] = note
    if not sets:
        return 0
    return db.execute(
        f"UPDATE transactions SET {', '.join(sets)} WHERE txn_id = %(txn_id)s",
        params,
    )
