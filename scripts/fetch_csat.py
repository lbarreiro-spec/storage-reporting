#!/usr/bin/env python3
"""
AnyVan Storage — CSAT & NPS Weekly Fetcher
Sources:
  CSAT: HARMONISED.PRODUCTION.TWILIO_EVENTS (Ops team, 4 agents)
  NPS:  CONFORMED.PRODUCTION.FCT_FEEDBACK + FCT_STORAGE
Writes to Supabase: csat_weekly, nps_weekly

Usage:
  python3 fetch_csat.py              # last completed week
  python3 fetch_csat.py --weeks 3   # last 3 completed weeks
  python3 fetch_csat.py --all       # all data from 2025-09-01
"""

import os
import sys
import requests
import snowflake.connector
from datetime import date, timedelta

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

NPS_HISTORY_START = date(2025, 9, 1)

# Snowflake full name → display name (Ops team only for CSAT)
CSAT_AGENTS = {
    "Shafwaan Titus":  "Shafwaan",
    "Shaun Gae":       "Shaun",
    "Emmanuel Nsenga": "Emmanuel",
    "Theo Johannes":   "Theo",
}

TODAY       = date.today()
THIS_MONDAY = TODAY - timedelta(days=TODAY.isoweekday() - 1)
LAST_MONDAY = THIS_MONDAY - timedelta(days=7)


# ─── WEEK RANGE ────────────────────────────────────────────────────────────────

def resolve_weeks():
    run_all = "--all" in sys.argv
    weeks_n = 1
    if "--weeks" in sys.argv:
        try:
            weeks_n = int(sys.argv[sys.argv.index("--weeks") + 1])
        except (IndexError, ValueError):
            pass

    if run_all:
        # All Mondays from NPS_HISTORY_START through last completed week
        start = NPS_HISTORY_START
        # Align to nearest Monday on or after start
        if start.isoweekday() != 1:
            start += timedelta(days=(8 - start.isoweekday()) % 7)
        weeks, d = [], start
        while d <= LAST_MONDAY:
            weeks.append(d)
            d += timedelta(weeks=1)
        return weeks
    else:
        return [LAST_MONDAY - timedelta(weeks=i) for i in range(weeks_n - 1, -1, -1)]


# ─── SNOWFLAKE ─────────────────────────────────────────────────────────────────

def get_sf_token():
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    return toml.split('token = "')[1].split('"')[0]


def fetch_csat(cur, range_start, range_end):
    cur.execute(f"""
WITH score_clean AS (
    SELECT
        TRIM(CALLSID) AS CALLSID,
        TYPE,
        CASE
            WHEN REGEXP_LIKE(TRIM(CALLTAGS), '^[0-9]+')
                THEN TRY_CAST(REGEXP_SUBSTR(TRIM(CALLTAGS), '^[0-9]+') AS NUMBER)
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'one%'   THEN 1
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'two%'   THEN 2
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'three%' THEN 3
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'four%'  THEN 4
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'five%'  THEN 5
            ELSE NULL
        END AS SCORE
    FROM HARMONISED.PRODUCTION.TWILIO_EVENTS
    WHERE TYPE LIKE '%CSAT%'
      AND EVENTTYPE = 'task.created'
      AND EVENTTIMESTAMP >= '{range_start}'
      AND EVENTTIMESTAMP <  '{range_end}'
),
csat_per_call AS (
    SELECT
        CALLSID,
        MAX(CASE WHEN TYPE = 'CSAT_Interaction' AND SCORE BETWEEN 1 AND 5 THEN SCORE END) AS CSAT_INTERACTION,
        MAX(CASE WHEN TYPE = 'CSAT_Resolution'  AND SCORE BETWEEN 1 AND 5 THEN SCORE END) AS CSAT_RESOLUTION
    FROM score_clean
    GROUP BY CALLSID
),
calls AS (
    SELECT
        CASE WHEN a.WORKERFULLNAME LIKE 'Theo J%' THEN 'Theo Johannes'
             ELSE a.WORKERFULLNAME
        END                                        AS AGENT_NAME,
        DATE_TRUNC('week', a.EVENTTIMESTAMP)::DATE AS WEEK_COMMENCING,
        c.CSAT_INTERACTION,
        c.CSAT_RESOLUTION
    FROM HARMONISED.PRODUCTION.TWILIO_EVENTS a
    LEFT JOIN csat_per_call c ON a.CUSTOMERCALLSID = c.CALLSID
    WHERE a.WORKERTEAM = 'Storage'
      AND a.EVENTTYPE  = 'task.completed'
      AND a.EVENTTIMESTAMP >= '{range_start}'
      AND a.EVENTTIMESTAMP <  '{range_end}'
      AND (
            a.WORKERFULLNAME IN ('Shafwaan Titus', 'Shaun Gae', 'Emmanuel Nsenga')
         OR a.WORKERFULLNAME LIKE 'Theo J%'
      )
)
SELECT
    AGENT_NAME,
    WEEK_COMMENCING,
    COUNT(*)                                                                   AS interactions,
    COUNT(CSAT_INTERACTION)                                                    AS responses,
    ROUND(AVG(CSAT_INTERACTION), 2)                                            AS avg_csat,
    ROUND(
        SUM(CASE WHEN CSAT_RESOLUTION >= 4 THEN 1.0 ELSE 0.0 END)
        / NULLIF(COUNT(CSAT_RESOLUTION), 0) * 100
    , 1)                                                                       AS resolution_pct
FROM calls
GROUP BY 1, 2
ORDER BY 2, 1
""")
    result = {}
    for agent, wc, interactions, responses, avg_csat, resolution_pct in cur.fetchall():
        wc_str = wc.strftime("%Y-%m-%d") if hasattr(wc, "strftime") else str(wc)[:10]
        result[(wc_str, agent)] = (interactions, responses, avg_csat, resolution_pct)
    return result


def fetch_nps(cur, range_start, range_end):
    cur.execute(f"""
WITH STORAGE_DETAILS AS (
    SELECT
        LISTING_ID,
        MAX(DEAL_ID)            AS DEAL_ID,
        MAX(STORAGE_EVENT_TYPE) AS EVENT_TYPE
    FROM CONFORMED.PRODUCTION.FCT_STORAGE
    GROUP BY LISTING_ID
    HAVING MAX(DEAL_ID) IS NOT NULL
),
FEEDBACK AS (
    SELECT
        LISTING_ID,
        MIN(OPERATIONS_CUSTOMER_FEEDBACK_DATE) AS FEEDBACK_DATE,
        MIN(OPERATIONS_FEEDBACK_SCORE_NPS)     AS NPS_SCORE
    FROM CONFORMED.PRODUCTION.FCT_FEEDBACK
    WHERE OPERATIONS_CUSTOMER_FEEDBACK_DATE IS NOT NULL
      AND OPERATIONS_FEEDBACK_SCORE_NPS IS NOT NULL
    GROUP BY LISTING_ID
)
SELECT
    DATE_TRUNC('week', F.FEEDBACK_DATE)::DATE               AS WEEK_COMMENCING,
    SD.EVENT_TYPE,
    COUNT(*)                                                 AS responses,
    SUM(CASE WHEN F.NPS_SCORE = 100  THEN 1 ELSE 0 END)     AS promoters,
    SUM(CASE WHEN F.NPS_SCORE = 0    THEN 1 ELSE 0 END)     AS passives,
    SUM(CASE WHEN F.NPS_SCORE = -100 THEN 1 ELSE 0 END)     AS detractors,
    ROUND(AVG(F.NPS_SCORE), 0)                               AS nps_score
FROM FEEDBACK F
INNER JOIN STORAGE_DETAILS SD ON F.LISTING_ID = SD.LISTING_ID
WHERE F.FEEDBACK_DATE >= '{range_start}'
  AND F.FEEDBACK_DATE <  '{range_end}'
GROUP BY 1, 2
ORDER BY 1, 2
""")
    weekly = {}
    for wc, event_type, responses, promoters, passives, detractors, nps_score in cur.fetchall():
        wc_str = wc.strftime("%Y-%m-%d") if hasattr(wc, "strftime") else str(wc)[:10]
        key = "collection" if event_type == "Collection" else "redelivery"
        weekly.setdefault(wc_str, {})[key] = {
            "score":      int(nps_score)  if nps_score  is not None else 0,
            "responses":  int(responses),
            "promoters":  int(promoters),
            "passives":   int(passives),
            "detractors": int(detractors),
        }
    # Build combined
    for wc_str, types in weekly.items():
        col   = types.get("collection", {"responses": 0, "promoters": 0, "passives": 0, "detractors": 0})
        red   = types.get("redelivery", {"responses": 0, "promoters": 0, "passives": 0, "detractors": 0})
        total = col["responses"]  + red["responses"]
        prom  = col["promoters"]  + red["promoters"]
        det   = col["detractors"] + red["detractors"]
        types["combined"] = {
            "score":      round((prom - det) / total * 100) if total > 0 else 0,
            "responses":  total,
            "promoters":  prom,
            "passives":   col["passives"] + red["passives"],
            "detractors": det,
        }
    return weekly


# ─── SUPABASE ──────────────────────────────────────────────────────────────────

def upsert(table, rows):
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "Storage reporting now reads live from Snowflake via AV Dashboards managed\n"
        "queries, and hand-entered data lives in AV Dashboards state.\n"
        "See dashboards.anyvan.com/operations/storage-nav-config."
    )
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=SUPABASE_HEADERS,
        json=rows,
    )
    if resp.status_code not in (200, 201):
        print(f"   ❌ Supabase ({table}): {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    weeks       = resolve_weeks()
    range_start = weeks[0].strftime("%Y-%m-%d")
    range_end   = (weeks[-1] + timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"\n🚀 fetch_csat.py — {len(weeks)} week(s): {range_start} → {weeks[-1]}\n")

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

    # ── CSAT ──────────────────────────────────────────────────────────────────
    print("📞 Fetching CSAT...")
    csat_data = fetch_csat(cur, range_start, range_end)

    csat_rows = []
    for week_monday in weeks:
        wc_str     = week_monday.strftime("%Y-%m-%d")
        week_count = 0
        for sf_name, display_name in CSAT_AGENTS.items():
            key = (wc_str, sf_name)
            if key not in csat_data:
                continue
            interactions, responses, avg_csat, resolution_pct = csat_data[key]
            csat_rows.append({
                "week_commencing": wc_str,
                "agent":           display_name,
                "interactions":    int(interactions)      if interactions   is not None else None,
                "responses":       int(responses)         if responses      is not None else None,
                "avg_csat":        float(avg_csat)        if avg_csat       is not None else None,
                "resolution_pct":  float(resolution_pct)  if resolution_pct is not None else None,
            })
            week_count += 1
        print(f"   {wc_str} CSAT — {week_count} agents")

    # ── NPS ───────────────────────────────────────────────────────────────────
    print("📊 Fetching NPS...")
    nps_data = fetch_nps(cur, range_start, range_end)

    nps_rows = []
    for wc_str, types in nps_data.items():
        for nps_type, vals in types.items():
            nps_rows.append({
                "week_commencing": wc_str,
                "type":            nps_type,
                "score":           vals["score"],
                "responses":       vals["responses"],
                "promoters":       vals["promoters"],
                "passives":        vals["passives"],
                "detractors":      vals["detractors"],
            })
    print(f"   {len(nps_data)} weeks of NPS data")

    cur.close()
    conn.close()

    print(f"\n📤 Upserting {len(csat_rows)} CSAT rows + {len(nps_rows)} NPS rows...")
    for i in range(0, len(csat_rows), 100):
        upsert("csat_weekly", csat_rows[i:i+100])
    for i in range(0, len(nps_rows), 100):
        upsert("nps_weekly", nps_rows[i:i+100])

    print("✅ Done.\n")


if __name__ == "__main__":
    main()
