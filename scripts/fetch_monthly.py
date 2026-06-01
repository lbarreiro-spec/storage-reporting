#!/usr/bin/env python3
"""
Fetch monthly YoY data from Google Sheet → data/monthly.json
For any months where the sheet has no value, falls back to Supabase
(mtd_monthly + mtd_transport_monthly) so the report stays current even
when the sheet hasn't been updated yet.

Usage: python3 scripts/fetch_monthly.py
"""

import csv, io, json, os, re
import requests
from datetime import date
from pathlib import Path

SHEET_ID = "1Yr8Xehprf0EaV5QZUp1iUi2ohhT-eRMSOyCUeaxL-pQ"
GID      = "1047873355"
CSV_URL  = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"

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
}

MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

# (row_index, key, label, format, years)
SECTIONS = [
    (1,   "invoice_revenue",         "Invoice Revenue",        "money", ["2023","2024","2025","2026"]),
    (22,  "paid_cash",               "Paid Cash",              "money", ["2023","2024","2025","2026"]),
    (43,  "transport_fee",           "Transport Fee",          "money", ["2023","2024","2025","2026"]),
    (64,  "invoiced_plus_transport", "Invoiced + Transport",   "money", ["2023","2024","2025","2026"]),
    (86,  "paid_plus_transport",     "Paid + Transport",       "money", ["2023","2024","2025","2026"]),
    (107, "customers_booked",        "New Customers Booked",   "int",   ["2023","2024","2025","2026"]),
    (130, "customers_entering",      "New Customers Entering", "int",   ["2023","2024","2025","2026"]),
    (154, "customers_exiting",       "New Customers Exiting",  "int",   ["2023","2024","2025","2026"]),
    (174, "promo_pct",               "Promo % of Revenue",     "pct",   ["2024","2025","2026"]),
    (197, "total_invoices",          "Total Invoices Raised",  "int",   ["2024","2025","2026"]),
    (219, "sqft_booked",             "SqFt Booked",            "float", ["2024","2025","2026"]),
    (242, "sqft_entering",           "SqFt Entering",          "float", ["2024","2025","2026"]),
    (265, "sqft_exiting",            "SqFt Exiting",           "float", ["2024","2025","2026"]),
    (285, "net_sqft",                "Net SqFt",               "float", ["2024","2025","2026"]),
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

def cast_val(raw, fmt):
    if raw is None:
        return None
    try:
        if fmt == "money":
            return round(float(raw), 2)
        elif fmt == "pct":
            return round(float(raw), 2)
        elif fmt == "int":
            return int(raw)
        elif fmt == "float":
            return round(float(raw), 2)
    except (ValueError, TypeError):
        return None

def fetch_supabase_monthly():
    """
    Returns a dict keyed by 'YYYY-MM' → metric values from Supabase.
    All revenue values are ex-VAT (divided by 1.2).
    Books invoiced/paid and Snowflake transport all come in INC VAT.
    """
    print("Fetching Supabase fallback data...")
    monthly_resp   = requests.get(f"{SUPABASE_URL}/rest/v1/mtd_monthly?select=*",
                                  headers=SUPABASE_HEADERS, timeout=20)
    transport_resp = requests.get(f"{SUPABASE_URL}/rest/v1/mtd_transport_monthly?select=*",
                                  headers=SUPABASE_HEADERS, timeout=20)

    monthly_rows   = monthly_resp.json()   if monthly_resp.ok   else []
    transport_rows = transport_resp.json() if transport_resp.ok else []
    transport_by   = {r["label"]: r for r in transport_rows}

    result = {}
    for row in monthly_rows:
        lbl  = row["label"]
        inv  = round(row["invoiced_revenue"] / 1.2, 2) if row.get("invoiced_revenue") is not None else None
        paid = round(row["paid_revenue"]     / 1.2, 2) if row.get("paid_revenue")     is not None else None
        t    = transport_by.get(lbl, {})
        coll_ex  = round(t["coll_av_fee"]  / 1.2, 2) if t.get("coll_av_fee")  is not None else None
        redel_ex = round(t["redel_av_fee"] / 1.2, 2) if t.get("redel_av_fee") is not None else None
        tr = round(coll_ex + redel_ex, 2) if coll_ex is not None and redel_ex is not None else None

        sqft_in  = row.get("sqft_entering")
        sqft_out = row.get("sqft_exiting")
        net      = round(sqft_in - sqft_out, 2) if sqft_in is not None and sqft_out is not None else None

        result[lbl] = {
            "invoice_revenue":         inv,
            "paid_cash":               paid,
            "transport_fee":           tr,
            "transport_collection":    coll_ex,
            "transport_redelivery":    redel_ex,
            "invoiced_plus_transport": round(inv + tr, 2) if inv is not None and tr is not None else None,
            "paid_plus_transport":     round(paid + tr, 2) if paid is not None and tr is not None else None,
            "customers_booked":        row.get("customers_booked"),
            "customers_entering":      row.get("customers_entering"),
            "customers_exiting":       row.get("customers_exiting"),
            "promo_pct":               row.get("promo_pct"),
            "total_invoices":          row.get("invoice_count"),
            "sqft_booked":             row.get("sqft_booked"),
            "sqft_entering":           sqft_in,
            "sqft_exiting":            sqft_out,
            "net_sqft":                net,
            "gross_profit":            row.get("gross_profit"),
        }

    print(f"   Supabase: {len(result)} months available as fallback")
    return result


def main():
    print("Fetching CSV from Google Sheets...")
    resp = requests.get(CSV_URL, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    rows = list(csv.reader(io.StringIO(resp.text)))

    supabase_data = fetch_supabase_monthly()

    # Only show complete months — current in-progress month is excluded
    today = date.today()
    first_of_month = today.replace(day=1)
    last_complete = (first_of_month - __import__('datetime').timedelta(days=1)).strftime("%Y-%m")
    current_month = last_complete

    output_metrics = []

    # Gross Profit: embedded in Invoice Revenue section cols 14-16 = 2024-2026
    gp_years  = ["2024", "2025", "2026"]
    gp_series = {y: [] for y in gp_years}
    for m in range(12):
        data_row = rows[4 + m]
        for col, year in zip([14, 15, 16], gp_years):
            month_key = f"{year}-{str(m + 1).zfill(2)}"
            val = parse_val(data_row[col], "money") if col < len(data_row) else None
            if month_key > current_month:
                val = None
            # Sheet has no value — try Supabase fallback (gross_profit column)
            if val is None and month_key <= last_complete:
                raw = supabase_data.get(month_key, {}).get("gross_profit")
                val = cast_val(raw, "money")
            gp_series[year].append(val)
    output_metrics.append({
        "key": "gross_profit", "label": "Gross Profit", "format": "money",
        "years": gp_years, "series": gp_series,
    })
    print("  Gross Profit: ['2024', '2025', '2026']")

    for row_idx, key, label, fmt, years in SECTIONS:
        series = {y: [] for y in years}
        for m in range(12):
            data_row = rows[row_idx + 2 + m]
            for col, year in enumerate(years, start=1):
                month_key = f"{year}-{str(m + 1).zfill(2)}"
                if month_key > current_month:
                    series[year].append(None)
                    continue

                val = parse_val(data_row[col], fmt) if col < len(data_row) else None

                # Sheet has no value — try Supabase fallback
                if val is None:
                    raw = supabase_data.get(month_key, {}).get(key)
                    val = cast_val(raw, fmt)

                series[year].append(val)

        output_metrics.append({
            "key": key, "label": label, "format": fmt,
            "years": years, "series": series,
        })
        print(f"  {label}: {years}")

    # NB: the Transport — Collection / Transport — Redelivery sub-metrics were
    # previously appended here but were removed from the Monthly Trends view
    # (top-line Transport Fee already covers the combined number). Leaving the
    # cast_val plumbing above so the values still flow into the supabase row,
    # but they're no longer published as their own metrics block.

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
