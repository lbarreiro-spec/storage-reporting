#!/usr/bin/env python3
"""
Backfill internal (private) triage notes onto open Storage Freshdesk tickets.

- Source of RAG + reason: the published board feed (data/triage.json).
- Window: open tickets with age_h <= MAX_AGE_H (default 120 = last 5 days).
- Coverage: reds (detailed note = reason + customer's verbatim latest message) and
  amber (concise one-liner). Blue/green skipped.
- Safety: notes are posted ONLY to POST /tickets/{id}/notes with private=true
  (internal, zero recipients). Never /reply, never public. A sampled post-write
  assertion confirms private==true and to_emails==[].
- Dup-guard: a local state file (~/.anyvan/triage_notes_state.json) records noted
  ticket ids so re-runs never restack. Pre-seeded with tickets already noted by hand.
- Rate-limit: honours Retry-After on 429, plus a small delay between writes.

Usage:
  python3 freshdesk_notes_backfill.py            # live
  python3 freshdesk_notes_backfill.py --dry      # build payload + print plan, no writes
  python3 freshdesk_notes_backfill.py --max-age 120
"""
import os, re, json, base64, html, time, sys, urllib.request, urllib.error
from datetime import datetime, timezone

ROOT   = os.path.expanduser("~/Documents/storage-reporting")
FEED   = os.path.join(ROOT, "data", "triage.json")
STATE  = os.path.expanduser("~/.anyvan/triage_notes_state.json")
LOG    = "/tmp/triage_notes_backfill.log"
MAX_AGE_H = 120.0
DRY = "--dry" in sys.argv
if "--max-age" in sys.argv:
    MAX_AGE_H = float(sys.argv[sys.argv.index("--max-age")+1])

# tickets already noted by hand in earlier manual tests -> never re-note
PRESEED_NOTED = {2141957, 2140291}

DATESTAMP = datetime.now(timezone.utc).strftime("%-d %b %Y")
FOOTER = f"<i>🤖 AI triage · {DATESTAMP} · internal agent-only note — customer cannot see this</i>"

cfg = open(os.path.expanduser("~/.anyvan/config.txt")).read()
def grab(k):
    m = re.search(rf"{k}\s*=\s*(.+)", cfg); return m.group(1).strip().strip('"').strip("'") if m else None
KEY, DOM = grab("FRESHDESK_API_KEY"), grab("FRESHDESK_DOMAIN")
AUTH = base64.b64encode(f"{KEY}:X".encode()).decode()
BASE = f"https://{DOM}.freshdesk.com/api/v2"

def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def call(req, tries=20):
    for i in range(tries):
        try:
            return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "60")) + 3
                log(f"  429 throttled — sleeping {wait}s (attempt {i+1}/{tries})")
                time.sleep(wait); continue
            raise
    raise SystemExit("gave up after retries")

def clean(h):
    t = re.sub(r"<[^>]+>", " ", h or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()

def load_state():
    if os.path.exists(STATE):
        try: return set(json.load(open(STATE)).get("noted", []))
        except Exception: pass
    return set()

def save_state(noted):
    json.dump({"noted": sorted(noted)}, open(STATE, "w"), indent=0)

def latest_customer_msg(tid):
    """Return the most recent inbound customer message text for a ticket."""
    t = json.loads(call(urllib.request.Request(f"{BASE}/tickets/{tid}?include=conversations",
                    headers={"Authorization": f"Basic {AUTH}"})).read())
    incoming = [c for c in t.get("conversations", []) if c.get("incoming")]
    body = (incoming[-1].get("body") if incoming else t.get("description")) or ""
    return clean(body)

def red_note(reason, cust_msg, owner):
    excerpt = cust_msg[:550] + ("…" if len(cust_msg) > 550 else "")
    excerpt = html.escape(excerpt)
    parts = [
        "🔴🔴🔴 <b>TRIAGE: RED — URGENT</b> 🔴🔴🔴<br>",
        f"<b>Why red:</b> {html.escape(reason.rstrip(';. '))}<br>",
        f"<b>Customer's latest message:</b> “{excerpt}”<br>",
    ]
    if owner: parts.append(f"<b>Assigned:</b> {html.escape(owner)}<br>")
    parts.append("<b>Next step:</b> Review the booking/inventory + history, then action and reply.<br>")
    parts.append(FOOTER)
    return "".join(parts)

def amber_note(reason):
    return (f"🟠 <b>TRIAGE: AMBER</b> — real but not on fire<br>"
            f"<b>Reason:</b> {html.escape(reason.rstrip(';. '))}<br>{FOOTER}")

def post_note(tid, body, assert_private=False):
    n = json.loads(call(urllib.request.Request(f"{BASE}/tickets/{tid}/notes",
            data=json.dumps({"body": body, "private": True}).encode(),
            headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
            method="POST")).read())
    if assert_private:
        assert n.get("private") is True and not n.get("to_emails"), f"SAFETY FAIL on {tid}: {n.get('private')} {n.get('to_emails')}"
        log(f"  [assert] #{tid} private={n.get('private')} to_emails={n.get('to_emails')} ✓")
    return n.get("id")

def main():
    feed = json.load(open(FEED))["tickets"]
    def agef(x):
        try: return float(x.get("age_h"))
        except Exception: return 9e9
    window = [x for x in feed if str(x.get("resolved")).lower() != "true" and agef(x) <= MAX_AGE_H]
    reds  = sorted([x for x in window if x["rag"] == "red"],   key=agef)
    amber = sorted([x for x in window if x["rag"] == "amber"], key=agef)

    noted = load_state() | PRESEED_NOTED
    reds  = [x for x in reds  if int(x["id"]) not in noted]
    amber = [x for x in amber if int(x["id"]) not in noted]

    log(f"=== notes backfill | window<= {MAX_AGE_H}h | reds={len(reds)} amber={len(amber)} | dry={DRY} ===")
    if DRY:
        log("RED plan:");  [log(f"  #{x['id']} {x['subject'][:60]}") for x in reds[:50]]
        log(f"AMBER plan: {len(amber)} one-liner notes")
        return

    done = 0
    # reds first (fetch body -> detailed note)
    for i, x in enumerate(reds):
        tid = int(x["id"])
        try:
            msg = latest_customer_msg(tid)
            time.sleep(0.5)
            nid = post_note(tid, red_note(x.get("reason",""), msg, x.get("owner","")), assert_private=(i < 3))
            noted.add(tid); done += 1
            log(f"RED  #{tid} noted ({nid})  [{i+1}/{len(reds)}]")
        except Exception as e:
            log(f"RED  #{tid} ERROR: {e}")
        save_state(noted); time.sleep(1.2)

    # amber (one-liner from feed reason)
    for i, x in enumerate(amber):
        tid = int(x["id"])
        try:
            nid = post_note(tid, amber_note(x.get("reason","")), assert_private=(i < 3))
            noted.add(tid); done += 1
            if (i+1) % 20 == 0 or i == 0:
                log(f"AMBER progress {i+1}/{len(amber)} (last #{tid})")
        except Exception as e:
            log(f"AMBER #{tid} ERROR: {e}")
        save_state(noted); time.sleep(1.0)

    log(f"=== DONE — {done} notes posted; {len(noted)} total in state ===")

if __name__ == "__main__":
    main()
