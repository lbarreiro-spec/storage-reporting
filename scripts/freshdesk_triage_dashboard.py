#!/usr/bin/env python3
"""
Build the Storage Freshdesk Triage REPORTING board (v2) as a SELF-CONTAINED page with the
data inlined — no external fetch. Writes /tmp/triage_dashboard.html; upload to
operations/storage-freshdesk-triage via the AV Dashboards Upload API (get_upload_token -> PUT).
All AnyVan infra — no GitHub.

v2 reporting layer (9 Jun 2026): headline KPIs · per-AGENT panel (consistent colours, volume+%,
RAG split, oldest/avg, #stale) · AGEING by IDLE (time since last touched, falls back to age if a
ticket has no idle_h yet) · buckets by queue + lane · worklist. All analytics computed client-side.

Merge state lives LOCAL at ~/.anyvan/triage_feed.json (seeded once from the old repo feed).
  default   — MERGE this run into the local feed.  --replace — rebuild from this run alone.
"""
import json, sys, os
from datetime import datetime, timezone
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from triage_classify import theme_of, partner_of

SRC='/tmp/triage_dashboard.json'
OUT='/tmp/triage_dashboard.html'
FEED=os.path.expanduser('~/.anyvan/triage_feed.json')
SEED_OLD=os.path.expanduser('~/Documents/storage-reporting/data/triage.json')
REPLACE='--replace' in sys.argv or '--full' in sys.argv

delta=json.load(open(SRC))
def _recount(tk):
    c=Counter((t.get('rag') or '').lower() for t in tk)
    return {'red':c.get('red',0),'amber':c.get('amber',0),'blue':c.get('blue',0),'green':c.get('green',0)}
_prev=FEED if os.path.exists(FEED) else (SEED_OLD if os.path.exists(SEED_OLD) else None)
if REPLACE or not _prev:
    d=delta
else:
    prev=json.load(open(_prev)); by_id={t['id']:t for t in prev.get('tickets',[])}
    for t in delta.get('tickets',[]): by_id[t['id']]=t
    merged=list(by_id.values())
    d={'generated_at':delta.get('generated_at',prev.get('generated_at','')),'counts':_recount(merged),
       'resolved':sum(1 for t in merged if t.get('resolved')),
       'flagged':sum(1 for t in merged if t.get('action')=='flag'),
       'total':len(merged),'tickets':merged}

tickets=d.get('tickets',[])

# THEME + PARTNER classification (18 Jun) — keyword-derived, refreshed every run so the WHOLE
# feed gets classified (no re-judge needed). A judge-emitted `theme` (if present) wins.
for t in tickets:
    t['partner']=partner_of(t)
    if not t.get('theme'): t['theme']=theme_of(t)

# Standing backlog (for the Slack summary block) — computed off IDLE if present, else age.
def _metric(t): return t.get('idle_h') if t.get('idle_h') is not None else t.get('age_h')
def _bucket(h):
    h=float(h or 0)
    if h<24: return 'new'
    if h<72: return 'd13'
    if h<168:return 'd37'
    return 'gt7'
_open=[t for t in tickets if not t.get('resolved')]
backlog={}
for rag in ('red','amber','blue','green'):
    rows=[t for t in _open if t.get('rag')==rag]; bk={'new':0,'d13':0,'d37':0,'gt7':0}
    for t in rows: bk[_bucket(_metric(t))]+=1
    oldest=max([( _metric(t) or 0) for t in rows], default=0)/24.0
    backlog[rag]={'total':len(rows),'oldest_d':round(oldest),**bk}
backlog['open_total']=len(_open); d['backlog']=backlog
try: json.dump(backlog,open('/tmp/triage_backlog.json','w'),indent=2)
except Exception: pass

# Inject standing-backlog into the Slack summary the assign step wrote.
_EMO={'red':'🔴','amber':'🟠','blue':'🔵'}
_bl=[f"📊 *Standing backlog: {backlog['open_total']} open*"]
for rag in ('red','amber','blue'):
    s=backlog[rag]
    if s['total']: _bl.append(f"   {_EMO[rag]} *{s['total']}* {rag} — {s['new']} new · {s['d13']} (1–3d) · {s['d37']} (3–7d) · *{s['gt7']} >7d* (oldest {s['oldest_d']}d)")
_SLK='/tmp/triage_slack_assign.txt'
try:
    _txt=open(_SLK).read()
    if '📊 *Standing backlog' not in _txt and '🧑‍💻' in _txt:
        open(_SLK,'w').write(_txt.replace('🧑‍💻','\n'.join(_bl)+'\n\n🧑‍💻',1))
except Exception: pass

# --- Daily history snapshot (Stage 2, 18 Jun) → trend analytics. One row/day, latest run wins. ---
HIST=os.path.expanduser('~/.anyvan/triage_history.jsonl')
_day=((delta.get('generated_at') or '')[:10]) or datetime.now(timezone.utc).strftime('%Y-%m-%d')
snap={'date':_day,'open':len(_open),
      'red':backlog['red']['total'],'amber':backlog['amber']['total'],
      'blue':backlog['blue']['total'],'green':backlog['green']['total'],
      'resolved':d.get('resolved',0),'flagged':d.get('flagged',0),
      'themes':dict(Counter(t.get('theme') for t in _open)),
      'agents':dict(Counter((t.get('owner') or 'Unassigned') for t in _open))}
hist=[]
try:
    if os.path.exists(HIST): hist=[json.loads(l) for l in open(HIST) if l.strip()]
except Exception: hist=[]
hist=[h for h in hist if h.get('date')!=snap['date']]+[snap]
hist.sort(key=lambda h:h.get('date',''))
try: open(HIST,'w').write('\n'.join(json.dumps(h) for h in hist)+'\n')
except Exception: pass
d['history']=hist[-60:]

# Owners: this run's assignments win; else keep what the ticket already carried.
OWNER={}
try:
    asg=json.load(open('/tmp/triage_assignments.json'))
    for name,ids in asg.get('assignments',{}).items():
        for i in ids: OWNER[i]=name
    for tid,resp in asg.get('kept',[]): OWNER.setdefault(tid,'· owned')
except Exception: pass
for t in tickets: t['owner']=OWNER.get(t['id'], t.get('owner','') or '')

# Persist merged feed LOCALLY.
try:
    os.makedirs(os.path.dirname(FEED),exist_ok=True); json.dump(d,open(FEED,'w'),indent=1)
    feedmsg=f"feed -> {FEED} ({len(tickets)} tickets) [{'replace' if REPLACE else 'merged'}]"
except Exception as e: feedmsg=f"(could not write local feed: {e})"

DATA_JS=json.dumps(d).replace('<','\\u003c')

SHELL=r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Storage Freshdesk Triage</title>
<script src="https://assets.anyvan.com/tracking/av-track.7fa8936e.js"></script>
<script>window.av && window.av.configureTracking && window.av.configureTracking({ baseUrl: 'https://www.anyvan.com' })</script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--sans:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}
html,body{height:100%}
body{font-family:var(--sans);background:#f5f7fa;color:#111827;display:flex}
.sidebar{width:220px;min-width:220px;background:#002333;display:flex;flex-direction:column;padding:24px 0;flex-shrink:0;height:100vh;position:sticky;top:0;border-top:4px solid #ffc907}
.sidebar-brand{padding:0 24px 24px;margin-bottom:6px;border-bottom:1px solid rgba(255,255,255,0.08)}
.sidebar-label{font-size:18px;font-weight:800;letter-spacing:-.02em;color:#fff;margin-bottom:2px}
.sidebar-label span{color:#41a5dd}
.sidebar-title{font-size:12px;font-weight:500;color:rgba(255,255,255,0.55);text-transform:uppercase;letter-spacing:.12em}
.nav-link{display:block;padding:10px 24px;font-size:13px;color:rgba(255,255,255,0.65);text-decoration:none;border-left:3px solid transparent}
.nav-link:hover{color:rgba(255,255,255,0.85)}
.nav-link.active{color:#fff;border-left-color:#41a5dd;background:rgba(65,165,221,0.12)}
.sidebar-bottom{margin-top:auto;padding:16px 24px;border-top:1px solid rgba(255,255,255,0.08)}
.sidebar-home{font-size:13px;color:rgba(255,255,255,0.65);text-decoration:none}
.main{flex:1;height:100vh;overflow:auto}
.wrap{max-width:1240px;margin:0 auto;padding:36px 40px}
.head{margin-bottom:22px}
.head-row{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}
.head .lbl{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#41a5dd;font-weight:700;margin-bottom:6px}
.head h1{font-size:26px;font-weight:800;color:#002333}
.head p{font-size:13px;color:#6b7280;margin-top:8px;max-width:760px;line-height:1.5}
.refresh{flex-shrink:0;display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:700;border:1px solid #d1d9e0;background:#fff;color:#002333;border-radius:9px;padding:9px 16px;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.05);transition:background .12s,color .12s,border-color .12s}
.refresh:hover{background:#002333;color:#fff;border-color:#002333}
.refresh:active{transform:translateY(1px)}
.sec{margin-bottom:28px}
.sec-h{font-size:13px;font-weight:800;color:#002333;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
.sec-h small{font-weight:600;text-transform:none;letter-spacing:0;color:#9ca3af;margin-left:8px}
.cards{display:grid;grid-template-columns:repeat(7,1fr);gap:12px}
.kpi{background:#fff;border:1px solid #e2e2e2;border-radius:13px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
.kpi .n{font-size:26px;font-weight:800;line-height:1}
.kpi .l{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:#9ca3af;font-weight:700;margin-top:7px}
.kpi.red .n{color:#dc2626}.kpi.amber .n{color:#d97706}.kpi.blue .n{color:#2563eb}.kpi.res .n{color:#106799}.kpi.flag .n{color:#7c3aed}.kpi.stale .n{color:#b45309}
.card{background:#fff;border:1px solid #e2e2e2;border-radius:13px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,0.05)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:#9ca3af;font-weight:700;padding:9px 10px;border-bottom:1px solid #eef1f4}
td{padding:9px 10px;border-bottom:1px solid #f4f6f8;font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:middle}
.who{font-weight:700;color:#1f2937}
.bar{height:8px;border-radius:6px;background:#eef1f4;overflow:hidden;min-width:80px}
.bar > i{display:block;height:100%;border-radius:6px}
.rag-mini{display:flex;height:9px;border-radius:6px;overflow:hidden;min-width:90px;background:#eef1f4}
.rag-mini > i{display:block;height:100%}
.pill{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.04em;padding:3px 8px;border-radius:20px}
.pill.red{background:#fee2e2;color:#b91c1c}.pill.amber{background:#fef3c7;color:#b45309}.pill.blue{background:#dbeafe;color:#1d4ed8}.pill.green{background:#dcfce7;color:#15803d}
.stale{color:#b45309;font-weight:800}
.stk{display:flex;height:22px;border-radius:6px;overflow:hidden;background:#eef1f4;margin-bottom:6px}
.stk > i{display:block;height:100%}
.lgd{font-size:11px;color:#6b7280;display:flex;gap:14px;flex-wrap:wrap;margin-top:4px}
.lgd span{display:inline-flex;align-items:center}
.tid a{color:#106799;text-decoration:none;font-weight:600;white-space:nowrap}
.q{color:#6b7280;white-space:nowrap}.subj{font-weight:600;color:#1f2937;max-width:300px}
.rsn{color:#6b7280;line-height:1.4;max-width:340px}.age{white-space:nowrap}
.filters{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.fbtn{font-size:12px;font-weight:600;border:1px solid #e2e2e2;background:#fff;color:#6b7280;border-radius:20px;padding:5px 13px;cursor:pointer}
.fbtn.on{background:#002333;color:#fff;border-color:#002333}
.foot{margin-top:8px;font-size:12px;color:#9ca3af;border-top:1px solid #e8edf2;padding-top:14px}
.state{padding:30px;text-align:center;color:#6b7280}
</style></head><body>
<nav class="sidebar">
  <div class="sidebar-brand"><div class="sidebar-label">Any<span>Van</span></div><div class="sidebar-title">Storage Reporting</div></div>
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
  <div class="sidebar-bottom"><a class="sidebar-home" href="/operations/storage">← All boards (Hub)</a></div>
</nav>
<div class="main"><div class="wrap">
  <div class="head">
    <div class="head-row">
      <div>
        <div class="lbl">Storage · AI Ticket Triage</div>
        <h1>Freshdesk Triage — Reporting</h1>
      </div>
      <button class="refresh" onclick="location.reload(true)" title="Reload the board to pull the latest published triage run">&#8635; Refresh</button>
    </div>
    <p>Open Storage tickets scored 🔴/🟠/🔵/🟢, by team member, queue and how long they've sat untouched. <span id="meta"></span></p>
  </div>
  <div class="sec"><div class="cards" id="cards"></div></div>
  <div class="sec"><div class="sec-h">By team member <small id="teamsub"></small></div><div class="card"><div id="team"></div></div></div>
  <div class="sec"><div class="sec-h">Ageing <small id="agesub"></small></div><div class="card" id="ageing"></div></div>
  <div class="sec"><div class="sec-h">Trend <small>open backlog over time — builds as the daily run accumulates</small></div><div class="card" id="trend"></div></div>
  <div class="sec"><div class="sec-h">Response &amp; SLA <small>first-response speed across open tickets</small></div><div class="card" id="sla"></div></div>
  <div class="sec"><div class="sec-h">By theme <small>what the tickets are about, split by urgency</small></div><div class="card" id="thememx"></div></div>
  <div class="sec"><div class="grid2">
    <div><div class="sec-h">By partner <small>facility / removals routing</small></div><div class="card"><div id="bypartner"></div></div></div>
    <div><div class="sec-h">By queue</div><div class="card"><div id="byqueue"></div></div></div>
  </div></div>
  <div class="sec"><div class="grid2">
    <div><div class="sec-h">By type (lane)</div><div class="card"><div id="bylane"></div></div></div>
    <div><div class="sec-h">Customer vs partner</div><div class="card"><div id="bysource"></div></div></div>
  </div></div>
  <div class="sec"><div class="sec-h">Worklist <small>reds · awaiting-customer · flagged bounces</small></div>
    <div class="filters" id="filters"></div><div id="table"><div class="state">Loading…</div></div>
    <div class="foot" id="foot"></div>
  </div>
</div></div>
<script>
const DATA=__DATA_JSON__;
// consistent team colours used across every chart
const TEAM_COLOUR={'Sage':'#2563eb','Emmanuel':'#16a34a','Theo J':'#ea580c','Shafwaan':'#7c3aed','Carla J':'#0d9488'};
const RAG_COLOUR={red:'#dc2626',amber:'#d97706',blue:'#2563eb',green:'#16a34a'};
const ORDER={red:0,blue:1,amber:2,green:3};
function ownerColour(o){ if(!o||o==='—') return '#9ca3af'; return TEAM_COLOUR[o]||'#64748b'; }
function ownerLabel(o){ return (!o||o==='· owned')?'Unassigned':o; }
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
// ageing metric: prefer IDLE (since last touched), fall back to age (since created)
const USE_IDLE = DATA.tickets.some(t=>t.idle_h!=null);
function metric(t){ return USE_IDLE ? (t.idle_h!=null?+t.idle_h:(+t.age_h||0)) : (+t.age_h||0); }
function durLabel(h){ if(h==null) return '—'; h=+h; return h>=48?Math.round(h/24)+'d':Math.round(h)+'h'; }
function fmt(s){if(!s)return '';const d=new Date(s);return isNaN(d)?'':d.toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});}
const OPEN = DATA.tickets.filter(t=>!t.resolved);
const TOT = OPEN.length || 1;
const pct = n => Math.round(100*n/TOT);

function cards(){
  const c={}; OPEN.forEach(t=>c[t.rag]=(c[t.rag]||0)+1);
  const ages=OPEN.map(metric); const oldest=ages.length?Math.max(...ages):0;
  const avg=ages.length?ages.reduce((a,b)=>a+b,0)/ages.length:0;
  const stale=OPEN.filter(t=>metric(t)>=168).length;
  const rows=[['','Open',OPEN.length],['red','🔴 Urgent',c.red||0],['amber','🟠 Can wait',c.amber||0],
    ['blue','🔵 Awaiting',c.blue||0],['res','Auto-resolved',DATA.resolved||0],
    ['stale','Sat >7d',stale],['flag','Oldest / avg',Math.round(oldest/24)+'d / '+Math.round(avg/24)+'d']];
  document.getElementById('cards').innerHTML=rows.map(([k,l,n])=>`<div class="kpi ${k}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
  document.getElementById('meta').textContent=`Last run ${fmt(DATA.generated_at)} · ${OPEN.length} open.`;
  document.getElementById('agesub').textContent = USE_IDLE ? '(time since last touched — idle)' : '(time since created — idle data populates on next run)';
}

function team(){
  const by={}; OPEN.forEach(t=>{const o=ownerLabel(t.owner); (by[o]=by[o]||[]).push(t);});
  // order: named team first by volume, Unassigned last
  const names=Object.keys(by).sort((a,b)=> (a==='Unassigned')-(b==='Unassigned') || by[b].length-by[a].length);
  const maxV=Math.max(1,...names.map(n=>by[n].length));
  let rows='';
  names.forEach(o=>{
    const ts=by[o], col=ownerColour(o==='Unassigned'?'':o);
    const rc={red:0,amber:0,blue:0,green:0}; ts.forEach(t=>rc[t.rag]=(rc[t.rag]||0)+1);
    const ages=ts.map(metric), oldest=Math.max(0,...ages), avg=ages.reduce((a,b)=>a+b,0)/ts.length;
    const stale=ts.filter(t=>metric(t)>=168).length;
    const mini=['red','amber','blue','green'].filter(r=>rc[r]).map(r=>`<i style="width:${100*rc[r]/ts.length}%;background:${RAG_COLOUR[r]}"></i>`).join('');
    rows+=`<tr>
      <td class="who"><span class="dot" style="background:${col}"></span>${esc(o)}</td>
      <td><div class="bar"><i style="width:${100*ts.length/maxV}%;background:${col}"></i></div></td>
      <td class="num">${ts.length} <span style="color:#9ca3af;font-weight:500">(${pct(ts.length)}%)</span></td>
      <td><div class="rag-mini">${mini}</div></td>
      <td class="num">${rc.red||0}</td>
      <td class="num">${durLabel(oldest)}</td>
      <td class="num">${durLabel(avg)}</td>
      <td class="num ${stale?'stale':''}">${stale||'—'}</td>
    </tr>`;
  });
  document.getElementById('team').innerHTML=`<table><thead><tr>
    <th>Agent</th><th>Volume</th><th class="num">Open (%)</th><th>RAG split</th>
    <th class="num">🔴</th><th class="num">Oldest</th><th class="num">Avg</th><th class="num">Sat&gt;7d</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  document.getElementById('teamsub').textContent='volume, RAG mix and how long their queue has sat';
}

function ageing(){
  const labels=[['new','&lt;24h'],['d13','1–3d'],['d37','3–7d'],['gt7','&gt;7d']];
  const order=['red','amber','blue'];
  let html='<table><thead><tr><th>Category</th><th>Total</th><th class="num">&lt;24h</th><th class="num">1–3d</th><th class="num">3–7d</th><th class="num">&gt;7d</th><th class="num">Oldest</th></tr></thead><tbody>';
  function bk(h){h=+h||0; return h<24?'new':h<72?'d13':h<168?'d37':'gt7';}
  order.forEach(rag=>{
    const ts=OPEN.filter(t=>t.rag===rag); if(!ts.length) return;
    const b={new:0,d13:0,d37:0,gt7:0}; ts.forEach(t=>b[bk(metric(t))]++);
    const oldest=Math.max(0,...ts.map(metric));
    html+=`<tr><td><span class="pill ${rag}">${rag.toUpperCase()}</span></td><td class="num">${ts.length}</td>
      <td class="num">${b.new}</td><td class="num">${b.d13}</td><td class="num">${b.d37}</td>
      <td class="num stale">${b.gt7}</td><td class="num">${Math.round(oldest/24)}d</td></tr>`;
  });
  html+='</tbody></table>';
  // overall stacked bar of idle buckets
  const all={new:0,d13:0,d37:0,gt7:0}; OPEN.forEach(t=>all[bk(metric(t))]++);
  const bcol={new:'#16a34a',d13:'#d97706',d37:'#ea580c',gt7:'#b91c1c'};
  const stk=labels.map(([k])=>all[k]?`<i style="width:${100*all[k]/TOT}%;background:${bcol[k]}" title="${k}: ${all[k]}"></i>`:'').join('');
  const lgd=labels.map(([k,l])=>`<span><span class="dot" style="background:${bcol[k]}"></span>${l}: ${all[k]} (${pct(all[k])}%)</span>`).join('');
  document.getElementById('ageing').innerHTML=`<div class="stk">${stk}</div><div class="lgd" style="margin-bottom:16px">${lgd}</div>${html}`;
}

function bucketTable(elId,keyFn,emptyLabel){
  const by={}; OPEN.forEach(t=>{const k=keyFn(t)||emptyLabel; by[k]=(by[k]||0)+1;});
  const keys=Object.keys(by).sort((a,b)=>by[b]-by[a]); const maxV=Math.max(1,...keys.map(k=>by[k]));
  const rows=keys.map(k=>`<tr><td class="who" style="font-weight:600">${esc(k)}</td>
    <td><div class="bar"><i style="width:${100*by[k]/maxV}%;background:#41a5dd"></i></div></td>
    <td class="num">${by[k]} <span style="color:#9ca3af;font-weight:500">(${pct(by[k])}%)</span></td></tr>`).join('');
  document.getElementById(elId).innerHTML=`<table><tbody>${rows||'<tr><td class=state>none</td></tr>'}</tbody></table>`;
}

// RESPONSE & SLA — first-response speed across open tickets (+ per-agent)
function sla(){
  const el=document.getElementById('sla');
  if(!OPEN.some(t=>t.responded!=null)){ el.innerHTML='<div class="state">⏱️ SLA data populates after the next enriched run.</div>'; return; }
  const fr=OPEN.filter(t=>t.frt_h!=null).map(t=>+t.frt_h).sort((a,b)=>a-b);
  const responded=OPEN.filter(t=>t.responded===true).length;
  const awaiting=OPEN.filter(t=>t.responded===false).length;
  const breached=OPEN.filter(t=>t.fr_breach).length;
  const med=fr.length?fr[Math.floor(fr.length/2)]:0, avg=fr.length?fr.reduce((a,b)=>a+b,0)/fr.length:0;
  const within24=OPEN.filter(t=>t.frt_h!=null&&+t.frt_h<=24).length;
  const denom=responded+awaiting||1;
  const tiles=[['Responded',`${responded} (${Math.round(100*responded/denom)}%)`,'#106799'],
    ['Awaiting 1st reply',awaiting,'#d97706'],['Median 1st response',durLabel(med),'#111827'],
    ['Avg 1st response',durLabel(avg),'#111827'],['Replied ≤24h',within24,'#16a34a'],
    ['FR-SLA breached',breached,'#dc2626']];
  const th=tiles.map(([l,v,c])=>`<div style="flex:1;min-width:120px"><div style="font-size:22px;font-weight:800;color:${c}">${v}</div><div style="color:#9ca3af;font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-top:5px">${l}</div></div>`).join('');
  const by={}; OPEN.forEach(t=>{const o=ownerLabel(t.owner); (by[o]=by[o]||[]).push(t);});
  const names=Object.keys(by).filter(o=>o!=='Unassigned').sort((a,b)=>by[b].length-by[a].length);
  const arows=names.map(o=>{
    const ts=by[o], frs=ts.filter(t=>t.frt_h!=null).map(t=>+t.frt_h).sort((a,b)=>a-b);
    const m=frs.length?frs[Math.floor(frs.length/2)]:null;
    const aw=ts.filter(t=>t.responded===false).length, rp=ts.filter(t=>t.responded===true).length;
    return `<tr><td class="who"><span class="dot" style="background:${ownerColour(o)}"></span>${esc(o)}</td>
      <td class="num">${rp}/${ts.length}</td><td class="num ${aw?'stale':''}">${aw||'—'}</td><td class="num">${m!=null?durLabel(m):'—'}</td></tr>`;
  }).join('');
  el.innerHTML=`<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:16px">${th}</div>
    <table><thead><tr><th>Agent</th><th class="num">Responded</th><th class="num">Awaiting</th><th class="num">Median FRT</th></tr></thead><tbody>${arows}</tbody></table>`;
}
// TREND — open backlog (with red portion) per day; builds as snapshots accumulate
function trend(){
  const H=(DATA.history||[]); const el=document.getElementById('trend');
  if(H.length<2){ el.innerHTML=`<div class="state">📈 Trend starts today — this fills in as the daily run logs a snapshot each day (currently <b>${H.length}</b> day${H.length===1?'':'s'} logged). Come back tomorrow.</div>`; return; }
  const max=Math.max(1,...H.map(h=>h.open));
  const bars=H.map(h=>{
    const ph=Math.round(100*h.open/max), rp=Math.round(100*(h.red||0)/(h.open||1));
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0">
      <div style="font-size:10px;color:#6b7280;font-weight:700">${h.open}</div>
      <div style="height:120px;width:60%;max-width:30px;background:#eef1f4;border-radius:5px;display:flex;flex-direction:column-reverse;overflow:hidden" title="${h.date}: ${h.open} open, ${h.red||0} red">
        <div style="height:${ph}%;background:#41a5dd;display:flex;flex-direction:column-reverse"><div style="height:${rp}%;background:#dc2626"></div></div></div>
      <div style="font-size:9px;color:#9ca3af">${h.date.slice(5)}</div></div>`;
  }).join('');
  const f=H[0],l=H[H.length-1], dO=l.open-f.open, dR=(l.red||0)-(f.red||0);
  el.innerHTML=`<div style="display:flex;align-items:flex-end;gap:6px;padding:4px 0">${bars}</div>
    <div class="lgd" style="margin-top:10px"><span><span class="dot" style="background:#41a5dd"></span>Open backlog</span><span><span class="dot" style="background:#dc2626"></span>of which 🔴 red</span>
    <span style="margin-left:auto;font-weight:600">Since ${f.date.slice(5)}: open ${dO>=0?'+':''}${dO} · reds ${dR>=0?'+':''}${dR}</span></div>`;
}
// THEME × RAG matrix — rows = themes by volume, cols = RAG counts + a mini stacked bar
function themeMatrix(){
  const by={}; OPEN.forEach(t=>{const k=t.theme||'Other / general'; (by[k]=by[k]||{red:0,amber:0,blue:0,green:0,_n:0}); by[k][t.rag]=(by[k][t.rag]||0)+1; by[k]._n++;});
  const keys=Object.keys(by).sort((a,b)=>by[b]._n-by[a]._n);
  const maxV=Math.max(1,...keys.map(k=>by[k]._n));
  const rows=keys.map(k=>{
    const r=by[k];
    const mini=['red','amber','blue','green'].filter(x=>r[x]).map(x=>`<i style="width:${100*r[x]/r._n}%;background:${RAG_COLOUR[x]}"></i>`).join('');
    return `<tr><td class="who" style="font-weight:600">${esc(k)}</td>
      <td><div class="bar"><i style="width:${100*r._n/maxV}%;background:#41a5dd"></i></div></td>
      <td class="num">${r._n} <span style="color:#9ca3af;font-weight:500">(${pct(r._n)}%)</span></td>
      <td><div class="rag-mini">${mini}</div></td>
      <td class="num" style="color:#dc2626">${r.red||0}</td>
      <td class="num" style="color:#d97706">${r.amber||0}</td>
      <td class="num" style="color:#2563eb">${r.blue||0}</td>
      <td class="num" style="color:#16a34a">${r.green||0}</td></tr>`;
  }).join('');
  document.getElementById('thememx').innerHTML=`<table><thead><tr><th>Theme</th><th>Volume</th><th class="num">Open (%)</th><th>RAG split</th><th class="num">🔴</th><th class="num">🟠</th><th class="num">🔵</th><th class="num">🟢</th></tr></thead><tbody>${rows}</tbody></table>`;
}
// Customer-direct vs partner-routed split
function sourceSplit(){
  let cust=0,part=0; OPEN.forEach(t=>{ (t.partner==='Customer (direct)'?cust++:part++); });
  const tot=cust+part||1;
  document.getElementById('bysource').innerHTML=
    `<div class="stk"><i style="width:${100*cust/tot}%;background:#41a5dd" title="Customer"></i><i style="width:${100*part/tot}%;background:#7c3aed" title="Partner"></i></div>
     <div class="lgd"><span><span class="dot" style="background:#41a5dd"></span>Customer (direct): ${cust} (${Math.round(100*cust/tot)}%)</span>
     <span><span class="dot" style="background:#7c3aed"></span>Partner-routed: ${part} (${Math.round(100*part/tot)}%)</span></div>`;
}

let FILTER='work';
function worklist(){
  const owners=[...new Set(OPEN.map(t=>ownerLabel(t.owner)))].filter(o=>o!=='Unassigned').sort();
  document.getElementById('filters').innerHTML=
    [['work','Worklist'],['red','🔴 Reds'],['blue','🔵 Awaiting'],['flag','Bounces']].concat(owners.map(o=>[o,o]))
    .map(([k,l])=>`<button class="fbtn ${FILTER===k?'on':''}" onclick="setF('${String(k).replace(/'/g,"\\'")}')">${esc(l)}</button>`).join('');
  let rows=OPEN.slice();
  if(FILTER==='work') rows=rows.filter(t=>t.rag==='red'||t.rag==='blue'||t.action==='flag');
  else if(FILTER==='red') rows=rows.filter(t=>t.rag==='red');
  else if(FILTER==='blue') rows=rows.filter(t=>t.rag==='blue');
  else if(FILTER==='flag') rows=rows.filter(t=>t.action==='flag');
  else rows=rows.filter(t=>ownerLabel(t.owner)===FILTER);
  rows.sort((a,b)=>(ORDER[a.rag]-ORDER[b.rag])||(metric(b)-metric(a)));
  const body=rows.map(t=>{
    const badge=t.resolved?'resolved ✓':t.action==='flag'?'resend':t.action==='close'?'to resolve':'';
    const o=ownerLabel(t.owner);
    return `<tr><td><span class="pill ${t.rag}">${t.rag.toUpperCase()}</span></td>
      <td class="tid"><a href="${esc(t.url)}" target="_blank">#${t.id}</a></td>
      <td class="q">${esc(t.queue)}</td><td class="subj">${esc(t.subject)} ${badge?'<span style="color:#9ca3af;font-size:11px">· '+badge+'</span>':''}</td>
      <td class="who"><span class="dot" style="background:${ownerColour(o==='Unassigned'?'':o)}"></span>${esc(o)}</td>
      <td class="rsn">${esc(t.reason)}</td><td class="age">${durLabel(metric(t))}</td></tr>`;
  }).join('');
  document.getElementById('table').innerHTML=`<div class="card" style="padding:0"><table><thead><tr><th>RAG</th><th>Ticket</th><th>Queue</th><th>Subject</th><th>Owner</th><th>Why</th><th>Idle</th></tr></thead><tbody>${body||'<tr><td colspan=7 class=state>No tickets.</td></tr>'}</tbody></table></div>`;
  const ha=OPEN.filter(t=>t.rag==='amber'&&t.action!=='flag').length, hg=DATA.tickets.filter(t=>t.rag==='green').length;
  document.getElementById('foot').innerHTML=`Showing ${rows.length} of ${OPEN.length} open. Also tagged: <b>${ha}</b> 🟠 amber (batch verify) · <b>${hg}</b> 🟢 green. "Idle" = time since last touched. Run "storage triage" to refresh.`;
}
function setF(f){FILTER=f;worklist();}
cards(); team(); ageing(); trend(); sla(); themeMatrix(); bucketTable('bypartner',t=>t.partner,'Customer (direct)'); sourceSplit(); bucketTable('byqueue',t=>t.queue,'(no queue)'); bucketTable('bylane',t=>t.lane&&t.lane!=='none'?t.lane:'—','—'); worklist();
</script>
<!-- Shared storage-reporting nav: wipes the hardcoded fallback links above and regenerates the full board list + active state. EDIT nav.js (not this file) to change the sidebar. -->
<script src="https://lbarreiro-spec.github.io/storage-reporting/nav.js"></script>
</body></html>"""

HTML=SHELL.replace('__DATA_JSON__',DATA_JS)
open(OUT,'w').write(HTML)
print(f"wrote {OUT} ({len(HTML)} bytes, {len(_open)} open of {len(tickets)} tickets)")
print(feedmsg)
print("upload: AV Dashboards get_upload_token -> PUT /production/upload (path operations/storage-freshdesk-triage)")
