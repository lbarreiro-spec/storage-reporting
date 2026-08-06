#!/usr/bin/env python3
"""
⚠️ DEPRECATED (2026-06-29) — DO NOT USE.
The local HUBSPOT_TOKEN it needs is not in this repo's .env (only in GitHub Actions
secrets), so this script can't run here. Total Leads is now pulled through the HubSpot
MCP and patched in — see the /StorageWeekly skill. Kept only for the lead definition /
epoch-window reference below. Run the weekly board via /StorageWeekly instead.

Automate the "Total Leads" row on the Weekly KPI board (operations/storage-weekly).

Source: HubSpot CRM API — same definition as fetch_hs_data.py's `leads_created`
  - raw count of deals in the AVC-UK-STORAGE pipeline (694358880)
  - by createdate, Monday–Sunday week (UTC)
  - NOT deduped (matches the board's hand-entered "TOTAL Leads Created")

Validated 2026-06-09 against the board's hand-entered figures and HubSpot's own
totals (reconciles within ~2%). Snowflake HUBSPOT_DEAL is NOT used — it retains
deleted/merged deals and bulk-migrated stage history, over-counting badly.

Target: patches the Supabase `weekly_board` JSON doc (id=1), Leads section,
        "Total Leads" row, per week column (matched by the week's iso Monday).

Requires HUBSPOT_TOKEN in the environment (or ~/Documents/storage-reporting/.env).
SUPABASE_URL / SUPABASE_ANON_KEY come from the same .env.

Usage:
  python3 scripts/fetch_weekly_leads.py                 # most recent COMPLETED week (appends column if missing)
  python3 scripts/fetch_weekly_leads.py --week 2026-06-01
  python3 scripts/fetch_weekly_leads.py --backfill      # every existing week, BLANK cells only
  python3 scripts/fetch_weekly_leads.py --backfill --overwrite
  python3 scripts/fetch_weekly_leads.py --dry-run
"""

import os, sys, requests
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── env (.env then ~/.anyvan/config.txt) ───────────────────────────────────
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

HUBSPOT_TOKEN = os.environ["HUBSPOT_TOKEN"]
SUPA_URL      = os.environ["SUPABASE_URL"]
SUPA_KEY      = os.environ["SUPABASE_ANON_KEY"]

TABLE       = "weekly_board"
SECTION     = "Leads"
ROW_LEADS   = "Total Leads"
PIPELINE_ID = "694358880"

MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

DRY       = "--dry-run"   in sys.argv
BACKFILL  = "--backfill"  in sys.argv
OVERWRITE = "--overwrite" in sys.argv


def arg_val(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def supa_headers(extra=None):
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def load_doc():
    r = requests.get(f"{SUPA_URL}/rest/v1/{TABLE}", headers=supa_headers(),
                     params={"id": "eq.1", "select": "doc"}, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        sys.exit("weekly_board row id=1 not found")
    return rows[0]["doc"]


def save_doc(doc):
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "The weekly board now lives in AV Dashboards state:\n"
        "  dashboards.anyvan.com/operations/storage-weekly (state object 'weekly_board')."
    )
    today = date.today().isoformat()
    doc["updated"] = today
    r = requests.patch(f"{SUPA_URL}/rest/v1/{TABLE}",
                       headers=supa_headers({"Prefer": "return=minimal"}),
                       params={"id": "eq.1"},
                       json={"doc": doc, "updated_at": today}, timeout=30)
    if r.status_code not in (200, 204):
        sys.exit(f"Supabase PATCH failed: {r.status_code} {r.text[:300]}")


def week_ms(mon: date):
    """Epoch-ms [start, end) for the Mon–Sun week beginning `mon` (UTC)."""
    start = datetime(mon.year, mon.month, mon.day, tzinfo=timezone.utc)
    end   = start + timedelta(days=7)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def leads_created(mon: date) -> int:
    start_ms, end_ms = week_ms(mon)
    filters = [
        {"propertyName": "pipeline",   "operator": "EQ",  "value": PIPELINE_ID},
        {"propertyName": "createdate", "operator": "GTE", "value": str(start_ms)},
        {"propertyName": "createdate", "operator": "LT",  "value": str(end_ms)},
    ]
    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/deals/search",
        headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"},
        json={"filterGroups": [{"filters": filters}], "properties": ["hs_object_id"], "limit": 1},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["total"]


def find_row(doc, section, name):
    for sec in doc["sections"]:
        if sec["name"] == section:
            for row in sec["rows"]:
                if row["metric"] == name:
                    return row
    sys.exit(f"Row not found: {section} / {name}")


def main():
    doc      = load_doc()
    weeks    = doc["weeks"]
    iso_list = [w["iso"] for w in weeks]
    today    = date.today()

    this_mon       = today - timedelta(days=today.weekday())
    last_completed = (this_mon - timedelta(days=7)).isoformat()

    wk = arg_val("--week")
    if BACKFILL:
        targets = list(iso_list)
    elif wk:
        targets = [wk]
    else:
        targets = [last_completed]

    if not BACKFILL:
        for iso in targets:
            if iso in iso_list:
                continue
            d = date.fromisoformat(iso)
            if iso_list and iso != (date.fromisoformat(iso_list[-1]) + timedelta(days=7)).isoformat():
                sys.exit(f"Refusing to append {iso}: not contiguous with last column {iso_list[-1]}. "
                         f"Run with --week for each missing week in order.")
            weeks.append({"label": f"{d.day} {MONTHS[d.month - 1]}", "iso": iso})
            for sec in doc["sections"]:
                for row in sec["rows"]:
                    row["values"].append("")
            iso_list.append(iso)
            print(f"  + appended week column {d.day} {MONTHS[d.month-1]} ({iso})")

    row = find_row(doc, SECTION, ROW_LEADS)

    wrote = 0
    for iso in targets:
        if iso not in iso_list:
            print(f"  ⚠ week {iso} not a column on the board — skipped")
            continue
        idx = iso_list.index(iso)
        if BACKFILL and not OVERWRITE and (row["values"][idx] or "").strip() not in ("", "—"):
            continue
        n = leads_created(date.fromisoformat(iso))
        print(f"  w/c {iso}: total_leads={n}"
              + (f"   (was {row['values'][idx]})" if row['values'][idx] else ""))
        if not DRY:
            row["values"][idx] = str(n)
        wrote += 1

    if DRY:
        print(f"\n[dry-run] {wrote} week(s) computed, nothing written.")
        return
    if wrote:
        save_doc(doc)
        print(f"\n✅ Wrote {wrote} week(s) to Supabase weekly_board.")
    else:
        print("\nNothing to write (all targets already populated).")


if __name__ == "__main__":
    main()
