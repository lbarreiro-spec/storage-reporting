#!/usr/bin/env python3
"""
Website Lead Conversion Rate — the TRUE storage-website-lead CR.

Feeds the "Website Lead Conversion" section of the
operations/storage-sales-performance dashboard.

WHY THIS EXISTS
  The board's old CR divided Zoho bookings by *all* HubSpot leads in the
  AVC-UK-STORAGE pipeline (694358880). But that pipeline is ~80% non-website
  noise: 'Removals -' / 'Furniture -' transport jobs, bought 'Pinlocal Lead' /
  'Stashbee Lead' partner lead-gen, 'Competitor Storage -' scrapes. Only the
  'Storage Lead - <id>' deals are genuine inbound storage WEBSITE enquiries.

  A website lead also doesn't get marked "won" in HubSpot when it books — the
  booking/onboarding lives in Zoho. So conversion is established by matching the
  lead's email to a Zoho qualifying booking.

DEFINITION
  Denominator : unique 'Storage Lead' website-enquiry customers (by email),
                bucketed into the cohort month of the HubSpot createdate,
                attributed to the HubSpot lead OWNER.
  Numerator   : those whose email appears on a Zoho qualifying booking
                (deal Created within the window, excl Cancel / Prospect /
                Enquiry / Estimate sent / Quoted by Sales).
  CR          : numerator / denominator.

  NOTE this is a maturing cohort: a month's leads keep converting after month
  end, so recent months read LOW until they age. Matching is cumulative across
  the whole window so older cohorts climb on each run.

WINDOW (delete-then-insert each run, so re-runs correct earlier months):
  default            : 1st of LAST month -> today (current + previous cohort)
  --full             : 2025-01-01 -> today
  --since YYYY-MM-DD : custom start
  --dry-run          : compute + print, write nothing

Requires: HUBSPOT_TOKEN, ZOHO_CLIENT_ID/SECRET, ZOHO_(CRM_)REFRESH_TOKEN,
          SUPABASE_URL, SUPABASE_ANON_KEY  (.env then ~/.anyvan/config.txt).
"""

import os, sys, requests
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── config ──────────────────────────────────────────────────────────────────
AUTH_URL       = "https://accounts.zoho.eu/oauth/v2/token"
CRM_BASE       = "https://www.zohoapis.eu/crm/v3"
HS_SEARCH      = "https://api.hubapi.com/crm/v3/objects/deals/search"
HS_OWNERS      = "https://api.hubapi.com/crm/v3/owners"
HS_PIPELINE_ID = "694358880"            # AVC - UK - STORAGE
LEAD_NAME_TOKEN = "Storage Lead"        # 'Storage Lead - <id>' = website enquiry
EXCLUDE_STAGES = {"Cancel", "Prospect", "Enquiry", "Estimate sent", "Quoted by Sales"}
TABLE          = "website_lead_cr_monthly"

DRY  = "--dry-run" in sys.argv
FULL = "--full"    in sys.argv


def _load_kvfile(path):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_kvfile(Path(__file__).parent.parent / ".env")
_load_kvfile(Path.home() / ".anyvan" / "config.txt")

SUPA_URL      = os.environ["SUPABASE_URL"]
SUPA_KEY      = os.environ["SUPABASE_ANON_KEY"]
HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN = os.getenv("ZOHO_CRM_REFRESH_TOKEN") or os.environ["ZOHO_REFRESH_TOKEN"]


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def norm(e):
    return (e or "").strip().lower()


def ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


# ── HubSpot ───────────────────────────────────────────────────────────────────
def hs_headers():
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def fetch_website_leads(start: date, end_excl: date):
    """All 'Storage Lead' deals created in [start, end_excl). Most-recent first."""
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": "pipeline",   "operator": "EQ",  "value": HS_PIPELINE_ID},
            {"propertyName": "createdate", "operator": "GTE", "value": str(ms(start))},
            {"propertyName": "createdate", "operator": "LT",  "value": str(ms(end_excl))},
            {"propertyName": "dealname",   "operator": "CONTAINS_TOKEN", "value": LEAD_NAME_TOKEN},
        ]}],
        "properties": ["email", "hubspot_owner_id", "createdate"],
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
        "limit": 200,
    }
    out, after = [], None
    while True:
        if after:
            body["after"] = after
        r = requests.post(HS_SEARCH, headers=hs_headers(), json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return out


def fetch_owner_names(owner_ids):
    """id -> 'First Last' for the owner ids we saw (paginate the owners list)."""
    names, after = {}, None
    while True:
        params = {"limit": 500}
        if after:
            params["after"] = after
        r = requests.get(HS_OWNERS, headers=hs_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for o in data.get("results", []):
            full = " ".join(x for x in [o.get("firstName"), o.get("lastName")] if x).strip()
            names[str(o.get("id"))] = full or (o.get("email") or f"Owner {o.get('id')}")
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {oid: names.get(oid, f"Owner {oid}") for oid in owner_ids}


# ── Zoho ────────────────────────────────────────────────────────────────────
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


def fetch_booking_emails(start: date, end: date):
    """Lower-cased emails of Zoho qualifying bookings created start..end inclusive."""
    crit = (f"(Created_Time:between:{start.isoformat()}T00:00:00+00:00,"
            f"{end.isoformat()}T23:59:59+00:00)")
    emails, page = set(), 1
    while True:
        r = requests.get(f"{CRM_BASE}/Deals/search",
                         headers={"Authorization": f"Zoho-oauthtoken {crm_token()}"},
                         params={"criteria": crit, "fields": "Email,Stage,Created_Time",
                                 "per_page": 200, "page": page}, timeout=30)
        if r.status_code == 204:
            break
        r.raise_for_status()
        data = r.json()
        for d in data.get("data", []):
            if d.get("Stage") in EXCLUDE_STAGES:
                continue
            e = norm(d.get("Email"))
            if e:
                emails.add(e)
        if not data.get("info", {}).get("more_records"):
            break
        page += 1
    return emails


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    today = date.today()
    if FULL:
        start = date(2025, 1, 1)
    elif _arg("--since"):
        start = date.fromisoformat(_arg("--since"))
    else:
        ly, lm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = date(ly, lm, 1)
    end_excl = today + timedelta(days=1)

    print(f"Window: leads created {start} → {today} (cohort by createdate month)")

    leads = fetch_website_leads(start, end_excl)
    print(f"  HubSpot 'Storage Lead' deal rows: {len(leads)}")

    bookings = fetch_booking_emails(start, today)
    print(f"  Zoho qualifying booking emails:   {len(bookings)}")

    # Dedupe to unique customer per cohort month (most-recent owner wins — list is DESC).
    seen = set()                                  # (month, email)
    owner_of = {}                                 # (month, email) -> owner_id
    months_present = set()
    for d in leads:
        p = d.get("properties", {})
        email = norm(p.get("email"))
        cd = p.get("createdate") or ""
        if not email or not cd:
            continue
        try:
            month = datetime.fromtimestamp(int(cd) / 1000, tz=timezone.utc).strftime("%Y-%m")
        except (ValueError, TypeError):
            month = cd[:7]
        key = (month, email)
        if key in seen:
            continue
        seen.add(key)
        owner_of[key] = str(p.get("hubspot_owner_id") or "")
        months_present.add(month)

    # Aggregate per (month, owner).
    agg = defaultdict(lambda: {"leads": 0, "converted": 0})
    for (month, email), owner_id in owner_of.items():
        a = agg[(month, owner_id)]
        a["leads"] += 1
        if email in bookings:
            a["converted"] += 1

    owner_ids = {oid for (_m, oid) in agg.keys() if oid}
    names = fetch_owner_names(owner_ids)
    names[""] = "Unassigned"

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for (month, owner_id), m in sorted(agg.items()):
        rows.append({
            "month": month, "owner_id": owner_id or "",
            "owner_name": names.get(owner_id, f"Owner {owner_id}"),
            "leads": m["leads"], "converted": m["converted"], "updated_at": now_iso,
        })

    # Per-month team summary for the log.
    for month in sorted(months_present):
        mr = [r for r in rows if r["month"] == month]
        L = sum(r["leads"] for r in mr)
        C = sum(r["converted"] for r in mr)
        print(f"  {month}: {C}/{L} = {0 if not L else round(C/L*100,1)}% team CR "
              f"({len(mr)} owners)")
    if DRY:
        for r in sorted(rows, key=lambda r: (-r["leads"]))[:12]:
            cr = 0 if not r["leads"] else round(r["converted"] / r["leads"] * 100, 1)
            print(f"    {r['month']} | {r['owner_name'][:22]:22} | "
                  f"{r['converted']:>3}/{r['leads']:<3} = {cr}%")
        print(f"\n[dry-run] {len(rows)} rows computed, nothing written.")
        return

    # delete-then-insert the cohort months in the window
    start_month = start.strftime("%Y-%m")
    dr = requests.delete(f"{SUPA_URL}/rest/v1/{TABLE}",
                         headers=supa_headers({"Prefer": "return=minimal"}),
                         params={"month": f"gte.{start_month}"}, timeout=30)
    if dr.status_code not in (200, 204):
        sys.exit(f"Supabase DELETE failed: {dr.status_code} {dr.text[:300]}")
    for i in range(0, len(rows), 500):
        ir = requests.post(f"{SUPA_URL}/rest/v1/{TABLE}",
                           headers=supa_headers({"Prefer": "return=minimal"}),
                           json=rows[i:i + 500], timeout=60)
        if ir.status_code not in (200, 201, 204):
            sys.exit(f"Supabase INSERT failed: {ir.status_code} {ir.text[:300]}")
    print(f"  ✅ Wrote {len(rows)} row(s) → {TABLE} (months ≥ {start_month})")
    print("Done!")


if __name__ == "__main__":
    main()
