"""
BhashaBiz — "Speak it. Snap it. Ask it." — an AI CFO for India's small shops.

The owner talks (Malayalam by default, or English / Manglish / Tamil / Hindi),
snaps a supplier bill, or asks a question — and the AI assistant keeps their books,
answers with EXACT numbers, compares weeks, and checks bank-readiness.

Built on Sarvam AI:
  Speech-to-Text   : Saaras v4    (browser mic -> text, auto language detection,
                                    known customer names passed as keyterms)
  Chat + Tools     : Sarvam-105B  (direct Chat Completions API with tool calling; no hosted Voice Agent)
  Text-to-Speech   : Bulbul v3    (the agent's voice)
  Language ID      : text LID     (picks the voice language for typed input)
  Transliteration  : Mayura       (customer names -> English letters)
  Document AI      : Sarvam Vision 1.5 (reads supplier bills for the Scan tab)

CRITICAL RULES ENFORCED IN CODE, NOT JUST THE PROMPT:
  - The model cannot save anything: propose_entry only stages a pending entry;
    confirm_entry is the ONLY function that writes (and only if something is pending).
  - Undo and correction work the same way: propose first, confirm after a yes.
  - A payment larger than what that person owes is refused, not saved.
  - Every number comes from the deterministic Python tools; the model never computes.

Run:  python app.py   then open http://127.0.0.1:5000
"""

import io
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from flask import Flask, jsonify, render_template, request
from sarvamai import SarvamAI
from sarvamai.core.api_error import ApiError
from sarvamai.types.chat_completion_request_tool_message import ChatCompletionRequestToolMessage

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "bhashabiz_data.json")

CHAT_MODEL = "sarvam-105b"
STT_MODEL = "saaras:v4"
TTS_MODEL = "bulbul:v3"

DEFAULT_LANG = "ml-IN"  # Malayalam first
TTS_LANGS = {"bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN",
              "od-IN", "pa-IN", "ta-IN", "te-IN"}

ENTRY_TYPES = ["cash_sale", "credit_sale", "payment_received", "stock_purchase",
               "expense", "withdrawal"]

TYPE_LABELS = {
    "cash_sale": "cash sale",
    "credit_sale": "credit sale (credit / kadan)",
    "payment_received": "payment received",
    "stock_purchase": "stock purchase",
    "expense": "running expense",
    "withdrawal": "owner withdrawal (money taken home)",
}

# The active Sarvam client is set per request so tools can transliterate names.
_ACTIVE = {"client": None}


# ---------------------------------------------------------------------------
# Storage (a simple JSON file — perfect for a demo, survives restarts)
# ---------------------------------------------------------------------------

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("entries", [])
                d.setdefault("reminders", [])
                d.setdefault("pending", None)
                return d
        except (json.JSONDecodeError, OSError):
            pass
    return {"entries": [], "reminders": [], "pending": None}


def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Demo data — Thomas Stores, a small grocery in Kasaragod, Kerala. TWO weeks.
# Story: this week sales are UP vs last week, but cash in hand went DOWN —
# because of a big supplier payment and more goods given on credit that has
# not come back. Dates are relative to today so the demo never goes stale.
# ---------------------------------------------------------------------------

def demo_entries():
    t = date.today()
    d = lambda days_ago: (t - timedelta(days=days_ago)).isoformat()  # noqa: E731
    return [
        # ---------------- LAST WEEK ----------------
        {"date": d(13), "person": "",                 "description": "counter sales",           "amount": 3200, "type": "cash_sale"},
        {"date": d(13), "person": "Metro Cash & Carry", "description": "stock: rice, oil, sugar",  "amount": 10000, "type": "stock_purchase"},
        {"date": d(13), "person": "Suresh",           "description": "provisions - credit",      "amount": 1500, "type": "credit_sale"},
        {"date": d(12), "person": "",                 "description": "counter sales",           "amount": 3600, "type": "cash_sale"},
        {"date": d(12), "person": "Mini",             "description": "groceries - credit",       "amount": 800, "type": "credit_sale"},
        {"date": d(11), "person": "",                 "description": "counter sales",           "amount": 3100, "type": "cash_sale"},
        {"date": d(11), "person": "",                 "description": "shop rent",               "amount": 4000, "type": "expense"},
        {"date": d(10), "person": "",                 "description": "counter sales",           "amount": 3800, "type": "cash_sale"},
        {"date": d(10), "person": "Girija",           "description": "provisions - credit",      "amount": 1200, "type": "credit_sale"},
        {"date": d(10), "person": "Suresh",           "description": "paid old credit",         "amount": 1500, "type": "payment_received"},
        {"date": d(9),  "person": "",                 "description": "counter sales",           "amount": 3500, "type": "cash_sale"},
        {"date": d(9),  "person": "Kumble Traders",   "description": "stock: snacks, tea",      "amount": 6500, "type": "stock_purchase"},
        {"date": d(8),  "person": "",                 "description": "counter sales",           "amount": 3900, "type": "cash_sale"},
        {"date": d(8),  "person": "Anwar",            "description": "provisions - credit",      "amount": 2000, "type": "credit_sale"},
        {"date": d(8),  "person": "",                 "description": "electricity bill",         "amount": 1200, "type": "expense"},
        {"date": d(8),  "person": "",                 "description": "helper wages",             "amount": 2000, "type": "expense"},
        {"date": d(7),  "person": "",                 "description": "counter sales",           "amount": 3300, "type": "cash_sale"},
        {"date": d(7),  "person": "",                 "description": "money taken home",         "amount": 2000, "type": "withdrawal"},
        {"date": d(7),  "person": "",                 "description": "auto for goods",           "amount": 400, "type": "expense"},
        {"date": d(7),  "person": "Mini",             "description": "paid old credit",         "amount": 800, "type": "payment_received"},
        # ---------------- THIS WEEK ----------------
        {"date": d(6),  "person": "",                 "description": "counter sales",           "amount": 3400, "type": "cash_sale"},
        {"date": d(6),  "person": "Metro Cash & Carry", "description": "stock: full order",       "amount": 20000, "type": "stock_purchase"},
        {"date": d(6),  "person": "Mini",             "description": "groceries - credit",       "amount": 1000, "type": "credit_sale"},
        {"date": d(5),  "person": "",                 "description": "counter sales",           "amount": 3900, "type": "cash_sale"},
        {"date": d(5),  "person": "Girija",           "description": "provisions - credit",      "amount": 2500, "type": "credit_sale"},
        {"date": d(5),  "person": "Girija",           "description": "paid part of credit",      "amount": 500, "type": "payment_received"},
        {"date": d(4),  "person": "",                 "description": "counter sales",           "amount": 3300, "type": "cash_sale"},
        {"date": d(4),  "person": "",                 "description": "shop rent",               "amount": 4000, "type": "expense"},
        {"date": d(3),  "person": "",                 "description": "counter sales",           "amount": 4200, "type": "cash_sale"},
        {"date": d(3),  "person": "Rasheed",          "description": "provisions - credit",      "amount": 3500, "type": "credit_sale"},
        {"date": d(3),  "person": "Kumble Traders",   "description": "stock: biscuits, milk",   "amount": 7500, "type": "stock_purchase"},
        {"date": d(2),  "person": "",                 "description": "counter sales",           "amount": 3800, "type": "cash_sale"},
        {"date": d(2),  "person": "Anwar",            "description": "provisions - credit",      "amount": 4000, "type": "credit_sale"},
        {"date": d(2),  "person": "",                 "description": "electricity bill",         "amount": 1300, "type": "expense"},
        {"date": d(2),  "person": "",                 "description": "helper wages",             "amount": 2000, "type": "expense"},
        {"date": d(1),  "person": "",                 "description": "counter sales",           "amount": 4400, "type": "cash_sale"},
        {"date": d(1),  "person": "Fathima",          "description": "provisions - credit",      "amount": 1800, "type": "credit_sale"},
        {"date": d(1),  "person": "",                 "description": "money taken home",         "amount": 2500, "type": "withdrawal"},
        {"date": d(1),  "person": "",                 "description": "auto for goods",           "amount": 500, "type": "expense"},
        {"date": d(0),  "person": "",                 "description": "counter sales (so far)",   "amount": 2800, "type": "cash_sale"},
        {"date": d(0),  "person": "Rasheed",          "description": "provisions - credit",      "amount": 1500, "type": "credit_sale"},
    ]


# ---------------------------------------------------------------------------
# Deterministic bookkeeping — ALL numbers come from here, never from the model
# ---------------------------------------------------------------------------

def inr(n):
    """Format a rupee amount the Indian way: 1234567 -> 12,34,567 rupees (plain text for speech)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "an unknown amount"
    negative = n < 0
    n = int(round(abs(n)))
    digits = str(n)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts) + "," + tail
    return ("minus " if negative else "") + digits + " rupees"


def norm(name):
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def parse_date(s):
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def in_period(entry, period, ref=None):
    d = parse_date(entry.get("date"))
    today = ref or date.today()
    if period == "today":
        return d == today
    if period == "week":
        # the last 7 days INCLUDING today
        return today - timedelta(days=6) <= d <= today
    if period == "month":
        return d >= today.replace(day=1)
    return True


def person_balance(entries, person):
    """How much `person` currently owes (credit given minus payments made)."""
    key = norm(person)
    bal = 0.0
    for e in entries:
        if norm(e.get("person")) != key:
            continue
        if e["type"] == "credit_sale":
            bal += e["amount"]
        elif e["type"] == "payment_received":
            bal -= e["amount"]
    return round(bal, 2)


def all_dues(entries):
    people = {}
    for e in entries:
        if e["type"] in ("credit_sale", "payment_received") and norm(e.get("person")):
            people.setdefault(norm(e["person"]), 0.0)
            people[norm(e["person"])] += e["amount"] if e["type"] == "credit_sale" else -e["amount"]
    dues = [{"person": k.title(), "owes": round(v, 2)} for k, v in people.items() if v > 0.5]
    dues.sort(key=lambda x: x["owes"], reverse=True)
    return dues


def analyse(entries, period="all"):
    e = [x for x in entries if in_period(x, period)] if period != "all" else entries
    cash_sales = sum(x["amount"] for x in e if x["type"] == "cash_sale")
    credit_sales = sum(x["amount"] for x in e if x["type"] == "credit_sale")
    revenue = cash_sales + credit_sales
    stock = sum(x["amount"] for x in e if x["type"] == "stock_purchase")
    expenses = sum(x["amount"] for x in e if x["type"] == "expense")
    withdrawals = sum(x["amount"] for x in e if x["type"] == "withdrawal")
    payments = sum(x["amount"] for x in e if x["type"] == "payment_received")
    costs = stock + expenses
    profit = revenue - costs
    margin = (profit / revenue) if revenue else None
    cash_in = cash_sales + payments
    cash_out = stock + expenses + withdrawals
    net_cash = cash_in - cash_out
    dues = all_dues(entries)          # dues are always current, all-time
    total_owed = sum(d["owes"] for d in dues)
    collection = (payments / credit_sales) if credit_sales > 0 else None
    # biggest customer share is computed over the SAME entries as revenue
    sales_by = {}
    for x in e:
        if x["type"] in ("cash_sale", "credit_sale") and norm(x.get("person")):
            sales_by[norm(x["person"])] = sales_by.get(norm(x["person"]), 0.0) + x["amount"]
    top = max(sales_by, key=sales_by.get) if sales_by else None
    top_share = (sales_by[top] / revenue) if (top and revenue) else None
    return {
        "period": period, "cash_sales": cash_sales, "credit_sales": credit_sales,
        "revenue": revenue, "stock": stock, "expenses": expenses, "withdrawals": withdrawals,
        "payments": payments, "costs": costs, "profit": profit, "margin": margin,
        "cash_in": cash_in, "cash_out": cash_out, "net_cash": net_cash,
        "dues": dues, "total_owed": total_owed, "collection": collection,
        "top_customer": top.title() if top else None, "top_share": top_share,
    }


def weeks_range(entries):
    """The two 7-day windows: this week (last 7 days incl. today) and last week."""
    today = date.today()
    return (today - timedelta(days=6), today,          # this week
            today - timedelta(days=13), today - timedelta(days=7))  # last week


def week_stats(entries, start, end):
    e = [x for x in entries if start <= parse_date(x.get("date")) <= end]
    cash_sales = sum(x["amount"] for x in e if x["type"] == "cash_sale")
    credit_sales = sum(x["amount"] for x in e if x["type"] == "credit_sale")
    payments = sum(x["amount"] for x in e if x["type"] == "payment_received")
    stock = sum(x["amount"] for x in e if x["type"] == "stock_purchase")
    expenses = sum(x["amount"] for x in e if x["type"] == "expense")
    withdrawals = sum(x["amount"] for x in e if x["type"] == "withdrawal")
    return {
        "sales": cash_sales + credit_sales,
        "cash_in": cash_sales + payments,
        "cash_out": stock + expenses + withdrawals,
        "stock_payments": stock,
        "credit_given": credit_sales,
        "credit_collected": payments,
        "net_cash": cash_sales + payments - stock - expenses - withdrawals,
    }


def compare_weeks_core(entries):
    t0, t1, l0, l1 = weeks_range(entries)
    this = week_stats(entries, t0, t1)
    last = week_stats(entries, l0, l1)
    diff = {k: round(this[k] - last[k], 2) for k in this}
    return {"this_week": this, "last_week": last, "difference": diff}


def bank_readiness_core(entries):
    """Transparent 0-100 check of what a bank looks for. Same maths as before,
    framed as: what a bank looks for / where the shop stands / what's weak."""
    a = analyse(entries, "all")
    factors = []

    if a["revenue"] <= 0:
        factors.append(["Profit the bank can see", 0,
                        "No sales recorded yet, so profit cannot be judged.",
                        "Start recording sales — even cash counter sales."])
    else:
        m = a["margin"]
        pts = 25 if m >= 0.20 else 18 if m >= 0.10 else 10 if m >= 0 else 0
        why = (f"Money left after costs is {inr(a['profit'])} on sales of {inr(a['revenue'])}"
               f" — that is {round(m*100)} percent.")
        factors.append(["Profit the bank can see", pts, why,
                        "A shop that keeps more of what it earns looks safer to lend to."])

    if a["revenue"] <= 0:
        factors.append(["Cash coming in", 0, "No cash movement recorded yet.",
                        "Banks check whether cash comes in as regularly as it goes out."])
    else:
        r = a["net_cash"] / a["revenue"]
        pts = 20 if r >= 0 else 12 if r >= -0.10 else 6 if r >= -0.25 else 0
        factors.append(["Cash coming in", pts,
                        f"Cash that came in minus cash that went out is {inr(a['net_cash'])}.",
                        "Money sitting outside as credit is the usual reason cash falls behind."])

    if a["credit_sales"] <= 0:
        factors.append(["Getting credit money back", 20,
                        "No goods were given on credit, so no money is stuck outside.", "Nothing missing here."])
    else:
        c = a["collection"]
        pts = 20 if c >= 0.80 else 12 if c >= 0.50 else 6 if c > 0 else 0
        factors.append(["Getting credit money back", pts,
                        f"Of the {inr(a['credit_sales'])} given on credit, {inr(a['payments'])} has come back"
                        f" — {round(c*100)} percent.",
                        "Banks want to see credit coming back before they trust new lending."])

    if a["top_share"] is None:
        factors.append(["Not depending on one customer", 15,
                        "No single customer dominates the sales.", "Nothing missing here."])
    else:
        s = a["top_share"]
        pts = 15 if s <= 0.25 else 9 if s <= 0.40 else 5 if s <= 0.60 else 0
        factors.append(["Not depending on one customer", pts,
                        f"{a['top_customer']} alone is {round(s*100)} percent of total sales.",
                        "If one customer stops buying, income should not collapse."])

    dates = [parse_date(x.get("date")) for x in entries]
    if not dates:
        factors.append(["Regular records", 0, "No entries recorded yet.",
                        "A passbook of daily sales is exactly what a bank wants to see."])
    else:
        first, today = min(dates), date.today()
        span = max((today - first).days + 1, 1)
        days_with = len({d.isoformat() for d in dates})
        pts = round(20 * min(days_with / span, 1.0))
        factors.append(["Regular records", pts,
                        f"Entries were kept on {days_with} of the last {span} days.",
                        "Missing days are the first thing a bank notices in a shop's records."])

    return {"total": sum(f[1] for f in factors), "factors": factors}


def old_credit(entries, days=3):
    """Credit pending a long time, capped at the person's CURRENT balance
    (credit minus payments). People who owe nothing now are never listed."""
    today = date.today()
    per = {}
    for e in entries:
        if e["type"] == "credit_sale" and norm(e.get("person")):
            age = (today - parse_date(e.get("date"))).days
            if age >= days:
                k = norm(e["person"])
                per.setdefault(k, {"oldest": age, "credit": 0.0})
                per[k]["oldest"] = max(per[k]["oldest"], age)
                per[k]["credit"] += e["amount"]
    out = []
    for k, v in per.items():
        balance = person_balance(entries, k)   # current balance, all-time
        waiting = min(v["credit"], balance)    # never more than what is still owed
        if balance > 0.5 and waiting > 0.5:
            out.append({"person": k.title(), "amount": round(waiting), "days_old": v["oldest"]})
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Name handling: always store names in English letters, match to the book
# ---------------------------------------------------------------------------

# kinship / respect suffixes owners naturally add to names (Malayalam first)
NAME_SUFFIXES = {"chettan", "chetta", "chechi", "chechichi", "etta", "ettan", "ichi",
                 "ikka", "icha", "ustad", "mon", "mole", "vally", "akka", "anna",
                 "bhai", "bai", "amma", "aunty", "uncle", "mama", "thambi", "tambi",
                 "chachi", "dada", "didi", "ji", "sir", "madam", "bro", "thatha",
                 "paatti", "chithi", "mami", "manni", "periappa", "chithappa"}

_NON_LATIN = re.compile(r"[^\x00-\x7F]")


def romanize_name(name):
    """Bring a spoken name (Malayalam/Tamil/Hindi script or already Latin) into English
    letters. Uses Sarvam transliteration when available; falls back to the text as-is."""
    name = str(name or "").strip()
    if not name or not _NON_LATIN.search(name):
        return name
    client = _ACTIVE.get("client")
    if client is None:
        return name
    try:
        r = client.text.transliterate(input=name[:60], source_language_code="auto",
                                      target_language_code="en-IN")
        out = (getattr(r, "transliterated_text", None) or "").strip()
        return out if out else name
    except Exception:
        return name


def strip_suffix(tokens):
    while tokens and tokens[-1] in NAME_SUFFIXES:
        tokens = tokens[:-1]
    return tokens


def name_key(name):
    """Comparable form of a name: lowercase, no punctuation, no kinship suffixes."""
    tokens = re.sub(r"[^\w\s&]", " ", str(name or "").lower()).split()
    tokens = strip_suffix(tokens)
    return " ".join(tokens)


def match_person(spoken_name, entries):
    """Match a spoken name (any script, spelling variants, suffixes) to an existing
    name in the book. Returns the stored name, or None if no good match."""
    existing = {e.get("person", "") for e in entries}
    existing = {x for x in existing if x and x.strip()}
    if not existing or not str(spoken_name or "").strip():
        return None
    a = name_key(romanize_name(spoken_name))
    if not a:
        return None
    best, best_score = None, 0.0
    for cand in existing:
        b = name_key(cand)
        if not b:
            continue
        if a == b or b in a or a in b:
            return cand  # exact after cleaning, or one contains the other
        score = SequenceMatcher(None, a, b).ratio()
        if score > best_score:
            best, best_score = cand, score
    if best_score >= 0.72 and min(len(a), len(name_key(best))) >= 3:
        return best
    return None


def resolve_person(spoken_name, entries):
    """Returns (name_to_store, matched_existing_name_or_None). Names are always
    stored in English letters; existing spellings win over new variants."""
    raw = str(spoken_name or "").strip()
    if not raw:
        return "", None
    matched = match_person(raw, entries)
    if matched:
        return matched, matched
    roman = romanize_name(raw)
    return (roman.title() if roman else raw.title()), None


def known_people(entries):
    """Distinct person names in the book, in English letters."""
    seen, out = set(), []
    for e in entries:
        p = (e.get("person") or "").strip()
        if p and norm(p) not in seen:
            seen.add(norm(p))
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Read-back helper (the model speaks this back and asks for confirmation)
# ---------------------------------------------------------------------------

def entry_readback(entry):
    parts = [entry.get("person") or "counter sale",
             entry.get("description") or "—",
             inr(entry["amount"]),
             TYPE_LABELS.get(entry["type"], entry["type"])]
    return f"{parts[0]} | {parts[1]} | {parts[2]} | {parts[3]}"


def overpayment_check(entries, person, amount):
    """Returns a warning dict if a payment received is more than the person owes."""
    bal = person_balance(entries, person)
    if amount > bal + 0.5:
        return {
            "warning": (f"{str(person).title()} owes {inr(bal)} right now, but the payment said is {inr(amount)}, "
                        "which is more. Ask the owner to double-check the amount or the name before saving."),
            "current_balance": inr(bal),
        }
    return None


# ---------------------------------------------------------------------------
# Tool implementations (what the model is allowed to call)
# ---------------------------------------------------------------------------

def tool_propose_entry(entry_type, amount, person="", description="", entry_date=None):
    """Stage an entry for confirmation. NEVER saves. Returns the read-back to speak."""
    if entry_type not in ENTRY_TYPES:
        return {"error": f"entry_type must be one of {ENTRY_TYPES}"}
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    if amount <= 0:
        return {"error": "amount must be more than zero"}
    if entry_type in ("credit_sale", "payment_received") and not str(person or "").strip():
        return {"error": "person (customer name) is needed for credit sales and payments received"}

    d = load_data()

    if entry_type in ("credit_sale", "payment_received", "stock_purchase") and str(person or "").strip():
        final_name, matched = resolve_person(person, d["entries"])
    else:
        final_name, matched = str(person or "").strip(), None

    if entry_type == "payment_received":
        warn = overpayment_check(d["entries"], final_name, amount)
        if warn:
            d["pending"] = None  # a refused payment also clears any stale proposal
            save_data(d)
            return warn  # refuse before even staging

    entry = {
        "date": parse_date(entry_date).isoformat() if entry_date else date.today().isoformat(),
        "person": final_name,
        "description": str(description or "").strip()[:120],
        "amount": amount,
        "type": entry_type,
    }
    d["pending"] = {"kind": "add", "entry": entry}
    save_data(d)

    out = {
        "staged": True,
        "read_back": entry_readback(entry),
        "next_step": "read this back to the owner and ask if it is correct. Only after they say yes, call confirm_entry.",
        "amount_spoken": inr(amount),
    }
    if matched:
        out["matched_existing_name"] = matched
    if entry_type == "credit_sale":
        out["person_total_owed_after_saving"] = inr(person_balance(d["entries"], final_name) + amount)
    return out


def tool_confirm_entry():
    """The ONLY function that writes to the book. Works only if something is staged."""
    d = load_data()
    p = d.get("pending")
    if not p:
        return {"error": "there is nothing waiting for confirmation. First call propose_entry, propose_undo "
                         "or propose_correction, read it back, and get the owner's yes."}

    if p["kind"] == "add":
        entry = p["entry"]
        if entry["type"] == "payment_received":
            warn = overpayment_check(d["entries"], entry["person"], entry["amount"])
            if warn:  # balance may have changed since staging; refuse to save
                d["pending"] = None
                save_data(d)
                return warn
        d["entries"].append(entry)
        d["pending"] = None
        save_data(d)
        out = {"saved": True, "entry": entry, "read_back": entry_readback(entry)}
        if entry["type"] == "credit_sale":
            out["person_total_owed"] = inr(person_balance(d["entries"], entry["person"]))
        if entry["type"] == "payment_received":
            bal = person_balance(d["entries"], entry["person"])
            out["person_total_owed"] = inr(bal) if bal > 0.5 else "nothing now"
        return out

    if p["kind"] == "undo":
        if not d["entries"]:
            d["pending"] = None
            save_data(d)
            return {"error": "the book is empty, there is nothing to undo"}
        removed = d["entries"].pop()
        d["pending"] = None
        save_data(d)
        out = {"deleted": True, "removed_entry": removed, "read_back": entry_readback(removed)}
        if removed["type"] == "credit_sale":
            out["person_total_owed"] = inr(person_balance(d["entries"], removed["person"]))
        return out

    if p["kind"] == "correct":
        i, changes = p["index"], p["changes"]
        entry = d["entries"][i]
        old = dict(entry)
        for k, v in changes.items():
            entry[k] = v
        d["pending"] = None
        save_data(d)
        out = {"corrected": True, "before": old, "after": entry,
               "read_back_before": entry_readback(old), "read_back_after": entry_readback(entry)}
        if entry["type"] == "credit_sale":
            out["person_total_owed"] = inr(person_balance(d["entries"], entry["person"]))
        return out

    d["pending"] = None
    save_data(d)
    return {"error": "unknown staged action"}


def tool_propose_undo():
    """Stage the removal of the most recent entry. NEVER deletes; confirm_entry does."""
    d = load_data()
    if not d["entries"]:
        return {"error": "the book is empty, there is nothing to undo"}
    last = d["entries"][-1]
    d["pending"] = {"kind": "undo"}
    save_data(d)
    return {"staged": True, "read_back": entry_readback(last),
            "next_step": "tell the owner which entry this is and ask if they want to delete it. "
                         "Only after they say yes, call confirm_entry."}


def tool_propose_correction(person=None, description_contains=None, amount=None,
                            new_amount=None, new_type=None, new_person=None,
                            new_description=None, new_date=None):
    """Stage a fix to the most recent entry matching the given clues. NEVER changes the book."""
    d = load_data()
    if not d["entries"]:
        return {"error": "the book is empty, there is nothing to correct"}

    def matches(e):
        if amount is not None:
            try:
                if abs(e["amount"] - float(amount)) > 0.5:
                    return False
            except (TypeError, ValueError):
                return False
        if person and norm(person) not in norm(e.get("person")) and not SequenceMatcher(
                None, name_key(romanize_name(person)), name_key(e.get("person"))).ratio() >= 0.6:
            return False
        if description_contains and str(description_contains).lower() not in str(e.get("description", "")).lower():
            return False
        return True

    idx = None
    for i in range(len(d["entries"]) - 1, -1, -1):
        if matches(d["entries"][i]):
            idx = i
            break
    if idx is None:
        return {"error": "no entry in the book matches those details"}

    entry = d["entries"][idx]
    changes = {}
    if new_amount is not None:
        try:
            changes["amount"] = round(float(new_amount), 2)
        except (TypeError, ValueError):
            return {"error": "new_amount must be a number"}
    if new_type is not None:
        if new_type not in ENTRY_TYPES:
            return {"error": f"new_type must be one of {ENTRY_TYPES}"}
        changes["type"] = new_type
    if new_person is not None:
        final_name, _ = resolve_person(new_person, d["entries"])
        changes["person"] = final_name
    if new_description is not None:
        changes["description"] = str(new_description).strip()[:120]
    if new_date is not None:
        changes["date"] = parse_date(new_date).isoformat()
    if not changes:
        return {"error": "say what to change: new_amount, new_type, new_person, new_description or new_date"}

    if "amount" in changes and entry["type"] == "payment_received":
        others = d["entries"][:idx] + d["entries"][idx + 1:]
        warn = overpayment_check(others, entry["person"], changes["amount"])
        if warn:
            return warn

    d["pending"] = {"kind": "correct", "index": idx, "changes": changes}
    save_data(d)
    after = dict(entry)
    after.update(changes)
    return {"staged": True,
            "read_back_before": entry_readback(entry),
            "read_back_after": entry_readback(after),
            "next_step": "read both lines to the owner (old then new) and ask if the change is correct. "
                         "Only after they say yes, call confirm_entry."}


def tool_list_customers():
    d = load_data()
    rows = []
    for name in known_people(d["entries"]):
        bal = person_balance(d["entries"], name)
        rows.append({"name": name, "owes_now": inr(bal) if bal > 0.5 else "nothing"})
    return {"customers": rows}


def tool_dues(person=None):
    d = load_data()
    if norm(person):
        final, _ = resolve_person(person, d["entries"])
        bal = person_balance(d["entries"], final)
        return {"person": final, "owes": inr(bal) if bal > 0.5 else "nothing",
                "owes_number": round(bal, 2)}
    dues = all_dues(d["entries"])
    return {"dues": [{"person": x["person"], "owes": inr(x["owes"])} for x in dues],
            "total_owed": inr(sum(x["owes"] for x in dues))}


def tool_sales(period="all"):
    d = load_data()
    a = analyse(d["entries"], period)
    return {"period": period,
            "cash_sales": inr(a["cash_sales"]), "credit_sales": inr(a["credit_sales"]),
            "total_sales": inr(a["revenue"]),
            "note": "profit assumes stock bought in the period was also sold in the period"}


def tool_profit(period="all"):
    d = load_data()
    a = analyse(d["entries"], period)
    if a["revenue"] <= 0:
        return {"error": "no sales recorded in this period"}
    return {"period": period, "sales": inr(a["revenue"]), "costs": inr(a["costs"]),
            "profit": inr(a["profit"]),
            "profit_margin": f"{round(a['margin']*100)} percent of sales",
            "assumption": "stock bought in the period is counted as sold in the period"}


def tool_cash(period="today"):
    d = load_data()
    a = analyse(d["entries"], period)
    return {"period": period, "cash_in": inr(a["cash_in"]), "cash_out": inr(a["cash_out"]),
            "net_cash": inr(a["net_cash"])}


def tool_compare_weeks():
    """This week vs last week, exact numbers with differences. Use it whenever the
    owner asks why cash is lower, or how this week compares with last week."""
    d = load_data()
    cmp = compare_weeks_core(d["entries"])
    def fmt(k):
        tw, lw, df = cmp["this_week"][k], cmp["last_week"][k], cmp["difference"][k]
        direction = "up" if df > 0 else ("down" if df < 0 else "same")
        return {"this_week": inr(tw), "last_week": inr(lw),
                "change": inr(abs(df)), "direction": direction}
    return {
        "sales": fmt("sales"), "cash_in": fmt("cash_in"), "cash_out": fmt("cash_out"),
        "supplier_stock_payments": fmt("stock_payments"),
        "new_credit_given": fmt("credit_given"), "credit_collected": fmt("credit_collected"),
        "net_cash": fmt("net_cash"),
        "note": ("this week means the last 7 days including today; last week means the 7 days before that. "
                 "Use these numbers exactly when explaining why cash changed."),
    }


def tool_health():
    d = load_data()
    a = analyse(d["entries"], "all")
    if a["revenue"] <= 0:
        return {"error": "not enough entries recorded yet"}
    return {
        "sales": inr(a["revenue"]),
        "money_left_after_costs_profit": inr(a["profit"]),
        "profit_margin": f"{round(a['margin']*100)} percent",
        "cash_in": inr(a["cash_in"]), "cash_out": inr(a["cash_out"]),
        "net_cash": inr(a["net_cash"]),
        "total_owed_by_customers": inr(a["total_owed"]),
        "biggest_debtors": [{"person": x["person"], "owes": inr(x["owes"])} for x in a["dues"][:3]],
        "credit_given": inr(a["credit_sales"]), "credit_collected": inr(a["payments"]),
        "share_collected": f"{round(a['collection']*100)} percent" if a["collection"] is not None else "no credit given",
        "biggest_customer": a["top_customer"],
        "biggest_customer_share": f"{round(a['top_share']*100)} percent of sales" if a["top_share"] else None,
        "assumption": "stock bought is counted as sold",
    }


def tool_bank_readiness():
    """The bank-readiness check: what a bank looks for, where the shop stands, what's weak."""
    d = load_data()
    s = bank_readiness_core(d["entries"])
    return {"score_out_of_100": s["total"],
            "factors": [{"what_a_bank_looks_for": f[0], "points": f[1], "where_the_shop_stands": f[2],
                         "what_is_missing_or_weak": f[3]} for f in s["factors"]],
            "must_say": ("This is not a credit score or a lending decision — it only shows how the shop's "
                         "records look through a bank's eyes. Banks decide for themselves.")}


def tool_eod(entry_date=None):
    d = load_data()
    day = parse_date(entry_date) if entry_date else date.today()
    day_entries = [x for x in d["entries"] if parse_date(x.get("date")) == day]
    sales = sum(x["amount"] for x in day_entries if x["type"] in ("cash_sale", "credit_sale"))
    cash_sales = sum(x["amount"] for x in day_entries if x["type"] == "cash_sale")
    credit_given = sum(x["amount"] for x in day_entries if x["type"] == "credit_sale")
    payments = sum(x["amount"] for x in day_entries if x["type"] == "payment_received")
    spent = sum(x["amount"] for x in day_entries if x["type"] in ("stock_purchase", "expense"))
    taken_home = sum(x["amount"] for x in day_entries if x["type"] == "withdrawal")
    net_cash = cash_sales + payments - spent - taken_home
    return {"date": day.isoformat(), "entries_today": len(day_entries),
            "total_sales": inr(sales), "cash_sales": inr(cash_sales),
            "new_credit_given": inr(credit_given), "payments_received": inr(payments),
            "money_spent": inr(spent), "money_taken_home": inr(taken_home),
            "cash_change": inr(net_cash)}


def tool_old_credit(days=3):
    d = load_data()
    old = old_credit(d["entries"], days)
    if not old:
        return {"message": "no old pending credit"}
    return {"old_credit": [{"person": x["person"], "amount": inr(x["amount"]),
                            "waiting_days": x["days_old"]} for x in old],
            "note": "amounts are capped at what each person still owes today"}


def tool_add_reminder(person):
    d = load_data()
    d.setdefault("reminders", []).append({"person": str(person).strip(), "date": date.today().isoformat()})
    save_data(d)
    return {"saved": True, "person": str(person).strip()}


TOOL_IMPLS = {
    "propose_entry": tool_propose_entry, "confirm_entry": tool_confirm_entry,
    "propose_undo": tool_propose_undo, "propose_correction": tool_propose_correction,
    "list_customers": tool_list_customers,
    "get_dues": tool_dues, "get_sales": tool_sales, "get_profit": tool_profit,
    "get_cash_flow": tool_cash, "compare_weeks": tool_compare_weeks,
    "health_check": tool_health, "bank_readiness": tool_bank_readiness,
    "end_of_day_summary": tool_eod, "old_credit": tool_old_credit,
    "add_reminder": tool_add_reminder,
}

TOOLS = [
    {"type": "function", "function": {
        "name": "propose_entry",
        "description": "Stage a new book entry and get the read-back to speak to the owner. "
                       "This NEVER saves. After the owner says yes, call confirm_entry.",
        "parameters": {"type": "object", "properties": {
            "entry_type": {"type": "string", "enum": ENTRY_TYPES,
                           "description": "cash_sale, credit_sale, payment_received, stock_purchase, expense, or withdrawal"},
            "amount": {"type": "number", "description": "amount in rupees, digits only"},
            "person": {"type": "string", "description": "customer or supplier name; empty for counter sales"},
            "description": {"type": "string", "description": "short note of what it was for"},
            "entry_date": {"type": "string", "description": "optional YYYY-MM-DD; omit for today"},
        }, "required": ["entry_type", "amount"]}}},
    {"type": "function", "function": {
        "name": "confirm_entry",
        "description": "Save the staged action (new entry, undo, or correction) after the owner said yes. "
                       "This is the ONLY tool that changes the book.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_undo",
        "description": "Stage removal of the most recent entry and get its read-back. NEVER deletes by itself; "
                       "the owner must say yes, then call confirm_entry.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_correction",
        "description": "Stage a fix to an existing entry (found by person, description or amount) and get the "
                       "before/after read-back. NEVER changes the book; the owner must say yes, then confirm_entry.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "who the entry belongs to, if known"},
            "description_contains": {"type": "string", "description": "a word from the entry's note, e.g. 'rent'"},
            "amount": {"type": "number", "description": "the (wrong) amount currently in the book"},
            "new_amount": {"type": "number"},
            "new_type": {"type": "string", "enum": ENTRY_TYPES},
            "new_person": {"type": "string"},
            "new_description": {"type": "string"},
            "new_date": {"type": "string", "description": "YYYY-MM-DD"},
        }}}},
    {"type": "function", "function": {
        "name": "list_customers",
        "description": "Everyone already in the book, with what they owe now. Use it to check names before "
                       "recording something for a person the owner mentioned.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_dues",
        "description": "Who owes money. With a person name: that person's balance. Without: everyone, largest first.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "optional customer name"},
        }}}},
    {"type": "function", "function": {
        "name": "get_sales",
        "description": "Total sales for a period. Week means the last 7 days including today.",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["today", "week", "month", "all"], "description": "optional, default all"},
        }}}},
    {"type": "function", "function": {
        "name": "get_profit",
        "description": "Exact profit (money left after costs) for a period.",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["today", "week", "month", "all"], "description": "optional, default all"},
        }}}},
    {"type": "function", "function": {
        "name": "get_cash_flow",
        "description": "How much cash came in and went out for a period.",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "enum": ["today", "week", "month", "all"], "description": "optional, default today"},
        }}}},
    {"type": "function", "function": {
        "name": "compare_weeks",
        "description": "This week vs last week: sales, cash in, cash out, supplier payments, new credit given, "
                       "credit collected, net cash — with exact differences. Use it for questions like "
                       "'why is my cash lower even though sales are up?' or 'how does this week compare'.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "health_check",
        "description": "Full picture of the business: profit, cash flow, money people owe, credit collection, dependence on one customer.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "bank_readiness",
        "description": "The bank-readiness check: what a bank looks for, where the shop stands on each, "
                       "and which records are missing or weak.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "end_of_day_summary",
        "description": "Summary of one day: sales, money spent, cash change, new credit given. Defaults to today.",
        "parameters": {"type": "object", "properties": {
            "entry_date": {"type": "string", "description": "optional YYYY-MM-DD; omit for today"},
        }}}},
    {"type": "function", "function": {
        "name": "old_credit",
        "description": "Credit pending a long time (for payment reminders). Amounts never exceed what each person still owes today.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "how many days counts as old; default 3"},
        }}}},
    {"type": "function", "function": {
        "name": "add_reminder",
        "description": "Note down that the owner wants to remind a customer about pending money.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string"},
        }, "required": ["person"]}}},
]


# ---------------------------------------------------------------------------
# The agent brain (system prompt = conversation design)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are BhashaBiz — the AI CFO and trusted family accountant for a small Indian shop owner (kirana store, grocery, tea stall, small trader). You are warm, patient and encouraging, like a trusted family accountant. You are NEVER like a bank, never cold, never a robot. You are never judgmental about the owner's business or record keeping. You celebrate small wins.

LANGUAGE: You speak MALAYALAM by default (the owner's shop is in Kasaragod, Kerala). If the owner speaks English, Manglish, Tamil or Hindi, or mixes languages, follow them exactly — same language, same mix. If they switch, you switch.

HOW YOU SPEAK (this is a phone conversation, not writing):
- Short sentences, made for listening. Maximum 4 sentences in a normal reply (a health check may take 6-8 short ones).
- No lists, no bullet points, no symbols, no emojis, no markdown, no headings.
- One question at a time. Never two questions at once.
- Everyday words first: say "money people owe you" not "receivables", "money left after costs (this is called profit)" not "profit margin". You may teach the proper word once, gently.
- Write rupee amounts in digits like "5,800 rupees" — always with commas for thousands, and always say "rupees".
- When you give an important number, repeat it once shortly after.

RECORDING A TRANSACTION (the owner says something like "Selvi took 500 rupees of rice on credit"):
1. Work out: person, what it was for, amount, and the type. Types: cash sale, credit sale (credit / kadan), payment received, stock purchase, expense (rent, electricity, wages, transport), owner withdrawal (money taken home).
2. If the amount, the person's name, or whether it was cash or credit is unclear — ask ONE short question. Do not guess. Do not invent. If unsure who the person is, call list_customers and check.
3. If it is clear, call propose_entry. It stages the entry and gives you a read-back. Speak that read-back in the owner's language, in one short line, and ask if it is correct. Example: "Selvi, rice, 500 rupees, credit. Correct?"
4. Only after the owner says yes / aano / correct / haan — call confirm_entry. Then confirm it is saved, and tell them the person's new total owed from the tool result. If the owner corrects something, propose again with the correction.
5. Nothing is ever saved without the read-back and a yes. If you are unsure whether the owner confirmed, ask again — one short question.

NAMES: names in the book are stored in English letters, and the tool matches the spoken name to existing people automatically (Malayalam, Tamil or Hindi script, spelling differences, suffixes like chettan, chechi, akka, bhai are all handled). If the tool says the name matched an existing one, use that existing name when you speak.

FIXING MISTAKES ("that was wrong", "I said 6,000 not 5,000"):
- To delete the last entry: propose_undo, tell the owner exactly which entry it is, get a yes, then confirm_entry.
- To change something: propose_correction with what you know (person, a word from the note like "rent", or the wrong amount) and the new value. Read back the old line and the new line, get a yes, then confirm_entry.

PAYMENTS: if propose_entry returns a warning that a payment is more than the person owes, do NOT save it. Kindly ask the owner to double-check the amount or the name. Maybe it was a different person, or a smaller amount.

NUMBERS — THE MOST IMPORTANT RULE:
- You NEVER calculate, estimate, or remember numbers yourself. Every number you say about the business MUST come from a tool result in this same conversation turn. If no tool result has the number, call a tool.
- If a tool returns an error or empty data, say honestly that you cannot say for sure right now. Never make up or guess a number.
- Use the exact numbers and words from the tool results. Do not round, do not add, do not subtract.

WHEN THE OWNER ASKS ABOUT THE BUSINESS:
- "who owes me money" / "how much does X owe" -> get_dues
- "how much did I sell" -> get_sales with the right period (week = last 7 days including today)
- "did I make a profit" -> get_profit with the right period
- "how much cash came in / went out today" -> get_cash_flow with period today
- "why is my cash lower even though sales are up" / "how is this week compared to last" -> compare_weeks, then explain the reasons using ONLY the tool's numbers, and suggest one or two practical actions
- "how is my business doing" / health -> health_check, then explain simply
- bank readiness / "can a bank trust my records" / "am I ready for a loan" -> bank_readiness
- end of day summary -> end_of_day_summary
- reminders / old pending money -> old_credit

EXPLAINING THE CASH QUESTION (very important): when sales are up but cash is down, explain step by step in short sentences, using only the compare_weeks numbers: how much sales went up, how much more went to suppliers, how much more credit is sitting outside, how little of it came back. Then suggest one or two actions, like collecting from the biggest person who owes.

BANK-READINESS (not a loan promise): present it as what a bank looks for, where the shop stands on each, and which records are missing or weak. You MUST clearly say this is not a credit score or a lending decision, and that banks decide for themselves. Never promise a loan. For investment, tax or legal questions, kindly suggest a bank officer or an accountant.

REMINDERS: if old_credit shows a customer waiting a long time, mention it warmly and offer to note a reminder (add_reminder).

BILL SCANS: if the owner mentions a bill or invoice they photographed, tell them to use the Scan tab on the screen — it reads the bill and saves it after they check it.

BOUNDARIES:
- No investment, tax, or legal advice. For those, kindly suggest talking to a bank officer or an accountant.
- Never invent entries or numbers. If the owner did not say it, it did not happen.
- The owner's data is private. Never discuss one customer with anyone else.
- If the owner talks about something unrelated, reply kindly in one short sentence and gently bring it back to the shop.
- If the owner sounds rushed, be quicker and shorter."""


# ---------------------------------------------------------------------------
# API key + client helpers
# ---------------------------------------------------------------------------

def get_api_key():
    key = request.headers.get("X-Sarvam-Key", "").strip()
    if key:
        return key
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if key:
        return key
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            m = re.match(r"\s*SARVAM_API_KEY\s*=\s*['\"]?([^'\"\s]+)", line)
            if m:
                return m.group(1)
    return ""


def friendly_error(e):
    if isinstance(e, ApiError):
        status = getattr(e, "status_code", None)
        body = str(getattr(e, "body", "") or "")[:300]
        if status in (401, 403):
            return "Your Sarvam API key was rejected. Open Settings and paste a valid key (from dashboard.sarvam.ai)."
        if status == 429:
            return "Sarvam's servers are busy for a moment. Please wait a few seconds and try again."
        return f"The Sarvam service returned an error (HTTP {status}): {body}"
    return f"Something went wrong while talking to Sarvam AI: {str(e)[:300]}"


def safe_history(raw):
    """Parse the conversation history without ever crashing: bad JSON, wrong types
    and unknown roles are dropped; entries are kept as plain role/content dicts."""
    try:
        h = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(h, list):
        return []
    clean = []
    for m in h[-30:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        clean.append({"role": role, "content": content[:4000]})
    return clean


def run_tool_call(name, args_json):
    impl = TOOL_IMPLS.get(name)
    if not impl:
        return {"error": f"unknown tool {name}"}
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError:
        return {"error": "could not read the tool arguments"}
    if not isinstance(args, dict):
        return {"error": "tool arguments must be an object"}
    try:
        return impl(**args)
    except TypeError as e:
        return {"error": f"wrong arguments for {name}: {e}"}


def agent_reply(client, history):
    """Run the agent loop: model -> tool calls -> exact results -> final spoken text."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-30:]
    for _ in range(4):  # allow a few tool rounds per turn
        resp = client.chat.completions(
            model=CHAT_MODEL,
            messages=messages,
            tools=TOOLS,
            temperature=0.4,
            max_tokens=3000,
            reasoning_effort="low",
        )
        msg = resp.choices[0].message
        calls = list(getattr(msg, "tool_calls", None) or [])
        if not calls:
            return (msg.content or "").strip() or "I am here. Please tell me."

        messages.append({"role": "assistant",
                         "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments or "{}"}}
                                        for c in calls]})
        for c in calls:
            result = run_tool_call(c.function.name, c.function.arguments)
            messages.append(ChatCompletionRequestToolMessage(
                role="tool", tool_call_id=c.id, content=json.dumps(result, ensure_ascii=False)))
    return "I could not finish that. Shall we try once more?"


# ---------------------------------------------------------------------------
# Bill scanning (Document AI -> LLM extraction -> owner confirms in the UI)
# ---------------------------------------------------------------------------

BILL_SYSTEM = """You read OCR text of a supplier bill, invoice or receipt for a small Indian shop.
Return ONLY a JSON object (no markdown fences) with exactly these keys:
{"supplier": "name of the supplier shop, or empty string",
 "date": "date on the bill as YYYY-MM-DD if readable, else empty string",
 "items": [{"name": "item name", "amount": number}],
 "total": "total amount in rupees as a number (digits only)",
 "type": "stock_purchase if it is goods bought for the shop, expense if it is rent/electricity/wages/transport"}
Use the bill's own numbers — do not add, compute or estimate anything. If a value is not on the bill, use empty string, empty list, or 0."""


def extract_json_from_text(text):
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def digitise_bill(client, file_bytes, filename, mime, language):
    """Read a bill with Sarvam Document AI and return the OCR text."""
    job = client.doc_ai.digitise(
        file=[(filename, io.BytesIO(file_bytes), mime or "application/octet-stream")],
        language=language,
        output_format="md",
        content_type="mixed",
    )
    terminal = {"completed", "partially_completed", "failed", "rejected"}
    deadline = time.time() + 120
    st = None
    while time.time() < deadline:
        st = client.doc_ai.get_status(job_id=job.job_id)
        if str(st.status).lower() in terminal:
            break
        time.sleep(3)
    else:
        raise TimeoutError("Reading the bill took too long.")
    if str(st.status).lower() not in ("completed", "partially_completed"):
        raise RuntimeError(f"Document AI could not read this bill (status: {st.status}).")
    results = client.doc_ai.get_results(job_id=job.job_id)
    pages = []
    for doc in (results.documents or []):
        for page in (doc.pages or []):
            if page.content:
                pages.append(page.content)
    return "\n".join(pages).strip()


def parse_bill(client, ocr_text):
    """Turn bill OCR text into structured fields with Sarvam-105B."""
    resp = client.chat.completions(
        model=CHAT_MODEL,
        messages=[
            {"role": "system", "content": BILL_SYSTEM},
            {"role": "user", "content": "Bill OCR text:\n\n" + ocr_text[:12000] + "\n\nReturn the JSON now."},
        ],
        temperature=0.1,
        max_tokens=1500,
        reasoning_effort="low",
    )
    data = extract_json_from_text(resp.choices[0].message.content)
    if not isinstance(data, dict):
        return None
    items = []
    for it in (data.get("items") or []):
        if isinstance(it, dict) and it.get("name"):
            try:
                amt = float(str(it.get("amount", 0)).replace(",", "") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            items.append({"name": str(it["name"])[:60], "amount": round(amt, 2)})
    try:
        total = float(str(data.get("total", 0)).replace(",", "") or 0)
    except (TypeError, ValueError):
        total = 0.0
    btype = data.get("type")
    if btype not in ("stock_purchase", "expense"):
        btype = "stock_purchase"
    return {
        "supplier": str(data.get("supplier", "") or "").strip()[:60],
        "date": str(data.get("date", "") or "")[:10],
        "items": items,
        "total": round(total, 2),
        "type": btype,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def api_state():
    d = load_data()
    dues = all_dues(d["entries"])
    return jsonify({"entries": d["entries"], "reminders": d.get("reminders", []),
                    "dues": dues, "total_owed": sum(x["owes"] for x in dues)})


@app.route("/api/insights", methods=["GET"])
def api_insights():
    """Everything for the Insights tab, computed in Python (never by the model)."""
    d = load_data()
    today = analyse(d["entries"], "today")
    cmp = compare_weeks_core(d["entries"])
    bank = bank_readiness_core(d["entries"])
    return jsonify({
        "today": {"sales": today["revenue"], "cash_in": today["cash_in"],
                  "cash_out": today["cash_out"], "net_cash": today["net_cash"]},
        "weeks": cmp,
        "dues": all_dues(d["entries"]),
        "bank": {"total": bank["total"],
                 "factors": [{"name": f[0], "points": f[1], "why": f[2]} for f in bank["factors"]]},
        "entry_count": len(d["entries"]),
    })


@app.route("/api/demo", methods=["POST"])
def api_demo():
    save_data({"entries": demo_entries(), "reminders": [], "pending": None})
    return jsonify({"ok": True, "count": len(demo_entries())})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    save_data({"entries": [], "reminders": [], "pending": None})
    return jsonify({"ok": True})


@app.route("/api/speak", methods=["POST"])
def api_speak():
    """Direct API pipeline: uploaded audio -> Saaras STT API -> Sarvam chat completions/tool calls -> Bulbul TTS API. No managed Voice Agent deployment."""
    key = get_api_key()
    if not key:
        return jsonify({"error": "no_key"}), 401
    client = SarvamAI(api_subscription_key=key)
    _ACTIVE["client"] = client  # lets the tools transliterate names

    f = request.files.get("audio")
    history = safe_history(request.form.get("history", "[]"))  # never crashes on bad input
    speaker = request.form.get("speaker", "shubh")
    fallback_lang = request.form.get("language", DEFAULT_LANG) or DEFAULT_LANG

    if not f or not f.filename:
        return jsonify({"error": "No audio was received. Tap the mic and speak again."}), 400

    try:
        # known customer names help the recogniser hear them correctly
        keyterms = known_people(load_data()["entries"])[:40]
        stt = client.speech_to_text.transcribe(
            file=[(f.filename, f.stream, f.mimetype or "audio/webm")],
            model=STT_MODEL,
            mode="transcribe",
            language_code="unknown",
            keyterms=keyterms or None,
        )
    except Exception as e:
        return jsonify({"error": "I could not hear that clearly. " + friendly_error(e)}), 502

    user_text = (stt.transcript or "").strip()
    spoken_lang = stt.language_code or fallback_lang
    if not user_text:
        return jsonify({"reply": "I could not hear anything. Please speak again, a little closer to the mic.",
                        "audio": None, "user_text": "", "language": spoken_lang, "history": history})

    history.append({"role": "user", "content": user_text})
    try:
        reply = agent_reply(client, history)
    except Exception as e:
        return jsonify({"error": friendly_error(e), "user_text": user_text}), 502
    history.append({"role": "assistant", "content": reply})

    audio_b64 = None
    try:
        tts_lang = spoken_lang if spoken_lang in TTS_LANGS else fallback_lang
        audio = client.text_to_speech.convert(
            text=reply[:2400], language_code=tts_lang if tts_lang in TTS_LANGS else DEFAULT_LANG,
            model=TTS_MODEL, speaker=speaker, output_audio_codec="mp3",
        )
        audios = getattr(audio, "audios", None) or []
        if audios:
            audio_b64 = audios[0]
    except Exception:
        audio_b64 = None  # the text reply still works; voice is best-effort

    return jsonify({"reply": reply, "audio": audio_b64, "user_text": user_text,
                    "language": spoken_lang, "history": history})


@app.route("/api/type", methods=["POST"])
def api_type():
    """Typed input: direct Sarvam Chat Completions API; no speech recognition needed."""
    key = get_api_key()
    if not key:
        return jsonify({"error": "no_key"}), 401
    client = SarvamAI(api_subscription_key=key)
    _ACTIVE["client"] = client

    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    text = (data.get("text") or "").strip() if isinstance(data.get("text"), str) else ""
    history = safe_history(data.get("history", []))
    speaker = data.get("speaker", "shubh") if isinstance(data.get("speaker"), str) else "shubh"
    fallback_lang = data.get("language", DEFAULT_LANG) or DEFAULT_LANG
    if not text:
        return jsonify({"error": "Please type something first."}), 400

    try:
        lid = client.text.identify_language(input=text[:1000])
        spoken_lang = lid.language_code or fallback_lang
    except Exception:
        spoken_lang = fallback_lang

    history.append({"role": "user", "content": text})
    try:
        reply = agent_reply(client, history)
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 502
    history.append({"role": "assistant", "content": reply})

    audio_b64 = None
    try:
        tts_lang = spoken_lang if spoken_lang in TTS_LANGS else fallback_lang
        audio = client.text_to_speech.convert(
            text=reply[:2400], language_code=tts_lang if tts_lang in TTS_LANGS else DEFAULT_LANG,
            model=TTS_MODEL, speaker=speaker, output_audio_codec="mp3",
        )
        if getattr(audio, "audios", None):
            audio_b64 = audio.audios[0]
    except Exception:
        audio_b64 = None

    return jsonify({"reply": reply, "audio": audio_b64, "language": spoken_lang, "history": history})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Read a supplier bill photo/PDF with Document AI + Sarvam-105B, return editable fields."""
    key = get_api_key()
    if not key:
        return jsonify({"error": "no_key"}), 401
    client = SarvamAI(api_subscription_key=key)
    _ACTIVE["client"] = client

    f = request.files.get("file")
    language = request.form.get("language", DEFAULT_LANG) or DEFAULT_LANG
    if not f or not f.filename:
        return jsonify({"error": "Please choose a photo or PDF of the bill first."}), 400
    file_bytes = f.read()

    try:
        ocr_text = digitise_bill(client, file_bytes, f.filename, f.mimetype, language)
        if len(ocr_text.strip()) < 20:  # retry once in English in case of language mix
            ocr_text = digitise_bill(client, file_bytes, f.filename, f.mimetype, "en-IN")
    except Exception as e:
        return jsonify({"error": friendly_error(e) + " You can also use the Talk tab by voice instead — "
                                       "just tap the mic and say what you bought."}), 502

    if len(ocr_text.strip()) < 20:
        return jsonify({"error": "Could not read any text from this bill. Try a clearer photo with good light — "
                                 "or use the Talk tab by voice instead."}), 422

    try:
        bill = parse_bill(client, ocr_text)
    except Exception as e:
        return jsonify({"error": friendly_error(e)}), 502
    if not bill or (not bill["total"] and not bill["items"]):
        return jsonify({"error": "The bill was read, but no amount could be understood from it. "
                                 "Check the amount by hand, or use the Talk tab by voice."}), 422

    # description from the item names (read from the bill, not computed)
    names = [i["name"] for i in bill["items"] if i["name"]]
    bill["items_note"] = ", ".join(names[:4]) if names else "bill"
    return jsonify({"bill": bill, "ocr_preview": ocr_text[:400]})


@app.route("/api/bill-confirm", methods=["POST"])
def api_bill_confirm():
    """Save the owner-confirmed bill through the same propose -> confirm flow."""
    key = get_api_key()
    if not key:
        return jsonify({"error": "no_key"}), 401
    client = SarvamAI(api_subscription_key=key)
    _ACTIVE["client"] = client

    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    supplier = str(data.get("supplier", "") or "").strip()
    try:
        total = round(float(str(data.get("total", 0)).replace(",", "") or 0), 2)
    except (TypeError, ValueError):
        total = 0.0
    btype = data.get("type", "stock_purchase")
    if btype not in ("stock_purchase", "expense"):
        btype = "stock_purchase"
    bill_date = str(data.get("date", "") or "")[:10]
    items_note = str(data.get("items_note", "") or "").strip()[:100] or "bill"

    if total <= 0:
        return jsonify({"error": "Please check the amount — it must be more than zero."}), 400

    # the owner's Confirm click in the UI is the "yes": stage and save in one go
    staged = tool_propose_entry(
        entry_type=btype, amount=total, person=supplier,
        description=items_note, entry_date=bill_date or None)
    if not staged.get("staged"):
        return jsonify({"error": staged.get("warning") or staged.get("error") or "The bill could not be saved."}), 400
    saved = tool_confirm_entry()
    if not saved.get("saved"):
        return jsonify({"error": saved.get("warning") or saved.get("error") or "The bill could not be saved."}), 400
    return jsonify({"saved": True, "entry": saved["entry"], "read_back": saved["read_back"]})


if __name__ == "__main__":
    if not os.path.exists(DATA_FILE):
        save_data({"entries": demo_entries(), "reminders": [], "pending": None})
    print("\n  BhashaBiz is running at  http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
