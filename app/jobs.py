"""Scheduled jobs (APScheduler, Asia/Singapore). All idempotent and read-safe.

Stocks/flows rule: the recurring poster writes ONLY to ``transactions``; the
rollover writes ONLY to ``balances`` (a stock carry-forward, not a flow).
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import date, datetime, timedelta

import httpx

from . import fx, queries
from .db import SGT

log = logging.getLogger("expenses.jobs")

EVERY_N_DAYS = 30  # cadence for the "every30" frequency (SaaS/Netflix-style subs)


def _today():
    return datetime.now(SGT).date()


def _month_end_day(day_of_month: int, year: int, month: int) -> int:
    """Clamp a chosen day to the last day of short months (e.g. 31 → 30/28)."""
    return min(day_of_month, calendar.monthrange(year, month)[1])


def is_due(r: dict, today: date) -> bool:
    """Whether a recurring template should post on ``today``.

    monthly  → every month on day_of_month (clamped to month-end)
    yearly   → once a year, in month_of_year on day_of_month
    every30  → every 30 days counted from start_date (the last subscription date)
    """
    freq = r.get("frequency") or "monthly"
    if freq == "every30":
        anchor = r.get("start_date")
        if not anchor:
            return False
        diff = (today - anchor).days
        return diff > 0 and diff % EVERY_N_DAYS == 0
    if freq == "yearly" and today.month != r.get("month_of_year"):
        return False
    return today.day == _month_end_day(r["day_of_month"], today.year, today.month)


def next_due(r: dict, today: date) -> date | None:
    """Estimated next renewal/post date on or after ``today`` (None if past end_date)."""
    freq = r.get("frequency") or "monthly"
    end = r.get("end_date")
    cand: date | None = None

    if freq == "every30":
        anchor = r.get("start_date")
        if not anchor:
            return None
        if today <= anchor:
            cand = anchor + timedelta(days=EVERY_N_DAYS)
        else:
            rem = (today - anchor).days % EVERY_N_DAYS
            cand = today if rem == 0 else today + timedelta(days=EVERY_N_DAYS - rem)
    else:
        # Walk forward month by month (≤12 steps) until the day matches.
        y, m = today.year, today.month
        for _ in range(13):
            if freq != "yearly" or m == r.get("month_of_year"):
                d = _month_end_day(r["day_of_month"], y, m)
                c = date(y, m, d)
                if c >= today:
                    cand = c
                    break
            m += 1
            if m > 12:
                m, y = 1, y + 1

    start = r.get("start_date")
    if cand and start and cand < start:
        cand = start
    if cand and end and cand > end:
        return None
    return cand


# ── recurring poster (daily 05:00) ──────────────────────────────────────────

def recurring_poster() -> dict:
    """Post Tally-managed recurring templates due today.

    ``external_pipeline`` rows are deliberately absent from
    ``queries.active_recurring``. They remain available to dashboard projection
    queries, while the email expense pipeline owns the actual ledger entry.
    """
    today = _today()
    posted = 0
    skipped = 0

    for r in queries.active_recurring(today):
        # Keep this guard even though the query filters these rows. It makes the
        # ownership rule explicit and protects against stale callers/mocks.
        if r.get("external_pipeline"):
            continue
        if not is_due(r, today):
            continue
        txn_id = f"T{today.strftime('%Y%m%d')}-{r['recur_id']}"
        rows = queries.insert_transaction(
            txn_id=txn_id,
            txn_date=today,
            account_id=r["account_id"],
            flow=r["flow"],
            category=r["category"],
            amount=r["amount"],
            currency=r["currency"],
            source=f"recurring:{r['recur_id']}",
            note="auto",
            on_conflict_nothing=True,
        )
        if rows:
            posted += 1
            _notify_recurring(r)
        else:
            skipped += 1

    log.info("recurring_poster %s: posted=%d skipped=%d", today, posted, skipped)
    return {"date": today.isoformat(), "posted": posted, "skipped": skipped}


def _notify_recurring(r: dict) -> None:
    """Send a Discord webhook notification for a posted recurring expense.

    Reads ``DISCORD_RECURRING_WEBHOOK`` from the environment.  Silent no-op
    when unset — the poster works the same with or without notifications.
    Failures are logged but never propagate (posting already succeeded).
    """
    webhook_url = os.environ.get("DISCORD_RECURRING_WEBHOOK")
    if not webhook_url:
        return
    try:
        resp = httpx.post(
            webhook_url,
            json={
                "content": (
                    f"📅 Recurring: ${r['amount']:,.2f} {r['name']}"
                    f" → {r['category']} ({r['account_id']})"
                ),
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — webhook is best-effort
        log.warning("recurring notify failed for %s: %s", r["recur_id"], exc)


# ── monthly rollover (1st 00:05) ────────────────────────────────────────────

def monthly_rollover() -> dict:
    """Carry each account's latest balance forward to a 1st-of-month snapshot."""
    today = _today()
    first = today.replace(day=1)
    inserted = 0

    for row in queries.latest_balance_rows():
        rows = queries.rollover_balance(
            snap_date=first,
            account_id=row["account_id"],
            balance=row["balance"],
            currency=row["currency"],
        )
        inserted += rows

    log.info("monthly_rollover %s: inserted=%d", first, inserted)
    return {"date": first.isoformat(), "inserted": inserted}


# ── FX rate refresh (daily 00:20 + on boot) ─────────────────────────────────

def fx_update() -> dict:
    """Refresh stored FX rates from the web source. Keeps last rates on failure."""
    try:
        rates = fx.fetch_rates()
    except Exception as exc:  # noqa: BLE001 — never propagate; keep last/fallback
        log.warning("fx_update: fetch failed (%s); keeping stored rates", exc)
        return {"updated": False, "reason": str(exc)}
    n = queries.upsert_fx_rates(rates, source="open.er-api.com")
    log.info("fx_update: refreshed %d rates", n)
    return {"updated": True, "count": n}


# ── weekly digest (Mon 07:00) ───────────────────────────────────────────────

def _digest_html(m: dict) -> str:
    nc = m["net_cash"]
    rows = "".join(
        f"<tr><td>{c['category']}</td>"
        f"<td style='text-align:right'>{queries.d2(c['sgd'])}</td></tr>"
        for c in m["spend_by_category"]
    )
    return f"""
    <div style="font-family:system-ui,sans-serif">
      <h2>Expenses — week of {m['today']}</h2>
      <p><b>Net cash</b>: SGD {nc['SGD']} · MYR {nc['MYR']} · Combined SGD {nc['combined_sgd']}</p>
      <p><b>Total expenses MTD (SGD)</b>: {m['total_expenses_sgd']}<br>
         <b>Days elapsed</b>: {m['days_elapsed']} / {m['days_in_month']}<br>
         <b>Avg daily burn</b>: {m['avg_daily_burn']}<br>
         <b>Projected month</b>: {m['projected_month']}</p>
      <table cellpadding="4" style="border-collapse:collapse">
        <tr><th align="left">Category</th><th align="right">SGD MTD</th></tr>
        {rows}
      </table>
    </div>
    """


def weekly_digest() -> dict:
    """Email the weekly summary via Resend. No-op if RESEND_API_KEY is unset."""
    today = _today()
    metrics = queries.dashboard(today)

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        log.info("weekly_digest %s: RESEND_API_KEY unset, no-op", today)
        return {"sent": False, "reason": "RESEND_API_KEY unset"}

    digest_from = os.environ.get("DIGEST_FROM", "")
    digest_to = os.environ.get("DIGEST_TO", "")
    if not digest_from or not digest_to:
        log.info("weekly_digest %s: DIGEST_FROM/TO unset, no-op", today)
        return {"sent": False, "reason": "DIGEST_FROM/TO unset"}

    body = {
        "from": digest_from,
        "to": [digest_to],
        "subject": f"Expenses weekly digest — {today.isoformat()}",
        "html": _digest_html(metrics),
    }
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        log.info("weekly_digest %s: sent to %s", today, digest_to)
        return {"sent": True, "to": digest_to}
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly_digest %s: send failed (%s)", today, exc)
        return {"sent": False, "reason": str(exc)}


# ── scheduler wiring ────────────────────────────────────────────────────────

def register_jobs(scheduler) -> None:
    from apscheduler.triggers.cron import CronTrigger

    tz = SGT
    scheduler.add_job(
        recurring_poster, CronTrigger(hour=5, minute=0, timezone=tz),
        id="recurring_poster", replace_existing=True,
    )
    scheduler.add_job(
        monthly_rollover, CronTrigger(day=1, hour=0, minute=5, timezone=tz),
        id="monthly_rollover", replace_existing=True,
    )
    scheduler.add_job(
        weekly_digest, CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=tz),
        id="weekly_digest", replace_existing=True,
    )
    scheduler.add_job(
        fx_update, CronTrigger(hour=0, minute=20, timezone=tz),
        id="fx_update", replace_existing=True,
    )
