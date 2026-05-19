#!/usr/bin/env python3
"""Bake data/call_grading_sections.json into reports/call-grading.html.

The report HTML has inline `const WEEKS=[...]` and `const SECTIONS=[...]`
arrays. This script updates them from the JSON produced by
fetch_call_grading.py without requiring a JS fetch.

Keeps only the last MAX_WEEKS columns to stop the board sprawling.

Usage:
  python3 scripts/bake_call_grading_html.py
"""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JSON_IN = REPO / "data" / "call_grading_sections.json"
HTML = REPO / "reports" / "call-grading.html"

# Trim the board to the most recent N weekly columns.
MAX_WEEKS = 8

payload = json.loads(JSON_IN.read_text())
new_weeks = payload["WEEKS"]
new_sections = payload["SECTIONS"]

html = HTML.read_text()

# Locate the inline WEEKS and SECTIONS arrays we're going to overwrite.
m_weeks = re.search(r'(const WEEKS=)(\[[^\]]+\])', html)
existing_weeks = json.loads(m_weeks.group(2))
m_sections = re.search(r'const SECTIONS=\[\n(.*?)\n\];', html, re.DOTALL)

# new_weeks is the chronologically ordered trailing window from the fetch.
# Take the most recent MAX_WEEKS directly — no merge with HTML history needed
# (the fetch always returns more weeks than we display).
merged_weeks = new_weeks[-MAX_WEEKS:]
slice_start = len(new_weeks) - len(merged_weeks)

def fmt_val(v):
    if v is None:
        return "N"
    if v == int(v):
        return str(int(v))
    return str(v)

# Map section.id -> section.label for header consistency
new_section_lines = []
for sec in new_sections:
    label = sec["label"]
    group = sec["group"]
    sid = sec["id"]
    new_section_lines.append(f'  {{id:"{sid}",group:"{group}",label:"{label}",agents:[')
    for a in sec["agents"]:
        r_arr = [fmt_val(v) for v in a["r"][slice_start:]]
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
