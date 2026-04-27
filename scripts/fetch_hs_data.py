#!/usr/bin/env python3
"""
AnyVan Storage — HubSpot Data Fetcher
Source: HubSpot CRM API (AVC - UK - STORAGE pipeline, ID: 694358880)
Writes to Supabase: hs_weekly_stats

Runs Monday morning to capture the completed Mon–Sun week.

Usage:
  python3 fetch_hs_data.py              # last completed week (default)
  python3 fetch_hs_data.py 2026-04-20   # specific Monday
"""

import os
import sys
import requests
from datetime import date, timedelta, datetime, timezone

SUPABASE_URL      = "[SUPABASE_URL_REMOVED]"
SUPABASE_ANON_KEY = "[SUPABASE_ANON_KEY_REMOVED]"
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

HUBSPOT_TOKEN  = os.environ["HUBSPOT_TOKEN"]
HS_PIPELINE_ID = "694358880"  # AVC - UK - STORAGE


# ─── DATE HELPERS ──────────────────────────────────────────────────────────────

def resolve_monday() -> date:
    if len(sys.argv) > 1:
        d = date.fromisoformat(sys.argv[1].strip())
        assert d.weekday() == 0, f"{d} is not a Monday"
        return d
    today = date.today()
    return today - timedelta(days=today.weekday() + 7)  # last completed Monday


def week_ms(monday: date):
    start = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    end   = start + timedelta(days=7)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ─── HUBSPOT ───────────────────────────────────────────────────────────────────

def hs_count(filters: list) -> int:
    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/deals/search",
        headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"},
        json={"filterGroups": [{"filters": filters}], "properties": ["hs_object_id"], "limit": 1},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["total"]


# ─── SUPABASE ──────────────────────────────────────────────────────────────────

def upsert(rows: list):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/hs_weekly_stats",
        headers=SUPABASE_HEADERS,
        json=rows,
        timeout=30,
    )
    r.raise_for_status()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

monday          = resolve_monday()
start_ms, end_ms = week_ms(monday)

print(f"Fetching week commencing {monday} ({start_ms} – {end_ms})")

leads_created = hs_count([
    {"propertyName": "pipeline",    "operator": "EQ",  "value": HS_PIPELINE_ID},
    {"propertyName": "createdate",  "operator": "GTE", "value": str(start_ms)},
    {"propertyName": "createdate",  "operator": "LT",  "value": str(end_ms)},
])

print(f"  leads_created : {leads_created}")

upsert([{
    "week_commencing": monday.isoformat(),
    "leads_created":   leads_created,
}])

print("Done.")
