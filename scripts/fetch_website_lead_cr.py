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
  genuine inbound storage WEBSITE enquiries should count.

  A website lead also doesn't get marked "won" in HubSpot when it books — the
  booking/onboarding lives in Zoho. So conversion is established by matching the
  lead's email to a Zoho qualifying booking.

SOURCE (changed 2026-06-26)
  The HubSpot lead set now comes from the managed Snowflake warehouse
  (HARMONISED.PRODUCTION.HUBSPOT_DEAL), NOT the HubSpot REST API. The old API
  path identified a website enquiry by deal name ('Storage Lead - <id>'); the
  Snowflake table carries no deal name, but it DOES carry PROPERTY_CATEGORY_NAME,
  and category='storage' with no partner reproduces that population exactly
  (validated 2026-06-26: May 2026 = 709 leads via both the API and Snowflake).
  This removes the HUBSPOT_TOKEN dependency that previously broke this job
  (the token/secret went missing on 18 Jun 2026 and froze the website-lead CR).

DEFINITION
  Denominator : unique storage WEBSITE-enquiry customers (by email),
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

Requires: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, ~/.snowflake/connections.toml (PAT),
          ZOHO_CLIENT_ID/SECRET, ZOHO_(CRM_)REFRESH_TOKEN,
          SUPABASE_URL, SUPABASE_ANON_KEY  (.env then ~/.anyvan/config.txt).
"""

import os, sys, requests
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import snowflake.connector

# ── config ──────────────────────────────────────────────────────────────────
AUTH_URL       = "https://accounts.zoho.eu/oauth/v2/token"
CRM_BASE       = "https://www.zohoapis.eu/crm/v3"
STORAGE_PIPELINE_LABEL = "AVC - UK - STORAGE"
EXCLUDE_STAGES = {"Cancel", "Prospect", "Enquiry", "Estimate sent", "Quoted by Sales"}
TABLE          = "website_lead_cr_monthly"
SNOWFLAKE_WH   = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE = "MART_SALES_OPS_GROUP"

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

SUPA_URL          = os.environ["SUPABASE_URL"]
SUPA_KEY          = os.environ["SUPABASE_ANON_KEY"]
CLIENT_ID         = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET     = os.environ["ZOHO_CLIENT_SECRET"]
REFRESH_TOKEN     = os.getenv("ZOHO_CRM_REFRESH_TOKEN") or os.environ["ZOHO_REFRESH_TOKEN"]
SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"]
SNOWFLAKE_USER    = os.environ["SNOWFLAKE_USER"]


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def norm(e):
    return (e or "").strip().lower()


def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


# ── HubSpot (via managed Snowflake warehouse, no REST API / no token) ──────────
def get_sf_token():
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    return toml.split('token = "')[1].split('"')[0]


# One row per unique (cohort-month, customer email) storage WEBSITE enquiry,
# attributed to the most-recent lead owner. category='storage' + no partner is the
# Snowflake equivalent of the old 'Storage Lead - <id>' dealname filter; the
# current-stage-in-storage-pipeline join keeps it to genuine storage deals.
WEBSITE_LEAD_SQL = """
WITH storage_stages AS (
  SELECT s.STAGE_ID
  FROM HARMONISED.PRODUCTION.HUBSPOT_DEAL_PIPELINE_STAGE s
  JOIN HARMONISED.PRODUCTION.HUBSPOT_DEAL_PIPELINE p ON s.PIPELINE_ID = p.PIPELINE_ID
  WHERE p.LABEL = %(pipeline)s
),
rd AS (
  SELECT DEAL_ID,
         LOWER(NULLIF(TRIM(PROPERTY_EMAIL), '')) AS email,
         OWNER_ID,
         PROPERTY_CREATEDATE,
         TO_CHAR(DATE_TRUNC('month', PROPERTY_CREATEDATE), 'YYYY-MM') AS month
  FROM HARMONISED.PRODUCTION.HUBSPOT_DEAL
  WHERE PROPERTY_CREATEDATE >= %(start)s
    AND PROPERTY_CREATEDATE <  %(end_excl)s
    AND LOWER(TRIM(PROPERTY_CATEGORY_NAME)) = 'storage'
    AND COALESCE(TRIM(PROPERTY_PARTNER_NAME), '') = ''
    AND PROPERTY_EMAIL IS NOT NULL AND TRIM(PROPERTY_EMAIL) <> ''
),
hist AS (
  SELECT s.DEAL_ID, s.VALUE AS STAGE_ID,
         ROW_NUMBER() OVER (PARTITION BY s.DEAL_ID ORDER BY s.DATE_ENTERED DESC) rn
  FROM HARMONISED.PRODUCTION.HUBSPOT_DEAL_STAGE s
  JOIN rd r ON s.DEAL_ID = r.DEAL_ID
),
cs AS (
  SELECT h.DEAL_ID
  FROM hist h JOIN storage_stages ss ON h.STAGE_ID = ss.STAGE_ID
  WHERE h.rn = 1
),
dedup AS (
  SELECT rd.month, rd.email, rd.OWNER_ID,
         ROW_NUMBER() OVER (PARTITION BY rd.month, rd.email
                            ORDER BY rd.PROPERTY_CREATEDATE DESC) rk
  FROM cs JOIN rd ON cs.DEAL_ID = rd.DEAL_ID
)
SELECT d.month                              AS MONTH,
       d.email                              AS EMAIL,
       TO_VARCHAR(d.OWNER_ID)               AS OWNER_ID,
       COALESCE(NULLIF(TRIM(o.FIRST_NAME || ' ' || o.LAST_NAME), ''),
                'Owner ' || TO_VARCHAR(d.OWNER_ID)) AS OWNER_NAME
FROM dedup d
LEFT JOIN HARMONISED.PRODUCTION.HUBSPOT_OWNER o ON o.OWNER_ID = d.OWNER_ID
WHERE d.rk = 1
"""


def fetch_website_leads(start: date, end_excl: date):
    """Deduped storage-website-enquiry rows in [start, end_excl) from Snowflake.

    Returns dicts: MONTH (YYYY-MM), EMAIL (lower), OWNER_ID (str|None), OWNER_NAME.
    """
    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        authenticator="programmatic_access_token",
        token=get_sf_token(),
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
    )
    try:
        cur = conn.cursor()
        cur.execute(WEBSITE_LEAD_SQL, {
            "pipeline": STORAGE_PIPELINE_LABEL,
            "start":    start.isoformat(),
            "end_excl": end_excl.isoformat(),
        })
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


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
    print(f"  Snowflake storage website-lead rows (deduped): {len(leads)}")

    bookings = fetch_booking_emails(start, today)
    print(f"  Zoho qualifying booking emails:                {len(bookings)}")

    # Rows are already unique per (month, email) from SQL — aggregate per (month, owner).
    agg = defaultdict(lambda: {"leads": 0, "converted": 0})
    owner_name_of = {}
    months_present = set()
    for r in leads:
        email = norm(r.get("EMAIL"))
        month = r.get("MONTH")
        if not email or not month:
            continue
        owner_id = "" if r.get("OWNER_ID") in (None, "") else str(r.get("OWNER_ID"))
        owner_name_of[owner_id] = r.get("OWNER_NAME") or "Unassigned"
        a = agg[(month, owner_id)]
        a["leads"] += 1
        if email in bookings:
            a["converted"] += 1
        months_present.add(month)

    owner_name_of.setdefault("", "Unassigned")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for (month, owner_id), m in sorted(agg.items()):
        rows.append({
            "month": month, "owner_id": owner_id or "",
            "owner_name": owner_name_of.get(owner_id, f"Owner {owner_id}"),
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
