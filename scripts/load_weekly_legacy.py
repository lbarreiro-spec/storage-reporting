#!/usr/bin/env python3
"""
Load legacy weekly KPI data from Google Sheet into Supabase.
One-time script to seed historical data (Jan 2025 → present).

Sheet: https://docs.google.com/spreadsheets/d/1Yr8Xehprf0EaV5QZUp1iUi2ohhT-eRMSOyCUeaxL-pQ
Tab GID: 1195893543

Usage: python3 scripts/load_weekly_legacy.py
"""

import csv, io, re, sys
import requests
from datetime import date, timedelta

SHEET_ID = "1Yr8Xehprf0EaV5QZUp1iUi2ohhT-eRMSOyCUeaxL-pQ"
GID      = "1195893543"
CSV_URL  = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"

SUPABASE_URL      = "[SUPABASE_URL_REMOVED]"
SUPABASE_ANON_KEY = "[SUPABASE_ANON_KEY_REMOVED]"
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

# Column 4 in the CSV (data index 0) = week commencing 6 Jan 2025
WEEK_0 = date(2025, 1, 6)

def week_date(i):
    return (WEEK_0 + timedelta(weeks=i)).isoformat()

def parse_money(v):
    if not v or v.strip() in ('', '-', '-%', '--%', '—'):
        return None
    try:
        return round(float(re.sub(r'[£,\s]', '', v.strip())), 2)
    except ValueError:
        return None

def parse_int(v):
    if not v or v.strip() in ('', '-', '—'):
        return None
    try:
        return int(float(v.replace(',', '').strip()))
    except ValueError:
        return None

def parse_pct(v):
    """Store as plain number: 90.07 for 90.07%"""
    if not v or v.strip() in ('', '-', '—', '#DIV/0!'):
        return None
    try:
        return round(float(v.strip().rstrip('%')), 2)
    except ValueError:
        return None

def parse_float(v):
    if not v or v.strip() in ('', '-', '—'):
        return None
    try:
        return round(float(v.strip()), 2)
    except ValueError:
        return None

# Metric name in sheet → (supabase column, parser)
TARGET_METRICS = {
    "Invoice Revenue":               ("invoice_revenue",    parse_money),
    "Cash Collected (Invoices)":     ("cash_collected",     parse_money),
    "Transport Net Rev (In and Out)":("transport_net_rev",  parse_money),
    "TOTAL Storage Sales":           ("total_sales",        parse_int),
    "TOTAL Leads Created":           ("total_leads",        parse_int),
    "BAU Leads  (Storage website)":  ("bau_leads",          parse_int),
    "Total Number of inbound calls": ("inbound_calls",      parse_int),
    "Answer Rate":                   ("answer_rate",        parse_pct),
    "Total tickets raised":          ("tickets_raised",     parse_int),
    "Total Tickets Resolved":        ("tickets_resolved",   parse_int),
    "Storage CSAT":                  ("csat",               parse_float),
}

def main():
    print("Fetching CSV from Google Sheets...")
    resp = requests.get(CSV_URL, allow_redirects=True, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    rows   = list(reader)
    if not rows:
        print("Empty response — is the sheet publicly accessible?")
        sys.exit(1)

    # Columns 0-3: category, team, owner, metric. Columns 4+ are weekly data.
    num_data_cols = len(rows[0]) - 4
    print(f"  {num_data_cols} week columns found (weeks {week_date(0)} → {week_date(num_data_cols - 1)})")

    weekly = [{} for _ in range(num_data_cols)]

    for row in rows[1:]:
        if len(row) < 4:
            continue
        metric = row[3].strip()
        if metric not in TARGET_METRICS:
            continue
        col_key, parser = TARGET_METRICS[metric]
        for i in range(num_data_cols):
            raw = row[4 + i] if (4 + i) < len(row) else ''
            val = parser(raw)
            if val is not None:
                weekly[i][col_key] = val

    # All rows must have identical keys for Supabase batch upsert
    all_cols = ["invoice_revenue","cash_collected","transport_net_rev","total_sales",
                "total_leads","bau_leads","inbound_calls","answer_rate",
                "tickets_raised","tickets_resolved","csat"]

    upsert_rows = []
    for i, data in enumerate(weekly):
        if not data:
            continue
        row = {"week_commencing": week_date(i)}
        for col in all_cols:
            row[col] = data.get(col)  # None for missing values
        upsert_rows.append(row)

    print(f"  {len(upsert_rows)} weeks with data to upsert")

    # Supabase max payload ~1MB — batch in chunks of 100 to be safe
    BATCH = 100
    total = 0
    for i in range(0, len(upsert_rows), BATCH):
        chunk = upsert_rows[i:i + BATCH]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/weekly_kpi",
            headers=SUPABASE_HEADERS,
            json=chunk,
        )
        if r.status_code not in (200, 201):
            print(f"  Error {r.status_code}: {r.text[:400]}")
            r.raise_for_status()
        total += len(chunk)
        print(f"  Upserted {total}/{len(upsert_rows)} rows...")

    print("Done!")

if __name__ == "__main__":
    main()
