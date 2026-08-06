#!/usr/bin/env python3
"""
Generic patcher for the Weekly KPI board (operations/storage-weekly), Supabase `weekly_board`.

Used by the /StorageWeekly skill: each data source computes its rows, then calls this to
write metric→value into a given week column of the JSON doc. Appends the week column if it
doesn't exist yet (label "D Mon", iso = Monday). Matches rows by metric name across all
sections (errors if a name is ambiguous). Idempotent — re-running overwrites the same cells.

Usage:
  # set values for a week (iso = the Monday of the week)
  python3 patch_weekly_board.py --week 2026-06-15 --set '{"Total Leads": 1017, "Total Sales": 130}'
  python3 patch_weekly_board.py --week 2026-06-15 --set-file /tmp/vals.json
  python3 patch_weekly_board.py --week 2026-06-15 --set '{...}' --dry-run
  python3 patch_weekly_board.py --week 2026-06-15 --set '{...}' --overwrite   # overwrite non-empty cells too

By default only BLANK cells are filled (safe). Pass --overwrite to replace existing values.
Values: numbers are written as-is (stringified); pre-formatted strings ("£1,234", "12.3%") pass through.
"""

import sys
sys.exit(
    "Supabase has been retired (Aug 2026). This script is disabled.\n"
    "The weekly board now lives in AV Dashboards state:\n"
    "  dashboards.anyvan.com/operations/storage-weekly (state object 'weekly_board').\n"
)

import argparse, json, sys, urllib.request
from datetime import date, datetime

SUPA = 'https://loyvicdabsncwhssjkxy.supabase.co/rest/v1/weekly_board'
KEY  = 'sb_publishable_QWQOo0CWifid4QyyRzRYvQ_P5DP8ecZ'
H = {'apikey': KEY, 'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'}
MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

def fetch():
    req = urllib.request.Request(SUPA + '?id=eq.1&select=doc,updated_at', headers=H)
    return json.load(urllib.request.urlopen(req))[0]

def save(doc, updated_at):
    payload = json.dumps({'id': 1, 'doc': doc, 'updated_at': updated_at}).encode()
    req = urllib.request.Request(SUPA + '?id=eq.1', data=payload,
                                 headers={**H, 'Prefer': 'return=minimal'}, method='PATCH')
    urllib.request.urlopen(req)

def col_index(doc, iso):
    """Return the column index for the week with this iso Monday; append the column if missing."""
    for i, w in enumerate(doc['weeks']):
        if w.get('iso') == iso:
            return i
    d = datetime.strptime(iso, '%Y-%m-%d').date()
    label = f"{d.day} {MONTHS[d.month-1]}"
    doc['weeks'].append({'label': label, 'iso': iso})
    for s in doc['sections']:
        for r in s['rows']:
            r['values'].append('')
    return len(doc['weeks']) - 1

def find_rows(doc, metric):
    return [(s['name'], r) for s in doc['sections'] for r in s['rows'] if r['metric'] == metric]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--week', required=True, help='Monday iso date of the target week, YYYY-MM-DD')
    ap.add_argument('--set', help='JSON object {metric: value}')
    ap.add_argument('--set-file', help='path to a JSON file {metric: value}')
    ap.add_argument('--overwrite', action='store_true', help='replace non-empty cells too')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    vals = {}
    if a.set: vals.update(json.loads(a.set))
    if a.set_file: vals.update(json.load(open(a.set_file)))
    if not vals:
        sys.exit('nothing to set (use --set or --set-file)')

    row = fetch(); doc = row['doc']
    ci = col_index(doc, a.week)
    wk_label = doc['weeks'][ci]['label']

    applied, skipped, missing, ambiguous = [], [], [], []
    for metric, value in vals.items():
        matches = find_rows(doc, metric)
        if not matches:
            missing.append(metric); continue
        if len(matches) > 1:
            ambiguous.append(metric); continue
        _, r = matches[0]
        cur = r['values'][ci] if ci < len(r['values']) else ''
        sval = '' if value is None else str(value)
        if cur not in ('', None) and not a.overwrite:
            skipped.append(f"{metric} (has {cur!r})"); continue
        r['values'][ci] = sval
        applied.append(f"{metric} = {sval}")

    print(f"Week {wk_label} ({a.week}), col #{ci}")
    for m in applied:   print("  set    ", m)
    for m in skipped:   print("  skip   ", m)
    for m in missing:   print("  MISSING", m)
    for m in ambiguous: print("  AMBIG  ", m)

    if a.dry_run:
        print("\nDRY RUN — not saved."); return
    if not applied:
        print("\nNothing applied — not saved."); return
    today = date.today().isoformat()
    doc['updated'] = today
    save(doc, today)
    print(f"\nSAVED {len(applied)} cell(s).")

if __name__ == '__main__':
    main()
