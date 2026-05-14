#!/usr/bin/env python3
"""Split week_snippets.json into batch files for Haiku classification."""

import json, math
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
src = json.loads((DATA / "week_snippets.json").read_text())
calls = src["calls"]

BATCH_SIZE = 200
batches_dir = DATA / "batches"
batches_dir.mkdir(exist_ok=True)
for p in batches_dir.glob("batch_*.json"):
    p.unlink()

n_batches = math.ceil(len(calls) / BATCH_SIZE)
for i in range(n_batches):
    chunk = calls[i * BATCH_SIZE : (i + 1) * BATCH_SIZE]
    minimal = [
        {"event_id": c["event_id"], "agent_name": c["agent_name"], "snippets": c["snippets"]}
        for c in chunk
    ]
    (batches_dir / f"batch_{i+1:02d}.json").write_text(json.dumps(minimal, indent=2))

print(f"wrote {n_batches} batches of up to {BATCH_SIZE} calls each ({len(calls)} total)")
