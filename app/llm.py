"""Natural-language expense parsing via any OpenAI-compatible chat-completions API.

Works with any provider exposing ``POST /v1/chat/completions`` (OpenAI, OpenRouter,
Groq, Together, a LiteLLM proxy, Ollama, vLLM, …) — configured via ``LITELLM_URL`` /
``LITELLM_KEY`` / ``LITELLM_MODEL`` (names are historical, not provider-specific).

``parse()`` never raises — on any LLM error or junk output it returns the
regex-based parse so ``/log`` keeps working with the model endpoint down.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

from . import fx

log = logging.getLogger("expenses.llm")

LITELLM_URL = os.environ.get(
    "LITELLM_URL", "http://localhost:4000/v1/chat/completions"
)
LITELLM_KEY = os.environ.get("LITELLM_KEY", "")
LITELLM_MODEL = os.environ.get("LITELLM_MODEL", "openrouter/google/gemma-4-26b-a4b-it")

# Valid sets are validated again at the DB layer; these are parse-time defaults.
CATEGORIES = [
    "Allowance", "Housing", "Insurance", "Telco", "Transport",
    "Subscriptions", "Tax", "Food", "Family", "Shopping", "Other",
]
ACCOUNTS = [
    "DBS", "OCBC", "MBB_ISAAVY", "WISE", "CASH_SGD",
    "DBS_CC", "MBB_CC", "MBB_MYR", "TUNAI", "TNGO",
]
MYR_ACCOUNTS = {"MBB_MYR", "TUNAI", "TNGO"}

SYSTEM_PROMPT = (
    "Extract exactly one transaction from the user's text and reply with a single JSON "
    "object: {\"amount\": number, \"currency\": one of "
    f"{fx.CODES}, \"category\": one of "
    f"{CATEGORIES}, \"account\": one of {ACCOUNTS}, "
    "\"flow\": one of [\"expense\", \"income\", \"transfer\"], "
    "\"to_account\": one of {ACCOUNTS} or null, "
    "\"note\": short string}}. "
    "Rule: Grab/Gojek/Tada/taxi/MRT/bus/ezlink/fuel/parking => Transport, UNLESS the "
    "text clearly means food (e.g. GrabFood). "
    "Rule: shopee/lazada/amazon/buy/bought/ntuc/fairprice/groceries/"
    "daiso/muji/ikea/dept store/mall => Shopping (discretionary). "
    "Default account is CASH_SGD. "
    "Rule: PayLah, paylah, or any PayLah mention => DBS account (DBS Bank / Paylah). "
    "Rule: Wise => WISE card. " 
    "Currency: use the one named in the text (e.g. usd, rm/ringgit, yen, baht, "
    "euro); default SGD if none is mentioned. "
    "Flow: default is \"expense\". Use \"income\" for salary, received money, "
    "refunds, cashback, dividends, interest earned, gifts received, or any money "
    "coming in. Use \"transfer\" ONLY for moving money between your own accounts "
    "(credit card payments, bank transfers to another of your accounts, moving "
    "money between SGD/MYR). "
    "category: when flow=transfer, ALWAYS use \"Transfer\". "
    "to_account: REQUIRED when flow=transfer — the DESTINATION account the money "
    "moves TO (e.g. \"DBS to cash\" → to_account=CASH_SGD, "
    "\"withdrew from DBS to cash\" → to_account=CASH_SGD). "
    "Set to_account=null for expense/income. Reply with JSON only, no prose."
)

# ── regex fallback ──────────────────────────────────────────────────────────

_NUM = re.compile(r"(\d+(?:\.\d+)?)")

_FOOD_KW = re.compile(
    r"\b(food|rice|kopi|coffee|nasi|caifan|cai fan|chicken rice|kfc|mcd|"
    r"mcdonald|subway|bread|breakfast|lunch|dinner|nescafe|grabfood|teh|makan|"
    r"hawker|cafe|restaurant)\b",
    re.I,
)
_TRANSPORT_KW = re.compile(
    r"\b(grab|gojek|tada|taxi|cab|mrt|bus|ezlink|ez-link|fuel|petrol|parking|"
    r"toll|train|transport|grabcar)\b",
    re.I,
)
_SUBS_KW = re.compile(
    r"\b(netflix|icloud|spotify|youtube|claude|chatgpt|openai|subscription|"
    r"prime|disney|apple\s*music)\b",
    re.I,
)
_INCOME_KW = re.compile(
    r"\b(salary|paycheck|received|refund|cashback|dividend|interest\s*earned|"
    r"gift\s*received|bonus|side\s*hustle|freelance|reimburse)\b",
    re.I,
)
_SHOPPING_KW = re.compile(
    r"\b(shop|buy|bought|purchase|shopee|lazada|amazon|daiso|muji|uniqlo|"
    r"deca?thlon|ikea|watsons|guardian|ntuc|fairprice|cold\s*storage|"
    r"grocer|department\s*store|mall|retail)\b",
    re.I,
)
_TRANSFER_KW = re.compile(
    r"\b(transfer|pay\s*(credit\s*card|cc)|cc\s*payment|credit\s*card\s*payment|"
    r"move\s*(to|from|money)|send\s*(to|money)|top\s*up|balik|withdr[ae]w|withr[ae]w)\b",
    re.I,
)

# Order matters: most specific account aliases first.
_ACCOUNT_KW = [
    (re.compile(r"\bdbs\s*cc\b", re.I), "DBS_CC"),
    (re.compile(r"\bmbb\s*cc\b", re.I), "MBB_CC"),
    (re.compile(r"\bisaavy\b", re.I), "MBB_ISAAVY"),
    (re.compile(r"\bdbs\b", re.I), "DBS"),
    (re.compile(r"\bocbc\b", re.I), "OCBC"),
    (re.compile(r"\bpaylah\b", re.I), "DBS"),
    (re.compile(r"\bwise\b", re.I), "WISE"),
    (re.compile(r"\btngo\b", re.I), "TNGO"),
    (re.compile(r"\btunai\b", re.I), "TUNAI"),
    (re.compile(r"\b(maybank|myr)\b", re.I), "MBB_MYR"),
    (re.compile(r"\bcash\b", re.I), "CASH_SGD"),
]


def currency_for_account(account_id: str) -> str:
    return "MYR" if account_id in MYR_ACCOUNTS else "SGD"


def regex_parse(text: str) -> dict:
    text = text or ""

    m = _NUM.search(text)
    amount = float(m.group(1)) if m else 0.0

    if _FOOD_KW.search(text):
        category = "Food"
    elif _TRANSPORT_KW.search(text):
        category = "Transport"
    elif _SUBS_KW.search(text):
        category = "Subscriptions"
    elif _SHOPPING_KW.search(text):
        category = "Shopping"
    else:
        category = "Other"

    account = "CASH_SGD"
    for pat, acc in _ACCOUNT_KW:
        if pat.search(text):
            account = acc
            break

    # Explicit currency in the text wins; else derive from the account.
    currency = fx.detect_currency(text) or currency_for_account(account)

    if _INCOME_KW.search(text):
        flow = "income"
    elif _TRANSFER_KW.search(text):
        flow = "transfer"
        category = "Transfer"
    else:
        flow = "expense"

    # Detect destination account for transfers ("to cash", "as cash", etc.)
    to_account = None
    if flow == "transfer":
        # First: explicit "to X" or "as X" pattern
        for pat, acc in _ACCOUNT_KW:
            if re.search(r"\b(?:to|as)\s+" + pat.pattern, text, re.I):
                to_account = acc
                break
        # Second: if the source is a bank and no destination found, default to CASH_SGD
        if to_account is None and account != "CASH_SGD":
            to_account = "CASH_SGD"

    return {
        "amount": amount,
        "category": category,
        "account": account,
        "note": text.strip(),
        "currency": currency,
        "flow": flow,
        "to_account": to_account,
    }


# ── LLM parse ───────────────────────────────────────────────────────────────

def _extract_json(content: str) -> dict | None:
    """Pull the first {...} block out of possibly-fenced/prose-wrapped output."""
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _coerce(obj: dict, text: str) -> dict:
    """Coerce LLM output to the canonical shape, defaulting missing fields."""
    fallback = regex_parse(text)

    try:
        amount = float(obj.get("amount"))
    except (TypeError, ValueError):
        amount = fallback["amount"]

    category = obj.get("category")
    if category not in CATEGORIES:
        category = fallback["category"]

    account = obj.get("account")
    if account not in ACCOUNTS:
        account = fallback["account"]

    note = obj.get("note") or fallback["note"]

    # Currency precedence: valid LLM value > explicit mention in text > account.
    currency = obj.get("currency")
    if currency not in fx.SUPPORTED:
        currency = fx.detect_currency(text) or currency_for_account(account)

    flow = obj.get("flow")
    if flow not in ("expense", "income", "transfer"):
        flow = fallback["flow"]
    # Hard override: transfers ALWAYS get category "Transfer"
    if flow == "transfer":
        category = "Transfer"
    to_account = obj.get("to_account")
    if to_account not in ACCOUNTS:
        to_account = fallback.get("to_account")

    return {
        "amount": amount,
        "category": category,
        "account": account,
        "note": str(note)[:200],
        "currency": currency,
        "flow": flow,
        "to_account": to_account if to_account in ACCOUNTS else None,
    }


def parse(text: str) -> dict:
    """Parse NL text into an expense dict. Never raises."""
    if not LITELLM_KEY:
        return regex_parse(text)

    payload = {
        "model": LITELLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }
    headers = {"Authorization": f"Bearer {LITELLM_KEY}"}

    try:
        resp = httpx.post(LITELLM_URL, json=payload, headers=headers, timeout=8.0)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        obj = _extract_json(content)
        if obj is None:
            log.warning("LLM returned no JSON; using regex fallback")
            return regex_parse(text)
        return _coerce(obj, text)
    except Exception as exc:  # noqa: BLE001 — must never propagate to a 5xx
        log.warning("LLM parse failed (%s); using regex fallback", exc)
        return regex_parse(text)
