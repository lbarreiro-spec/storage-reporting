#!/usr/bin/env python3
"""
Weekly YoY review — W16 2026 vs W16 2025
W16 2026: 2026-04-13 to 2026-04-19
W16 2025: 2025-04-14 to 2025-04-20
"""

import os, sys, time, warnings, requests
from datetime import date
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
_repo_root  = os.path.join(os.path.dirname(__file__), '..')
_local_env  = os.path.join(os.path.dirname(__file__), '..', '..', 'anyvan-kpi', '.env')
load_dotenv(os.path.join(_repo_root, '.env'))
if not os.getenv("ZOHO_CLIENT_ID"):
    load_dotenv(_local_env)

ZOHO_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_ORG_ID        = os.getenv("ZOHO_BOOKS_ORG_ID")
ZOHO_REGION        = os.getenv("ZOHO_REGION", "eu")
BOOKS_BASE = f"https://www.zohoapis.{ZOHO_REGION}/books/v3"
TOKEN_URL  = f"https://accounts.zoho.{ZOHO_REGION}/oauth/v2/token"
STATUSES   = ["sent", "draft", "overdue", "paid", "void", "unpaid"]

CY_START = date(2026, 4, 13)
CY_END   = date(2026, 4, 19)
PY_START = date(2025, 4, 14)
PY_END   = date(2025, 4, 20)

SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"]
SNOWFLAKE_USER    = os.environ["SNOWFLAKE_USER"]
SNOWFLAKE_WH      = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE    = "MART_SALES_OPS_GROUP"


def get_zoho_token():
    resp = requests.post(TOKEN_URL, params={
        "refresh_token": ZOHO_REFRESH_TOKEN,
        "client_id":     ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho token refresh failed: {data}")
    return data["access_token"]


def fetch_invoices(token, start, end, label=""):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    all_invoices, seen_ids = [], set()
    print(f"  Fetching {label} ({start} → {end})...")
    for status in STATUSES:
        page = 1
        while True:
            params = {
                "organization_id": ZOHO_ORG_ID,
                "status": status,
                "per_page": 200,
                "page": page,
                "sort_column": "date",
                "sort_order": "A",
                "date_start": start.isoformat(),
                "date_end": end.isoformat(),
            }
            resp = None
            for attempt in range(3):
                try:
                    resp = requests.get(f"{BOOKS_BASE}/invoices", headers=headers,
                                        params=params, timeout=30)
                    if resp.status_code == 429:
                        time.sleep(15 * (attempt + 1))
                        resp = None
                        continue
                    if resp.status_code == 400:
                        resp = None
                        break
                    resp.raise_for_status()
                    break
                except (requests.ConnectionError, requests.Timeout):
                    if attempt < 2:
                        time.sleep(10 * (attempt + 1))
                    else:
                        resp = None
            if not resp:
                break
            data = resp.json()
            batch = data.get("invoices", [])
            new = [i for i in batch if i.get("invoice_id") not in seen_ids]
            seen_ids.update(i.get("invoice_id") for i in new)
            all_invoices.extend(new)
            if not data.get("page_context", {}).get("has_more_page", False):
                break
            page += 1
            time.sleep(0.8)
    print(f"    → {len(all_invoices)} invoices")
    return all_invoices


def is_promo(inv):
    return (float(inv.get("total") or 0) == 0
            and float(inv.get("balance") or 0) == 0
            and not inv.get("last_payment_date"))


def aggregate(invoices):
    invoiced_revenue = 0.0
    paid_revenue = 0.0
    invoice_count = 0
    promo_count = 0
    for inv in invoices:
        total = float(inv.get("total") or 0)
        invoice_count += 1
        invoiced_revenue += total
        if is_promo(inv):
            promo_count += 1
        lpd = inv.get("last_payment_date", "")
        if lpd:
            paid_revenue += total
    return {
        "invoiced_revenue": round(invoiced_revenue, 2),
        "paid_revenue":     round(paid_revenue, 2),
        "invoice_count":    invoice_count,
        "promo_count":      promo_count,
    }


def fetch_transport(cy_start, cy_end, py_start, py_end):
    import snowflake.connector
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    token = toml.split('token = "')[1].split('"')[0]
    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        authenticator="programmatic_access_token",
        token=token,
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
    )
    cur = conn.cursor()
    print("  Fetching transport from Snowflake...")
    cur.execute(f"""
        SELECT
            CASE WHEN PICK_UP_DATE BETWEEN '{cy_start}' AND '{cy_end}' THEN 'CY'
                 WHEN PICK_UP_DATE BETWEEN '{py_start}' AND '{py_end}' THEN 'PY'
            END AS period,
            ROUND(SUM(REVENUE_FINAL_AV_FEE), 2) AS av_fee,
            COUNT(*) AS jobs,
            SUM(CASE WHEN DEAL_STAGE != 'Cancel' THEN 1 ELSE 0 END) AS completed_jobs
        FROM CONFORMED.PRODUCTION.FCT_STORAGE
        WHERE PICK_UP_DATE BETWEEN '{py_start}' AND '{cy_end}'
          AND STORAGE_EVENT_TYPE IN ('Collection', 'Re-Delivery')
        GROUP BY 1
        ORDER BY 1
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = {}
    for period, av_fee, jobs, completed in rows:
        if period:
            result[period] = {"av_fee": float(av_fee or 0), "jobs": int(jobs), "completed": int(completed)}
    print(f"    → {result}")
    return result


def pct_change(cy, py):
    if py and py != 0:
        return round((cy - py) / abs(py) * 100, 1)
    return None


def fmt_pct(v):
    if v is None:
        return "n/a"
    sign = "+" if v > 0 else ""
    return f"{sign}{v}%"


def fmt_gbp(v):
    return f"£{v:,.2f}"


def main():
    print(f"\nWeekly YoY Review")
    print(f"  W16 2026 (CY): {CY_START} → {CY_END}")
    print(f"  W16 2025 (PY): {PY_START} → {PY_END}\n")

    token = get_zoho_token()
    print("✅ Zoho authenticated\n")

    cy_inv = fetch_invoices(token, CY_START, CY_END, "W16 2026")
    py_inv = fetch_invoices(token, PY_START, PY_END, "W16 2025")

    cy = aggregate(cy_inv)
    py = aggregate(py_inv)

    transport = fetch_transport(CY_START, CY_END, PY_START, PY_END)
    cy_transport = transport.get("CY", {}).get("av_fee", 0)
    py_transport = transport.get("PY", {}).get("av_fee", 0)

    print("\n" + "="*60)
    print(f"  WEEKLY YoY — W16  (ISO Week 16)")
    print("="*60)
    print(f"{'Metric':<30} {'W16 2026':>12} {'W16 2025':>12} {'YoY':>8}")
    print("-"*60)

    metrics = [
        ("Invoiced Revenue",    fmt_gbp(cy["invoiced_revenue"]), fmt_gbp(py["invoiced_revenue"]), fmt_pct(pct_change(cy["invoiced_revenue"], py["invoiced_revenue"]))),
        ("Paid Revenue",        fmt_gbp(cy["paid_revenue"]),     fmt_gbp(py["paid_revenue"]),     fmt_pct(pct_change(cy["paid_revenue"], py["paid_revenue"]))),
        ("Transport Revenue",   fmt_gbp(cy_transport),           fmt_gbp(py_transport),           fmt_pct(pct_change(cy_transport, py_transport))),
        ("Invoices Raised",     str(cy["invoice_count"]),        str(py["invoice_count"]),        fmt_pct(pct_change(cy["invoice_count"], py["invoice_count"]))),
        ("Promo Invoices (£0)", str(cy["promo_count"]),          str(py["promo_count"]),          fmt_pct(pct_change(cy["promo_count"], py["promo_count"]))),
    ]

    for name, cy_val, py_val, yoy in metrics:
        print(f"  {name:<28} {cy_val:>12} {py_val:>12} {yoy:>8}")

    print("="*60)
    print()


if __name__ == "__main__":
    main()
