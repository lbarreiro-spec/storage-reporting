#!/usr/bin/env python3
"""Bake data/call_grading_sections.json into reports/call-grading.html.

The report HTML has inline `const WEEKS=[...]` and `const SECTIONS=[...]`
arrays. This script updates them from the JSON produced by
fetch_call_grading.py without requiring a JS fetch.

Usage:
  python3 scripts/bake_call_grading_html.py
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JSON_IN = REPO / "data" / "call_grading_sections.json"
HTML = REPO / "reports" / "call-grading.html"

payload = json.loads(JSON_IN.read_text())
new_weeks = payload["WEEKS"]
new_sections = payload["SECTIONS"]

html = HTML.read_text()

# Parse existing WEEKS and SECTIONS so we can merge — the HTML may have
# historical weeks we want to preserve, with new-week data appended.
m_weeks = re.search(r'(const WEEKS=)(\[[^\]]+\])', html)
existing_weeks = json.loads(m_weeks.group(2))

# Build merged WEEKS list (preserve order; append new weeks that aren't there)
merged_weeks = list(existing_weeks)
for w in new_weeks:
    if w not in merged_weeks:
        merged_weeks.append(w)

# Build map: agent_name -> existing r: array (so we keep historical values)
m_sections = re.search(r'const SECTIONS=\[\n(.*?)\n\];', html, re.DOTALL)
sections_text = m_sections.group(1)
existing_agent_r = {}
for line in sections_text.split("\n"):
    m = re.match(r'\s*\{n:"([^"]+)"\s*,r:\[([^\]]*)\]', line)
    if m:
        existing_agent_r[m.group(1)] = [v.strip() for v in m.group(2).split(",")]

# Build new SECTIONS text from the JSON payload, but merge each agent's r
# array with their historical values: extend existing to len(merged_weeks)
# padding with N, then overwrite the indexes corresponding to new_weeks.
def fmt_val(v):
    if v is None:
        return "N"
    if v == int(v):
        return str(int(v))
    return str(v)

def merged_r_for_agent(name, new_r):
    existing = list(existing_agent_r.get(name, []))
    # extend up to len(merged_weeks) with "N"
    while len(existing) < len(merged_weeks):
        existing.append("N")
    # overwrite the position(s) of new_weeks
    for i, wk in enumerate(new_weeks):
        idx = merged_weeks.index(wk)
        existing[idx] = fmt_val(new_r[i])
    return existing

# Map section.id -> section.label for header consistency
new_section_lines = []
for sec in new_sections:
    label = sec["label"]
    group = sec["group"]
    sid = sec["id"]
    new_section_lines.append(f'  {{id:"{sid}",group:"{group}",label:"{label}",agents:[')
    for a in sec["agents"]:
        r_arr = merged_r_for_agent(a["n"], a["r"])
        new_section_lines.append(f'    {{n:"{a["n"]}",r:[{",".join(r_arr)}]}},')
    new_section_lines.append("  ]},")

new_sections_text = "\n".join(new_section_lines)

new_html = html.replace(
    m_weeks.group(0),
    f"{m_weeks.group(1)}{json.dumps(merged_weeks, separators=(',', ':'))}",
)
new_html = new_html.replace(
    m_sections.group(0),
    f"const SECTIONS=[\n{new_sections_text}\n];",
)

HTML.write_text(new_html)
print(f"✓ baked {HTML.name}")
print(f"  weeks: {len(merged_weeks)} (added: {[w for w in new_weeks if w not in existing_weeks]})")
print(f"  sections: {len(new_sections)}")
print(f"  agents: {sum(len(s['agents']) for s in new_sections)}")
