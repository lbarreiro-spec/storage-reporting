#!/usr/bin/env python3
"""
AnyVan Storage — MTD Revenue Intelligence Fetcher
Sources: Zoho Books, Zoho CRM, Snowflake (transport)
Writes to Supabase (mtd_monthly, mtd_transport_monthly, mtd_fees_monthly, mtd_yoy)

Usage:
  python3 fetch_mtd.py          # Current month start → today + YoY (past months left in Supabase)
  python3 fetch_mtd.py 2025     # Full 2025 history (no YoY)
  python3 fetch_mtd.py 2024     # Full 2024 history (no YoY)
"""

import os
import sys
import json
import time
import warnings
import requests
from datetime import date, timedelta, datetime
from dateutil.relativedelta import relativedelta
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
# Load .env from repo root (GitHub Actions) or fall back to anyvan-kpi dir (local)
_repo_root = os.path.join(os.path.dirname(__file__), '..')
_local_env  = os.path.join(os.path.dirname(__file__), '..', '..', 'anyvan-kpi', '.env')
load_dotenv(os.path.join(_repo_root, '.env'))
if not os.getenv("ZOHO_CLIENT_ID"):
    load_dotenv(_local_env)

# ─── CONFIG ────────────────────────────────────────────────────────────────────

ZOHO_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
ZOHO_ORG_ID        = os.getenv("ZOHO_BOOKS_ORG_ID")
ZOHO_REGION        = os.getenv("ZOHO_REGION", "eu")

BOOKS_BASE = f"https://www.zohoapis.{ZOHO_REGION}/books/v3"
CRM_BASE   = f"https://www.zohoapis.{ZOHO_REGION}/crm/v6"
TOKEN_URL  = f"https://accounts.zoho.{ZOHO_REGION}/oauth/v2/token"
STATUSES   = ["sent", "draft", "overdue", "paid", "void", "unpaid"]

FEE_TARGETS = [
    ("Overdue Admin Fee", "overdue_admin", "65498000052054617"),
    ("Early Release fee", "early_release", "65498000056749797"),
]

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"]
SNOWFLAKE_USER    = os.environ["SNOWFLAKE_USER"]
SNOWFLAKE_WH      = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE    = "MART_SALES_OPS_GROUP"

HISTORY_START = date(2024, 1, 1)
TODAY         = date.today()
YESTERDAY     = TODAY - timedelta(days=1)

YEAR_ARG = sys.argv[1] if len(sys.argv) > 1 else None
IS_MTD   = not YEAR_ARG or str(YEAR_ARG).strip().lower() == "mtd"

def resolve_start():
    if not YEAR_ARG or str(YEAR_ARG).strip().lower() == "mtd":
        # Only re-run current month — past months stay in Supabase as-is
        return date(YESTERDAY.year, YESTERDAY.month, 1)
    try:
        year = int(YEAR_ARG)
        if 2000 <= year <= 2100:
            return date(year, 1, 1)
    except ValueError:
        pass
    return HISTORY_START

START_DATE = resolve_start()


# ─── DATE HELPERS ──────────────────────────────────────────────────────────────

def month_label(d):
    return d.strftime("%Y-%m")

def month_end(d):
    return (d.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)

def build_month_buckets(start, end=None):
    end = end or YESTERDAY
    cursor, last = start.replace(day=1), end.replace(day=1)
    buckets = {}
    while cursor <= last:
        lbl = month_label(cursor)
        buckets[lbl] = {"label": lbl, "from": cursor.isoformat(),
                        "to": month_end(cursor).isoformat()}
        cursor += relativedelta(months=1)
    return buckets


# ─── ZOHO AUTH ─────────────────────────────────────────────────────────────────

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
    print("✅ Authenticated with Zoho")
    return data["access_token"]


# ─── ZOHO BOOKS ────────────────────────────────────────────────────────────────

def fetch_invoices(token, start, end=None, label=""):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    all_invoices, seen_ids = [], set()
    print(f"📥 Fetching invoices {label or f'from {start}'}{f' to {end}' if end else ''}...")

    for status in STATUSES:
        page = 1
        while True:
            params = {
                "organization_id": ZOHO_ORG_ID,
                "status":          status,
                "per_page":        200,
                "page":            page,
                "sort_column":     "date",
                "sort_order":      "A",
                "date_start":      start.isoformat(),
            }
            if end:
                params["date_end"] = end.isoformat()

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

    print(f"   ✅ {len(all_invoices)} invoices")
    return all_invoices


def fetch_line_item_fees(invoices, token):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    fee_keys = [(name.lower(), col_key) for name, col_key, _id in FEE_TARGETS]

    current_month = month_label(YESTERDAY)
    current_month_prefix = YESTERDAY.strftime("%Y-%m-")
    candidates = [i for i in invoices
                  if (i.get("date") or "").startswith(current_month_prefix)]

    inv_to_month = {}
    for inv in candidates:
        d_str = inv.get("date", "")
        if d_str:
            try:
                inv_to_month[inv["invoice_id"]] = month_label(date.fromisoformat(d_str))
            except ValueError:
                pass

    promo_ids = {i["invoice_id"] for i in candidates
                 if i.get("cf_apply_promotion_unformatted") is True}

    print(f"📥 Scanning invoice line items for fees ({len(candidates)} this month)...")

    def fetch_detail(inv):
        inv_id   = inv["invoice_id"]
        mo       = inv_to_month.get(inv_id, "")
        is_promo = inv_id in promo_ids
        try:
            for attempt in range(3):
                r = requests.get(
                    f"{BOOKS_BASE}/invoices/{inv_id}",
                    headers=headers,
                    params={"organization_id": ZOHO_ORG_ID},
                    timeout=15,
                )
                if r.status_code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    return []
                break
            else:
                return []

            detail = r.json().get("invoice", {})
            hits   = []
            promo_cost = 0.0

            for li in detail.get("line_items", []):
                text = (li.get("name", "") + " " + li.get("description", "")).lower()
                for search, col_key in fee_keys:
                    if search in text:
                        hits.append({"month": mo, "col_key": col_key,
                                     "amount": float(li.get("item_total", 0))})
                if is_promo:
                    rate_charged  = float(li.get("rate", 0) or 0)
                    rate_standard = float(li.get("sales_rate", 0) or 0)
                    qty           = float(li.get("quantity", 1) or 1)
                    if rate_standard > rate_charged:
                        promo_cost += (rate_standard - rate_charged) * qty

            if is_promo and promo_cost > 0:
                hits.append({"month": mo, "col_key": "__promo__", "amount": promo_cost})
            return hits
        except Exception:
            return []

    all_hits = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for result in ex.map(fetch_detail, candidates):
            all_hits.extend(result)

    monthly = {}
    for bkt in build_month_buckets(START_DATE).values():
        lbl = bkt["label"]
        monthly[lbl] = {
            "label": lbl,
            **{f"{col_key}_count": 0   for _, col_key, _id in FEE_TARGETS},
            **{f"{col_key}_total": 0.0 for _, col_key, _id in FEE_TARGETS},
        }

    for hit in all_hits:
        mo = hit["month"]
        if mo in monthly and hit["col_key"] != "__promo__":
            monthly[mo][f"{hit['col_key']}_count"] += 1
            monthly[mo][f"{hit['col_key']}_total"] += hit["amount"]

    result = sorted(monthly.values(), key=lambda x: x["label"])
    for r in result:
        for _, col_key, _id in FEE_TARGETS:
            r[f"{col_key}_total"] = round(r[f"{col_key}_total"], 2)

    print(f"   ✅ {len(all_hits)} fee line items across {len(result)} months")
    return result


# ─── ZOHO CRM ──────────────────────────────────────────────────────────────────

SQFT_EXCLUDED_STAGES = {'cancel', 'prospect', 'enquiry', 'estimate sent', 'quoted by sales'}

def fetch_crm_deals(token):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    deals, page_token = [], None
    # Always fetch from HISTORY_START — a deal created in 2024 may have a
    # redelivery date in 2026, so we cannot filter by deal creation date alone.
    print(f"📥 Fetching CRM deals from {HISTORY_START}...")

    for _ in range(50):
        params = {"fields": "Deal_Name,Stage,Created_Time,Estimated_sq_ft1,Moving_Date,Confirmed_Redelivery_Date",
                  "per_page": 200}
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(f"{CRM_BASE}/Deals", headers=headers,
                                params=params, timeout=30)
        except Exception as e:
            print(f"   ❌ CRM fetch error: {e}")
            break
        if resp.status_code in (204, 400):
            break
        resp.raise_for_status()
        data  = resp.json()
        batch = data.get("data", [])
        if not batch:
            break

        past_start = False
        for deal in batch:
            ct = deal.get("Created_Time", "")
            d  = date.fromisoformat(ct[:10]) if ct else None
            if d and d >= HISTORY_START:
                deals.append(deal)
            elif d:
                past_start = True

        if past_start:
            break
        info = data.get("info", {})
        if not info.get("more_records"):
            break
        page_token = info.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.5)

    print(f"   ✅ {len(deals)} deals")
    return deals


# ─── SNOWFLAKE TRANSPORT ───────────────────────────────────────────────────────

def get_snowflake_token():
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    return toml.split('token = "')[1].split('"')[0]


def fetch_transport():
    import snowflake.connector

    sf_token  = get_snowflake_token()
    conn      = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        authenticator="programmatic_access_token",
        token=sf_token,
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
    )
    cur = conn.cursor()

    cy_start  = START_DATE.isoformat()
    py_start  = (START_DATE - relativedelta(years=1)).isoformat()
    py_end    = (YESTERDAY - relativedelta(years=1)).isoformat()
    today_str = YESTERDAY.isoformat()

    print(f"📥 Fetching transport from Snowflake ({cy_start} → {today_str})...")

    cur.execute(f"""
        WITH cy AS (
            SELECT
                DATE_TRUNC('month', PICK_UP_DATE)::DATE                              AS period,
                TO_CHAR(DATE_TRUNC('month', PICK_UP_DATE), 'YYYY-MM')                AS label,
                STORAGE_EVENT_TYPE,
                COUNT(*)                                                              AS total_jobs,
                SUM(CASE WHEN DEAL_STAGE != 'Cancel' THEN 1 ELSE 0 END)              AS completed_jobs,
                ROUND(SUM(REVENUE_FINAL_AV_FEE), 2)                                  AS av_fee
            FROM CONFORMED.PRODUCTION.FCT_STORAGE
            WHERE PICK_UP_DATE >= '{cy_start}'
              AND PICK_UP_DATE <= '{today_str}'
              AND STORAGE_EVENT_TYPE IN ('Collection', 'Re-Delivery')
            GROUP BY 1, 2, 3
        ),
        py AS (
            SELECT
                DATEADD(year, 1, DATE_TRUNC('month', PICK_UP_DATE))::DATE AS period,
                STORAGE_EVENT_TYPE,
                ROUND(SUM(REVENUE_FINAL_AV_FEE), 2)                      AS av_fee
            FROM CONFORMED.PRODUCTION.FCT_STORAGE
            WHERE PICK_UP_DATE >= '{py_start}'
              AND PICK_UP_DATE <= '{py_end}'
              AND STORAGE_EVENT_TYPE IN ('Collection', 'Re-Delivery')
            GROUP BY 1, 2
        )
        SELECT cy.label, cy.period, cy.storage_event_type,
               cy.total_jobs, cy.completed_jobs, cy.av_fee,
               py.av_fee AS prior_year_av_fee,
               CASE WHEN py.av_fee IS NOT NULL AND py.av_fee != 0
                    THEN ROUND(((cy.av_fee - py.av_fee) / py.av_fee) * 100, 1)
                    ELSE NULL END AS yoy_pct
        FROM cy LEFT JOIN py
          ON py.period = cy.period AND py.storage_event_type = cy.storage_event_type
        ORDER BY cy.period, cy.storage_event_type
    """)
    monthly_raw = cur.fetchall()
    cur.close()
    conn.close()

    periods = {}
    for label, period, event_type, total_jobs, completed_jobs, av_fee, py_av_fee, yoy_pct in monthly_raw:
        label = str(label)
        if label not in periods:
            period_date = period if isinstance(period, date) else date.fromisoformat(str(period)[:10])
            periods[label] = {
                "label": label,
                "coll_jobs": 0, "coll_completed": 0, "coll_av_fee": 0.0,
                "coll_av_fee_prior_year": None, "coll_yoy_pct": None,
                "redel_jobs": 0, "redel_completed": 0, "redel_av_fee": 0.0,
                "redel_av_fee_prior_year": None, "redel_yoy_pct": None,
            }
        p = periods[label]
        if event_type == "Collection":
            p["coll_jobs"]              = int(total_jobs)
            p["coll_completed"]         = int(completed_jobs)
            p["coll_av_fee"]            = float(av_fee or 0)
            p["coll_av_fee_prior_year"] = float(py_av_fee) if py_av_fee is not None else None
            p["coll_yoy_pct"]           = float(yoy_pct) if yoy_pct is not None else None
        elif event_type == "Re-Delivery":
            p["redel_jobs"]              = int(total_jobs)
            p["redel_completed"]         = int(completed_jobs)
            p["redel_av_fee"]            = float(av_fee or 0)
            p["redel_av_fee_prior_year"] = float(py_av_fee) if py_av_fee is not None else None
            p["redel_yoy_pct"]           = float(yoy_pct) if yoy_pct is not None else None

    monthly = sorted(periods.values(), key=lambda x: x["label"])
    print(f"   ✅ Transport: {len(monthly)} months")
    return monthly


# ─── AGGREGATION ───────────────────────────────────────────────────────────────

def is_promo(inv):
    return (float(inv.get("total") or 0) == 0
            and float(inv.get("balance") or 0) == 0
            and not inv.get("last_payment_date"))

def is_writeoff(inv):
    return (float(inv.get("total") or 0) > 0
            and float(inv.get("balance") or 0) == 0
            and not inv.get("last_payment_date"))

def aggregate_monthly(invoices, deals):
    month_m = {k: {**v,
                   "invoiced_revenue": 0.0, "invoice_count": 0,
                   "_customer_ids": set(),
                   "paid_revenue": 0.0, "paid_count": 0,
                   "promo_count": 0,
                   "writeoff_count": 0, "writeoff_value": 0.0,
                   "sqft_booked": 0.0, "sqft_entering": 0.0, "sqft_exiting": 0.0,
                   "customers_booked": 0, "customers_entering": 0, "customers_exiting": 0}
               for k, v in build_month_buckets(START_DATE).items()}

    for inv in invoices:
        total       = float(inv.get("total") or 0)
        customer_id = inv.get("customer_id", "")
        promo       = is_promo(inv)
        writeoff    = is_writeoff(inv)

        inv_date_str = inv.get("date", "")
        if inv_date_str:
            try:
                d = date.fromisoformat(inv_date_str)
                mo = month_label(d)
                if mo in month_m:
                    month_m[mo]["invoiced_revenue"] += total
                    month_m[mo]["invoice_count"]    += 1
                    if customer_id:
                        month_m[mo]["_customer_ids"].add(customer_id)
                    if promo:
                        month_m[mo]["promo_count"] += 1
                    if writeoff:
                        month_m[mo]["writeoff_count"] += 1
                        month_m[mo]["writeoff_value"] += total
            except ValueError:
                pass

        lpd_str = inv.get("last_payment_date", "")
        if lpd_str:
            try:
                d = date.fromisoformat(lpd_str)
                mo = month_label(d)
                if mo in month_m:
                    month_m[mo]["paid_revenue"] += total
                    month_m[mo]["paid_count"]   += 1
            except ValueError:
                pass

    for deal in deals:
        stage    = (deal.get("Stage") or "").strip().lower()
        sqft_ok  = stage not in SQFT_EXCLUDED_STAGES
        exit_ok  = stage not in SQFT_EXCLUDED_STAGES
        sq       = float(deal.get("Estimated_sq_ft1") or 0)

        # Booked — by Created_Time
        ct = deal.get("Created_Time", "")
        if ct:
            try:
                d = date.fromisoformat(ct[:10])
                mo = month_label(d)
                if mo in month_m:
                    if sqft_ok and sq > 0:
                        month_m[mo]["sqft_booked"] += sq
                    if sqft_ok:
                        month_m[mo]["customers_booked"] += 1
            except ValueError:
                pass

        # Entering — by Moving_Date
        md = deal.get("Moving_Date", "")
        if md:
            try:
                d = date.fromisoformat(str(md)[:10])
                mo = month_label(d)
                if mo in month_m:
                    if sqft_ok and sq > 0:
                        month_m[mo]["sqft_entering"] += sq
                    if sqft_ok:
                        month_m[mo]["customers_entering"] += 1
            except ValueError:
                pass

        # Exiting — by Confirmed_Redelivery_Date (includes cancelled deals — items left warehouse)
        rd = deal.get("Confirmed_Redelivery_Date", "")
        if rd:
            try:
                d = date.fromisoformat(str(rd)[:10])
                mo = month_label(d)
                if mo in month_m:
                    if exit_ok and sq > 0:
                        month_m[mo]["sqft_exiting"] += sq
                    if exit_ok:
                        month_m[mo]["customers_exiting"] += 1
            except ValueError:
                pass

    result = sorted(month_m.values(), key=lambda x: x["from"])
    for r in result:
        r["invoiced_revenue"] = round(r["invoiced_revenue"], 2)
        r["paid_revenue"]     = round(r["paid_revenue"], 2)
        r["writeoff_value"]   = round(r["writeoff_value"], 2)
        r["unique_customers"] = len(r.pop("_customer_ids"))
        n = r["invoice_count"]
        r["promo_pct"] = round(r["promo_count"] / n * 100, 1) if n > 0 else None
    return result


# ─── YOY COMPUTATION (MTD only) ────────────────────────────────────────────────

def compute_yoy(token, current_months, transport_monthly):
    cur_month_label = month_label(TODAY)
    cur_month = next((m for m in current_months if m["label"] == cur_month_label), {})

    days_elapsed  = (TODAY - date(TODAY.year, TODAY.month, 1)).days + 1
    days_in_month = (date(TODAY.year, TODAY.month % 12 + 1, 1) -
                     date(TODAY.year, TODAY.month, 1)).days if TODAY.month < 12 else 31

    py_month_start = date(TODAY.year - 1, TODAY.month, 1)
    py_same_day    = date(TODAY.year - 1, TODAY.month, TODAY.day)
    py_month_end_  = month_end(py_month_start)

    py_inv_month = fetch_invoices(token, py_month_start, py_same_day,
                                  f"prior year same days ({py_month_start} → {py_same_day})")
    py_inv_full  = fetch_invoices(token, py_month_start, py_month_end_,
                                  f"prior year full month ({py_month_start} → {py_month_end_})")

    def _d(s):
        try:
            return date.fromisoformat(s) if s else None
        except ValueError:
            return None

    def sum_inv(invs, start, end):
        return round(sum(float(i.get("total") or 0) for i in invs
                         if start <= _d(i.get("date")) <= end
                         if _d(i.get("date"))), 2)

    def sum_paid(invs, start, end):
        return round(sum(float(i.get("total") or 0) for i in invs
                         if i.get("last_payment_date")
                         if start <= _d(i.get("last_payment_date")) <= end
                         if _d(i.get("last_payment_date"))), 2)

    def count_unique(invs, start, end):
        return len({i.get("customer_id") for i in invs
                    if i.get("customer_id") and _d(i.get("date"))
                    if start <= _d(i.get("date")) <= end})

    def yoy(cy, py):
        if py and py != 0:
            return round((cy - py) / abs(py) * 100, 1)
        return None

    cy_act_inv  = cur_month.get("invoiced_revenue", 0)
    cy_act_paid = cur_month.get("paid_revenue", 0)
    cy_act_cust = cur_month.get("unique_customers", 0)

    py_act_inv   = sum_inv(py_inv_month,  py_month_start, py_same_day)
    py_act_paid  = sum_paid(py_inv_month, py_month_start, py_same_day)
    py_full_inv  = sum_inv(py_inv_full,   py_month_start, py_month_end_)
    py_full_paid = sum_paid(py_inv_full,  py_month_start, py_month_end_)
    py_act_cust  = count_unique(py_inv_month, py_month_start, py_same_day)
    py_full_cust = count_unique(py_inv_full,  py_month_start, py_month_end_)

    cy_fore_inv  = round(cy_act_inv  / days_elapsed * days_in_month, 2) if days_elapsed else 0
    cy_fore_paid = round(cy_act_paid / days_elapsed * days_in_month, 2) if days_elapsed else 0

    t = next((m for m in transport_monthly if m["label"] == cur_month_label), {})

    return {
        "id":                            1,
        "period":                        cur_month_label,
        "as_of":                         TODAY.isoformat(),
        "days_elapsed":                  days_elapsed,
        "days_in_month":                 days_in_month,
        "cy_invoiced":                   cy_act_inv,
        "py_invoiced_same_days":         py_act_inv,
        "invoiced_actual_yoy_pct":       yoy(cy_act_inv, py_act_inv),
        "cy_invoiced_forecast":          cy_fore_inv,
        "py_invoiced_full_month":        py_full_inv,
        "invoiced_forecast_yoy_pct":     yoy(cy_fore_inv, py_full_inv),
        "cy_paid":                       cy_act_paid,
        "py_paid_same_days":             py_act_paid,
        "paid_actual_yoy_pct":           yoy(cy_act_paid, py_act_paid),
        "cy_paid_forecast":              cy_fore_paid,
        "py_paid_full_month":            py_full_paid,
        "paid_forecast_yoy_pct":         yoy(cy_fore_paid, py_full_paid),
        "cy_unique_customers":           cy_act_cust,
        "py_unique_customers_same_days": py_act_cust,
        "customers_actual_yoy_pct":      yoy(cy_act_cust, py_act_cust),
        "cy_customers_forecast":         round(cy_act_cust / days_elapsed * days_in_month) if days_elapsed else 0,
        "py_customers_full_month":       py_full_cust,
        "customers_forecast_yoy_pct":    yoy(round(cy_act_cust / days_elapsed * days_in_month) if days_elapsed else 0, py_full_cust),
        "coll_av_fee":                   t.get("coll_av_fee", 0),
        "coll_av_fee_prior_year":        t.get("coll_av_fee_prior_year"),
        "coll_yoy_pct":                  t.get("coll_yoy_pct"),
        "redel_av_fee":                  t.get("redel_av_fee", 0),
        "redel_av_fee_prior_year":       t.get("redel_av_fee_prior_year"),
        "redel_yoy_pct":                 t.get("redel_yoy_pct"),
    }


# ─── DAILY REVENUE ─────────────────────────────────────────────────────────────

def build_daily_revenue(invoices, token):
    """Aggregate daily invoiced revenue for current month vs prior year same month."""
    cur_label      = month_label(YESTERDAY)
    py_month_start = date(YESTERDAY.year - 1, YESTERDAY.month, 1)
    py_month_end_  = month_end(py_month_start)

    py_invoices = fetch_invoices(token, py_month_start, py_month_end_,
                                 f"prior year daily ({py_month_start} → {py_month_end_})")

    def daily_totals(inv_list, year, month):
        totals = {}
        for inv in inv_list:
            d_str = inv.get("date", "")
            if not d_str:
                continue
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                continue
            if d.year == year and d.month == month:
                totals[d.day] = totals.get(d.day, 0.0) + float(inv.get("total") or 0)
        return totals

    # NB: anchor on YESTERDAY (the month MTD is reporting), not TODAY — otherwise on the
    # 1st of a new month TODAY.month points at the empty new month and date(PY, TODAY.month, 31)
    # blows up for months where day-31 doesn't exist (e.g. Jun 1 → date(2025,6,31)).
    cy_totals = {d: v for d, v in daily_totals(invoices, YESTERDAY.year, YESTERDAY.month).items() if d <= YESTERDAY.day}
    py_totals = daily_totals(py_invoices, YESTERDAY.year - 1, YESTERDAY.month)

    days_in_month = (py_month_end_ - py_month_start).days + 1
    rows = []
    for day in range(1, days_in_month + 1):
        d_py = date(YESTERDAY.year - 1, YESTERDAY.month, day)
        try:
            d_cy = date(YESTERDAY.year, YESTERDAY.month, day)
        except ValueError:
            d_cy = d_py  # fallback if current year month is shorter
        rows.append({
            "label":      cur_label,
            "day":        day,
            "day_name":   d_cy.strftime("%a"),
            "cy_revenue": round(cy_totals.get(day, 0.0), 2),
            "py_revenue": round(py_totals.get(day, 0.0), 2),
        })
    return rows


# ─── SUPABASE UPSERTS ──────────────────────────────────────────────────────────

def upsert(table, rows):
    if not rows:
        return
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=SUPABASE_HEADERS, json=rows)
    if resp.status_code not in (200, 201):
        print(f"   ❌ Supabase {table}: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()
    print(f"   ✅ Upserted {len(rows)} rows → {table}")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n🚀 fetch_mtd.py — {'MTD (current month only, past months preserved in Supabase)' if IS_MTD else YEAR_ARG}")
    print(f"   Period: {START_DATE} → {YESTERDAY}\n")

    token = get_zoho_token()

    invoices  = fetch_invoices(token, START_DATE, end=TODAY, label=f"{START_DATE} → {TODAY}")
    deals     = fetch_crm_deals(token)
    fee_data  = fetch_line_item_fees(invoices, token)
    transport = fetch_transport()
    monthly   = aggregate_monthly(invoices, deals)

    # Build Supabase rows for mtd_monthly
    monthly_rows = []
    for m in monthly:
        monthly_rows.append({
            "label":            m["label"],
            "invoiced_revenue": m["invoiced_revenue"],
            "paid_revenue":     m["paid_revenue"],
            "invoice_count":    m["invoice_count"],
            "paid_count":       m["paid_count"],
            "unique_customers": m["unique_customers"],
            "promo_pct":        m.get("promo_pct"),
            "writeoff_count":   m["writeoff_count"],
            "writeoff_value":   m["writeoff_value"],
            "sqft_booked":        round(m["sqft_booked"], 1),
            "sqft_entering":      round(m["sqft_entering"], 1),
            "sqft_exiting":       round(m["sqft_exiting"], 1),
            "customers_booked":   m["customers_booked"],
            "customers_entering": m["customers_entering"],
            "customers_exiting":  m["customers_exiting"],
        })

    # Build Supabase rows for mtd_transport_monthly
    transport_rows = []
    for t in transport:
        transport_rows.append({
            "label":          t["label"],
            "coll_jobs":      t["coll_jobs"],
            "coll_completed": t["coll_completed"],
            "coll_av_fee":    t["coll_av_fee"],
            "redel_jobs":     t["redel_jobs"],
            "redel_completed":t["redel_completed"],
            "redel_av_fee":   t["redel_av_fee"],
        })

    # Build Supabase rows for mtd_fees_monthly
    fees_rows = []
    for f in fee_data:
        fees_rows.append({
            "label":               f["label"],
            "overdue_admin_count": f.get("overdue_admin_count", 0),
            "overdue_admin_total": f.get("overdue_admin_total", 0.0),
            "early_release_count": f.get("early_release_count", 0),
            "early_release_total": f.get("early_release_total", 0.0),
        })

    print("\n📤 Writing to Supabase...")
    upsert("mtd_monthly",           monthly_rows)
    upsert("mtd_transport_monthly", transport_rows)
    upsert("mtd_fees_monthly",      fees_rows)

    if IS_MTD:
        yoy_data = compute_yoy(token, monthly, transport)
        upsert("mtd_yoy", [yoy_data])
        daily_rows = build_daily_revenue(invoices, token)
        upsert("mtd_daily_revenue", daily_rows)

    print("\n✅ Done.\n")


if __name__ == "__main__":
    main()
