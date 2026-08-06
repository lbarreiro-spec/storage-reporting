#!/usr/bin/env python3
"""
Automate the Bookings rows on the Weekly KPI board (operations/storage-weekly).

Source: Zoho CRM Deals — same definition as fetch_bookings.py
  - count by Created_Time, Monday–Sunday week (UTC, matching the monthly script)
  - exclude stages: Cancel, Prospect, Enquiry, Estimate sent, Quoted by Sales
  - Admin Booked (Storage)    = the 5 storage admins (see STORAGE_ADMINS)
  - Admin Booked (Sales Team) = all other deal owners
  - Total Sales               = storage + sales team

Target: patches the Supabase `weekly_board` JSON doc (id=1), Sales section,
        per week column (matched by the week's iso Monday date).

Usage:
  python3 scripts/fetch_weekly_bookings.py                  # most recent COMPLETED week (appends the column if missing)
  python3 scripts/fetch_weekly_bookings.py --week 2026-06-01
  python3 scripts/fetch_weekly_bookings.py --backfill       # every existing week, BLANK cells only
  python3 scripts/fetch_weekly_bookings.py --backfill --overwrite   # every week, replace existing values
  python3 scripts/fetch_weekly_bookings.py --dry-run        # compute + print, write nothing
"""

import sys, importlib.util, requests
from datetime import date, timedelta
from pathlib import Path

# Reuse Zoho logic + env loading from fetch_bookings.py (it loads .env + ~/.anyvan/config.txt at import)
_spec = importlib.util.spec_from_file_location("fb", Path(__file__).parent / "fetch_bookings.py")
fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fb)

SUPA_URL = fb.SUPA_URL
SUPA_KEY = fb.SUPA_KEY
TABLE    = "weekly_board"
SECTION  = "Sales"

STORAGE_ADMINS = {
    "Andrew Njiokwuemegi", "Michelle Jemsana", "Dylan Christian",
    "Prosper Mubata", "Carla Jacobs",
}

ROW_STORAGE = "Admin Booked (Storage)"
ROW_SALES   = "Admin Booked (Sales Team)"
ROW_TOTAL   = "Total Sales"

MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

DRY       = "--dry-run"  in sys.argv
BACKFILL  = "--backfill" in sys.argv
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


def week_split(mon: date):
    """Return (total, storage, sales_team) for the Mon–Sun week beginning `mon`."""
    sun = mon + timedelta(days=6)
    criteria = (f"(Created_Time:between:"
                f"{mon.isoformat()}T00:00:00+00:00,"
                f"{sun.isoformat()}T23:59:59+00:00)")
    total, owners, _sqft = fb._fetch_and_count(criteria)
    storage = sum(v for k, v in owners.items() if k in STORAGE_ADMINS)
    return total, storage, total - storage


def find_row(doc, name):
    for sec in doc["sections"]:
        if sec["name"] == SECTION:
            for row in sec["rows"]:
                if row["metric"] == name:
                    return row
    sys.exit(f"Row not found: {SECTION} / {name}")


def main():
    doc      = load_doc()
    weeks    = doc["weeks"]
    iso_list = [w["iso"] for w in weeks]
    today    = date.today()

    this_mon       = today - timedelta(days=today.weekday())   # Monday of current week
    last_completed = (this_mon - timedelta(days=7)).isoformat()  # most recent finished week

    # ── Resolve target week(s) ──────────────────────────────────────────────
    wk = arg_val("--week")
    if BACKFILL:
        targets = list(iso_list)
    elif wk:
        targets = [wk]
    else:
        targets = [last_completed]

    # ── Append the column if a single target week isn't on the board yet ────
    if not BACKFILL:
        for iso in targets:
            if iso in iso_list:
                continue
            d = date.fromisoformat(iso)
            if iso_list and iso != (date.fromisoformat(iso_list[-1]) + timedelta(days=7)).isoformat():
                sys.exit(f"Refusing to append {iso}: not contiguous with last column {iso_list[-1]} "
                         f"(would leave a gap). Run with --week for each missing week in order.")
            label = f"{d.day} {MONTHS[d.month - 1]}"
            weeks.append({"label": label, "iso": iso})
            for sec in doc["sections"]:
                for row in sec["rows"]:
                    row["values"].append("")
            iso_list.append(iso)
            print(f"  + appended week column {label} ({iso})")

    row_storage = find_row(doc, ROW_STORAGE)
    row_sales   = find_row(doc, ROW_SALES)
    row_total   = find_row(doc, ROW_TOTAL)

    wrote = 0
    for iso in targets:
        if iso not in iso_list:
            print(f"  ⚠ week {iso} not a column on the board — skipped")
            continue
        idx = iso_list.index(iso)

        # backfill = blanks only (unless --overwrite); single-week runs always write
        if BACKFILL and not OVERWRITE:
            existing = (row_storage["values"][idx] or "").strip()
            if existing not in ("", "—"):
                continue

        mon = date.fromisoformat(iso)
        total, storage, sales = week_split(mon)
        print(f"  w/c {iso}: total={total}  storage={storage}  sales_team={sales}"
              + (f"   (was {row_storage['values'][idx]}/{row_sales['values'][idx]})"
                 if row_storage['values'][idx] else ""))

        if not DRY:
            row_storage["values"][idx] = str(storage)
            row_sales["values"][idx]   = str(sales)
            row_total["values"][idx]   = str(total)
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
