#!/usr/bin/env python3
"""
AnyVan Storage — HubSpot Data Fetcher
Source: HubSpot CRM API (AVC - UK - STORAGE pipeline, ID: 694358880)
Writes to Supabase: hs_weekly_stats

Week definition: Saturday 00:00 Europe/London → Friday 23:59 Europe/London

Usage:
  python3 fetch_hs_data.py              # last completed Sat–Fri week
  python3 fetch_hs_data.py 2026-04-18   # specific Saturday
"""

import os
import sys
import requests
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")

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
HS_NEW_STAGE   = "1015548829"  # New Storage Lead


# ─── DATE HELPERS ──────────────────────────────────────────────────────────────

def resolve_saturday() -> date:
    if len(sys.argv) > 1:
        d = date.fromisoformat(sys.argv[1].strip())
        assert d.weekday() == 5, f"{d} is not a Saturday"
        return d
    today = date.today()
    # days_since_saturday: Mon=2, Tue=3, ... Sat=7→0 via (weekday+2)%7
    days_back = (today.weekday() + 2) % 7
    if days_back == 0:
        days_back = 7  # if today is Saturday, go back to last Saturday
    last_sat = today - timedelta(days=days_back)
    # ensure the full Sat–Fri week has completed (i.e. today is at least the Saturday after)
    next_sat = last_sat + timedelta(days=7)
    if today < next_sat:
        last_sat -= timedelta(days=7)
    return last_sat


def week_ms(saturday: date):
    start = datetime(saturday.year, saturday.month, saturday.day, 0, 0, 0, tzinfo=UK_TZ)
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

saturday         = resolve_saturday()
start_ms, end_ms = week_ms(saturday)

print(f"Fetching week {saturday} – {saturday + timedelta(days=6)} ({start_ms} – {end_ms})")

base_filters = [
    {"propertyName": "pipeline",   "operator": "EQ",  "value": HS_PIPELINE_ID},
    {"propertyName": "createdate", "operator": "GTE", "value": str(start_ms)},
    {"propertyName": "createdate", "operator": "LT",  "value": str(end_ms)},
]

leads_created      = hs_count(base_filters)
leads_in_new_stage = hs_count(base_filters + [{"propertyName": "dealstage", "operator": "EQ", "value": HS_NEW_STAGE}])

print(f"  leads_created      : {leads_created}")
print(f"  leads_in_new_stage : {leads_in_new_stage}")

upsert([{
    "week_commencing":    saturday.isoformat(),
    "leads_created":      leads_created,
    "leads_in_new_stage": leads_in_new_stage,
}])

print("Done.")
