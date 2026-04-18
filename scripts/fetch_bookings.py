#!/usr/bin/env python3
"""
Fetch new bookings from Zoho CRM, month by month.
Counts deals by Created_Time, excluding: Cancel, Prospect, Enquiry,
Estimate sent, Quoted by Sales.
Saves → data/bookings.json

Usage: python3 scripts/fetch_bookings.py
"""

import json, os, calendar, requests
from collections import defaultdict
from datetime import date
from pathlib import Path

START = (2025, 1)
EXCLUDE_STAGES = {"Cancel", "Prospect", "Enquiry", "Estimate sent", "Quoted by Sales"}
AUTH_URL = "https://accounts.zoho.eu/oauth/v2/token"
API_BASE = "https://www.zohoapis.eu/crm/v3"


def _load_kvfile(path):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


# .env written by GitHub Actions; config.txt used locally
_load_kvfile(Path(__file__).parent.parent / ".env")
_load_kvfile(Path.home() / ".anyvan" / "config.txt")

CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN = os.getenv("ZOHO_CRM_REFRESH_TOKEN") or os.environ["ZOHO_REFRESH_TOKEN"]


def get_token():
    r = requests.post(AUTH_URL, params={
        "refresh_token": REFRESH_TOKEN, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_month(token, year, month):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    today = date.today()
    is_current = (year == today.year and month == today.month)
    last_day = today.day if is_current else calendar.monthrange(year, month)[1]
    criteria = (
        f"(Created_Time:between:"
        f"{year}-{month:02d}-01T00:00:00+00:00,"
        f"{year}-{month:02d}-{last_day:02d}T23:59:59+00:00)"
    )
    deals, page = [], 1
    while True:
        resp = requests.get(f"{API_BASE}/Deals/search", headers=headers, params={
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
    return deals


def month_range(sy, sm, ey, em):
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m = 1 if m == 12 else m + 1
        if m == 1:
            y += 1


def main():
    print("Fetching Zoho CRM bookings...")
    token = get_token()
    today = date.today()
    months = list(month_range(*START, today.year, today.month))

    total    = {}
    by_owner = defaultdict(dict)

    for year, month in months:
        key   = f"{year}-{month:02d}"
        deals = fetch_month(token, year, month)
        owner_counts = defaultdict(int)
        for d in deals:
            if d.get("Stage") in EXCLUDE_STAGES:
                continue
            owner = d.get("Owner", {})
            name  = owner.get("name", "Unknown") if isinstance(owner, dict) else "Unknown"
            owner_counts[name] += 1

        count        = sum(owner_counts.values())
        total[key]   = count
        for name, c in owner_counts.items():
            by_owner[name][key] = c
        print(f"  {key}: {count}")

    month_keys = [f"{y}-{m:02d}" for y, m in months]
    for name in by_owner:
        for k in month_keys:
            by_owner[name].setdefault(k, 0)

    out = Path(__file__).parent.parent / "data" / "bookings.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "updated": today.isoformat(),
        "months":   month_keys,
        "total":    total,
        "by_owner": dict(by_owner),
    }, indent=2))
    print(f"\nWritten → {out}")


if __name__ == "__main__":
    main()
