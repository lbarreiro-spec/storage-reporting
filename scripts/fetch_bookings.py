#!/usr/bin/env python3
"""
Fetch storage bookings, entering, and APP data from Zoho CRM + Books → Supabase.

  Bookings : count by Created_Time month        → bookings_monthly
  Entering : count by Moving_Date month         → entering_monthly
  APP      : CRM count (Did_the_customer_take_APP=Yes) by Created_Time month
             + Books "AnyVan Protection Plus Cover" revenue for current month
                                                → app_monthly

Stage exclusions (all CRM metrics):
  Cancel, Prospect, Enquiry, Estimate sent, Quoted by Sales

Incremental run (default):
  Queries Modified_Time last 48h → re-fetches only affected months per metric
  + always re-fetches the current month for all metrics.

Full run:
  python3 scripts/fetch_bookings.py --full
  Seeds/overwrites all months Jan 2025 → present.

Finalise a completed month (after the calendar has rolled over):
  python3 scripts/fetch_bookings.py --month 2026-07
  Treats that month as "current" so bookings, entering and APP are all re-fetched
  for it — a normal run only ever refreshes the real current month, so a just-ended
  month otherwise keeps whatever partial figures its last in-month run captured.
"""

import os, re, sys, calendar, warnings, requests, time
warnings.filterwarnings("ignore")
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

START          = (2025, 1)
FUTURE_MONTHS  = 3          # how many months ahead to show in the entering chart
EXCLUDE_STAGES = {"Cancel", "Prospect", "Enquiry", "Estimate sent", "Quoted by Sales"}
AUTH_URL       = "https://accounts.zoho.eu/oauth/v2/token"
CRM_BASE       = "https://www.zohoapis.eu/crm/v3"
BOOKS_BASE     = "https://www.zohoapis.eu/books/v3"
FULL_MODE      = "--full" in sys.argv


def _resolve_month_arg():
    """Return the last day of the month given by `--month YYYY-MM`, else None.

    Everything in main() keys off `today`, so pinning `today` to a completed month's
    last day makes that month the "current" month again and every metric — including
    APP, which only ever fetches the current month — gets recomputed for it.
    """
    vals = [sys.argv[i + 1] for i, a in enumerate(sys.argv)
            if a == "--month" and i + 1 < len(sys.argv)]
    vals += [a.split("=", 1)[1] for a in sys.argv if a.startswith("--month=")]
    if not vals:
        return None
    val = vals[0]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", val):
        sys.exit(f"❌ --month expects YYYY-MM, got {val!r}")
    year, month = int(val[:4]), int(val[5:7])
    end = date(year, month, calendar.monthrange(year, month)[1])
    if end >= date.today():
        sys.exit(f"❌ {val} is not a completed month (ends {end}) — just run normally.")
    return end


MONTH_END = _resolve_month_arg()


def _load_kvfile(path):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_kvfile(Path(__file__).parent.parent / ".env")
_load_kvfile(Path.home() / ".anyvan" / "config.txt")

BOOKS_ORG_ID   = os.environ.get("ZOHO_ORG_ID") or os.environ["ZOHO_BOOKS_ORG_ID"]
SUPA_URL       = os.environ["SUPABASE_URL"]
SUPA_KEY       = os.environ["SUPABASE_ANON_KEY"]

CLIENT_ID          = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET      = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN      = os.getenv("ZOHO_CRM_REFRESH_TOKEN") or os.environ["ZOHO_REFRESH_TOKEN"]
BOOKS_REFRESH_TOKEN = os.getenv("ZOHO_BOOKS_REFRESH_TOKEN") or REFRESH_TOKEN

_crm_token   = None
_books_token = None


def crm_token():
    global _crm_token
    if _crm_token:
        return _crm_token
    r = requests.post(AUTH_URL, params={
        "refresh_token": REFRESH_TOKEN, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    _crm_token = r.json()["access_token"]
    return _crm_token


def books_token():
    global _books_token
    if _books_token:
        return _books_token
    r = requests.post(AUTH_URL, params={
        "refresh_token": BOOKS_REFRESH_TOKEN, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    _books_token = r.json()["access_token"]
    return _books_token


def crm_headers():
    return {"Authorization": f"Zoho-oauthtoken {crm_token()}"}


def books_headers():
    return {"Authorization": f"Zoho-oauthtoken {books_token()}"}


def supa_headers():
    return {
        "apikey":        SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }


def get_modified_months(since: date):
    """
    Returns (booked_months, entering_months) — sets of YYYY-MM strings
    for months that had deal modifications since `since`.
    Fetches both Created_Time and Moving_Date so each metric gets its
    own affected month set.
    """
    start    = since.strftime("%Y-%m-%dT00:00:00+00:00")
    end      = date.today().strftime("%Y-%m-%dT23:59:59+00:00")
    criteria = f"(Modified_Time:between:{start},{end})"
    booked, entering = set(), set()
    page = 1
    while True:
        resp = requests.get(f"{CRM_BASE}/Deals/search", headers=crm_headers(), params={
            "criteria": criteria, "fields": "Created_Time,Moving_Date",
            "per_page": 200, "page": page,
        }, timeout=30)
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        data = resp.json()
        for d in data.get("data", []):
            ct = d.get("Created_Time", "")
            md = d.get("Moving_Date", "")
            if ct:
                booked.add(ct[:7])
            if md and len(md) >= 7:
                entering.add(md[:7])
        if not data.get("info", {}).get("more_records"):
            break
        page += 1
    return booked, entering


def _fetch_and_count(criteria: str):
    """Paginate Deals/search for a criteria, return (total, {owner: count}, sqft)."""
    deals, page = [], 1
    while True:
        resp = requests.get(f"{CRM_BASE}/Deals/search", headers=crm_headers(), params={
            "criteria": criteria, "fields": "Stage,Owner,Estimated_sq_ft",
            "per_page": 200, "page": page,
        }, timeout=30)
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        data = resp.json()
        deals.extend(data.get("data", []))
        if not data.get("info", {}).get("more_records"):
            break
        page += 1

    owner_counts = defaultdict(int)
    total_sqft   = 0
    for d in deals:
        if d.get("Stage") in EXCLUDE_STAGES:
            continue
        owner = d.get("Owner", {})
        name  = owner.get("name", "Unknown") if isinstance(owner, dict) else "Unknown"
        owner_counts[name] += 1
        total_sqft += int(d.get("Estimated_sq_ft") or 0)
    return sum(owner_counts.values()), dict(owner_counts), total_sqft


def fetch_bookings_month(year: int, month: int):
    """Bookings: filter by Created_Time, cap current month at today."""
    today    = date.today()
    last_day = today.day if (year == today.year and month == today.month) \
               else calendar.monthrange(year, month)[1]
    criteria = (
        f"(Created_Time:between:"
        f"{year}-{month:02d}-01T00:00:00+00:00,"
        f"{year}-{month:02d}-{last_day:02d}T23:59:59+00:00)"
    )
    return _fetch_and_count(criteria)


def fetch_entering_month(year: int, month: int):
    """Entering: filter by Moving_Date, always use full month (future dates included)."""
    last_day = calendar.monthrange(year, month)[1]
    criteria = (
        f"(Moving_Date:between:"
        f"{year}-{month:02d}-01,"
        f"{year}-{month:02d}-{last_day:02d})"
    )
    return _fetch_and_count(criteria)


def fetch_app_crm_month(year: int, month: int):
    """APP CRM count: deals with Did_the_customer_take_APP=Yes, by Created_Time month."""
    today    = date.today()
    last_day = today.day if (year == today.year and month == today.month) \
               else calendar.monthrange(year, month)[1]
    criteria = (
        f"(Created_Time:between:"
        f"{year}-{month:02d}-01T00:00:00+00:00,"
        f"{year}-{month:02d}-{last_day:02d}T23:59:59+00:00)"
        f"and(Did_the_customer_take_APP:equals:Yes)"
    )
    deals, page = [], 1
    while True:
        resp = requests.get(f"{CRM_BASE}/Deals/search", headers=crm_headers(), params={
            "criteria": criteria, "fields": "Stage",
            "per_page": 200, "page": page,
        }, timeout=30)
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        data = resp.json()
        deals.extend(data.get("data", []))
        if not data.get("info", {}).get("more_records"):
            break
        page += 1
    return sum(1 for d in deals if d.get("Stage") not in EXCLUDE_STAGES)


def fetch_app_books_revenue(year: int, month: int):
    """Scan Books invoices for the month, sum AnyVan Protection Plus Cover lines (ex-VAT)."""
    last_day  = calendar.monthrange(year, month)[1]
    date_from = f"{year}-{month:02d}-01"
    date_to   = f"{year}-{month:02d}-{last_day:02d}"

    invoice_ids, page = [], 1
    while True:
        for attempt in range(4):
            try:
                resp = requests.get(f"{BOOKS_BASE}/invoices", headers=books_headers(), params={
                    "organization_id": BOOKS_ORG_ID,
                    "date_start": f"{year}-{month:02d}-01",
                    "date_end":   f"{year}-{month:02d}-{last_day:02d}",
                    "per_page": 200, "page": page,
                }, timeout=30)
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        for inv in data.get("invoices", []):
            invoice_ids.append(inv["invoice_id"])
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1

    if not invoice_ids:
        return 0.0

    total_inc_vat = 0.0

    def _fetch_detail(inv_id):
        for attempt in range(4):
            try:
                r = requests.get(f"{BOOKS_BASE}/invoices/{inv_id}", headers=books_headers(), params={
                    "organization_id": BOOKS_ORG_ID,
                }, timeout=30)
            except (requests.ConnectionError, requests.Timeout):
                # Retry rather than propagate: this runs inside a thread pool, so a
                # single dropped connection on one of ~1k invoices used to raise out
                # of fut.result() and abort the entire run after all prior work.
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json().get("invoice", {}).get("line_items", [])
        return []

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_fetch_detail, i): i for i in invoice_ids}
        for fut in as_completed(futures):
            for li in fut.result():
                name = li.get("name", "") or li.get("description", "")
                if "AnyVan Protection Plus" in name:
                    total_inc_vat += float(li.get("item_total", 0) or 0)

    return round(total_inc_vat / 1.2, 2)


def upsert(table: str, rows: list):
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "Storage reporting now reads live from Snowflake via AV Dashboards managed\n"
        "queries, and hand-entered data lives in AV Dashboards state.\n"
        "See dashboards.anyvan.com/operations/storage-nav-config."
    )
    if not rows:
        return
    resp = requests.post(
        f"{SUPA_URL}/rest/v1/{table}",
        headers=supa_headers(),
        json=rows,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  ❌ Supabase {table}: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
    print(f"  ✅ Upserted {len(rows)} row(s) → {table}")


def month_range(sy, sm, ey, em):
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m = 1 if m == 12 else m + 1
        if m == 1:
            y += 1


def _to_month_list(month_set, start_key, current_key):
    filtered = {m for m in month_set if start_key <= m <= current_key}
    filtered.add(current_key)
    return [(int(m[:4]), int(m[5:])) for m in sorted(filtered)]


def main():
    today       = MONTH_END or date.today()
    if MONTH_END:
        print(f"Finalising completed month {today:%Y-%m} (anchored on {today})")
    current_key = f"{today.year}-{today.month:02d}"
    start_key   = f"{START[0]}-{START[1]:02d}"
    now_iso     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    all_months  = list(month_range(*START, today.year, today.month))

    # Future months: extend entering FUTURE_MONTHS ahead (customers with upcoming Moving_Dates)
    ey, em = today.year, today.month + FUTURE_MONTHS
    ey    += (em - 1) // 12
    em     = ((em - 1) % 12) + 1
    future_entering = [(y, m) for y, m in month_range(today.year, today.month, ey, em)
                       if f"{y}-{m:02d}" > current_key]

    if FULL_MODE:
        print(f"Full mode — fetching all months Jan 2025 → present + {FUTURE_MONTHS} future months for entering")
        booked_months   = all_months
        entering_months = list(month_range(*START, ey, em))
    else:
        since = today - timedelta(days=2)
        print(f"Incremental mode — checking modifications since {since}...")
        affected_b, affected_e = get_modified_months(since)

        historical_b = sorted({m for m in affected_b if start_key <= m < current_key})
        historical_e = sorted({m for m in affected_e if start_key <= m < current_key})
        if historical_b: print(f"  Booking months affected:  {historical_b}")
        if historical_e: print(f"  Entering months affected: {historical_e}")
        if not historical_b and not historical_e:
            print("  No historical modifications detected")

        booked_months   = _to_month_list(affected_b, start_key, current_key)
        # Always refresh future months — new bookings arrive daily with future Moving_Dates
        entering_months = sorted(set(_to_month_list(affected_e, start_key, current_key)) | set(future_entering))

    # ── Bookings ────────────────────────────────────────────────────────────
    print("\nBookings (by Created_Time)...")
    rows = []
    for year, month in booked_months:
        key                  = f"{year}-{month:02d}"
        total, owners, sqft  = fetch_bookings_month(year, month)
        rows.append({"label": key, "total": total, "by_owner": owners, "sqft": sqft, "updated_at": now_iso})
        print(f"  {key}: {total} bookings, {sqft:,} sq ft")
    upsert("bookings_monthly", rows)

    # ── Entering ────────────────────────────────────────────────────────────
    print("\nEntering (by Moving_Date)...")
    rows = []
    for year, month in entering_months:
        key              = f"{year}-{month:02d}"
        total, _, sqft   = fetch_entering_month(year, month)
        rows.append({"label": key, "total": total, "sqft": sqft, "updated_at": now_iso})
        print(f"  {key}: {total} entering, {sqft:,} sq ft")
    upsert("entering_monthly", rows)

    # ── APP ─────────────────────────────────────────────────────────────────
    # APP only went live Apr 2026 — only ever fetch current month
    print("\nAPP (Did_the_customer_take_APP=Yes + Books revenue)...")
    crm_count = fetch_app_crm_month(today.year, today.month)
    revenue   = fetch_app_books_revenue(today.year, today.month)
    print(f"  {current_key}: CRM={crm_count}  Books=£{revenue}")
    upsert("app_monthly", [{"label": current_key, "crm_count": crm_count,
                            "books_revenue": revenue, "updated_at": now_iso}])

    print("\nDone!")


if __name__ == "__main__":
    main()
