#!/usr/bin/env python3
"""
Build the branded RAG triage board HTML from /tmp/triage_dashboard.json (written by
freshdesk_triage.py apply). Prints HTML to /tmp/triage_dashboard.html — upload it to
the hub at operations/storage-freshdesk-triage via the AV Dashboards MCP.
Reds-first worklist; matches the operations/storage hub brand styling.
"""
import json, html, sys, os
from datetime import datetime, timezone
from collections import Counter

SRC='/tmp/triage_dashboard.json'
OUT='/tmp/triage_dashboard.html'
REPO_JSON=os.path.expanduser('~/Documents/storage-reporting/data/triage.json')
# --replace (alias --full): rebuild the published feed from this run alone (use after a
# `fetch --all` rebaseline, where the run IS the complete open set). Default = MERGE.
REPLACE='--replace' in sys.argv or '--full' in sys.argv

delta=json.load(open(SRC))

def _recount(tk):
    c=Counter((t.get('rag') or '').lower() for t in tk)
    return {'red':c.get('red',0),'amber':c.get('amber',0),'blue':c.get('blue',0),'green':c.get('green',0)}

if REPLACE or not os.path.exists(REPO_JSON):
    d=delta
else:
    # Incremental run: `apply` wrote DASH_JSON with ONLY this run's judged tickets, so
    # publishing it verbatim would shrink the board to just the latest batch (the bug
    # fixed here, 7 Jun 2026). Merge the run into the persistent published feed instead
    # — prior feed ∪ this run, run wins by id — so the board keeps the full open set.
    prev=json.load(open(REPO_JSON))
    by_id={t['id']:t for t in prev.get('tickets',[])}
    for t in delta.get('tickets',[]): by_id[t['id']]=t
    merged=list(by_id.values())
    d={'generated_at':delta.get('generated_at',prev.get('generated_at','')),
       'counts':_recount(merged),
       'resolved':sum(1 for t in merged if t.get('resolved')),
       'flagged':sum(1 for t in merged if (t.get('action')=='flag')),
       'total':len(merged),'tickets':merged}

c=d.get('counts',{}); tickets=d.get('tickets',[])
gen=d.get('generated_at','')
try:
    gd=datetime.fromisoformat(gen); genh=gd.strftime('%d %b %Y, %H:%M UTC')
except Exception: genh=gen

# ---- Standing backlog (FULL open set, not just this run) + ageing buckets ----
# Computed from the merged feed so the report shows total volumes still sitting in
# each RAG and how long they've been there, alongside the per-run "what's new" counts.
def _agebucket(h):
    h=float(h or 0)
    if h<24:  return 'new'   # <24h
    if h<72:  return 'd13'   # 1-3d
    if h<168: return 'd37'   # 3-7d
    return 'gt7'             # >7d
_open=[t for t in d.get('tickets',[]) if not t.get('resolved')]
backlog={}
for rag in ('red','amber','blue','green'):
    rows=[t for t in _open if t.get('rag')==rag]
    bk={'new':0,'d13':0,'d37':0,'gt7':0}
    for t in rows: bk[_agebucket(t.get('age_h'))]+=1
    oldest=max([(t.get('age_h') or 0) for t in rows], default=0)/24.0
    backlog[rag]={'total':len(rows),'oldest_d':round(oldest),**bk}
backlog['open_total']=len(_open)
d['backlog']=backlog
try: json.dump(backlog, open('/tmp/triage_backlog.json','w'), indent=2)
except Exception: pass

# Inject the standing-backlog block into the Slack summary the `assign` step wrote
# (assign runs first, then this generator, then the Slack post — so the file exists).
_EMO={'red':'🔴','amber':'🟠','blue':'🔵'}
_bl=[f"📊 *Standing backlog: {backlog['open_total']} open*"]
for rag in ('red','amber','blue'):
    s=backlog[rag]
    if not s['total']: continue
    _bl.append(f"   {_EMO[rag]} *{s['total']}* {rag} — {s['new']} new · {s['d13']} (1–3d) · {s['d37']} (3–7d) · *{s['gt7']} >7d* (oldest {s['oldest_d']}d)")
_BLOCK='\n'.join(_bl)
_SLK='/tmp/triage_slack_assign.txt'
try:
    _txt=open(_SLK).read()
    if '📊 *Standing backlog' not in _txt and '🧑‍💻' in _txt:
        open(_SLK,'w').write(_txt.replace('🧑‍💻', _BLOCK+'\n\n🧑‍💻', 1))
except Exception: pass

ORDER={'red':0,'amber':1,'blue':2,'green':3}
tickets=sorted(tickets,key=lambda t:(ORDER.get(t.get('rag'),9), -(t.get('age_h') or 0)))
# Worklist board: show the actionable tickets (reds, awaiting-customer blues, flagged bounces)
# as rows; amber (verify & close — batch task) and green (auto-notifications) are summarised.
hidden_amber=sum(1 for t in tickets if t.get('rag')=='amber' and t.get('action')!='flag')
hidden_green=sum(1 for t in tickets if t.get('rag')=='green')
worklist=[t for t in tickets if t.get('rag') in ('red','blue') or t.get('action')=='flag']
tickets=worklist

OWNER={}
try:
    asg=json.load(open('/tmp/triage_assignments.json'))
    for name,ids in asg.get('assignments',{}).items():
        for i in ids: OWNER[i]=name
    for tid,resp in asg.get('kept',[]): OWNER.setdefault(tid,'· owned')
except Exception: pass

def esc(s): return html.escape(str(s or ''))
def age_str(h):
    if not h: return ''
    h=float(h)
    return f"{h/24:.0f}d" if h>=48 else f"{h:.0f}h"

rows=[]
for t in tickets:
    rag=t.get('rag','')
    act=t.get('action','tag')
    actbadge={'close':'to resolve','flag':'resend','tag':''}.get(act,'')
    if t.get('resolved'): actbadge='resolved ✓'
    extra=f'<span class="actb {act}">{actbadge}</span>' if actbadge else ''
    rows.append(f"""<tr class="r-{rag}">
      <td class="rag"><span class="pill {rag}">{rag.upper()}</span></td>
      <td class="tid"><a href="{esc(t.get('url'))}" target="_blank">#{esc(t.get('id'))}</a></td>
      <td class="q">{esc(t.get('queue'))}</td>
      <td class="subj">{esc(t.get('subject'))}{extra}</td>
      <td class="own">{esc(OWNER.get(t.get('id'),'—'))}</td>
      <td class="rsn">{esc(t.get('reason'))}</td>
      <td class="age">{age_str(t.get('age_h'))}</td>
    </tr>""")

# Standing-backlog ageing strip: total open per RAG and how long they've been sitting.
_RAGMETA={'red':('🔴','Urgent'),'amber':('🟠','Can wait'),'blue':('🔵','Awaiting customer')}
agerows=[]
for rag in ('red','amber','blue'):
    s=backlog.get(rag,{})
    if not s.get('total'): continue
    emo,lbl=_RAGMETA[rag]
    agerows.append(f"""<tr class="ag-{rag}">
      <td class="agr"><span class="pill {rag}">{emo} {lbl}</span></td>
      <td class="agt">{s.get('total',0)}</td>
      <td>{s.get('new',0)}</td><td>{s.get('d13',0)}</td><td>{s.get('d37',0)}</td>
      <td class="agstale">{s.get('gt7',0)}</td>
      <td class="agold">{s.get('oldest_d',0)}d</td>
    </tr>""")
ageing_html=f"""<div class="ageing">
  <div class="ag-h">Standing backlog — {backlog.get('open_total',0)} open · ageing by category</div>
  <table class="agtbl">
    <thead><tr><th>Category</th><th>Total open</th><th>&lt;24h</th><th>1&ndash;3d</th><th>3&ndash;7d</th><th>&gt;7d</th><th>Oldest</th></tr></thead>
    <tbody>{''.join(agerows)}</tbody>
  </table>
</div>"""

HTML=f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Storage Freshdesk Triage</title>
<script src="https://assets.anyvan.com/tracking/av-track.7fa8936e.js"></script>
<script>window.av && window.av.configureTracking && window.av.configureTracking({{ baseUrl: 'https://www.anyvan.com' }})</script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--sans:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}}
html,body{{height:100%}}
body{{font-family:var(--sans);background:#f5f7fa;color:#111827;display:flex}}
.sidebar{{width:220px;min-width:220px;background:#002333;display:flex;flex-direction:column;padding:24px 0;flex-shrink:0;height:100vh;position:sticky;top:0;border-top:4px solid #ffc907}}
.sidebar-brand{{padding:0 24px 24px;margin-bottom:6px;border-bottom:1px solid rgba(255,255,255,0.08)}}
.sidebar-label{{font-size:18px;font-weight:800;letter-spacing:-.02em;color:#fff;margin-bottom:2px}}
.sidebar-label span{{color:#41a5dd}}
.sidebar-title{{font-size:12px;font-weight:500;color:rgba(255,255,255,0.55);text-transform:uppercase;letter-spacing:.12em}}
.nav-link{{display:block;padding:10px 24px;font-size:13px;color:rgba(255,255,255,0.65);text-decoration:none;transition:color .12s,background .12s;border-left:3px solid transparent}}
.nav-link:hover{{color:rgba(255,255,255,0.85)}}
.nav-link.active{{color:#fff;border-left-color:#41a5dd;background:rgba(65,165,221,0.12)}}
.sidebar-bottom{{margin-top:auto;padding:16px 24px;border-top:1px solid rgba(255,255,255,0.08)}}
.sidebar-home{{font-size:13px;color:rgba(255,255,255,0.65);text-decoration:none;transition:color .12s}}
.sidebar-home:hover{{color:#fff}}
.main{{flex:1;height:100vh;overflow:auto}}
.wrap{{max-width:1180px;margin:0 auto;padding:36px 40px}}
.head{{margin-bottom:24px}}
.head .lbl{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#41a5dd;font-weight:700;margin-bottom:6px}}
.head h1{{font-size:26px;font-weight:800;color:#002333;letter-spacing:-.01em}}
.head p{{font-size:13px;color:#6b7280;margin-top:8px;max-width:680px;line-height:1.5}}
.cards{{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:26px}}
.kpi{{background:#fff;border:1px solid #e2e2e2;border-radius:13px;padding:16px 18px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}}
.kpi .n{{font-size:30px;font-weight:800;line-height:1}}
.kpi .l{{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#9ca3af;font-weight:700;margin-top:8px}}
.kpi.red .n{{color:#dc2626}} .kpi.amber .n{{color:#d97706}} .kpi.blue .n{{color:#2563eb}} .kpi.green .n{{color:#16a34a}}
.kpi.flag .n{{color:#7c3aed}} .kpi.res .n{{color:#106799}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e2e2;border-radius:13px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.05)}}
th{{text-align:left;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#9ca3af;font-weight:700;padding:12px 14px;border-bottom:1px solid #eef1f4;background:#fafbfc}}
td{{padding:12px 14px;border-bottom:1px solid #f1f3f5;font-size:13px;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr.r-red{{background:#fef6f6}} tr.r-amber{{background:#fffaf2}} tr.r-blue{{background:#f5f9ff}}
.pill{{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.05em;padding:3px 8px;border-radius:20px}}
.pill.red{{background:#fee2e2;color:#b91c1c}} .pill.amber{{background:#fef3c7;color:#b45309}} .pill.blue{{background:#dbeafe;color:#1d4ed8}} .pill.green{{background:#dcfce7;color:#15803d}}
.tid a{{color:#106799;text-decoration:none;font-weight:600;white-space:nowrap}}
.q{{color:#6b7280;white-space:nowrap}}
.subj{{font-weight:600;color:#1f2937;max-width:340px}}
.own{{font-weight:600;color:#106799;white-space:nowrap}}
.rsn{{color:#6b7280;line-height:1.4;max-width:380px}}
.age{{color:#9ca3af;white-space:nowrap}}
.actb{{display:inline-block;margin-left:8px;font-size:9px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;padding:2px 7px;border-radius:20px;vertical-align:middle}}
.actb.close{{background:#dcfce7;color:#15803d}} .actb.flag{{background:#ede9fe;color:#6d28d9}}
.foot{{margin-top:26px;font-size:12px;color:#9ca3af;border-top:1px solid #e8edf2;padding-top:16px}}
.ageing{{background:#fff;border:1px solid #e2e2e2;border-radius:13px;padding:18px 20px;margin-bottom:26px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}}
.ag-h{{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#6b7280;font-weight:700;margin-bottom:12px}}
.agtbl{{box-shadow:none;border:none;border-radius:0}}
.agtbl th{{background:transparent;text-align:right;padding:6px 12px}}
.agtbl th:first-child{{text-align:left}}
.agtbl td{{text-align:right;padding:8px 12px;font-size:14px;font-weight:600;color:#1f2937;border-bottom:1px solid #f1f3f5}}
.agtbl td.agr{{text-align:left}}
.agtbl td.agt{{color:#002333;font-weight:800}}
.agtbl td.agstale{{color:#b45309;font-weight:800}}
.agtbl td.agold{{color:#9ca3af;font-weight:600}}
.agtbl tr:last-child td{{border-bottom:none}}
</style></head><body>
<nav class="sidebar">
  <div class="sidebar-brand">
    <div class="sidebar-label">Any<span>Van</span></div>
    <div class="sidebar-title">Storage Reporting</div>
  </div>
  <a class="nav-link" href="/operations/storage-mtd">MTD Revenue</a>
  <a class="nav-link" href="/operations/storage-monthly">Monthly Trends</a>
  <a class="nav-link" href="/operations/storage-weekly">Weekly KPI</a>
  <a class="nav-link" href="/operations/storage-voice-activity">Voice Activity</a>
  <a class="nav-link" href="/operations/storage-daily-activity">Daily Activity</a>
  <a class="nav-link" href="/operations/storage-whatsapp">WhatsApp</a>
  <a class="nav-link" href="/operations/storage-freshdesk">Freshdesk</a>
  <a class="nav-link active" href="/operations/storage-freshdesk-triage">Freshdesk Triage</a>
  <a class="nav-link" href="/operations/storage-csat">CSAT &amp; NPS</a>
  <a class="nav-link" href="/operations/storage-call-grading">Call Grading</a>
  <a class="nav-link" href="/operations/storage-debt">Debt Intelligence</a>
  <div class="sidebar-bottom">
    <a class="sidebar-home" href="/operations/storage">← All boards (Hub)</a>
  </div>
</nav>
<div class="main">
<div class="wrap">
  <div class="head">
    <div class="lbl">Storage · AI Ticket Triage</div>
    <h1>Freshdesk Triage Board</h1>
    <p>Every open Storage ticket scored 🔴 urgent / 🟠 can-wait / 🔵 awaiting-customer / 🟢 no-reply-needed, with a one-line reason. Reds first. E-Sign "done" greens auto-resolve; delivery failures are flagged for resend. Last run: <b>{esc(genh)}</b> · {len(tickets)} tickets.</p>
  </div>
  <div class="cards">
    <div class="kpi red"><div class="n">{c.get('red',0)}</div><div class="l">🔴 Urgent</div></div>
    <div class="kpi amber"><div class="n">{c.get('amber',0)}</div><div class="l">🟠 Can wait</div></div>
    <div class="kpi blue"><div class="n">{c.get('blue',0)}</div><div class="l">🔵 Awaiting customer</div></div>
    <div class="kpi green"><div class="n">{c.get('green',0)}</div><div class="l">🟢 No reply</div></div>
    <div class="kpi res"><div class="n">{d.get('resolved',0)}</div><div class="l">Auto-resolved</div></div>
    <div class="kpi flag"><div class="n">{d.get('flagged',0)}</div><div class="l">Flagged resend</div></div>
  </div>
  {ageing_html}
  <table>
    <thead><tr><th>RAG</th><th>Ticket</th><th>Queue</th><th>Subject</th><th>Owner</th><th>Why</th><th>Age</th></tr></thead>
    <tbody>
    {''.join(rows)}
    </tbody>
  </table>
  <div class="foot">Worklist shows 🔴 reds, 🔵 awaiting-customer and flagged bounces. Also tagged in Freshdesk but not listed: <b>{hidden_amber}</b> 🟠 amber (verify &amp; close — batch task) and <b>{hidden_green}</b> 🟢 green (auto-notifications). On-demand AI triage — run "storage triage" to refresh.</div>
</div>
</div>
<script src="https://robbosd.github.io/storage-reporting/nav.js"></script>
</body></html>"""

open(OUT,'w').write(HTML)
print(f"wrote {OUT} ({len(HTML)} bytes, {len(tickets)} tickets)")

# Publish the live-fetch data feed: full (merged) dataset + owner, written to the repo's
# Pages-served data dir. The board shell at operations/storage-freshdesk-triage fetches this,
# so the morning run just needs: regenerate -> git commit+push data/triage.json (no HTML push).
# `d` is already the full merged set (or this run, under --replace). Owners: this run's
# assignments win; otherwise keep whatever owner the ticket already carried in the feed.
for t in d.get('tickets',[]): t['owner']=OWNER.get(t['id'], t.get('owner','') or '')
try:
    json.dump(d,open(REPO_JSON,'w'),indent=1)
    print(f"wrote {REPO_JSON} ({len(d.get('tickets',[]))} tickets) [{'replace' if REPLACE else 'merged'}] — commit+push to publish")
except Exception as e:
    print(f"(could not write repo data json: {e})")
