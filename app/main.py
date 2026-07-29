"""FastAPI app: pages, JSON API, lifespan (connection pool + scheduler)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import calendar
import math
import os
import re
from urllib.parse import quote, urlencode

from starlette.middleware.gzip import GZipMiddleware

from . import auth, charts, db, fx, jobs, llm, queries
from .db import SGT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("expenses")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["money"] = lambda v: f"{Decimal(str(v or 0)):,.2f}"

# Branding (env-overridable so it's a one-line change).
APP_NAME = os.environ.get("APP_NAME", "Tally")
APP_TAGLINE = os.environ.get("APP_TAGLINE", "Your money, counted")
APP_VERSION = os.environ.get("APP_VERSION", "dev")
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["app_tagline"] = APP_TAGLINE
templates.env.globals["app_version"] = APP_VERSION

MONTHS = [(i, calendar.month_name[i]) for i in range(1, 13)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.open_pool()
    try:
        queries.ensure_fx_schema()  # additive: create + seed fx_rates if missing
        queries.ensure_recurring_schema()  # additive: frequency + month_of_year
    except Exception as exc:  # noqa: BLE001
        log.warning("schema ensure failed: %s", exc)
    scheduler = None
    try:
        from datetime import datetime as _dt

        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone=SGT)
        jobs.register_jobs(scheduler)
        # Refresh FX once shortly after boot (background thread; never blocks startup).
        scheduler.add_job(jobs.fx_update, "date", run_date=_dt.now(SGT), id="fx_boot")
        # Catch-up recurring poster at boot — if the app restarted after 00:10,
        # the day's trigger was lost. Idempotent via partial unique index.
        scheduler.add_job(jobs.recurring_poster, "date", run_date=_dt.now(SGT), id="recurring_boot")
        scheduler.start()
        app.state.scheduler = scheduler
        log.info("scheduler started with jobs: %s", [j.id for j in scheduler.get_jobs()])
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        db.close_pool()


app = FastAPI(title="Expenses Tracker", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Paths reachable without a session (login flow + static assets/CSS for it).
_PUBLIC_PATHS = {"/login", "/logout"}


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/static/") or auth.is_authed(request):
        return await call_next(request)
    if path.startswith("/api/"):
        # Programmatic clients (external agents) authenticate with the API key.
        if auth.is_api_authed(request):
            return await call_next(request)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return RedirectResponse(url=f"/login?next={quote(path)}", status_code=303)


def today():
    return datetime.now(SGT).date()


# ── helpers ─────────────────────────────────────────────────────────────────

def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"invalid amount: {value!r}")


# ── auth pages ──────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if auth.is_authed(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "next": next, "error": None}
    )


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username")
    password = form.get("password")
    nxt = form.get("next") or "/"
    if not nxt.startswith("/"):  # avoid open redirect
        nxt = "/"
    if auth.verify_credentials(username, password):
        resp = RedirectResponse(url=nxt, status_code=303)
        auth.set_cookie(resp)
        return resp
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": nxt, "error": "Invalid username or password."},
        status_code=401,
    )


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    auth.clear_cookie(resp)
    return resp


# ── pages ───────────────────────────────────────────────────────────────────

def base_ctx(request: Request, **extra) -> dict:
    """Context every page needs so the Balance/Log modals (included in base.html)
    can render on any page. Cheap reference reads."""
    ctx = {
        "request": request,
        "accounts": queries.accounts(),
        "categories": queries.categories(),
        "currencies": fx.CURRENCIES,
        "balance_rows": [r for r in queries.latest_balances() if r["active"]],
    }
    ctx.update(extra)
    return ctx


def _liquid_cash_series(fx) -> list:
    """Combined-SGD liquid cash per snapshot date, ascending (for the trend line)."""
    out = []
    for h in reversed(queries.balance_history()):
        ns, nm = queries.d2(h["net_sgd"]), queries.d2(h["net_myr"])
        combined = (ns + nm * fx).quantize(queries.TWO)
        out.append({
            "snap_date": h["snap_date"], "net_sgd": ns, "net_myr": nm, "combined": combined,
        })
    return out


@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request, month: str = ""):
    t = today()
    m = queries.dashboard(t)
    fx = m["fx_rate"]

    # Parse requested month (YYYY-MM), default to current
    import calendar as cal_mod
    from datetime import timedelta

    if month:
        try:
            parts = month.split("-")
            m_year, m_month = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            m_year, m_month = t.year, t.month
    else:
        m_year, m_month = t.year, t.month

    # Previous month
    if m_month == 1:
        prev_year, prev_month = m_year - 1, 12
    else:
        prev_year, prev_month = m_year, m_month - 1

    # Next month (for navigation)
    if m_month == 12:
        next_year, next_month = m_year + 1, 1
    else:
        next_year, next_month = m_year, m_month + 1

    # Fetch both months
    cur_rows = queries.balance_history_calendar_month(m_year, m_month)
    # Filter to exact calendar month
    cur_rows = [r for r in cur_rows
                if r["snap_date"].year == m_year and r["snap_date"].month == m_month]
    prev_rows = queries.balance_history_calendar_month(prev_year, prev_month)
    prev_rows = [r for r in prev_rows
                 if r["snap_date"].year == prev_year and r["snap_date"].month == prev_month]

    cur_days = cal_mod.monthrange(m_year, m_month)[1]
    prev_days = cal_mod.monthrange(prev_year, prev_month)[1]
    max_days = max(cur_days, prev_days)

    # Build day-of-month aligned slots for comparison_line_chart
    # Each series: 31-element list (day 1..31) of (label, value) or None
    # Current month truncates at today; previous month shows full month
    def _build_series(rows, year, month_val, num_days, cap_day=None):
        """Build a list of (day_label, combined_sgd) indexed by day-of-month (1-based)."""
        pts = [None] * max_days
        limit = min(cap_day, max_days) if cap_day else num_days
        for r in rows:
            day = r["snap_date"].day
            if 1 <= day <= limit:
                combined = float(
                    (queries.d2(r["net_sgd"]) + queries.d2(r["net_myr"]) * fx).quantize(queries.TWO)
                )
                idx = day - 1
                pts[idx] = (r["snap_date"].strftime("%-d %b"), combined)
        return pts

    # Truncate current month at today only when viewing the current month
    today_cap = t.day if (m_year == t.year and m_month == t.month) else None
    cur_pts = _build_series(cur_rows, m_year, m_month, cur_days, cap_day=today_cap)
    prev_pts = _build_series(prev_rows, prev_year, prev_month, prev_days)

    cur_month_label = f"{cal_mod.month_name[m_month]} {m_year}"
    prev_month_label = f"{cal_mod.month_name[prev_month]} {prev_year}"
    nw_svg = charts.comparison_line_chart(
        cur_pts, prev_pts,
        cur_label=cur_month_label,
        prev_label=prev_month_label,
    )

    # Month navigation
    prev_month_str = f"{prev_year}-{prev_month:02d}"
    next_month_str = f"{next_year}-{next_month:02d}"
    # Show next month link only if not in the future
    show_next = date(next_year, next_month, 1) <= t.replace(day=1)

    # 2) spend by category — ranked list (SGD-equivalent), sorted by amount desc
    cat_legend = []
    visible = sorted(
        [c for c in m["spend_by_category"] if c["sgd_eq"] > 0],
        key=lambda c: c["sgd_eq"], reverse=True,
    )
    total_sgd = float(m["total_expenses_sgd"])
    max_sgd = float(visible[0]["sgd_eq"]) if visible else 0
    for i, c in enumerate(visible):
        color = charts.SERIES[i % len(charts.SERIES)]
        sgd = float(c["sgd_eq"])
        cat_legend.append({
            "category": c["category"], "sgd": c["sgd_eq"],
            "color": color, "is_discretionary": c["is_discretionary"],
            "bar_pct": round(sgd / max_sgd * 100, 1) if max_sgd else 0,
            "pct": round(sgd / total_sgd * 100, 1) if total_sgd else 0,
        })

    # 3) daily discretionary spend trend (bars, day 1..elapsed)
    ds = queries.daily_spend(t.replace(day=1), t, discretionary_only=True)
    elapsed = m["days_elapsed"]
    bars = [{
        "label": str(day),
        "value": float(ds.get(t.replace(day=day), 0)),
        "show_label": (day == 1 or day % 5 == 0 or day == elapsed),
        "show_value": (
            float(ds.get(t.replace(day=day), 0)) > 0 and (
                elapsed <= 14
                or day == 1 or day % 5 == 0 or day == elapsed
                or day > elapsed - 7
            )
        ),
    } for day in range(1, elapsed + 1)]
    bars_svg = charts.bar_chart(bars)

    fx_detail = queries.fx_rates_detail()
    fx_updated = max((r["updated_at"] for r in fx_detail), default=None)
    return templates.TemplateResponse(
        "dashboard.html",
        base_ctx(request, m=m, nw_svg=nw_svg,
                 bars_svg=bars_svg, cat_legend=cat_legend, fx_updated=fx_updated,
                 cur_month=cur_month_label, prev_month_str=prev_month_str,
                 next_month_str=next_month_str, show_next=show_next),
    )


@app.get("/history", response_class=HTMLResponse)
def page_history(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    bal_page: int = 1,
    bal_per_page: int = 30,
    tab: str = "txns",
    account: str = "",
    category: str = "",
    flow: str = "",
    date_from: str = "",
    date_to: str = "",
    q: str = "",
):
    tab = tab if tab in ("txns", "balances") else "txns"
    filters = {
        "account": account.strip() or None,
        "category": category.strip() or None,
        "flow": flow.strip() or None,
        "date_from": date_from.strip() or None,
        "date_to": date_to.strip() or None,
        "q": q.strip() or None,
    }
    per_page = min(max(per_page, 5), 200)
    total = queries.count_filtered_transactions(filters)
    pages = max(1, math.ceil(total / per_page))
    page = min(max(page, 1), pages)
    offset = (page - 1) * per_page
    txns = [dict(t) for t in queries.filtered_transactions(filters, per_page, offset)]
    # Attach a current-FX SGD estimate to each foreign-currency row so the list
    # is readable at a glance. It's converted at *today's* rate, not the rate when
    # the money was spent, so it's an estimate — flagged as such in the template.
    rates = queries.get_fx_rates()
    has_fx = False
    for t in txns:
        if t["currency"] != "SGD":
            t["amount_sgd"] = queries.to_sgd(t["amount"], t["currency"], rates)
            has_fx = True

    # querystring of active filters (minus page) so pager links keep the filters
    keep = {k: v for k, v in {
        "account": account, "category": category, "flow": flow,
        "date_from": date_from, "date_to": date_to, "q": q, "per_page": per_page,
        "tab": tab,
    }.items() if v not in ("", None)}
    base_qs = urlencode(keep)

    fx = queries.fx_rate()
    history = _liquid_cash_series(fx)
    history.reverse()  # newest first for the table

    # balance history pagination
    bal_total = len(history)
    bal_pages = max(1, math.ceil(bal_total / bal_per_page))
    bal_page = min(max(bal_page, 1), bal_pages)
    bal_offset = (bal_page - 1) * bal_per_page
    history_page = history[bal_offset:bal_offset + bal_per_page]

    bal_qs_parts = {k: v for k, v in {
        "account": account, "category": category, "flow": flow,
        "date_from": date_from, "date_to": date_to, "q": q,
        "bal_per_page": bal_per_page,
        "tab": tab,
    }.items() if v not in ("", None)}
    bal_base_qs = urlencode(bal_qs_parts)

    return templates.TemplateResponse(
        "history.html",
        base_ctx(
            request,
            txns=txns,
            balance_history=history_page,
            fx_rate=fx,
            # filter form state
            f_account=account, f_category=category, f_flow=flow,
            f_date_from=date_from, f_date_to=date_to, f_q=q,
            has_filters=any(filters.values()),
            has_fx=has_fx,
            # pagination
            page=page, pages=pages, per_page=per_page, total=total,
            shown_from=(offset + 1 if total else 0),
            shown_to=min(offset + per_page, total),
            base_qs=base_qs,
            tab=tab,
            # balance pagination
            bal_page=bal_page, bal_pages=bal_pages, bal_total=bal_total,
            bal_per_page=bal_per_page, bal_base_qs=bal_base_qs,
        ),
    )


@app.post("/history/txn/{txn_id}/delete")
async def history_txn_delete(request: Request, txn_id: str):
    """Delete a transaction from the History page, then return to the same
    filtered/paged view (the `back` field carries the current querystring)."""
    queries.delete_transaction(txn_id)
    form = await request.form()
    back = form.get("back") or "/history"
    if not back.startswith("/history"):  # avoid open redirect
        back = "/history"
    return RedirectResponse(url=back, status_code=303)


@app.post("/history/txn/{txn_id}/update")
async def history_txn_update(request: Request, txn_id: str):
    """Update category and/or note of a transaction inline from the History page.
    Returns a short JSON response for the JS to update the DOM."""
    form = await request.form()
    category = form.get("category", "").strip() or None
    note = form.get("note")
    if note is not None:
        note = note.strip()
    if category or note is not None:
        queries.update_transaction(txn_id, category=category, note=note)
    return JSONResponse({"ok": True, "txn_id": txn_id, "category": category, "note": note})


# ── settings: shared ────────────────────────────────────────────────────────

@app.get("/settings")
def page_settings():
    return RedirectResponse(url="/settings/accounts", status_code=303)


# ── settings: FX rates ──────────────────────────────────────────────────────

@app.get("/settings/fx", response_class=HTMLResponse)
def page_fx(request: Request):
    detail = {r["currency"]: r for r in queries.fx_rates_detail()}
    rows = []
    for c in fx.CURRENCIES:
        r = detail.get(c["code"])
        if not r:
            continue
        to_sgd_v = Decimal(str(r["to_sgd"]))
        per_sgd = (Decimal("1") / to_sgd_v).quantize(Decimal("0.0001")) if to_sgd_v else Decimal("0")
        rows.append({
            "code": c["code"], "name": c["name"], "symbol": c["symbol"],
            "to_sgd": to_sgd_v, "per_sgd": per_sgd,
            "source": r["source"], "updated_at": r["updated_at"],
        })
    fx_updated = max((r["updated_at"] for r in detail.values()), default=None)
    return templates.TemplateResponse(
        "fx.html", base_ctx(request, active_tab="fx", fx_rows=rows, fx_updated=fx_updated)
    )


@app.get("/fx")
def fx_redirect():
    return RedirectResponse(url="/settings/fx", status_code=307)


# ── settings: accounts ──────────────────────────────────────────────────────

_ACCOUNT_ID_RE = re.compile(r"^[A-Z0-9_]{2,24}$")


def _parse_account_form(form) -> dict:
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    currency = (form.get("currency") or "SGD").upper()
    if currency not in fx.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"unsupported currency: {currency!r}")
    acct_type = form.get("type")
    if acct_type not in ("asset", "liability"):
        raise HTTPException(status_code=400, detail="type must be asset or liability")
    try:
        sort_order = int(form.get("sort_order") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid sort_order")
    return {
        "name": name, "currency": currency, "type": acct_type,
        "active": bool(form.get("active")), "sort_order": sort_order,
    }


@app.get("/settings/accounts", response_class=HTMLResponse)
def page_accounts(request: Request, edit: str = "", err: str = ""):
    editing = queries.get_account(edit) if edit else None
    items = []
    for a in queries.accounts():
        refs = queries.account_reference_counts(a["account_id"])
        a = dict(a)
        a["refs"] = (refs["txns"] + refs["bals"] + refs["recs"]) if refs else 0
        items.append(a)
    return templates.TemplateResponse(
        "accounts.html",
        base_ctx(request, active_tab="accounts", items=items, editing=editing,
                 err=err, next_sort=queries.next_account_sort()),
    )


@app.post("/settings/accounts/create")
async def account_create(request: Request):
    form = await request.form()
    account_id = (form.get("account_id") or "").strip().upper()
    if not _ACCOUNT_ID_RE.match(account_id):
        raise HTTPException(status_code=400, detail="account_id must be 2–24 of A–Z 0–9 _")
    if queries.get_account(account_id):
        raise HTTPException(status_code=400, detail=f"account {account_id} already exists")
    queries.create_account({"account_id": account_id, **_parse_account_form(form)})
    return RedirectResponse(url="/settings/accounts", status_code=303)


@app.post("/settings/accounts/{account_id}/update")
async def account_update(request: Request, account_id: str):
    if not queries.get_account(account_id):
        raise HTTPException(status_code=404, detail="account not found")
    form = await request.form()
    queries.update_account(account_id, _parse_account_form(form))
    return RedirectResponse(url="/settings/accounts", status_code=303)


@app.post("/settings/accounts/{account_id}/toggle")
def account_toggle(account_id: str):
    queries.toggle_account(account_id)
    return RedirectResponse(url="/settings/accounts", status_code=303)


@app.post("/settings/accounts/{account_id}/delete")
def account_delete(account_id: str):
    refs = queries.account_reference_counts(account_id)
    if refs and (refs["txns"] or refs["bals"] or refs["recs"]):
        msg = (f"Can't delete {account_id} — referenced by {refs['txns']} txns, "
               f"{refs['bals']} balances, {refs['recs']} recurring. Deactivate instead.")
        return RedirectResponse(url=f"/settings/accounts?err={quote(msg)}", status_code=303)
    queries.delete_account(account_id)
    return RedirectResponse(url="/settings/accounts", status_code=303)


# ── settings: recurring ─────────────────────────────────────────────────────

def _parse_recurring_form(form) -> dict:
    valid_accounts = queries.account_map()
    account_id = form.get("account_id")
    if account_id not in valid_accounts:
        raise HTTPException(status_code=400, detail=f"unknown account_id: {account_id!r}")
    category = form.get("category")
    if category not in queries.category_names():
        raise HTTPException(status_code=400, detail=f"unknown category: {category!r}")
    currency = (form.get("currency") or valid_accounts[account_id]["currency"]).upper()
    if currency not in fx.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"unsupported currency: {currency!r}")

    flow = form.get("flow") or "expense"
    if flow not in ("expense", "income", "transfer"):
        flow = "expense"
    frequency = form.get("frequency") or "monthly"
    if frequency not in ("monthly", "yearly", "every30"):
        frequency = "monthly"

    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    start_date = (form.get("start_date") or "").strip() or None
    moy = None

    if frequency == "every30":
        # The "last subscription date" is the anchor; renewals fall every 30 days.
        if not start_date:
            raise HTTPException(
                status_code=400,
                detail="last subscription date is required for every-30-day",
            )
        try:
            dom = date.fromisoformat(start_date).day
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid last subscription date")
    else:
        try:
            dom = max(1, min(31, int(form.get("day_of_month") or 1)))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid day_of_month")
        if frequency == "yearly":
            try:
                moy = max(1, min(12, int(form.get("month_of_year") or 1)))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="invalid month_of_year")

    return {
        "name": name,
        "account_id": account_id,
        "flow": flow,
        "category": category,
        "amount": _to_decimal(form.get("amount")),
        "currency": currency,
        "day_of_month": dom,
        "frequency": frequency,
        "month_of_year": moy,
        "active": bool(form.get("active")),
        "start_date": start_date,
        "end_date": (form.get("end_date") or "").strip() or None,
        "note": (form.get("note") or "").strip(),
    }


@app.get("/settings/recurring", response_class=HTMLResponse)
def page_recurring(request: Request, edit: str = ""):
    editing = queries.get_recurring(edit) if edit else None
    today = datetime.now(SGT).date()
    items = queries.list_recurring()
    for r in items:
        r["next_due"] = jobs.next_due(r, today)
    return templates.TemplateResponse(
        "recurring.html",
        base_ctx(request, active_tab="recurring", items=items,
                 editing=editing, months=MONTHS),
    )


@app.get("/recurring")
def recurring_redirect():
    return RedirectResponse(url="/settings/recurring", status_code=307)


@app.post("/settings/recurring/create")
async def recurring_create(request: Request):
    form = await request.form()
    queries.create_recurring(_parse_recurring_form(form))
    return RedirectResponse(url="/settings/recurring", status_code=303)


@app.post("/settings/recurring/{recur_id}/update")
async def recurring_update(request: Request, recur_id: str):
    if not queries.get_recurring(recur_id):
        raise HTTPException(status_code=404, detail="recurring not found")
    form = await request.form()
    queries.update_recurring(recur_id, _parse_recurring_form(form))
    return RedirectResponse(url="/settings/recurring", status_code=303)


@app.post("/settings/recurring/{recur_id}/delete")
def recurring_delete(recur_id: str):
    queries.delete_recurring(recur_id)
    return RedirectResponse(url="/settings/recurring", status_code=303)


@app.post("/settings/recurring/{recur_id}/toggle")
def recurring_toggle(recur_id: str):
    queries.toggle_recurring(recur_id)
    return RedirectResponse(url="/settings/recurring", status_code=303)


# Old standalone form URLs now open as modals on the dashboard (preserves deep
# links / Add-to-Home-Screen shortcuts).
@app.get("/balance")
def page_balance():
    return RedirectResponse(url="/?open=balance", status_code=307)


@app.get("/log")
def page_log():
    return RedirectResponse(url="/?open=log", status_code=307)


# ── JSON API ────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
def api_dashboard():
    return JSONResponse(_jsonable(queries.dashboard(today())))


@app.get("/api/liquid-cash")
def api_liquid_cash(date: str = ""):
    """Combined SGD liquid cash as of a specific date (YYYY-MM-DD).
    Carry-forward: uses the most recent balance snapshot per account on or
    before the requested date. Uses current FX rates. Returns {snap_date,
    net_sgd, net_myr, combined_sgd} or 404 if no balances exist for that date.
    Omit `date` to get today's snapshot."""
    from datetime import date as _date, datetime as _dt

    if date:
        try:
            target = _dt.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    else:
        target = today()

    result = queries.liquid_cash_on_date(target)
    if result is None:
        raise HTTPException(status_code=404,
                            detail=f"no balance data for {target.isoformat()}")
    return JSONResponse(_jsonable(result))


@app.get("/api/fx")
def api_fx():
    return JSONResponse(_jsonable({
        "rates": queries.fx_rates_detail(),
        "currencies": fx.CURRENCIES,
    }))


@app.get("/api/reference")
def api_reference():
    """Valid enumerations for building a transaction/balance: the live account
    list, category list, and supported currencies. Agents should read this
    rather than hard-coding ids, since accounts/categories are user-editable."""
    return JSONResponse(_jsonable({
        "accounts": queries.accounts(),
        "categories": queries.categories(),
        "currencies": fx.CURRENCIES,
    }))


@app.get("/api/transactions")
def api_transactions(
    date_from: str = "",
    date_to: str = "",
    account: str = "",
    category: str = "",
    flow: str = "",
    q: str = "",
    limit: int = 100,
):
    """Filtered transaction list + an SGD-equivalent expense total. Pass the same
    date in date_from and date_to for a single day (e.g. today's spend). All
    filters optional; dates are YYYY-MM-DD. Each row carries amount_sgd, and
    expense_total_sgd sums *all* matching expenses (not just the returned page)."""
    def _valid_date(value: str, field: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")
        return value

    filters = {
        "account": account.strip() or None,
        "category": category.strip() or None,
        "flow": flow.strip() or None,
        "date_from": _valid_date(date_from, "date_from"),
        "date_to": _valid_date(date_to, "date_to"),
        "q": q.strip() or None,
    }
    limit = min(max(limit, 1), 500)

    count = queries.count_filtered_transactions(filters)
    rows = queries.filtered_transactions(filters, limit, 0)
    rates = queries.get_fx_rates()
    txns = []
    for r in rows:
        r = dict(r)
        r["amount_sgd"] = queries.to_sgd(r["amount"], r["currency"], rates)
        txns.append(r)

    return JSONResponse(_jsonable({
        "filters": filters,
        "count": count,
        "returned": len(txns),
        "expense_total_sgd": queries.filtered_expense_total_sgd(filters),
        "transactions": txns,
    }))


@app.post("/api/balance")
async def api_balance(request: Request):
    body = await request.json()
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="rows must be a non-empty list")

    valid_accounts = queries.account_map()
    snap = today()
    n = 0
    for row in rows:
        account_id = row.get("account_id")
        if account_id not in valid_accounts:
            raise HTTPException(status_code=400, detail=f"unknown account_id: {account_id!r}")
        balance = _to_decimal(row.get("balance"))
        currency = row.get("currency") or valid_accounts[account_id]["currency"]
        queries.upsert_balance(snap, account_id, balance, currency, note=row.get("note", ""))
        n += 1
    return {"ok": True, "rows": n}


@app.post("/api/txn")
async def api_txn(request: Request):
    body = await request.json()
    account_id = body.get("account_id")
    category = body.get("category")
    to_account_id = body.get("to_account_id")

    valid_accounts = queries.account_map()
    if account_id not in valid_accounts:
        raise HTTPException(status_code=400, detail=f"unknown account_id: {account_id!r}")
    if category not in queries.category_names():
        raise HTTPException(status_code=400, detail=f"unknown category: {category!r}")
    if to_account_id and to_account_id not in valid_accounts:
        raise HTTPException(status_code=400, detail=f"unknown to_account_id: {to_account_id!r}")

    amount = _to_decimal(body.get("amount"))
    currency = (body.get("currency") or valid_accounts[account_id]["currency"]).upper()
    if currency not in fx.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"unsupported currency: {currency!r}")
    flow = body.get("flow") or "expense"
    note = (body.get("note") or "")[:200]

    now = datetime.now(SGT)
    txn_id = "T" + now.strftime("%Y%m%d%H%M%S")

    if flow == "transfer" and to_account_id:
        # Double-entry: debit source, credit destination
        txns = [
            {"account_id": account_id,    "flow": "expense"},
            {"account_id": to_account_id, "flow": "income"},
        ]
        result_ids = []
        for i, t in enumerate(txns):
            tid = f"{txn_id}_{'A' if i == 0 else 'B'}"
            queries.insert_transaction(
                txn_id=tid,
                txn_date=now.date(),
                account_id=t["account_id"],
                flow=t["flow"],
                category=category,
                amount=amount,
                currency=currency,
                source="manual",
                note=note,
            )
            result_ids.append(tid)
        return {
            "ok": True,
            "txn_id": txn_id,
            "txn_ids": result_ids,
            "from_account": account_id,
            "to_account": to_account_id,
        }

    queries.insert_transaction(
        txn_id=txn_id,
        txn_date=now.date(),
        account_id=account_id,
        flow=flow,
        category=category,
        amount=amount,
        currency=currency,
        source="manual",
        note=note,
    )
    return {"ok": True, "txn_id": txn_id}


@app.delete("/api/txn/{txn_id}")
def api_txn_delete(txn_id: str):
    """Delete a single transaction by id — lets an agent undo a mistaken log
    instead of leaving a double entry. Returns 404 if it doesn't exist."""
    existing = queries.get_transaction(txn_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"unknown txn_id: {txn_id!r}")
    queries.delete_transaction(txn_id)
    return {"ok": True, "deleted": txn_id}


@app.post("/api/parse")
async def api_parse(request: Request):
    body = await request.json()
    text = body.get("text", "")
    # llm.parse never raises -> never 5xx.
    result = llm.parse(text)
    result["_v"] = "2.1"
    return result


# ── manual job triggers (handy for ops / smoke tests; tailnet-only) ──────────

@app.post("/api/jobs/{name}")
def api_run_job(name: str):
    fn = {
        "recurring_poster": jobs.recurring_poster,
        "monthly_rollover": jobs.monthly_rollover,
        "weekly_digest": jobs.weekly_digest,
        "fx_update": jobs.fx_update,
    }.get(name)
    if fn is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {name}")
    return fn()


# ── JSON serialization for Decimal/date ─────────────────────────────────────

def _jsonable(obj):
    from datetime import date

    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj
