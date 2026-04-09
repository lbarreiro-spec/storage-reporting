#!/usr/bin/env python3
"""
AnyVan Storage — Daily Activity Fetcher
Source: Snowflake (FACT_AGENT_ACTIVITY + TWILIO_EVENTS)
Writes to Supabase: daily_activity

Usage:
  python3 fetch_daily_activity.py              # yesterday only
  python3 fetch_daily_activity.py 2026-03      # full month backfill
  python3 fetch_daily_activity.py 2026-03-15   # specific date
"""

import os
import sys
import requests
import snowflake.connector
from datetime import date, timedelta, timezone
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")

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

# Snowflake name → (display name, team)
AGENTS = {
    "Andy N":           ("Andy",      "sales"),
    "Carla Jacobs":     ("Carla",     "sales"),
    "Conni T":          ("Conni",     "sales"),
    "Dylan Christian":  ("Dylan",     "sales"),
    "Emmanuel Nsenga":  ("Emmanuel",  "ops"),
    "Michelle J":       ("Michelle",  "sales"),
    "Prosper Mubata":   ("Prosper",   "sales"),
    "Shaun Gae":        ("Shaun",     "ops"),
    "Theo J":           ("Theo",      "ops"),
    "Shafwaan Titus":   ("Shafwaan",  "ops"),
}

TODAY = date.today()


# ─── DATE RANGE ────────────────────────────────────────────────────────────────

def resolve_dates():
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else None
    if not arg:
        # On Mondays, backfill Friday + Saturday + Sunday
        if TODAY.weekday() == 0:
            return [TODAY - timedelta(days=3), TODAY - timedelta(days=2), TODAY - timedelta(days=1)]
        return [TODAY - timedelta(days=1)]
    # Month: 2026-03
    try:
        parts = arg.split("-")
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            start = date(year, month, 1)
            end   = min((start + relativedelta(months=1)) - timedelta(days=1), TODAY - timedelta(days=1))
            days  = []
            d = start
            while d <= end:
                days.append(d)
                d += timedelta(days=1)
            return days
    except ValueError:
        pass
    # Specific date: 2026-03-15
    return [date.fromisoformat(arg)]


# ─── SNOWFLAKE ─────────────────────────────────────────────────────────────────

def get_sf_token():
    toml = open(os.path.expanduser("~/.snowflake/connections.toml")).read()
    return toml.split('token = "')[1].split('"')[0]


def fetch_day(cur, date_str):
    # Activity duration totals (in seconds)
    cur.execute(f"""
        SELECT WORKERFULLNAME,
               AVAILABLE, BREAK, LUNCH, ADMIN, OB_ACTIVITY,
               OFFLINE, PERSONAL, SYSTEM_ISSUE, TICKETING, LIVE_CHAT, ONLINE_DURATION
        FROM MART_SALES_OPS.PRODUCTION.FACT_AGENT_ACTIVITY
        WHERE WORKERTEAM = 'Storage'
          AND DATE = '{date_str}'
    """)
    stats = {row[0]: row[1:] for row in cur.fetchall()}

    # First login / last logout
    cur.execute(f"""
        SELECT
            WORKERFULLNAME,
            MIN(CASE WHEN EVENTTYPE = 'worker.activity.update'
                      AND WORKERACTIVITYNAME IN ('Available', 'Admin')
                 THEN EVENTTIMESTAMP END) AS first_login,
            MAX(CASE WHEN EVENTTYPE = 'worker.activity.update'
                      AND WORKERACTIVITYNAME = 'Offline'
                 THEN EVENTTIMESTAMP END) AS last_logout
        FROM HARMONISED.PRODUCTION.TWILIO_EVENTS
        WHERE WORKERTEAM = 'Storage'
          AND DATE(EVENTTIMESTAMP) = '{date_str}'
        GROUP BY 1
    """)
    logins = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    return stats, logins


# ─── SUPABASE ──────────────────────────────────────────────────────────────────

def upsert_rows(rows):
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/daily_activity",
        headers=SUPABASE_HEADERS,
        json=rows,
    )
    if resp.status_code not in (200, 201):
        print(f"   ❌ Supabase: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    days = resolve_dates()
    print(f"\n🚀 fetch_daily_activity.py — {len(days)} day(s): {days[0]} → {days[-1]}\n")

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
        stats, logins = fetch_day(cur, date_str)

        day_agents = 0
        for sf_name, (display_name, team) in AGENTS.items():
            s = stats.get(sf_name)
            l = logins.get(sf_name, (None, None))

            if s is None and l[0] is None and l[1] is None:
                continue  # no data for this agent on this day

            first_login  = l[0].replace(tzinfo=timezone.utc).astimezone(UK_TZ).strftime("%H:%M") if l[0] else None
            last_logout  = l[1].replace(tzinfo=timezone.utc).astimezone(UK_TZ).strftime("%H:%M") if l[1] else None

            all_rows.append({
                "date":            date_str,
                "agent":           display_name,
                "team":            team,
                "available":       int(s[0])  if s and s[0]  is not None else None,
                "break_time":      int(s[1])  if s and s[1]  is not None else None,
                "lunch":           int(s[2])  if s and s[2]  is not None else None,
                "admin":           int(s[3])  if s and s[3]  is not None else None,
                "ob_activity":     int(s[4])  if s and s[4]  is not None else None,
                "offline":         int(s[5])  if s and s[5]  is not None else None,
                "personal":        int(s[6])  if s and s[6]  is not None else None,
                "system_issue":    int(s[7])  if s and s[7]  is not None else None,
                "ticketing":       int(s[8])  if s and s[8]  is not None else None,
                "live_chat":       int(s[9])  if s and s[9]  is not None else None,
                "online_duration": int(s[10]) if s and s[10] is not None else None,
                "first_login":     first_login,
                "last_logout":     last_logout,
            })
            day_agents += 1

        print(f"   {date_str} — {day_agents} agents")

    cur.close()
    conn.close()

    print(f"\n📤 Upserting {len(all_rows)} rows to Supabase...")
    # Upsert in batches of 100
    for i in range(0, len(all_rows), 100):
        upsert_rows(all_rows[i:i+100])

    print("✅ Done.\n")


if __name__ == "__main__":
    main()
