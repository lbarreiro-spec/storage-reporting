#!/usr/bin/env python3
"""
Fetch weekly KPI data from Google Sheet → data/weekly.json
All metrics as rows, weeks as columns, from w/c 3 Nov 2025 onwards.

Usage: python3 scripts/fetch_weekly.py
"""

import csv, io, json, sys
import requests
from datetime import date, timedelta
from pathlib import Path

SHEET_ID    = "1Yr8Xehprf0EaV5QZUp1iUi2ohhT-eRMSOyCUeaxL-pQ"
GID         = "1195893543"
CSV_URL     = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
WEEK_0      = date(2025, 1, 6)   # data column index 0 = 6 Jan 2025
START_IDX   = 43                  # w/c 3 Nov 2025

MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

def idx_to_date(i):
    d = WEEK_0 + timedelta(weeks=i)
    return d, f"{d.day} {MONTHS[d.month-1]}", d.isoformat()

def clean(v):
    v = v.strip()
    return '' if v in ('', '-', '-%', '--%', '—', '#DIV/0!') else v

# (csv_metric_name, display_label, section)
ROWS = [
    # Website
    ("Traffic",                                        "Traffic",                    "Website"),
    ("Form views",                                     "Form views",                 "Website"),
    ("Form view CR",                                   "Form view CR",               "Website"),
    ("Form CR%",                                       "Form CR%",                   "Website"),
    # Leads
    ("TOTAL Leads Created",                            "Total Leads",                "Leads"),
    ("De duplicated Lead Volumes",                     "De-duped Leads",             "Leads"),
    ("BAU Leads  (Storage website)",                   "BAU Leads (website)",        "Leads"),
    ("Pre-listing (REM Campaign)",                     "Pre-listing (REM)",          "Leads"),
    ("Pre-listing (FURN Campaign)",                    "Pre-listing (FURN)",         "Leads"),
    ("Lead Gen",                                       "Lead Gen",                   "Leads"),
    ("Listing Conversion (Campaign)",                  "Listing Conversion",         "Leads"),
    ("% of leads still in prospecting new",            "% Prospecting",              "Leads"),
    ("Attempt 1 email Open Rate",                      "Email 1 — Open Rate",        "Leads"),
    ("Attempt 1 email reply rate",                     "Email 1 — Reply Rate",       "Leads"),
    ("Attempt 3 email Open Rate",                      "Email 3 — Open Rate",        "Leads"),
    ("Attempt 3 email reply rate",                     "Email 3 — Reply Rate",       "Leads"),
    # Sales
    ("TOTAL Storage Sales",                            "Total Sales",                "Sales"),
    ("Total Admin Booked (Storage)",                   "Admin Booked (Storage)",     "Sales"),
    ("Conversion - Sales / Overall lead count",        "Conv. Rate",                 "Sales"),
    ("Total Admin Booked (Sales - Non Storage Staff)", "Admin Booked (Sales Team)",  "Sales"),
    ("Sales Team Contribution (% of overall sales)",   "Sales Team %",               "Sales"),
    ("Transport Net Rev (In and Out)",                 "Transport Net Rev",          "Sales"),
    ("Invoice Revenue",                                "Invoice Revenue",            "Sales"),
    ("Invoice Revenue - YoY Variance",                 "Invoice Rev — YoY",          "Sales"),
    ("Invoice QTY",                                    "Invoice QTY",                "Sales"),
    ("Invoice QTY YOY",                                "Invoice QTY — YoY",          "Sales"),
    ("Cash Collected (Invoices)",                      "Cash Collected",             "Sales"),
    ("Cash Collected - YoY Variance",                  "Cash Collected — YoY",       "Sales"),
    ("Storage Promotion",                              "Promotions",                 "Sales"),
    ("Storage Promotion as % of overall invoices",     "Promo %",                    "Sales"),
    ("Average Margin of New Bookings",                 "Avg Margin",                 "Sales"),
    # BAU
    ("Storage CSAT",                                   "CSAT /5",                    "BAU"),
    ("Total Number of inbound calls",                  "Inbound Calls",              "BAU"),
    ("Total Number of answered calls",                 "Answered Calls",             "BAU"),
    ("Answer Rate",                                    "Answer Rate",                "BAU"),
    ("Total tickets raised",                           "Tickets Raised",             "BAU"),
    ("Total Tickets Resolved",                         "Tickets Resolved",           "BAU"),
    ("Outstanding tickets",                            "Outstanding Tickets",         "BAU"),
    ("% of Tickets outstanding",                       "% Outstanding",              "BAU"),
    # Zoho
    ("Customer onboarded",                             "Customers Onboarded",        "Zoho"),
    ("Total outstanding customers not striped",        "Not Striped",                "Zoho"),
    ("Total outstanding customers not e-signed",       "Not E-Signed",               "Zoho"),
    ("Total of Refunds/Write off",                     "Refunds / Write-off",        "Zoho"),
]

def main():
    print("Fetching CSV from Google Sheets...")
    resp = requests.get(CSV_URL, allow_redirects=True, timeout=30)
    resp.raise_for_status()

    raw_rows = list(csv.reader(io.StringIO(resp.text)))
    if not raw_rows:
        print("Empty response"); sys.exit(1)

    num_data_cols = len(raw_rows[0]) - 4
    today = date.today()

    # Build week list from START_IDX up to today (don't include future weeks)
    weeks = []
    for i in range(START_IDX, num_data_cols):
        d, label, iso = idx_to_date(i)
        if d > today:
            break
        weeks.append({"label": label, "iso": iso, "idx": i})

    print(f"  {len(weeks)} weeks ({weeks[0]['label']} → {weeks[-1]['label']})")

    # Build metric lookup by exact CSV name
    metric_lookup = {}
    for row in raw_rows[1:]:
        if len(row) >= 4:
            metric_lookup[row[3].strip()] = row

    # Build sections
    section_order = []
    section_rows  = {}
    for csv_name, display, section in ROWS:
        if csv_name not in metric_lookup:
            continue
        row = metric_lookup[csv_name]
        values = []
        for w in weeks:
            col = 4 + w['idx']
            values.append(clean(row[col]) if col < len(row) else '')

        if section not in section_rows:
            section_order.append(section)
            section_rows[section] = []
        section_rows[section].append({"metric": display, "values": values})

    output = {
        "updated": today.isoformat(),
        "weeks":   [{"label": w["label"], "iso": w["iso"]} for w in weeks],
        "sections": [{"name": s, "rows": section_rows[s]} for s in section_order],
    }

    out = Path(__file__).parent.parent / "data" / "weekly.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(output, indent=2))

    total_rows = sum(len(s["rows"]) for s in output["sections"])
    print(f"  {total_rows} metric rows written → {out}")
    print("Done!")

if __name__ == "__main__":
    main()
