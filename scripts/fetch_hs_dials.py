#!/usr/bin/env python3
"""
AnyVan Storage — HubSpot Outbound Dial Performance (Monthly)
Source: Snowflake (HUBSPOT_DEAL + HUBSPOT_EVENTS_DEAL_WIDE + FCT_VOICE_INTERACTIONS + DIM_CALENDAR)
Pipeline: AVC – UK – STORAGE (id 694358880)
Owners tracked: Dylan Christian, Andy N, Prosper M, Carla Jacobs, Michelle J
Writes to Supabase: hs_dials_monthly_team, hs_dials_monthly_agent

Usage:
  python3 fetch_hs_dials.py            # default — trailing 6 full months ending last completed month
  python3 fetch_hs_dials.py 2026-04    # backfill from 2026-04 through last completed month
"""

import os
import sys
import requests
import snowflake.connector
from datetime import date
from dateutil.relativedelta import relativedelta

# ─── CONFIG ────────────────────────────────────────────────────────────────────

_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
if os.path.exists(_env_file):
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"]
SNOWFLAKE_USER    = os.environ["SNOWFLAKE_USER"]
SNOWFLAKE_WH      = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE    = "MART_SALES_OPS_GROUP"

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates",
}

PIPELINE_ID = 694358880

TODAY = date.today()


# ─── DATE RANGE ────────────────────────────────────────────────────────────────

def resolve_window():
    """
    Return (win_start_inclusive, win_end_exclusive) covering full calendar months only.

    Default: trailing 6 full months ending at start of current month.
    With YYYY-MM arg: from that month through end of last completed month.
    """
    win_end = date(TODAY.year, TODAY.month, 1)  # first of current (in-progress) month
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else None
    if arg:
        try:
            year, month = [int(x) for x in arg.split("-")]
            win_start = date(year, month, 1)
        except ValueError:
            sys.exit(f"❌ Bad date arg '{arg}' — expected YYYY-MM")
    else:
        win_start = win_end - relativedelta(months=6)
    if win_start >= win_end:
        sys.exit(f"❌ Window start ({win_start}) must precede window end ({win_end})")
    return win_start, win_end


# ─── SNOWFLAKE ─────────────────────────────────────────────────────────────────

def get_sf_token():
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    return toml.split('token = "')[1].split('"')[0]


SQL = """
WITH window_bounds AS (
  SELECT TO_DATE(%(win_start)s) AS WIN_START, TO_DATE(%(win_end)s) AS WIN_END
),
deal_pipeline AS (
  SELECT HUBSPOT_DEAL_ID, HUBSPOT_DEAL_PIPELINE_ID,
         ROW_NUMBER() OVER (PARTITION BY HUBSPOT_DEAL_ID ORDER BY EVENT_TIMESTAMP DESC) AS rn
  FROM HARMONISED.PRODUCTION.HUBSPOT_EVENTS_DEAL_WIDE
  WHERE HUBSPOT_DEAL_PIPELINE_ID IS NOT NULL
    AND EVENT_TIMESTAMP >= (SELECT DATEADD(month, -1, WIN_START) FROM window_bounds)
),
storage_deals AS (
  SELECT HUBSPOT_DEAL_ID FROM deal_pipeline
  WHERE rn = 1 AND HUBSPOT_DEAL_PIPELINE_ID = %(pipeline_id)s
),
owners AS (
  SELECT * FROM (VALUES
    (641005848, 'Dylan',    '[AGENT_EMAIL_REMOVED]'),
    (77534533,  'Andy',     '[AGENT_EMAIL_REMOVED]'),
    (77841901,  'Prosper',  '[AGENT_EMAIL_REMOVED]'),
    (425207042, 'Carla',    '[AGENT_EMAIL_REMOVED]'),
    (77344590,  'Michelle', '[AGENT_EMAIL_REMOVED]')
  ) v(OWNER_ID, AGENT_NAME, OWNER_EMAIL)
),
target_deals AS (
  SELECT d.DEAL_ID, d.OWNER_ID, o.AGENT_NAME, o.OWNER_EMAIL,
         CONVERT_TIMEZONE('Europe/London', d.PROPERTY_CREATEDATE)::TIMESTAMP_NTZ AS DEAL_CREATED_AT_UK,
         REGEXP_REPLACE(d.PROPERTY_PHONE_NUMBER, '[^0-9]', '') AS PHONE_NORM
  FROM HARMONISED.PRODUCTION.HUBSPOT_DEAL d
  INNER JOIN storage_deals s ON s.HUBSPOT_DEAL_ID = d.DEAL_ID
  INNER JOIN owners o ON o.OWNER_ID = d.OWNER_ID
  CROSS JOIN window_bounds w
  WHERE d.PROPERTY_CREATEDATE >= w.WIN_START
    AND d.PROPERTY_CREATEDATE <  w.WIN_END
),
outbound_calls AS (
  SELECT REGEXP_REPLACE(c.TO_NUMBER,'[^0-9]','') AS PHONE_NORM,
         c.WORKER_EMAIL,
         CONVERT_TIMEZONE('UTC','Europe/London', c.CALL_DATE_TIME) AS CALL_AT_UK
  FROM CONFORMED.PRODUCTION.FCT_VOICE_INTERACTIONS c
  CROSS JOIN window_bounds w
  WHERE c.CALL_DIRECTION = 'outbound'
    AND c.WORKER_EMAIL IN (SELECT OWNER_EMAIL FROM owners)
    AND c.CALL_DATE_TIME >= w.WIN_START
    AND c.CALL_DATE_TIME <  DATEADD(day, 21, w.WIN_END)
    AND c.TO_NUMBER IS NOT NULL
),
calls_with_deal AS (
  SELECT c.CALL_AT_UK, d.DEAL_ID,
         ROW_NUMBER() OVER (PARTITION BY c.WORKER_EMAIL, c.PHONE_NORM, c.CALL_AT_UK
                            ORDER BY d.DEAL_CREATED_AT_UK DESC) AS rn
  FROM outbound_calls c
  INNER JOIN target_deals d
    ON d.OWNER_EMAIL = c.WORKER_EMAIL
   AND d.PHONE_NORM  = c.PHONE_NORM
   AND d.DEAL_CREATED_AT_UK <= c.CALL_AT_UK
),
deal_call_summary AS (
  SELECT DEAL_ID, COUNT(*) AS DIAL_COUNT, MIN(CALL_AT_UK) AS FIRST_CALL_AT_UK
  FROM calls_with_deal WHERE rn = 1 GROUP BY DEAL_ID
),
deal_with_calls AS (
  SELECT td.AGENT_NAME, td.DEAL_ID, td.DEAL_CREATED_AT_UK,
         COALESCE(dcs.DIAL_COUNT, 0) AS DIAL_COUNT,
         dcs.FIRST_CALL_AT_UK,
         DATE_TRUNC(month, td.DEAL_CREATED_AT_UK)::DATE AS MONTH_START
  FROM target_deals td
  LEFT JOIN deal_call_summary dcs ON dcs.DEAL_ID = td.DEAL_ID
),
deal_with_t1adj AS (
  SELECT *,
    CASE
      WHEN FIRST_CALL_AT_UK IS NULL THEN NULL
      ELSE
        CASE
          WHEN DAYOFWEEKISO(DEAL_CREATED_AT_UK) BETWEEN 1 AND 5
           AND HOUR(DEAL_CREATED_AT_UK) >= 8 AND HOUR(DEAL_CREATED_AT_UK) < 18
          THEN DEAL_CREATED_AT_UK
          WHEN DAYOFWEEKISO(DEAL_CREATED_AT_UK) BETWEEN 1 AND 5
           AND HOUR(DEAL_CREATED_AT_UK) < 8
          THEN DATEADD(hour, 8, DATE_TRUNC(day, DEAL_CREATED_AT_UK))
          WHEN DAYOFWEEKISO(DEAL_CREATED_AT_UK) BETWEEN 1 AND 4
           AND HOUR(DEAL_CREATED_AT_UK) >= 18
          THEN DATEADD(hour, 8, DATE_TRUNC(day, DATEADD(day, 1, DEAL_CREATED_AT_UK)))
          WHEN DAYOFWEEKISO(DEAL_CREATED_AT_UK) = 5
           AND HOUR(DEAL_CREATED_AT_UK) >= 18
          THEN DATEADD(hour, 8, DATE_TRUNC(day, DATEADD(day, 3, DEAL_CREATED_AT_UK)))
          WHEN DAYOFWEEKISO(DEAL_CREATED_AT_UK) = 6
          THEN DATEADD(hour, 8, DATE_TRUNC(day, DATEADD(day, 2, DEAL_CREATED_AT_UK)))
          WHEN DAYOFWEEKISO(DEAL_CREATED_AT_UK) = 7
          THEN DATEADD(hour, 8, DATE_TRUNC(day, DATEADD(day, 1, DEAL_CREATED_AT_UK)))
        END
    END AS T1_ADJ
  FROM deal_with_calls
),
weekday_count AS (
  SELECT d.DEAL_ID, COUNT(cal.DATE) AS FULL_BIZ_DAYS
  FROM deal_with_t1adj d
  LEFT JOIN MART_SALES_OPS.PRODUCTION.DIM_CALENDAR cal
    ON cal.DATE > DATE(d.T1_ADJ)
   AND cal.DATE < DATE(d.FIRST_CALL_AT_UK)
   AND cal.IS_WEEKEND = FALSE
  WHERE d.T1_ADJ IS NOT NULL AND d.FIRST_CALL_AT_UK IS NOT NULL
  GROUP BY d.DEAL_ID
),
bh AS (
  SELECT
    d.AGENT_NAME, d.MONTH_START, d.DEAL_ID, d.DIAL_COUNT, d.FIRST_CALL_AT_UK, d.T1_ADJ,
    CASE
      WHEN d.FIRST_CALL_AT_UK IS NULL THEN NULL
      WHEN d.FIRST_CALL_AT_UK <= d.T1_ADJ THEN 0
      WHEN DATE(d.T1_ADJ) = DATE(d.FIRST_CALL_AT_UK)
        THEN DATEDIFF(second, d.T1_ADJ, d.FIRST_CALL_AT_UK)
      ELSE
        DATEDIFF(second, d.T1_ADJ, DATEADD(hour, 18, DATE_TRUNC(day, d.T1_ADJ)))
        + COALESCE(w.FULL_BIZ_DAYS, 0) * 36000
        + DATEDIFF(second, DATEADD(hour, 8, DATE_TRUNC(day, d.FIRST_CALL_AT_UK)), d.FIRST_CALL_AT_UK)
    END AS BH_SECONDS
  FROM deal_with_t1adj d
  LEFT JOIN weekday_count w ON w.DEAL_ID = d.DEAL_ID
)
SELECT AGENT_NAME, MONTH_START,
       COUNT(*) AS DEALS,
       SUM(DIAL_COUNT) AS TOTAL_DIALS,
       ROUND(AVG(DIAL_COUNT)::FLOAT, 2) AS AVG_DIALS_PER_LEAD,
       COUNT(FIRST_CALL_AT_UK) AS DEALS_WITH_CALL,
       ROUND(AVG(BH_SECONDS / 3600.0)::FLOAT, 2) AS AVG_HOURS_TO_FIRST_DIAL
FROM bh GROUP BY AGENT_NAME, MONTH_START
UNION ALL
SELECT 'Team', MONTH_START,
       COUNT(*), SUM(DIAL_COUNT),
       ROUND(AVG(DIAL_COUNT)::FLOAT, 2),
       COUNT(FIRST_CALL_AT_UK),
       ROUND(AVG(BH_SECONDS / 3600.0)::FLOAT, 2)
FROM bh GROUP BY MONTH_START
ORDER BY AGENT_NAME, MONTH_START
"""


# ─── SUPABASE ──────────────────────────────────────────────────────────────────

def upsert_rows(table, rows):
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SUPABASE_HEADERS,
        json=rows,
    )
    if resp.status_code not in (200, 201):
        print(f"   ❌ Supabase {table}: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    win_start, win_end = resolve_window()
    print(f"\n🚀 fetch_hs_dials.py — window: {win_start} → {win_end} (exclusive)\n")

    sf_token = get_sf_token()
    conn = snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        authenticator="programmatic_access_token",
        token=sf_token,
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
    )
    cur = conn.cursor()
    cur.execute(SQL, {
        "win_start": win_start.isoformat(),
        "win_end":   win_end.isoformat(),
        "pipeline_id": PIPELINE_ID,
    })

    team_rows  = []
    agent_rows = []
    for agent_name, month_start, deals, total_dials, avg_dpl, deals_with_call, avg_hours in cur.fetchall():
        row = {
            "month_start":             month_start.isoformat(),
            "deals":                   int(deals) if deals is not None else 0,
            "total_dials":             int(total_dials) if total_dials is not None else 0,
            "avg_dials_per_lead":      float(avg_dpl) if avg_dpl is not None else None,
            "deals_with_call":         int(deals_with_call) if deals_with_call is not None else 0,
            "avg_hours_to_first_dial": float(avg_hours) if avg_hours is not None else None,
        }
        if agent_name == "Team":
            team_rows.append(row)
        else:
            agent_rows.append({**row, "agent": agent_name})

    cur.close()
    conn.close()

    print(f"   {len(team_rows)} team rows · {len(agent_rows)} agent rows")
    print(f"\n📤 Upserting to Supabase ...")
    upsert_rows("hs_dials_monthly_team",  team_rows)
    upsert_rows("hs_dials_monthly_agent", agent_rows)
    print("✅ Done.\n")


if __name__ == "__main__":
    main()
