#!/usr/bin/env python3
"""
AnyVan Storage — HubSpot Data Fetcher
Source: HubSpot CRM API (AVC - UK - STORAGE pipeline, ID: 694358880)
Writes to Supabase: hs_weekly_stats, hs_agent_weekly_stats

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

_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

HUBSPOT_TOKEN  = os.environ["HUBSPOT_TOKEN"]
HS_PIPELINE_ID = "694358880"  # AVC - UK - STORAGE
HS_NEW_STAGE   = "1015548829"  # New Storage Lead

AGENTS = {
    "Dylan":    "641005848",
    "Andy":     "77534533",
    "Prosper":  "77841901",
    "Carla":    "425207042",
    "Michelle": "77344590",
}


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

def upsert_team(rows: list):
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "Storage reporting now reads live from Snowflake via AV Dashboards managed\n"
        "queries, and hand-entered data lives in AV Dashboards state.\n"
        "See dashboards.anyvan.com/operations/storage-nav-config."
    )
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/hs_weekly_stats",
        headers=SUPABASE_HEADERS,
        json=rows,
        timeout=30,
    )
    r.raise_for_status()


def upsert_agents(rows: list):
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "Storage reporting now reads live from Snowflake via AV Dashboards managed\n"
        "queries, and hand-entered data lives in AV Dashboards state.\n"
        "See dashboards.anyvan.com/operations/storage-nav-config."
    )
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/hs_agent_weekly_stats",
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

# Team totals
leads_created      = hs_count(base_filters)
leads_in_new_stage = hs_count(base_filters + [{"propertyName": "dealstage", "operator": "EQ", "value": HS_NEW_STAGE}])

print(f"  leads_created      : {leads_created}")
print(f"  leads_in_new_stage : {leads_in_new_stage}")

upsert_team([{
    "week_commencing":    saturday.isoformat(),
    "leads_created":      leads_created,
    "leads_in_new_stage": leads_in_new_stage,
}])

# Per-agent breakdown
agent_rows = []
named_lc_total = 0
named_lns_total = 0

for name, owner_id in AGENTS.items():
    agent_filters = base_filters + [{"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id}]
    lc  = hs_count(agent_filters)
    lns = hs_count(agent_filters + [{"propertyName": "dealstage", "operator": "EQ", "value": HS_NEW_STAGE}])
    named_lc_total  += lc
    named_lns_total += lns
    agent_rows.append({
        "week_commencing":    saturday.isoformat(),
        "agent":              name,
        "leads_created":      lc,
        "leads_in_new_stage": lns,
    })
    print(f"  {name:<12}: lc={lc}, lns={lns}")

# Other = team total minus named agents
other_lc  = leads_created      - named_lc_total
other_lns = leads_in_new_stage - named_lns_total
agent_rows.append({
    "week_commencing":    saturday.isoformat(),
    "agent":              "Other",
    "leads_created":      other_lc,
    "leads_in_new_stage": other_lns,
})
print(f"  {'Other':<12}: lc={other_lc}, lns={other_lns}")

upsert_agents(agent_rows)

print("Done.")
