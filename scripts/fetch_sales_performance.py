#!/usr/bin/env python3
"""
Sales Performance — per day, per person, from Zoho CRM Deals → Supabase.

Feeds the operations/storage-sales-performance dashboard.

For each qualifying deal (booking) we record, bucketed by (Created_Time date, Owner):
  - bookings     count of deals
  - sqft         sum of Estimated_sq_ft1            (NOTE: the real field is *_sq_ft1,
                                                      not Estimated_sq_ft used elsewhere)
  - forecast_rev sum of  Length_of_Storage(weeks) * Agreed_price_per_week * 1.2  (inc-VAT)
  - app_yes      count where Did_the_customer_take_APP == 'Yes'
  - sum_price/n_price    Agreed_price_per_week, for an average agreed price/week
  - sum_tenure/n_tenure  Length_of_Storage (weeks), for an average tenure

Booking definition matches fetch_bookings.py:
  count by Created_Time, exclude stages
  {Cancel, Prospect, Enquiry, Estimate sent, Quoted by Sales}.

Window (re-computed and rewritten each run — delete-then-insert so cancellations
and stage moves correctly DECREMENT a day):
  default   : 1st of LAST month -> today  (keeps current + previous month for the
              daily matrix month-selector)
  --full    : 2025-01-01 -> today
  --since YYYY-MM-DD : custom start
  --dry-run : compute + print, write nothing

Usage:
  python3 scripts/fetch_sales_performance.py
  python3 scripts/fetch_sales_performance.py --full
  python3 scripts/fetch_sales_performance.py --since 2026-04-01
  python3 scripts/fetch_sales_performance.py --dry-run
"""

import os, sys, warnings, requests
warnings.filterwarnings("ignore")
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

AUTH_URL       = "https://accounts.zoho.eu/oauth/v2/token"
CRM_BASE       = "https://www.zohoapis.eu/crm/v3"
VAT            = 1.2
EXCLUDE_STAGES = {"Cancel", "Prospect", "Enquiry", "Estimate sent", "Quoted by Sales"}
TABLE          = "sales_performance_daily"

DRY   = "--dry-run" in sys.argv
FULL  = "--full"    in sys.argv


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

SUPA_URL      = os.environ["SUPABASE_URL"]
SUPA_KEY      = os.environ["SUPABASE_ANON_KEY"]
CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN = os.getenv("ZOHO_CRM_REFRESH_TOKEN") or os.environ["ZOHO_REFRESH_TOKEN"]

_token = None


def crm_token():
    global _token
    if _token:
        return _token
    r = requests.post(AUTH_URL, params={
        "refresh_token": REFRESH_TOKEN, "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET, "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    _token = r.json()["access_token"]
    return _token


def crm_headers():
    return {"Authorization": f"Zoho-oauthtoken {crm_token()}"}


def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


FIELDS = ("Owner,Stage,Created_Time,Length_of_Storage,Agreed_price_per_week,"
          "Estimated_sq_ft1,Did_the_customer_take_APP")


def fetch_deals(start: date, end: date):
    """Paginate Deals/search by Created_Time between start..end (inclusive)."""
    crit = (f"(Created_Time:between:"
            f"{start.isoformat()}T00:00:00+00:00,"
            f"{end.isoformat()}T23:59:59+00:00)")
    deals, page = [], 1
    while True:
        r = requests.get(f"{CRM_BASE}/Deals/search", headers=crm_headers(), params={
            "criteria": crit, "fields": FIELDS, "per_page": 200, "page": page,
        }, timeout=30)
        if r.status_code == 204:
            break
        r.raise_for_status()
        data = r.json()
        deals.extend(data.get("data", []))
        if not data.get("info", {}).get("more_records"):
            break
        page += 1
    return deals


def aggregate(deals):
    """-> { (day_str, owner): {metrics} }  for qualifying deals only."""
    agg = defaultdict(lambda: {
        "bookings": 0, "sqft": 0.0, "forecast_rev": 0.0, "app_yes": 0,
        "sum_price": 0.0, "n_price": 0, "sum_tenure": 0.0, "n_tenure": 0,
    })
    for d in deals:
        if d.get("Stage") in EXCLUDE_STAGES:
            continue
        ct = d.get("Created_Time", "")
        if not ct:
            continue
        day = ct[:10]                       # YYYY-MM-DD (Zoho returns +TZ; date part is local-ish, fine)
        owner = d.get("Owner") or {}
        name = owner.get("name", "Unknown") if isinstance(owner, dict) else "Unknown"
        a = agg[(day, name)]

        a["bookings"] += 1

        sqft = _num(d.get("Estimated_sq_ft1"))
        if sqft:
            a["sqft"] += sqft

        price  = _num(d.get("Agreed_price_per_week"))
        weeks  = _num(d.get("Length_of_Storage"))
        if price is not None:
            a["sum_price"] += price
            a["n_price"]   += 1
        if weeks is not None:
            a["sum_tenure"] += weeks
            a["n_tenure"]   += 1
        if price is not None and weeks is not None:
            a["forecast_rev"] += weeks * price * VAT

        if (d.get("Did_the_customer_take_APP") or "") == "Yes":
            a["app_yes"] += 1
    return agg


def rewrite_window(start: date, rows):
    """Delete every row from `start` onwards, then bulk-insert the fresh set."""
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "Sales performance now reads live from Snowflake via the managed query\n"
        "storage_sales_by_owner_v1 on dashboards.anyvan.com/operations/storage-sales-performance."
    )
    # delete window
    dr = requests.delete(
        f"{SUPA_URL}/rest/v1/{TABLE}",
        headers=supa_headers({"Prefer": "return=minimal"}),
        params={"day": f"gte.{start.isoformat()}"}, timeout=30)
    if dr.status_code not in (200, 204):
        sys.exit(f"Supabase DELETE failed: {dr.status_code} {dr.text[:300]}")
    if not rows:
        print("  (no rows to insert)")
        return
    # insert in chunks
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        ir = requests.post(f"{SUPA_URL}/rest/v1/{TABLE}",
                           headers=supa_headers({"Prefer": "return=minimal"}),
                           json=chunk, timeout=60)
        if ir.status_code not in (200, 201, 204):
            sys.exit(f"Supabase INSERT failed: {ir.status_code} {ir.text[:300]}")
    print(f"  ✅ Rewrote {len(rows)} row(s) → {TABLE} (from {start.isoformat()})")


def main():
    today = date.today()
    if FULL:
        start = date(2025, 1, 1)
    elif _arg("--since"):
        start = date.fromisoformat(_arg("--since"))
    else:
        # 1st of last month
        ly, lm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = date(ly, lm, 1)

    print(f"Fetching Zoho deals created {start} → {today} ...")
    deals = fetch_deals(start, today)
    print(f"  {len(deals)} deals fetched")

    agg = aggregate(deals)
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for (day, owner), m in sorted(agg.items()):
        rows.append({
            "day": day, "owner": owner,
            "bookings": m["bookings"],
            "sqft": round(m["sqft"], 2),
            "forecast_rev": round(m["forecast_rev"], 2),
            "app_yes": m["app_yes"],
            "sum_price": round(m["sum_price"], 2), "n_price": m["n_price"],
            "sum_tenure": round(m["sum_tenure"], 2), "n_tenure": m["n_tenure"],
            "updated_at": now_iso,
        })

    tot_b = sum(r["bookings"] for r in rows)
    tot_f = sum(r["forecast_rev"] for r in rows)
    tot_s = sum(r["sqft"] for r in rows)
    print(f"  {len(rows)} (day,owner) rows | {tot_b} bookings | "
          f"£{tot_f:,.0f} forecast (inc-VAT) | {tot_s:,.0f} sq ft")

    if DRY:
        for r in rows[:15]:
            print(f"    {r['day']} | {r['owner'][:22]:22} | b={r['bookings']} "
                  f"sqft={r['sqft']:.0f} fc=£{r['forecast_rev']:,.0f} app={r['app_yes']}")
        print(f"\n[dry-run] {len(rows)} rows computed, nothing written.")
        return

    rewrite_window(start, rows)
    print("Done!")


if __name__ == "__main__":
    main()
