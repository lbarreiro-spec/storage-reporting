#!/usr/bin/env python3
"""
Storage RCS — Today's job fetcher
Queries Snowflake for today's Storage jobs and writes data/rcs_today.json.
Runs at 8am UK daily via GitHub Actions, or on demand via workflow_dispatch.
"""

import json
import os
import sys
from datetime import date, datetime, timezone
import snowflake.connector

SNOWFLAKE_ACCOUNT = "[SNOWFLAKE_ACCOUNT_REMOVED]"
SNOWFLAKE_USER    = "[SNOWFLAKE_USER_REMOVED]"
SNOWFLAKE_WH      = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE    = "MART_SALES_OPS_GROUP"

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

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'rcs_today.json')


def get_snowflake_token():
    path = os.path.expanduser("~/.snowflake/connections.toml")
    return open(path).read().split('token = "')[1].split('"')[0]


def main():
    today = str(date.today())
    print(f"Fetching Storage RCS jobs for {today}")

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
        r = dict(zip(cols, row))
        job_type = r.get('JOB_TYPE') or 'Unknown'
        jobs.append({
            'listing_id':   str(r['LISTING_ID']),
            'tp_full_name': r.get('TP_FULL_NAME') or '',
            'nickname':     r.get('LISTING_CHOSEN_PROVIDER_NICKNAME') or '',
            'phone_number': r.get('PHONE_NUMBER') or '',
            'job_type':     job_type,
            'template':     TEMPLATES.get(job_type, ''),
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
