#!/usr/bin/env python3
"""Dump candidate storage snippets for w/c 11 May 2026 grouped by call.
Output: data/week_snippets.json with one entry per call (agent + all storage lines)."""

import json, os, re, sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_call_grading as fcg
import snowflake.connector

WEEK_START = date(2026, 5, 11)
WEEK_END = WEEK_START + timedelta(days=7)

tok = re.search(r'token\s*=\s*"([^"]+)"', Path.home().joinpath(".snowflake/connections.toml").read_text()).group(1)
conn = snowflake.connector.connect(
    account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
    authenticator="programmatic_access_token", token=tok,
    warehouse=fcg.SNOWFLAKE_WH, role=fcg.SNOWFLAKE_ROLE,
)
cur = conn.cursor()
print(f"Fetching candidate lines for w/c {WEEK_START}…", flush=True)
rows = fcg.fetch_candidate_lines(cur, WEEK_START, WEEK_END)
print(f"  {len(rows)} candidate lines from {len({r['event_id'] for r in rows})} calls", flush=True)

# Group by call
by_call = defaultdict(lambda: {"agent_name": None, "agent_email": None, "team_name": None, "snippets": []})
for r in rows:
    eid = r["event_id"]
    by_call[eid]["agent_name"] = r["agent_name"]
    by_call[eid]["agent_email"] = r["agent_email"]
    by_call[eid]["team_name"] = r["team_name"]
    by_call[eid]["snippets"].append(fcg.build_snippet(r))

# Also fetch totals
totals_rows = fcg.fetch_total_calls_per_agent_week(cur, WEEK_START, WEEK_END)
totals = [
    {"agent_email": e, "agent_name": n, "team_name": t, "total_calls": int(c)}
    for e, n, t, c in totals_rows
]

outdir = Path(__file__).resolve().parent.parent / "data"
outdir.mkdir(exist_ok=True)
(outdir / "week_snippets.json").write_text(json.dumps({
    "week_start": WEEK_START.isoformat(),
    "calls": [{"event_id": eid, **info} for eid, info in by_call.items()],
    "totals": totals,
}, indent=2))
print(f"✓ wrote {outdir / 'week_snippets.json'}  ({len(by_call)} calls, {len(totals)} agents)")
cur.close(); conn.close()
