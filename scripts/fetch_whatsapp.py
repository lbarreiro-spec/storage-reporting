#!/usr/bin/env python3
"""
AnyVan Storage — WhatsApp Daily Fetcher
Sources:
  - IB/OB: Snowflake MART_SALES_OPS.PRODUCTION.FACT_WHATSAPP_ACTIVITY
  - Engagement: Snowflake HARMONISED.PRODUCTION.TWILIO_CONVERSATION_MESSAGE
Writes to Supabase: whatsapp_daily

Usage:
  python3 scripts/fetch_whatsapp.py              # yesterday
  python3 scripts/fetch_whatsapp.py 2026-03      # full month backfill
  python3 scripts/fetch_whatsapp.py 2026-03-15   # specific date
"""

import os
import sys
import requests
import snowflake.connector
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

# ─── CONFIG ────────────────────────────────────────────────────────────────────

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
    "Prefer":        "resolution=merge-duplicates",
}

# WORKERFULLNAME → (display name, team)
AGENTS = {
    "Theo J":          ("Theo",     "ops"),
    "Shaun Gae":       ("Shaun",    "ops"),
    "Shafwaan Titus":  ("Shafwaan", "ops"),
    "Emmanuel Nsenga": ("Emmanuel", "ops"),
    "Dylan Christian": ("Dylan",    "sales"),
    "Conni T":         ("Conni",    "sales"),
    "Andy N":          ("Andy",     "sales"),
    "Carla Jacobs":    ("Carla",    "sales"),
    "Michelle J":      ("Michelle", "sales"),
    "Prosper Mubata":  ("Prosper",  "sales"),
}

# URL-encoded email → display name (TWILIO_CONVERSATION_MESSAGE AUTHOR field)
AGENT_EMAILS = {
    "theo_2Ej_40anyvan_2Ecom":           "Theo",
    "shaun_2Egae_40anyvan_2Ecom":        "Shaun",
    "shafwaan_2Etitus_40anyvan_2Ecom":   "Shafwaan",
    "emmanuel_2Ensenga_40anyvan_2Ecom":  "Emmanuel",
    "dylan_2Echristian_40anyvan_2Ecom":  "Dylan",
    "conni_2Et_40anyvan_2Ecom":          "Conni",
    "andy_2En_40anyvan_2Ecom":           "Andy",
    "carla_2Ejacobs_40anyvan_2Ecom":     "Carla",
    "michelle_2Ej_40anyvan_2Ecom":       "Michelle",
    "prosper_2Em_40anyvan_2Ecom":        "Prosper",
}

# Reverse map: display name → encoded email (for engagement lookup)
NAME_TO_EMAIL = {v: k for k, v in AGENT_EMAILS.items()}

TODAY = date.today()


# ─── DATE RANGE ────────────────────────────────────────────────────────────────

def resolve_dates():
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else None
    if not arg:
        # On Mondays, backfill Friday + Saturday + Sunday
        if TODAY.weekday() == 0:
            return [TODAY - timedelta(days=3), TODAY - timedelta(days=2), TODAY - timedelta(days=1)]
        return [TODAY - timedelta(days=1)]
    try:
        parts = arg.split("-")
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            start = date(year, month, 1)
            end   = min((start + relativedelta(months=1)) - timedelta(days=1), TODAY - timedelta(days=1))
            days, d = [], start
            while d <= end:
                days.append(d)
                d += timedelta(days=1)
            return days
    except ValueError:
        pass
    return [date.fromisoformat(arg)]


# ─── SNOWFLAKE ─────────────────────────────────────────────────────────────────

def get_sf_token():
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    return toml.split('token = "')[1].split('"')[0]


def query_whatsapp_activity(cur, date_str, is_outbound):
    """Returns per-agent: chats, avg_wait, avg_first_response, avg_wrap, talk_time"""
    type_filter = (
        "TYPE IN ('Outbound WhatsApp Response', 'PreListing WhatsApp Response')"
        if is_outbound else
        "TYPE NOT IN ('Outbound WhatsApp Response', 'PreListing WhatsApp Response')"
    )
    cur.execute(f"""
        SELECT
            WORKERFULLNAME,
            COUNT(DISTINCT TASKID)              AS chats,
            ROUND(AVG(TOTAL_WAITING_TIME), 0)   AS avg_wait,
            ROUND(AVG(FIRSTRESPONSETIME), 0)    AS avg_first_response,
            ROUND(AVG(WRAPTIME), 0)             AS avg_wrap,
            ROUND(SUM(TALKTIME), 0)             AS talk_time
        FROM MART_SALES_OPS.PRODUCTION.FACT_WHATSAPP_ACTIVITY
        WHERE DATE = '{date_str}'
          AND WORKERTEAM = 'Storage'
          AND {type_filter}
        GROUP BY 1
    """)
    return {row[0]: row[1:] for row in cur.fetchall()}


def query_engagement(cur, date_str):
    """Returns per-agent: convos_initiated, messages_sent, customers_replied"""
    email_list = "','".join(AGENT_EMAILS.keys())
    cur.execute(f"""
        WITH initiated AS (
            SELECT CONVERSATION_ID, AUTHOR
            FROM HARMONISED.PRODUCTION.TWILIO_CONVERSATION_MESSAGE
            WHERE TRY_CAST("INDEX" AS INT) = 0
              AND AUTHOR IN ('{email_list}')
              AND CREATED_AT::DATE = '{date_str}'
        ),
        customer_replied AS (
            SELECT DISTINCT i.CONVERSATION_ID
            FROM initiated i
            JOIN HARMONISED.PRODUCTION.TWILIO_CONVERSATION_MESSAGE m
                ON m.CONVERSATION_ID = i.CONVERSATION_ID
            WHERE TRY_CAST(m."INDEX" AS INT) > 0
              AND m.AUTHOR NOT IN ('{email_list}')
        ),
        msgs_sent AS (
            SELECT AUTHOR, COUNT(*) AS msg_count
            FROM HARMONISED.PRODUCTION.TWILIO_CONVERSATION_MESSAGE
            WHERE CREATED_AT::DATE = '{date_str}'
              AND AUTHOR IN ('{email_list}')
            GROUP BY 1
        )
        SELECT
            i.AUTHOR,
            COUNT(DISTINCT i.CONVERSATION_ID)                                                   AS convos_initiated,
            COALESCE(ms.msg_count, 0)                                                           AS messages_sent,
            COUNT(DISTINCT CASE WHEN cr.CONVERSATION_ID IS NOT NULL THEN i.CONVERSATION_ID END) AS customers_replied
        FROM initiated i
        LEFT JOIN customer_replied cr ON i.CONVERSATION_ID = cr.CONVERSATION_ID
        LEFT JOIN msgs_sent ms ON i.AUTHOR = ms.AUTHOR
        GROUP BY i.AUTHOR, ms.msg_count
    """)
    return {row[0]: row[1:] for row in cur.fetchall()}


# ─── SUPABASE ──────────────────────────────────────────────────────────────────

def upsert_rows(rows):
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/whatsapp_daily",
        headers=SUPABASE_HEADERS,
        json=rows,
    )
    if resp.status_code not in (200, 201):
        print(f"   ❌ Supabase: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    days = resolve_dates()
    print(f"\n🚀 fetch_whatsapp.py — {len(days)} day(s): {days[0]} → {days[-1]}\n")

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

    all_rows = []
    for d in days:
        date_str = d.isoformat()
        ib  = query_whatsapp_activity(cur, date_str, is_outbound=False)
        ob  = query_whatsapp_activity(cur, date_str, is_outbound=True)
        eng = query_engagement(cur, date_str)

        day_agents = 0
        for sf_name, (display_name, team) in AGENTS.items():
            i     = ib.get(sf_name)
            o     = ob.get(sf_name)
            e_key = NAME_TO_EMAIL.get(display_name)
            e     = eng.get(e_key) if e_key else None

            if i is None and o is None and e is None:
                continue

            all_rows.append({
                "date":                  date_str,
                "agent":                 display_name,
                "team":                  team,
                "ib_chats":              int(i[0]) if i and i[0] is not None else None,
                "ib_avg_wait":           int(i[1]) if i and i[1] is not None else None,
                "ib_avg_first_response": int(i[2]) if i and i[2] is not None else None,
                "ib_avg_wrap":           int(i[3]) if i and i[3] is not None else None,
                "ib_talk_time":          int(i[4]) if i and i[4] is not None else None,
                "ob_chats":              int(o[0]) if o and o[0] is not None else None,
                "ob_talk_time":          int(o[4]) if o and o[4] is not None else None,
                "eng_convos_initiated":  int(e[0]) if e and e[0] is not None else None,
                "eng_messages_sent":     int(e[1]) if e and e[1] is not None else None,
                "eng_customers_replied": int(e[2]) if e and e[2] is not None else None,
            })
            day_agents += 1

        print(f"   {date_str} — {day_agents} agents")

    cur.close()
    conn.close()

    print(f"\n📤 Upserting {len(all_rows)} rows to Supabase...")
    for i in range(0, len(all_rows), 100):
        upsert_rows(all_rows[i:i+100])

    print("✅ Done.\n")


if __name__ == "__main__":
    main()
