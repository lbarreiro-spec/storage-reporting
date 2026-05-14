#!/usr/bin/env python3
"""Aggregate batch results → per-agent mention rates for w/c 11 May → append a column to call-grading.html."""

import json, re, sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
HTML = REPO / "reports" / "call-grading.html"

sys.path.insert(0, str(REPO / "scripts"))
import fetch_call_grading as fcg

# ── 1. Load batch results
classifications = {}  # event_id -> bool
for f in sorted((DATA / "results").glob("batch_*.json")):
    for eid, v in json.loads(f.read_text()).items():
        classifications[eid] = (v.strip().lower().startswith("y"))
print(f"loaded {len(classifications)} classifications")

# ── 2. Load source snippets (gives us event_id -> agent_name)
src = json.loads((DATA / "week_snippets.json").read_text())
event_to_agent = {c["event_id"]: c["agent_name"] for c in src["calls"]}

# ── 3. Tally pitched calls per agent_name
pitched = defaultdict(int)
for eid, is_pitch in classifications.items():
    if is_pitch:
        pitched[event_to_agent.get(eid, "")] += 1

# ── 4. Totals from src
totals = {t["agent_name"]: int(t["total_calls"]) for t in src["totals"]}
agent_team = {t["agent_name"]: t["team_name"] for t in src["totals"]}

# ── 5. Per-agent mention rate
rates = {}
for name, total in totals.items():
    p = pitched.get(name, 0)
    rates[name] = round(p * 100 / total, 1) if total else None

# ── 6. Map agent → section id
agent_section = {}
unknown_teams = set()
for name, team in agent_team.items():
    if team in fcg.TEAM_MAP:
        agent_section[name] = fcg.TEAM_MAP[team][2]
    else:
        unknown_teams.add(team)
if unknown_teams:
    print(f"⚠ unmapped teams (agents skipped): {sorted(unknown_teams)}")

# ── 7. Load existing HTML, parse WEEKS and SECTIONS
html = HTML.read_text()

# Find WEEKS line
m_weeks = re.search(r'(const WEEKS=)(\[[^\]]+\])', html)
weeks_arr = json.loads(m_weeks.group(2))
print(f"existing WEEKS: {weeks_arr}")

NEW_WEEK_LABEL = "11 May"
if NEW_WEEK_LABEL in weeks_arr:
    print(f"week {NEW_WEEK_LABEL} already present — replacing column")
    new_idx = weeks_arr.index(NEW_WEEK_LABEL)
    weeks_arr_new = weeks_arr
    insert_mode = "replace"
else:
    weeks_arr_new = weeks_arr + [NEW_WEEK_LABEL]
    new_idx = len(weeks_arr)
    insert_mode = "append"

# Find SECTIONS block
m_sections = re.search(r'const SECTIONS=\[\n(.*?)\n\];', html, re.DOTALL)
sections_text = m_sections.group(1)

# Parse each agent line: pattern `    {n:"Name",r:[v,v,...]},`
def update_agent_line(line):
    m = re.match(r'(\s*\{n:")([^"]+)("\s*,r:\[)([^\]]*)(\].*)', line)
    if not m:
        return line, None
    prefix, name, mid, vals_str, suffix = m.groups()
    # current values
    vals = [v.strip() for v in vals_str.split(",")]
    rate = rates.get(name)
    new_val = "N" if rate is None else (f"{int(rate)}" if rate == int(rate) else f"{rate}")
    if insert_mode == "append":
        vals.append(new_val)
    else:
        vals[new_idx] = new_val
    new_line = f"{prefix}{name}{mid}{','.join(vals)}{suffix}"
    return new_line, name

updated_names = set()
new_lines = []
for line in sections_text.split("\n"):
    nl, name = update_agent_line(line)
    new_lines.append(nl)
    if name is not None:
        updated_names.add(name)

new_sections_text = "\n".join(new_lines)
new_html = html.replace(m_sections.group(0), f"const SECTIONS=[\n{new_sections_text}\n];")
new_html = new_html.replace(m_weeks.group(0), f"{m_weeks.group(1)}{json.dumps(weeks_arr_new, separators=(',',':'))}")

HTML.write_text(new_html)
print(f"✓ updated {HTML}")
print(f"  agents updated: {len(updated_names)}")
print(f"  agents in rates not found in HTML: {sorted(set(rates) - updated_names)[:20]}")

# Print summary
total_pitched = sum(pitched.values())
total_calls = sum(totals.values())
print(f"  totals: {total_pitched} pitched / {total_calls} calls = {round(total_pitched*100/total_calls,1)}% overall")
