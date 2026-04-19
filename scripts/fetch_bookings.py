#!/usr/bin/env python3
"""
Fetch storage bookings + entering data from Zoho CRM → Supabase.

  Bookings : count by Created_Time month  → bookings_monthly
  Entering : count by Moving_Date month   → entering_monthly

Stage exclusions (both metrics):
  Cancel, Prospect, Enquiry, Estimate sent, Quoted by Sales

Incremental run (default):
  Queries Modified_Time last 48h → re-fetches only affected months per metric
  + always re-fetches the current month for both.

Full run:
  python3 scripts/fetch_bookings.py --full
  Seeds/overwrites all months Jan 2025 → present.
"""

import os, sys, calendar, warnings, requests
warnings.filterwarnings("ignore")
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

START          = (2025, 1)
EXCLUDE_STAGES = {"Cancel", "Prospect", "Enquiry", "Estimate sent", "Quoted by Sales"}
AUTH_URL       = "https://accounts.zoho.eu/oauth/v2/token"
CRM_BASE       = "https://www.zohoapis.eu/crm/v3"
SUPA_URL       = "[SUPABASE_URL_REMOVED]"
SUPA_KEY       = "[SUPABASE_ANON_KEY_REMOVED]"
FULL_MODE      = "--full" in sys.argv


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

CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN = os.getenv("ZOHO_CRM_REFRESH_TOKEN") or os.environ["ZOHO_REFRESH_TOKEN"]

_crm_token = None


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


def crm_headers():
    return {"Authorization": f"Zoho-oauthtoken {crm_token()}"}


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
    """Paginate Deals/search for a criteria, return (total, {owner: count})."""
    deals, page = [], 1
    while True:
        resp = requests.get(f"{CRM_BASE}/Deals/search", headers=crm_headers(), params={
            "criteria": criteria, "fields": "Stage,Owner",
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
    for d in deals:
        if d.get("Stage") in EXCLUDE_STAGES:
            continue
        owner = d.get("Owner", {})
        name  = owner.get("name", "Unknown") if isinstance(owner, dict) else "Unknown"
        owner_counts[name] += 1
    return sum(owner_counts.values()), dict(owner_counts)


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


def upsert(table: str, rows: list):
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
    today       = date.today()
    current_key = f"{today.year}-{today.month:02d}"
    start_key   = f"{START[0]}-{START[1]:02d}"
    now_iso     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    all_months  = list(month_range(*START, today.year, today.month))

    if FULL_MODE:
        print("Full mode — fetching all months Jan 2025 → present")
        booked_months   = all_months
        entering_months = all_months
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
        entering_months = _to_month_list(affected_e, start_key, current_key)

    # ── Bookings ────────────────────────────────────────────────────────────
    print("\nBookings (by Created_Time)...")
    rows = []
    for year, month in booked_months:
        key           = f"{year}-{month:02d}"
        total, owners = fetch_bookings_month(year, month)
        rows.append({"label": key, "total": total, "by_owner": owners, "updated_at": now_iso})
        print(f"  {key}: {total}")
    upsert("bookings_monthly", rows)

    # ── Entering ────────────────────────────────────────────────────────────
    print("\nEntering (by Moving_Date)...")
    rows = []
    for year, month in entering_months:
        key          = f"{year}-{month:02d}"
        total, _     = fetch_entering_month(year, month)
        rows.append({"label": key, "total": total, "updated_at": now_iso})
        print(f"  {key}: {total}")
    upsert("entering_monthly", rows)

    print("\nDone!")


if __name__ == "__main__":
    main()
