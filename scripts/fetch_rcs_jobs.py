#!/usr/bin/env python3
"""
Storage RCS — Today's job fetcher
Queries Snowflake for today's Storage jobs, enriches with Zoho CRM deal data
(unit number, access code, padlock), and writes data/rcs_today.json.
Runs at 8am UK daily via GitHub Actions, or on demand via workflow_dispatch.
"""

import json
import os
import requests
from datetime import date, datetime, timezone
import snowflake.connector

SNOWFLAKE_ACCOUNT = "[SNOWFLAKE_ACCOUNT_REMOVED]"
SNOWFLAKE_USER    = "[SNOWFLAKE_USER_REMOVED]"
SNOWFLAKE_WH      = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE    = "MART_SALES_OPS_GROUP"

ZOHO_AUTH_URL  = "https://accounts.zoho.eu/oauth/v2/token"
ZOHO_API_BASE  = "https://www.zohoapis.eu/crm/v3"
ZOHO_DEAL_FIELDS = "Unit_Numbers,Access_Code_For_Facility,Padlock_combination,Warehouse_Name1"

QUERY = """
SELECT
    EL.LISTING_ID,
    EL.LISTING_PICK_UP_DATE,
    EL.TP_FULL_NAME,
    EL.LISTING_CHOSEN_PROVIDER_NICKNAME,
    EL.CUSTOMER_FULL_NAME,
    EL.CUSTOMER_EMAIL_ADDRESS,
    EL.LISTING_SPECIAL_INSTRUCTIONS,
    TP.PHONE_NUMBER,
    CASE
        WHEN EL.STORAGE_COLLECTION_DEAL_ID IS NOT NULL
            THEN 'Collection'
        WHEN EL.STORAGE_REDELIVERY_DEAL_ID IS NOT NULL
         AND UPPER(EL.LISTING_SPECIAL_INSTRUCTIONS) LIKE '%DISPOSAL%'
            THEN 'Disposal'
        WHEN EL.STORAGE_REDELIVERY_DEAL_ID IS NOT NULL
            THEN 'Redelivery'
    END AS JOB_TYPE,
    W.COMPANY_NAME AS WAREHOUSE_NAME,
    CASE
        WHEN W.COMPANY_NAME ILIKE 'Access Self Storage%' THEN 'Access'
        WHEN W.COMPANY_NAME IS NOT NULL THEN 'Non-Access'
        ELSE 'Non-Access'
    END AS FACILITY_TYPE
FROM MART_ENTERPRISE.PRODUCTION.ENTERPRISE_LISTING_EXTRACT EL
JOIN CONFORMED.PRODUCTION.FCT_STORAGE FS
    ON EL.LISTING_ID = FS.LISTING_ID
LEFT JOIN MART_SALES_OPS.PRODUCTION.TP_DETAILS TP
    ON EL.LISTING_CHOSEN_PROVIDER_NICKNAME = TP.NICKNAME
LEFT JOIN CONFORMED.PRODUCTION.DIM_STORAGE_WAREHOUSE W
    ON CASE
        WHEN EL.STORAGE_COLLECTION_DEAL_ID IS NOT NULL THEN EL.LISTING_DELIVERY_ADDRESS
        ELSE EL.LISTING_PICK_UP_ADDRESS
       END ILIKE '%' || W.POST_CODE || '%'
WHERE
    EL.LISTING_PICK_UP_DATE = CURRENT_DATE()
    AND EL.LISTING_JOB_CLASSIFICATION_REGION IN ('AVC | V4 - UK', 'AVB - UK')
    AND (
        EL.STORAGE_COLLECTION_DEAL_ID IS NOT NULL
        OR EL.STORAGE_REDELIVERY_DEAL_ID IS NOT NULL
    )
    AND FS.DEAL_STAGE != 'Cancel'
"""

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'rcs_today.json')


def get_zoho_token():
    resp = requests.post(ZOHO_AUTH_URL, params={
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
        "client_id":     os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"Zoho token exchange failed: {resp.json()}")
    return token


def _parse_zoho_deal(d: dict) -> dict:
    return {
        "unit_number":    str(d["Unit_Numbers"]).strip()          if d.get("Unit_Numbers")                     else "",
        "access_code":    str(int(d["Access_Code_For_Facility"])) if d.get("Access_Code_For_Facility") is not None else "",
        "padlock":        str(int(d["Padlock_combination"]))       if d.get("Padlock_combination")       is not None else "",
        "warehouse_name": str(d["Warehouse_Name1"]).strip()       if d.get("Warehouse_Name1")                  else "",
    }


def fetch_zoho_deal(listing_id: str, job_type: str, customer_name: str, token: str) -> dict:
    """Looks up Zoho deal by the correct listing ID field, falls back to customer name search."""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    # Collection jobs use Listing_ID; Redelivery/Disposal use AV_Redelivery_Listing_ID
    id_field = "Listing_ID" if job_type == "Collection" else "AV_Redelivery_Listing_ID"

    # Primary: search by listing ID field
    try:
        resp = requests.get(
            f"{ZOHO_API_BASE}/Deals/search",
            headers=headers,
            params={"criteria": f"({id_field}:equals:{listing_id})", "fields": ZOHO_DEAL_FIELDS},
        )
        resp.raise_for_status()
        deals = resp.json().get("data", [])
        if deals:
            return _parse_zoho_deal(deals[0])
    except Exception as e:
        print(f"  WARN Zoho {id_field} lookup failed for {listing_id}: {e}")

    # Fallback: search by customer name
    if customer_name:
        try:
            # Use surname (last word) — more distinctive than first name
            parts = customer_name.strip().split()
            word = parts[-1] if parts else ""
            if len(word) >= 3:
                resp = requests.get(
                    f"{ZOHO_API_BASE}/Deals/search",
                    headers=headers,
                    params={"word": word, "fields": ZOHO_DEAL_FIELDS},
                )
                resp.raise_for_status()
                deals = resp.json().get("data", [])
                if deals:
                    print(f"  INFO Zoho name fallback matched '{word}' for listing {listing_id}")
                    return _parse_zoho_deal(deals[0])
        except Exception as e:
            print(f"  WARN Zoho name fallback failed for {listing_id} ('{customer_name}'): {e}")

    print(f"  WARN No Zoho deal found for listing {listing_id} ('{customer_name}')")
    return {}


def get_snowflake_token():
    path = os.path.expanduser("~/.snowflake/connections.toml")
    return open(path).read().split('token = "')[1].split('"')[0]


def build_message(job_type, facility_type, driver_name, customer_name,
                  facility_name, unit_number, padlock, access_code, listing_id):
    parts = driver_name.split() if driver_name else []
    first_name = next((p for p in parts if len(p) > 1), parts[0] if parts else 'there')
    unit_str   = unit_number  or '[See special instructions]'
    padlock_str = padlock     or '[See special instructions]'
    access_str  = access_code or '[See special instructions]'
    facility_str = facility_name or '[Facility]'

    if job_type == 'Collection' and facility_type == 'Access':
        return (
            f"📦 AnyVan Storage — Customer Collection → Storage\n"
            f"Ref: {listing_id}\n\n"
            f"Hi {first_name}, here are your key reminders for today's job:\n\n"
            f"👤 Customer: {customer_name}\n"
            f"🏢 Facility: {facility_str}\n"
            f"🔢 Unit: {unit_str}\n"
            f"🔑 Access code: {access_str}\n"
            f"🔒 Padlock: {padlock_str}\n\n"
            f"ℹ️ No unit number? Reception will allocate one on arrival — all items must go into your allocated unit.\n"
            f"⏰ Be at the facility before 4pm.\n"
            f"🚛 Load the unit carefully — you're responsible for any damage caused by poor loading.\n"
            f"📸 Take photos of everything loaded into the unit, anything not on the inventory list, and any pre-existing damage at the collection address.\n\n"
            f"If you need to contact the team about this Storage job, just reply to this message."
        )
    elif job_type == 'Collection':
        return (
            f"📦 AnyVan Storage — Customer Collection → Storage\n"
            f"Ref: {listing_id}\n\n"
            f"Hi {first_name}, here are your key reminders for today's job:\n\n"
            f"👤 Customer: {customer_name}\n"
            f"🏢 Facility: {facility_str}\n"
            f"🔢 Unit: {unit_str}\n\n"
            f"🦺 PPE required on site — high-vis vest and safety shoes must be worn before entering the facility. No exceptions.\n"
            f"⏰ Be at the facility before 4pm.\n"
            f"🚛 Load the unit carefully — you're responsible for any damage caused by poor loading.\n"
            f"📸 Take photos of everything loaded into the unit, anything not on the inventory list, and any pre-existing damage at the collection address.\n\n"
            f"If you need to contact the team about this Storage job, just reply to this message."
        )
    elif job_type == 'Redelivery' and facility_type == 'Access':
        return (
            f"🚚 AnyVan Storage — Storage → Customer Delivery\n"
            f"Ref: {listing_id}\n\n"
            f"Hi {first_name}, here are your key reminders for today's job:\n\n"
            f"👤 Customer: {customer_name}\n"
            f"🏢 Facility: {facility_str}\n"
            f"🔢 Unit: {unit_str}\n"
            f"🔑 Access code: {access_str}\n"
            f"🔒 Padlock: {padlock_str}\n\n"
            f"📸 Before you start loading — photograph everything in the unit. This is how we check against the original collection.\n"
            f"📸 Also photograph anything not on the inventory list and any damage before loading into your vehicle.\n"
            f"🏁 Before you leave: photo of the empty unit, then leave it unlocked with keys inside or hand the padlock to reception.\n\n"
            f"If you need to contact the team about this Storage job, just reply to this message."
        )
    elif job_type == 'Redelivery':
        return (
            f"🚚 AnyVan Storage — Storage → Customer Delivery\n"
            f"Ref: {listing_id}\n\n"
            f"Hi {first_name}, here are your key reminders for today's job:\n\n"
            f"👤 Customer: {customer_name}\n"
            f"🏢 Facility: {facility_str}\n"
            f"🔢 Unit: {unit_str}\n\n"
            f"🦺 PPE required on site — high-vis vest and safety shoes must be worn before entering the facility. No exceptions.\n"
            f"✅ Check in with reception on arrival and again when you're done.\n"
            f"⏰ You must arrive within your pre-arranged timeslot.\n"
            f"📸 Before you start loading — photograph everything in the unit, anything not on the inventory list, and any damage before loading into your vehicle.\n\n"
            f"If you need to contact the team about this Storage job, just reply to this message."
        )
    else:  # Disposal
        return (
            f"🗑️ AnyVan Storage — Collect & Dispose\n"
            f"Ref: {listing_id}\n\n"
            f"Hi {first_name}, here are your key reminders for today's job:\n\n"
            f"👤 Customer: {customer_name}\n"
            f"🏢 Facility: {facility_str}\n"
            f"🔢 Unit: {unit_str}\n\n"
            f"✅ All items must be removed — nothing left behind.\n"
            f"📸 Photograph everything in the unit before you start, and any pre-existing damage.\n"
            f"🏁 Before you leave: photo of the empty unit, then leave it unlocked or hand the padlock to reception.\n\n"
            f"If you need to contact the team about this Storage job, just reply to this message."
        )


def main():
    today = str(date.today())
    print(f"Fetching Storage RCS jobs for {today}")

    print("Getting Zoho CRM token...")
    zoho_token = get_zoho_token()
    print("Zoho token OK")

    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        token=get_snowflake_token(),
        authenticator="programmatic_access_token",
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
    )

    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cur.close()
    conn.close()

    print(f"Found {len(rows)} jobs")

    jobs = []
    for row in rows:
        r            = dict(zip(cols, row))
        job_type     = r.get('JOB_TYPE') or 'Unknown'
        facility_type = r.get('FACILITY_TYPE') or 'Non-Access'
        facility_name = r.get('WAREHOUSE_NAME') or ''
        driver_name  = r.get('TP_FULL_NAME') or ''
        customer_name = r.get('CUSTOMER_FULL_NAME') or ''
        listing_id   = str(r['LISTING_ID'])

        zoho = fetch_zoho_deal(listing_id, job_type, customer_name, zoho_token)
        unit_number  = zoho.get('unit_number', '')
        access_code  = zoho.get('access_code', '')
        padlock      = zoho.get('padlock', '')
        facility_name = facility_name or zoho.get('warehouse_name', '')

        message = build_message(
            job_type, facility_type, driver_name, customer_name,
            facility_name, unit_number, padlock, access_code, listing_id
        )

        jobs.append({
            'listing_id':    listing_id,
            'tp_full_name':  driver_name,
            'nickname':      r.get('LISTING_CHOSEN_PROVIDER_NICKNAME') or '',
            'customer_name': customer_name,
            'customer_email': r.get('CUSTOMER_EMAIL_ADDRESS') or '',
            'phone_number':  r.get('PHONE_NUMBER') or '',
            'job_type':      job_type,
            'facility_type': facility_type,
            'facility_name': facility_name,
            'unit_number':   unit_number,
            'padlock':       padlock,
            'access_code':   access_code,
            'message':       message,
        })

    output = {
        'job_date':   today,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'jobs':       jobs,
    }

    out_path = os.path.normpath(OUTPUT_PATH)
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"Written {len(jobs)} jobs to {out_path}")


if __name__ == '__main__':
    main()
