#!/usr/bin/env python3
"""
kpi_debt.py — Debt Intelligence Pipeline v2

Fetches all invoices from Zoho Books, classifies them (Promo / Write-off / Paid /
Partial / Overdue), then produces debt_data.json consumed by the KPI dashboard.

Key logic:
  - Promo   : last_payment_date blank AND total = 0  AND balance = 0
  - Write-off: last_payment_date blank AND total > 0 AND balance = 0
  - Exclude : status in (draft, open, void)
  - Curve denominator = ALL non-excluded invoice totals for that cohort month
  - Curve numerator   = amount actually collected (paid=total, partial=total-balance)
                        bucketed by days from invoice_date to last_payment_date
  - De-dup by invoice_number (keep first occurrence)

Usage:
    python kpi_debt.py

Output:
    ~/Documents/anyvan-kpi/data/debt_data.json
    ~/Documents/anyvan-kpi/data/debt_history.json  (appended)
"""

import os, json, time
from datetime import date, datetime, timedelta
from collections import defaultdict
import requests

# ── Constants ─────────────────────────────────────────────────────────────────

ZOHO_REGION = "eu"
ZOHO_ORG_ID = "[ZOHO_ORG_ID_REMOVED]"
TOKEN_URL   = f"https://accounts.zoho.{ZOHO_REGION}/oauth/v2/token"
BOOKS_BASE  = f"https://www.zohoapis.{ZOHO_REGION}/books/v3"

OUTPUT_FILE  = os.environ.get("OUTPUT_FILE",  os.path.expanduser("~/Documents/anyvan-kpi/data/debt_data.json"))
HISTORY_FILE = os.environ.get("HISTORY_FILE", os.path.expanduser("~/Documents/anyvan-kpi/data/debt_history.json"))

CREDS_FILES = [
    ".env",  # GitHub Actions writes creds here
    os.path.expanduser("~/Documents/anyvan-kpi/.env"),
    os.path.expanduser("~/Desktop/Debt Management/Config/credentials.txt"),
]

EXCLUDE_STATUSES = {"draft", "open", "void"}

BUCKET_ORDER = ["<1", "1-5", "6-10", "11-15", "16-20", "21-25",
                "26-30", "31-35", "36-40", "41-45", "46-51", ">51"]

MONTH_LABELS = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}

# ── Credentials ───────────────────────────────────────────────────────────────

def load_creds():
    if os.getenv("ZOHO_CLIENT_ID") and (os.getenv("ZOHO_REFRESH_TOKEN") or os.getenv("ZOHO_BOOKS_REFRESH_TOKEN")):
        return
    for path in CREDS_FILES:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
    if not os.getenv("ZOHO_CLIENT_ID"):
        raise RuntimeError(f"No credentials found. Checked: {CREDS_FILES}")

_token_cache: dict = {}

def get_token() -> str:
    if _token_cache.get("token") and _token_cache.get("expires", 0) > time.time() + 60:
        return _token_cache["token"]
    load_creds()
    resp = requests.post(TOKEN_URL, params={
        "refresh_token": os.environ.get("ZOHO_REFRESH_TOKEN") or os.environ["ZOHO_BOOKS_REFRESH_TOKEN"],
        "client_id":     os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type":    "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    _token_cache["token"]   = data["access_token"]
    _token_cache["expires"] = time.time() + 3300
    return _token_cache["token"]

def _books_get(path, params=None):
    headers = {"Authorization": f"Zoho-oauthtoken {get_token()}"}
    p = {"organization_id": ZOHO_ORG_ID, **(params or {})}
    for attempt in range(3):
        try:
            r = requests.get(f"{BOOKS_BASE}{path}", headers=headers, params=p, timeout=30)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  ⏳ Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
            else:
                raise RuntimeError(f"Books GET {path} failed: {e}")
    return {}

# ── Fetching ──────────────────────────────────────────────────────────────────

def _paginate(params, label=""):
    """Generic paginator. Returns deduplicated list by invoice_id."""
    invoices, seen, page = [], set(), 1
    while True:
        data = _books_get("/invoices", {"page": page, **params})
        batch = data.get("invoices", [])
        for inv in batch:
            iid = inv.get("invoice_id")
            if iid and iid not in seen:
                seen.add(iid)
                invoices.append(inv)
        has_more = data.get("page_context", {}).get("has_more_page", False)
        if page % 10 == 0 or not has_more:
            print(f"    {label}page {page} — {len(invoices):,} so far", flush=True)
        if not has_more:
            break
        page += 1
        time.sleep(0.4)
    return invoices

def fetch_by_status(status, date_start=None, date_end=None):
    """Fetch invoices for a given status, with optional date range."""
    params = {
        "status":      status,
        "per_page":    200,
        "sort_column": "date",
        "sort_order":  "A",
    }
    if date_start:
        params["date_start"] = date_start
    if date_end:
        params["date_end"] = date_end
    return _paginate(params, f"{status} ")

def fetch_overdue_all():
    """Fetch ALL overdue invoices with no date limit (catches old unpaid debt)."""
    return fetch_by_status("overdue")

# ── Date helpers ──────────────────────────────────────────────────────────────

def _to_date(val):
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt).date()
        except (ValueError, TypeError):
            pass
    return None

# ── Classification ────────────────────────────────────────────────────────────

def classify(inv):
    """
    Returns one of: 'exclude', 'promo', 'write_off', 'paid', 'partial', 'overdue'

    Rules (applied in order):
      exclude   : status in (draft, open, void)
      promo     : no payment date AND total = 0 AND balance = 0
      write_off : no payment date AND total > 0 AND balance = 0
      paid      : has payment date AND balance = 0
      partial   : has payment date AND balance > 0
      overdue   : no payment date AND balance > 0  (outstanding)
    """
    status = (inv.get("status") or "").lower().strip()
    if status in EXCLUDE_STATUSES:
        return "exclude"

    total       = float(inv.get("total",   0) or 0)
    balance     = float(inv.get("balance", 0) or 0)
    has_payment = bool((inv.get("last_payment_date") or "").strip())

    if not has_payment:
        if total == 0 and balance == 0:
            return "promo"
        if total > 0 and balance == 0:
            return "write_off"
        return "overdue"

    # Has a last_payment_date
    if balance == 0:
        return "paid"
    return "partial"

# ── Days-to-pay ───────────────────────────────────────────────────────────────

def calc_days_to_pay(inv):
    """Days from invoice_date to last_payment_date. Returns int >= 0, or None."""
    pay_str = (inv.get("last_payment_date") or "").strip()
    if not pay_str:
        return None
    inv_date = _to_date(inv.get("invoice_date") or inv.get("date"))
    pay_date = _to_date(pay_str)
    if not inv_date or not pay_date:
        return None
    return max((pay_date - inv_date).days, 0)

def _bucket(days):
    """Map days-to-pay integer to bucket label."""
    if days is None or days < 1: return "<1"
    if days <= 5:  return "1-5"
    if days <= 10: return "6-10"
    if days <= 15: return "11-15"
    if days <= 20: return "16-20"
    if days <= 25: return "21-25"
    if days <= 30: return "26-30"
    if days <= 35: return "31-35"
    if days <= 40: return "36-40"
    if days <= 45: return "41-45"
    if days <= 51: return "46-51"
    return ">51"

# ── Collection curve ──────────────────────────────────────────────────────────

def build_curve(invoices, months=6):
    """
    Collection rate curve — exact replication of the manual Excel method.

    For each invoice month cohort (last N complete months + current MTD):

      Denominator = sum of `total` for ALL non-excluded invoices in that cohort
                    (Closed + Overdue + Write-off + Promo[=0]; excludes draft/open/void)

      Numerator per bucket = amount ACTUALLY collected in that day-to-pay range:
                             paid    → full `total`
                             partial → `total - balance`

      Cumulative % = running sum of bucket amounts / denominator

    The gap between the final cumulative % and 100% is the uncollected rate
    (write-offs + still-outstanding debt from that cohort).
    """
    today      = date.today()
    current_mk = f"{today.year}-{today.month:02d}"

    # Oldest cohort month to include
    cutoff_date = today.replace(day=1) - timedelta(days=(months - 1) * 28)
    cutoff_mk   = f"{cutoff_date.year}-{cutoff_date.month:02d}"

    by_month = defaultdict(lambda: {"denominator": 0.0, "buckets": defaultdict(float)})

    for inv in invoices:
        inv_date = _to_date(inv.get("invoice_date") or inv.get("date"))
        if not inv_date:
            continue
        mk = f"{inv_date.year}-{inv_date.month:02d}"
        if mk < cutoff_mk:
            continue

        inv_type = classify(inv)
        if inv_type == "exclude":
            continue

        total   = float(inv.get("total",   0) or 0)
        balance = float(inv.get("balance", 0) or 0)

        by_month[mk]["denominator"] += total

        if inv_type in ("paid", "partial"):
            amount_paid = total if inv_type == "paid" else (total - balance)
            days        = calc_days_to_pay(inv)
            by_month[mk]["buckets"][_bucket(days)] += amount_paid

    result = []
    for mk in sorted(by_month.keys()):
        m     = by_month[mk]
        denom = m["denominator"]
        is_mtd = mk == current_mk

        cum     = 0.0
        buckets = []
        for b in BUCKET_ORDER:
            bval = m["buckets"].get(b, 0.0)
            cum += bval
            buckets.append({
                "bucket":         b,
                "amount":         round(bval, 2),
                "cumulative_pct": round(cum / denom * 100, 2) if denom else 0.0,
            })

        yr, mo = mk.split("-")
        result.append({
            "month":           mk,
            "label":           f"{MONTH_LABELS[mo]} {yr}" + (" MTD" if is_mtd else ""),
            "is_mtd":          is_mtd,
            "denominator":     round(denom, 2),
            "buckets":         buckets,
            "uncollected_pct": round(100 - (cum / denom * 100 if denom else 0), 2),
        })

    return result

# ── Outstanding by invoice cohort ─────────────────────────────────────────────

def build_outstanding_by_cohort(invoices, months=12):
    """
    For each invoice month (last 12), shows how much has been collected so far
    and what's still outstanding. Updates as payments come in over time.

    flag:
      active    < 4 months old
      at_risk   4–6 months old
      bad_debt  6+ months old
    """
    today      = date.today()
    current_mk = f"{today.year}-{today.month:02d}"

    cutoff_date = today.replace(day=1) - timedelta(days=(months - 1) * 28)
    cutoff_mk   = f"{cutoff_date.year}-{cutoff_date.month:02d}"

    by_month = defaultdict(lambda: {
        "invoiced": 0.0, "collected": 0.0, "collected_overdue": 0.0,
        "outstanding": 0.0, "written_off": 0.0,
    })

    for inv in invoices:
        inv_date = _to_date(inv.get("invoice_date") or inv.get("date"))
        if not inv_date:
            continue
        mk = f"{inv_date.year}-{inv_date.month:02d}"
        if mk < cutoff_mk:
            continue

        inv_type = classify(inv)
        if inv_type == "exclude" or inv_type == "promo":
            continue

        total   = float(inv.get("total",   0) or 0)
        balance = float(inv.get("balance", 0) or 0)

        by_month[mk]["invoiced"] += total

        if inv_type == "paid":
            by_month[mk]["collected"] += total
            days = calc_days_to_pay(inv)
            if days is not None and days >= 1:
                by_month[mk]["collected_overdue"] += total
        elif inv_type == "partial":
            amount_paid = total - balance
            by_month[mk]["collected"]  += amount_paid
            by_month[mk]["outstanding"] += balance
            days = calc_days_to_pay(inv)
            if days is not None and days >= 1:
                by_month[mk]["collected_overdue"] += amount_paid
        elif inv_type == "overdue":
            by_month[mk]["outstanding"] += balance
        elif inv_type == "write_off":
            by_month[mk]["written_off"] += total

    result = []
    for mk in sorted(by_month.keys(), reverse=True):
        m        = by_month[mk]
        invoiced = m["invoiced"]
        collected = m["collected"]
        outstanding = m["outstanding"]
        written_off = m["written_off"]
        collection_pct = round(collected / invoiced * 100, 1) if invoiced else 0.0

        yr, mo = mk.split("-")
        cohort_date = date(int(yr), int(mo), 1)
        months_old  = (today.year - cohort_date.year) * 12 + (today.month - cohort_date.month)

        if months_old >= 6:
            flag = "bad_debt"
        elif months_old >= 4:
            flag = "at_risk"
        else:
            flag = "active"

        collected_overdue = m["collected_overdue"]
        result.append({
            "month":                 mk,
            "label":                 f"{MONTH_LABELS[mo]} {yr}",
            "is_partial":            mk == current_mk,
            "invoiced":              round(invoiced, 2),
            "collected":             round(collected, 2),
            "collected_overdue":     round(collected_overdue, 2),
            "collected_overdue_pct": round(collected_overdue / collected * 100, 1) if collected else 0.0,
            "outstanding":           round(outstanding, 2),
            "written_off":           round(written_off, 2),
            "collection_pct":        collection_pct,
            "flag":                  flag,
            "months_old":            months_old,
        })

    return result

# ── Bad debt segmentation ─────────────────────────────────────────────────────

def build_bad_debt_segmentation(invoices):
    """
    Segments outstanding balance (invoices where balance > 0) by invoice age.

      active    < 4 months   — still being chased
      at_risk   4–6 months   — increasingly difficult
      bad_debt  6+ months    — write-off territory
    """
    today = date.today()
    segments = {"active": 0.0, "at_risk": 0.0, "bad_debt": 0.0}

    for inv in invoices:
        inv_type = classify(inv)
        # Only include invoices with a remaining balance (overdue + partially paid)
        if inv_type not in ("overdue", "partial"):
            continue

        inv_date = _to_date(inv.get("invoice_date") or inv.get("date"))
        if not inv_date:
            continue

        balance    = float(inv.get("balance", 0) or 0)
        months_old = (today.year - inv_date.year) * 12 + (today.month - inv_date.month)

        if months_old >= 6:
            segments["bad_debt"] += balance
        elif months_old >= 4:
            segments["at_risk"]  += balance
        else:
            segments["active"]   += balance

    total = sum(segments.values())
    return {
        "total":       round(total, 2),
        "active":      round(segments["active"],   2),
        "at_risk":     round(segments["at_risk"],  2),
        "bad_debt":    round(segments["bad_debt"], 2),
        "active_pct":   round(segments["active"]   / total * 100, 1) if total else 0.0,
        "at_risk_pct":  round(segments["at_risk"]  / total * 100, 1) if total else 0.0,
        "bad_debt_pct": round(segments["bad_debt"] / total * 100, 1) if total else 0.0,
    }

# ── Customer risk tiers ───────────────────────────────────────────────────────

def build_customer_risk_tiers(invoices):
    """
    Tiers all customers who have at least one outstanding invoice.

      Red   : 5+ overdue invoices, OR any invoice overdue > 28 days
      Amber : 3–4 overdue invoices (all <= 28 days overdue)
      Green : 1–2 overdue invoices (all <= 28 days overdue)

    'Overdue' for tier purposes = any invoice with balance > 0 (overdue + partial).
    Days overdue = days since due_date (due_date = invoice_date + 1 day).
    """
    today = date.today()
    by_customer = defaultdict(lambda: {"count": 0, "max_days_overdue": 0})

    for inv in invoices:
        inv_type = classify(inv)
        if inv_type not in ("overdue", "partial"):
            continue

        name     = (inv.get("customer_name") or inv.get("contact_name") or "Unknown").strip()
        inv_date = _to_date(inv.get("invoice_date") or inv.get("date"))
        due_date = inv_date + timedelta(days=1) if inv_date else None
        days_overdue = max((today - due_date).days, 0) if due_date else 0

        by_customer[name]["count"]           += 1
        by_customer[name]["max_days_overdue"] = max(
            by_customer[name]["max_days_overdue"], days_overdue
        )

    tiers = {"red": 0, "amber": 0, "green": 0}
    for data in by_customer.values():
        if data["count"] >= 5 or data["max_days_overdue"] > 28:
            tiers["red"]   += 1
        elif data["count"] >= 3:
            tiers["amber"] += 1
        else:
            tiers["green"] += 1

    total = sum(tiers.values())
    return {
        "total_customers": total,
        "red":             tiers["red"],
        "amber":           tiers["amber"],
        "green":           tiers["green"],
        "red_pct":         round(tiers["red"]   / total * 100, 1) if total else 0.0,
        "amber_pct":       round(tiers["amber"] / total * 100, 1) if total else 0.0,
        "green_pct":       round(tiers["green"] / total * 100, 1) if total else 0.0,
    }

# ── Summary ───────────────────────────────────────────────────────────────────

def build_summary(invoices):
    """Top-level overdue summary metrics."""
    today   = date.today()
    overdue = [i for i in invoices if classify(i) == "overdue"]

    balances  = [float(i.get("balance", 0) or 0) for i in overdue]
    total_due = sum(balances)

    days_list = []
    for inv in overdue:
        inv_date = _to_date(inv.get("invoice_date") or inv.get("date"))
        due_date = inv_date + timedelta(days=1) if inv_date else None
        if due_date:
            days_list.append(max((today - due_date).days, 0))

    unique_customers = len(set(
        (i.get("customer_name") or i.get("contact_name") or "").strip()
        for i in overdue
    ))

    return {
        "total_overdue_balance":    round(total_due, 2),
        "overdue_invoice_count":    len(overdue),
        "avg_days_overdue":         round(sum(days_list) / len(days_list), 1) if days_list else 0,
        "oldest_overdue_days":      max(days_list) if days_list else 0,
        "unique_overdue_customers": unique_customers,
    }

# ── WoW snapshot ──────────────────────────────────────────────────────────────

def snapshot_history(total_overdue, overdue_count):
    """
    Appends today's snapshot to debt_history.json and returns week-on-week change.
    History entry: {date, total_overdue_balance, overdue_count}
    """
    today_str = date.today().isoformat()

    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    # Replace today's entry if it exists, then append
    history = [h for h in history if h.get("date") != today_str]
    history.append({
        "date":                  today_str,
        "total_overdue_balance": total_overdue,
        "overdue_count":         overdue_count,
    })
    history.sort(key=lambda x: x["date"])

    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

    # Find the closest entry at or before 7 days ago
    target = (date.today() - timedelta(days=7)).isoformat()
    past   = next((h for h in reversed(history) if h["date"] <= target), None)

    if past and past["total_overdue_balance"]:
        wow_val = total_overdue - past["total_overdue_balance"]
        wow_pct = round(wow_val / past["total_overdue_balance"] * 100, 1)
        return {
            "wow_value": round(wow_val, 2),
            "wow_pct":   wow_pct,
            "vs_date":   past["date"],
        }

    return {"wow_value": None, "wow_pct": None, "vs_date": None}

# ── MTD recovered from overdue ────────────────────────────────────────────────

def build_mtd_recovered_overdue(invoices):
    """
    Revenue collected in the current calendar month where payment arrived
    1+ day after the invoice date — i.e. it went overdue before being recovered.

    Buckets by last_payment_date (not invoice date), so this answers:
    "how much did the debt team recover THIS month?"
    """
    today      = date.today()
    current_mk = f"{today.year}-{today.month:02d}"

    total_paid = 0.0
    recovered  = 0.0

    for inv in invoices:
        inv_type = classify(inv)
        if inv_type not in ("paid", "partial"):
            continue

        pay_str = (inv.get("last_payment_date") or "").strip()
        if not pay_str:
            continue

        pay_date = _to_date(pay_str)
        if not pay_date:
            continue

        # Only payments made in the current month
        if f"{pay_date.year}-{pay_date.month:02d}" != current_mk:
            continue

        total   = float(inv.get("total",   0) or 0)
        balance = float(inv.get("balance", 0) or 0)
        amount_paid = total if inv_type == "paid" else (total - balance)
        total_paid += amount_paid

        days = calc_days_to_pay(inv)
        if days is not None and days >= 1:
            recovered += amount_paid

    yr, mo = current_mk.split("-")
    return {
        "month":           current_mk,
        "month_label":     f"{MONTH_LABELS[mo]} {yr}",
        "total_paid_mtd":  round(total_paid, 2),
        "recovered":       round(recovered, 2),
        "on_time":         round(total_paid - recovered, 2),
        "recovered_pct":   round(recovered / total_paid * 100, 1) if total_paid else 0.0,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today     = date.today()
    date_13mo = (today - timedelta(days=396)).isoformat()
    today_str = today.isoformat()

    # Fetch by specific statuses for the last 13 months.
    # Write-offs (total>0, bal=0, no pay date) and promos (total=0, bal=0, no pay date)
    # come through Zoho as 'paid' (bal=0 = settled). classify() identifies them correctly.
    recent: list = []
    for status in ("paid", "sent", "partially_paid"):
        print(f"📥 Fetching {status} invoices ({date_13mo} → {today_str})...")
        batch = fetch_by_status(status, date_start=date_13mo, date_end=today_str)
        print(f"   → {len(batch):,}")
        recent.extend(batch)

    print(f"\n📥 Fetching ALL overdue invoices (no date limit)...")
    overdue_all = fetch_overdue_all()
    print(f"   → {len(overdue_all):,} overdue invoices fetched")

    # Merge and de-duplicate by invoice_number (keep first occurrence)
    print("\n🔄 De-duplicating by invoice_number...")
    seen_nums = set()
    merged    = []
    for inv in recent + overdue_all:
        num = inv.get("invoice_number") or inv.get("invoice_id")
        if num and num not in seen_nums:
            seen_nums.add(num)
            merged.append(inv)
    print(f"   → {len(merged):,} unique invoices")

    # Quick classification breakdown for visibility
    counts = defaultdict(int)
    for inv in merged:
        counts[classify(inv)] += 1
    print(f"   Classifications: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    print("\n🧮 Computing metrics...")
    summary = build_summary(merged)
    wow     = snapshot_history(summary["total_overdue_balance"], summary["overdue_invoice_count"])

    cohort_data = build_outstanding_by_cohort(merged, months=12)

    output = {
        "generated_at":          datetime.now().isoformat(timespec="seconds"),
        "summary":               {**summary, "wow": wow},
        "bad_debt_segmentation": build_bad_debt_segmentation(merged),
        "customer_risk_tiers":   build_customer_risk_tiers(merged),
        "mtd_recovered_overdue": build_mtd_recovered_overdue(merged),
        "debt_curve":            build_curve(merged, months=6),
        "outstanding_by_cohort": cohort_data,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    s   = output["summary"]
    wow = s["wow"]
    print(f"\n✅ Done → {OUTPUT_FILE}")
    print(f"   Overdue  : £{s['total_overdue_balance']:,.2f}  ({s['overdue_invoice_count']} invoices, {s['unique_overdue_customers']} customers)")
    print(f"   Avg overdue: {s['avg_days_overdue']}d · Oldest: {s['oldest_overdue_days']}d")
    if wow["wow_value"] is not None:
        sign = "+" if wow["wow_value"] >= 0 else ""
        print(f"   WoW vs {wow['vs_date']}: {sign}£{wow['wow_value']:,.2f} ({sign}{wow['wow_pct']}%)")
    else:
        print(f"   WoW: insufficient history (builds up over first week)")

    bd = output["bad_debt_segmentation"]
    print(f"\n   Debt segmentation (outstanding balance):")
    print(f"     Active   : £{bd['active']:,.2f}  ({bd['active_pct']}%)")
    print(f"     At-risk  : £{bd['at_risk']:,.2f}  ({bd['at_risk_pct']}%)")
    print(f"     Bad debt : £{bd['bad_debt']:,.2f}  ({bd['bad_debt_pct']}%)")

    rt = output["customer_risk_tiers"]
    print(f"\n   Customer risk tiers ({rt['total_customers']} customers with outstanding debt):")
    print(f"     Red   : {rt['red']}  ({rt['red_pct']}%)")
    print(f"     Amber : {rt['amber']}  ({rt['amber_pct']}%)")
    print(f"     Green : {rt['green']}  ({rt['green_pct']}%)")

if __name__ == "__main__":
    main()
