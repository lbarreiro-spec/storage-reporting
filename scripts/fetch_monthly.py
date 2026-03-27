#!/usr/bin/env python3
"""
Fetch monthly YoY data from Google Sheet → data/monthly.json

Usage: python3 scripts/fetch_monthly.py
"""

import csv, io, json, re, sys
import requests
from datetime import date
from pathlib import Path

SHEET_ID = "1Yr8Xehprf0EaV5QZUp1iUi2ohhT-eRMSOyCUeaxL-pQ"
GID      = "1047873355"
CSV_URL  = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"

MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

# (row_index, key, label, format, years)
SECTIONS = [
    (1,   "invoice_revenue",         "Invoice Revenue",                  "money", ["2023","2024","2025","2026"]),
    (22,  "paid_cash",               "Paid Cash",                        "money", ["2023","2024","2025","2026"]),
    (43,  "transport_fee",           "Transport Fee",                    "money", ["2023","2024","2025","2026"]),
    (64,  "invoiced_plus_transport", "Invoiced + Transport",             "money", ["2023","2024","2025","2026"]),
    (86,  "paid_plus_transport",     "Paid + Transport",                 "money", ["2023","2024","2025","2026"]),
    (107, "customers_booked",        "New Customers Booked",             "int",   ["2023","2024","2025","2026"]),
    (130, "customers_entering",      "New Customers Entering",           "int",   ["2023","2024","2025","2026"]),
    (154, "customers_exiting",       "New Customers Exiting",            "int",   ["2023","2024","2025","2026"]),
    (174, "promo_pct",               "Promo % of Revenue",               "pct",   ["2024","2025","2026"]),
    (197, "total_invoices",          "Total Invoices Raised",            "int",   ["2024","2025","2026"]),
    (219, "sqft_booked",             "SqFt Booked",                     "float", ["2024","2025","2026"]),
    (242, "sqft_entering",           "SqFt Entering",                   "float", ["2024","2025","2026"]),
    (265, "sqft_exiting",            "SqFt Exiting",                    "float", ["2024","2025","2026"]),
    (285, "net_sqft",                "Net SqFt",                        "float", ["2024","2025","2026"]),
]

def parse_val(v, fmt):
    v = v.strip()
    if not v or v in ('-', '—', '#DIV/0!', '#N/A'):
        return None
    try:
        if fmt == "money":
            return round(float(re.sub(r'[£,$,\s]', '', v)), 2)
        elif fmt == "pct":
            return round(float(v.rstrip('%')), 2)
        elif fmt == "int":
            return int(float(v.replace(',', '')))
        elif fmt == "float":
            return round(float(v.replace(',', '')), 2)
    except (ValueError, TypeError):
        return None
    return None

def main():
    print("Fetching CSV from Google Sheets...")
    resp = requests.get(CSV_URL, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    rows = list(csv.reader(io.StringIO(resp.text)))

    output_metrics = []
    for row_idx, key, label, fmt, years in SECTIONS:
        # Data rows are row_idx+2 through row_idx+13 (Jan-Dec)
        series = {y: [] for y in years}
        for m in range(12):
            data_row = rows[row_idx + 2 + m]
            for col, year in enumerate(years, start=1):
                val = parse_val(data_row[col], fmt) if col < len(data_row) else None
                series[year].append(val)

        output_metrics.append({
            "key":    key,
            "label":  label,
            "format": fmt,
            "years":  years,
            "series": series,
        })
        print(f"  {label}: {years}")

    output = {
        "updated": date.today().isoformat(),
        "metrics": output_metrics,
    }

    out = Path(__file__).parent.parent / "data" / "monthly.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(output, indent=2))
    print(f"\nWritten → {out}")
    print("Done!")

if __name__ == "__main__":
    main()
