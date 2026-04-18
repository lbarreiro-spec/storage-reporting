#!/usr/bin/env python3
"""
Fetch new bookings from Zoho CRM → Supabase (bookings_monthly table).

Incremental run (default):
  - Queries deals modified in the last 48h to detect affected months
  - Re-fetches only those months + the current month
  - Fast: typically 1–3 months re-fetched

Full run:
  - python3 scripts/fetch_bookings.py --full
  - Fetches every month from Jan 2025 to present
  - Use once to seed the table, or to force a full re-sync

Booking definition: deal Created_Time month, excluding stages:
  Cancel, Prospect, Enquiry, Estimate sent, Quoted by Sales
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


def get_modified_months(since: date) -> set:
    """Return set of YYYY-MM strings for months containing deals modified since `since`."""
    start    = since.strftime("%Y-%m-%dT00:00:00+00:00")
    end      = date.today().strftime("%Y-%m-%dT23:59:59+00:00")
    criteria = f"(Modified_Time:between:{start},{end})"
    affected, page = set(), 1
    while True:
        resp = requests.get(f"{CRM_BASE}/Deals/search", headers=crm_headers(), params={
            "criteria": criteria, "fields": "Created_Time",
            "per_page": 200, "page": page,
        }, timeout=30)
        if resp.status_code == 204:
            break
        resp.raise_for_status()
        data = resp.json()
        for d in data.get("data", []):
            ct = d.get("Created_Time", "")
            if ct:
                affected.add(ct[:7])
        if not data.get("info", {}).get("more_records"):
            break
        page += 1
    return affected


def fetch_month(year: int, month: int):
    """Fetch all deals for a month. Returns (total_count, {owner_name: count})."""
    today      = date.today()
    is_current = (year == today.year and month == today.month)
    last_day   = today.day if is_current else calendar.monthrange(year, month)[1]
    criteria   = (
        f"(Created_Time:between:"
        f"{year}-{month:02d}-01T00:00:00+00:00,"
        f"{year}-{month:02d}-{last_day:02d}T23:59:59+00:00)"
    )
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


def upsert(rows: list):
    if not rows:
        return
    resp = requests.post(
        f"{SUPA_URL}/rest/v1/bookings_monthly",
        headers=supa_headers(),
        json=rows,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"  ❌ Supabase error: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
    print(f"  ✅ Upserted {len(rows)} row(s) → bookings_monthly")


def month_range(sy, sm, ey, em):
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m = 1 if m == 12 else m + 1
        if m == 1:
            y += 1


def main():
    today       = date.today()
    current_key = f"{today.year}-{today.month:02d}"
    start_key   = f"{START[0]}-{START[1]:02d}"
    now_iso     = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if FULL_MODE:
        print("Full mode — fetching all months Jan 2025 → present")
        months_to_fetch = list(month_range(*START, today.year, today.month))
    else:
        since    = today - timedelta(days=2)
        print(f"Incremental mode — checking for deal modifications since {since}...")
        affected = get_modified_months(since)
        affected = {m for m in affected if start_key <= m <= current_key}
        affected.add(current_key)

        historical = sorted(affected - {current_key})
        if historical:
            print(f"  Modified historical months: {historical}")
        else:
            print(f"  No historical modifications detected")

        months_to_fetch = [(int(m[:4]), int(m[5:])) for m in sorted(affected)]

    rows = []
    for year, month in months_to_fetch:
        key            = f"{year}-{month:02d}"
        total, by_owner = fetch_month(year, month)
        rows.append({
            "label":      key,
            "total":      total,
            "by_owner":   by_owner,
            "updated_at": now_iso,
        })
        print(f"  {key}: {total} bookings")

    upsert(rows)
    print("Done!")


if __name__ == "__main__":
    main()
