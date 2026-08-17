#!/usr/bin/env python3
"""
SLA / SPEED enrichment (Stage 3, 18 Jun 2026) for the Freshdesk triage board.

Pulls OPEN Storage tickets from the DB-authoritative LIST endpoint with `include=stats`
(first_responded_at / resolved_at / SLA-escalation flags — cheap, ~one page set), computes
per-ticket speed fields, and MERGES them into the local feed (~/.anyvan/triage_feed.json) by id.

Per-ticket fields added:
  frt_h        first-response time in hours (created → first agent reply), None if not yet replied
  responded    bool — have we replied at all
  res_h        resolution time in hours (created → resolved), None if still open
  fr_breach    Freshdesk's own first-response SLA breach flag (fr_escalated)
  res_breach   Freshdesk's own resolution SLA breach flag (is_escalated)

Run BEFORE freshdesk_triage_dashboard.py so the board renders the SLA panel from fresh data.
Safe + additive: only sets these fields, never removes tickets or other fields.
"""
import json, os, sys
import freshdesk_triage as ft

FEED = os.path.expanduser('~/.anyvan/triage_feed.json')

def _hrs(a, b):
    """hours between two ISO timestamps a→b; None if either missing."""
    if not a or not b: return None
    da, db = ft.pdt(a), ft.pdt(b)
    if not da or not db: return None
    return round((db - da).total_seconds() / 3600.0, 2)

def pull_stats(days_back=45, max_pages=80):
    from datetime import timedelta
    import time
    since = (ft.now - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
    out = {}; page = 1
    while page <= max_pages:
        r = ft.rget(f"{ft.BASE}/tickets",
                    params={"updated_since": since, "order_by": "updated_at", "order_type": "desc",
                            "per_page": 100, "page": page, "include": "stats"}, timeout=30)
        if r is None or r.status_code != 200:
            if page == 1 and r is not None: print("stats HTTP", r.status_code, r.text[:120], file=sys.stderr)
            break
        batch = r.json()
        if not isinstance(batch, list) or not batch: break
        for t in batch:
            if t.get('group_id') in ft.GROUPS:
                out[t['id']] = t
        if len(batch) < 100: break
        page += 1; time.sleep(0.3)
    return out

def main():
    if not os.path.exists(FEED):
        print("no feed to enrich", file=sys.stderr); return
    feed = json.load(open(FEED))
    stats = pull_stats()
    enriched = 0; responded = 0
    for t in feed.get('tickets', []):
        s = stats.get(t['id'])
        if not s: continue
        st = s.get('stats') or {}
        created = s.get('created_at')
        fr = st.get('first_responded_at')
        res = st.get('resolved_at')
        t['frt_h'] = _hrs(created, fr)
        t['responded'] = bool(fr)
        t['res_h'] = _hrs(created, res)
        t['fr_breach'] = bool(s.get('fr_escalated'))
        t['res_breach'] = bool(s.get('is_escalated'))
        enriched += 1
        if fr: responded += 1
    json.dump(feed, open(FEED, 'w'), indent=1)
    print(f"SLA-enriched {enriched} tickets ({responded} have a first response); merged -> {FEED}")

if __name__ == '__main__':
    main()
