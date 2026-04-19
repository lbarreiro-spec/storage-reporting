#!/usr/bin/env python3
"""
Storage RCS — Today's job fetcher
Queries Snowflake for today's Storage jobs and upserts to Supabase rcs_jobs.
Runs at 8am UK daily via GitHub Actions, or on demand via workflow_dispatch.

Supabase table required:
  CREATE TABLE rcs_jobs (
    id           BIGSERIAL PRIMARY KEY,
    listing_id   TEXT NOT NULL,
    job_date     DATE NOT NULL,
    tp_full_name TEXT,
    nickname     TEXT,
    phone_number TEXT,
    job_type     TEXT,
    template     TEXT,
    excluded     BOOLEAN DEFAULT FALSE,
    sent         BOOLEAN DEFAULT FALSE,
    sent_at      TIMESTAMPTZ,
    send_requested BOOLEAN DEFAULT FALSE,
    fetched_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (listing_id, job_date)
  );
"""

import os
import sys
import requests
import snowflake.connector
from datetime import date

SNOWFLAKE_ACCOUNT = "[SNOWFLAKE_ACCOUNT_REMOVED]"
SNOWFLAKE_USER    = "[SNOWFLAKE_USER_REMOVED]"
SNOWFLAKE_WH      = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE    = "MART_SALES_OPS_GROUP"

SUPABASE_URL      = "[SUPABASE_URL_REMOVED]"
SUPABASE_ANON_KEY = "[SUPABASE_ANON_KEY_REMOVED]"
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates",
}

QUERY = """
SELECT
    EL.LISTING_ID,
    EL.LISTING_PICK_UP_DATE,
    EL.TP_FULL_NAME,
    EL.LISTING_CHOSEN_PROVIDER_NICKNAME,
    TP.PHONE_NUMBER,
    CASE
        WHEN EL.STORAGE_COLLECTION_DEAL_ID IS NOT NULL
            THEN 'Collection'
        WHEN EL.STORAGE_REDELIVERY_DEAL_ID IS NOT NULL
         AND UPPER(EL.LISTING_SPECIAL_INSTRUCTIONS) LIKE '%DISPOSAL%'
            THEN 'Disposal'
        WHEN EL.STORAGE_REDELIVERY_DEAL_ID IS NOT NULL
            THEN 'Redelivery'
    END AS JOB_TYPE
FROM MART_ENTERPRISE.PRODUCTION.ENTERPRISE_LISTING_EXTRACT EL
LEFT JOIN MART_SALES_OPS.PRODUCTION.TP_DETAILS TP
    ON EL.LISTING_CHOSEN_PROVIDER_NICKNAME = TP.NICKNAME
WHERE
    EL.LISTING_PICK_UP_DATE = CURRENT_DATE()
    AND EL.LISTING_JOB_CLASSIFICATION_REGION IN ('AVC | V4 - UK', 'AVB - UK')
    AND (
        EL.STORAGE_COLLECTION_DEAL_ID IS NOT NULL
        OR EL.STORAGE_REDELIVERY_DEAL_ID IS NOT NULL
    )
"""

TEMPLATES = {
    'Collection': 'This is a Storage Collection Job',
    'Redelivery': 'This is a Storage Redelivery Job',
    'Disposal':   'This is a Storage Disposal Job',
}


def get_snowflake_token():
    path = os.path.expanduser("~/.snowflake/connections.toml")
    content = open(path).read()
    return content.split('token = "')[1].split('"')[0]


def main():
    print(f"Fetching Storage RCS jobs for {date.today()}")

    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        token=get_snowflake_token(),
        authenticator="oauth",
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
    )

    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cur.close()
    conn.close()

    print(f"Found {len(rows)} jobs in Snowflake")

    if not rows:
        print("No Storage jobs today — nothing to upsert")
        return

    records = []
    for row in rows:
        r = dict(zip(cols, row))
        job_type = r.get('JOB_TYPE') or 'Unknown'
        records.append({
            'listing_id':   str(r['LISTING_ID']),
            'job_date':     str(r['LISTING_PICK_UP_DATE']),
            'tp_full_name': r.get('TP_FULL_NAME') or '',
            'nickname':     r.get('LISTING_CHOSEN_PROVIDER_NICKNAME') or '',
            'phone_number': r.get('PHONE_NUMBER') or '',
            'job_type':     job_type,
            'template':     TEMPLATES.get(job_type, ''),
            'excluded':     False,
            'sent':         False,
        })

    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/rcs_jobs",
        headers=SUPABASE_HEADERS,
        json=records,
    )
    print(f"Supabase upsert: {resp.status_code}")
    if resp.status_code not in (200, 201):
        print(resp.text)
        sys.exit(1)


if __name__ == '__main__':
    main()
