#!/usr/bin/env python3
"""
AnyVan Storage — Voice Daily Fetcher
Source: Snowflake (FACT_VOICE_ACTIVITY)
Writes to Supabase: voice_daily

Usage:
  python3 fetch_voice.py              # yesterday only
  python3 fetch_voice.py 2026-03      # full month backfill
  python3 fetch_voice.py 2026-03-15   # specific date
"""

import os
import sys
import requests
import snowflake.connector
from datetime import date, timedelta
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

# Snowflake WORKERFULLNAME → (display name, team)
AGENTS = {
    "Theo J":           ("Theo",      "ops"),
    "Shaun Gae":        ("Shaun",     "ops"),
    "Shafwaan Titus":   ("Shafwaan",  "ops"),
    "Emmanuel Nsenga":  ("Emmanuel",  "ops"),
    "Dylan Christian":  ("Dylan",     "sales"),
    "Conni T":          ("Conni",     "sales"),
    "Andy N":           ("Andy",      "sales"),
    "Carla Jacobs":     ("Carla",     "sales"),
    "Michelle J":       ("Michelle",  "sales"),
    "Prosper Mubata":   ("Prosper",   "sales"),
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


def query_voice(cur, date_str, voicetype_filter):
    cur.execute(f"""
        SELECT
            WORKERFULLNAME,
            SUM(DIALLED)                                         AS dialled,
            SUM(ANSWERED)                                        AS answered,
            ROUND(SUM(TALKTIME), 0)                              AS talk_time,
            ROUND(SUM(TALKTIME) / NULLIF(SUM(ANSWERED), 0), 0)  AS aht
        FROM MART_SALES_OPS.PRODUCTION.FACT_VOICE_ACTIVITY
        WHERE DATE = '{date_str}'
          AND {voicetype_filter}
          AND (UPPER(SPLIT_PART(WORKERFULLNAME, ' ', 1)) != 'SHAUN' OR WORKERFULLNAME = 'Shaun Gae')
        GROUP BY 1
    """)
    return {row[0]: row[1:] for row in cur.fetchall()}


# ─── SUPABASE ──────────────────────────────────────────────────────────────────

def upsert_rows(rows):
    raise RuntimeError(
        "Supabase has been retired (Aug 2026). This write is disabled.\n"
        "Storage reporting now reads live from Snowflake via AV Dashboards managed\n"
        "queries, and hand-entered data lives in AV Dashboards state.\n"
        "See dashboards.anyvan.com/operations/storage-nav-config."
    )
    if not rows:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/voice_daily",
        headers=SUPABASE_HEADERS,
        json=rows,
    )
    if resp.status_code not in (200, 201):
        print(f"   ❌ Supabase: {resp.status_code} {resp.text[:200]}")
        resp.raise_for_status()


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    days = resolve_dates()
    print(f"\n🚀 fetch_voice.py — {len(days)} day(s): {days[0]} → {days[-1]}\n")

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
        ib = query_voice(cur, date_str, "VOICETYPE = 'Inbound'")
        ob = query_voice(cur, date_str, "VOICETYPE IN ('Outbound', 'Manual')")

        day_agents = 0
        for sf_name, (display_name, team) in AGENTS.items():
            i = ib.get(sf_name)
            o = ob.get(sf_name)
            if i is None and o is None:
                continue

            all_rows.append({
                "date":        date_str,
                "agent":       display_name,
                "team":        team,
                "ib_dialled":  int(i[0]) if i and i[0] is not None else None,
                "ib_answered": int(i[1]) if i and i[1] is not None else None,
                "ib_talktime": int(i[2]) if i and i[2] is not None else None,
                "ib_aht":      int(i[3]) if i and i[3] is not None else None,
                "ob_dialled":  int(o[0]) if o and o[0] is not None else None,
                "ob_answered": int(o[1]) if o and o[1] is not None else None,
                "ob_talktime": int(o[2]) if o and o[2] is not None else None,
                "ob_aht":      int(o[3]) if o and o[3] is not None else None,
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
