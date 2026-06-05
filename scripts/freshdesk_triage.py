#!/usr/bin/env python3
"""
Freshdesk Storage ticket triage.
Modes:
  summary           — rules-only RAG counts over all open Storage tickets (quick health check)
  fetch [--limit N] [--since ISO]
                    — pull open Storage tickets (new/changed since last run), with latest message
                      + direction, write candidates JSON for the AI judge stage. No writes.
                      --since omitted => uses saved last-run state (first ever run = all open).
  greens [--limit N]— pull "done-pattern" candidates + bodies -> /tmp/triage_greens.json
  apply [--dry] [--no-close] [--notes] [--in FILE]
                    — write-back stage. Reads the AI judgements JSON (default
                      /tmp/triage_judgements.json), writes triage-<rag> tags, resolves the
                      done-greens (status 4), flags bounces (triage-bounce-resend), optionally
                      adds a private note with the reason, writes the dashboard JSON, and saves
                      last-run state. --dry = print only; --no-close = tag but never change status.

Judgements JSON shape (produced by Claude after reading /tmp/triage_candidates.json):
  [{"id":123,"rag":"red|amber|green","reason":"one line","action":"tag|close|flag"}]
    action: tag  = just add triage-<rag>
            close = add triage-green + resolve (greens only; suppressed by --no-close)
            flag  = add triage-amber + triage-bounce-resend (delivery failure, do NOT close)

Creds: env or ~/.anyvan/config.txt (FRESHDESK_API_KEY / FRESHDESK_DOMAIN).
"""
import os, base64, json, time, sys, argparse
from datetime import datetime, timezone
import requests

cfg={}
cp=os.path.expanduser('~/.anyvan/config.txt')
if os.path.exists(cp):
    for line in open(cp):
        line=line.strip()
        if '=' in line and not line.startswith('#'):
            k,v=line.split('=',1); cfg[k.strip()]=v.strip()
API=os.environ.get('FRESHDESK_API_KEY') or cfg.get('FRESHDESK_API_KEY')
DOM=os.environ.get('FRESHDESK_DOMAIN') or cfg.get('FRESHDESK_DOMAIN')
assert API and DOM, "missing FRESHDESK creds"
BASE=f"https://{DOM}.freshdesk.com/api/v2"
HDR={"Authorization":"Basic "+base64.b64encode(f"{API}:X".encode()).decode(),"Content-Type":"application/json"}

GROUPS={31000116715:"Storage",31000118020:"Storage Complaints",31000117596:"Storage Payments",
        31000117121:"Storage Support",31000117989:"Storage Warehouse",31000119031:"Storage Whs Redeliveries"}
OPEN_STATUSES=(2,3,10,11)   # this instance's open/pending states (1 is invalid; 4/5 = resolved/closed)
PRIO={1:"Low",2:"Medium",3:"High",4:"Urgent"}
RESOLVED=4   # auto-close target: Resolved (reversible) rather than Closed(5)
# This instance requires a responder before a ticket can be resolved. Attribute
# auto-resolves to the integration agent (not a human) so real agents' KPI stats stay clean.
RESOLVE_RESPONDER=31006345803   # "AnyVan Freshdesk API - DO NOT DELETE" (tech-api+freshdesk@anyvan.com)
STATE_FILE=os.path.expanduser('~/.anyvan/freshdesk_triage_state.json')
DASH_JSON='/tmp/triage_dashboard.json'
now=datetime.now(timezone.utc)

def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {}
def save_state(d):
    json.dump(d,open(STATE_FILE,'w'),indent=2)
URGENT_KW=['complaint','refund','cancel','legal','solicitor','ombudsman','chargeback','dispute','urgent',
           'asap','today','tomorrow','missing','lost','stolen','damage','damaged','not happy','furious','angry']

def pdt(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace('Z','+00:00'))
    except Exception: return None
def hrs(dt): return None if not dt else round((now-dt).total_seconds()/3600,1)

def search(q,max_pages=6):
    out=[];page=1
    while page<=max_pages:
        r=requests.get(f"{BASE}/search/tickets",headers=HDR,params={"query":'"'+q+'"',"page":page},timeout=30)
        if r.status_code==429: time.sleep(int(r.headers.get('Retry-After','2'))+1); continue
        if r.status_code!=200:
            if page==1: print("search HTTP",r.status_code,r.text[:120],file=sys.stderr)
            break
        res=r.json().get('results',[]); out+=res
        if len(res)<30: break
        page+=1; time.sleep(0.35)
    return out

def pull_open(since=None):
    seen={}
    for gid in GROUPS:
        for st in OPEN_STATUSES:
            q=f"group_id:{gid} AND status:{st}"
            if since: q+=f" AND updated_at:>'{since}'"
            for t in search(q): seen[t['id']]=t
            time.sleep(0.25)
    return list(seen.values())

def last_public_msg(tid):
    """Return (direction, text, when) of the latest public (non-note) message; falls back to description."""
    try:
        r=requests.get(f"{BASE}/tickets/{tid}",headers=HDR,params={"include":"conversations"},timeout=30)
        if r.status_code!=200: return (None,'',None)
        t=r.json(); convs=[c for c in (t.get('conversations') or []) if not c.get('private')]
        if convs:
            c=convs[-1]
            return ('customer' if c.get('incoming') else 'us',
                    (c.get('body_text') or '')[:600], c.get('created_at'))
        return ('customer',(t.get('description_text') or '')[:600], t.get('created_at'))
    except Exception:
        return (None,'',None)

def rules(t):
    s=0; why=[]
    created=pdt(t.get('created_at')); updated=pdt(t.get('updated_at'))
    due=pdt(t.get('due_by')); frdue=pdt(t.get('fr_due_by'))
    prio=t.get('priority',1); subj=(t.get('subject') or '').lower()
    g=GROUPS.get(t.get('group_id'),''); age=hrs(created); idle=hrs(updated)
    if due and due<now: s+=3; why.append('resolution SLA breached')
    elif due and (due-now).total_seconds()<4*3600: s+=2; why.append('SLA <4h')
    if frdue and frdue<now: s+=2; why.append('first-reply SLA overdue')
    if prio==4: s+=3; why.append('Urgent')
    elif prio==3: s+=2; why.append('High')
    if age and age>72: s+=1; why.append(f'open {int(age)}h')
    if idle and idle>48: s+=1; why.append(f'idle {int(idle)}h')
    hit=[k for k in URGENT_KW if k in subj]
    if hit: s+=2; why.append('kw:'+','.join(hit[:3]))
    if g=='Storage Complaints': s+=1; why.append('complaints')
    rag='RED' if s>=4 else ('AMBER' if s>=2 else 'GREEN')
    return rag,s,why

DONE_SUBJ=['cancel','confirm','closed','close ','thank','received','noted','no longer','resolved',
           'out of office','automatic','auto-reply','autoreply','completed','delivered','sorted',
           'read receipt','undeliverable','delivery status','accepted','approved']
def cmd_greens(limit):
    ts=pull_open()
    cand=[t for t in ts if any(k in (t.get('subject') or '').lower() for k in DONE_SUBJ)]
    cand.sort(key=lambda t:GROUPS.get(t.get('group_id'),''))
    if limit: cand=cand[:limit]
    out=[]
    for t in cand:
        direction,msg,when=last_public_msg(t['id'])
        rec={'id':t['id'],'queue':GROUPS.get(t.get('group_id'),''),'subject':(t.get('subject') or '')[:90],
             'last_from':direction,'last_msg':(msg or '').replace(chr(10),' ')[:240],'age_h':hrs(pdt(t.get('created_at')))}
        out.append(rec)
        print(f"#{rec['id']} [{rec['queue']}] last_from={rec['last_from']} age={rec['age_h']}h")
        print(f"   subj: {rec['subject']}")
        print(f"   last: {rec['last_msg']}")
        time.sleep(0.2)
    json.dump(out,open('/tmp/triage_greens.json','w'),indent=2)
    print(f"\n[{len(out)} green-candidate tickets]")

def cmd_summary():
    ts=pull_open(); c={'RED':0,'AMBER':0,'GREEN':0}
    for t in ts: c[rules(t)[0]]+=1
    print(f"open={len(ts)}  RED {c['RED']}  AMBER {c['AMBER']}  GREEN {c['GREEN']}")

def get_tags(tid):
    r=requests.get(f"{BASE}/tickets/{tid}",headers=HDR,timeout=30)
    if r.status_code!=200: return None
    return r.json().get('tags') or []

def put_ticket(tid,payload):
    r=requests.put(f"{BASE}/tickets/{tid}",headers=HDR,data=json.dumps(payload),timeout=30)
    if r.status_code==429:
        time.sleep(int(r.headers.get('Retry-After','2'))+1)
        r=requests.put(f"{BASE}/tickets/{tid}",headers=HDR,data=json.dumps(payload),timeout=30)
    return r

def add_note(tid,body):
    r=requests.post(f"{BASE}/tickets/{tid}/notes",headers=HDR,
                    data=json.dumps({"body":body,"private":True}),timeout=30)
    return r.status_code in (200,201)

def cmd_apply(infile,dry,no_close,notes):
    infile=infile or '/tmp/triage_judgements.json'
    js=json.load(open(infile))
    # index candidates (if present) for queue/subject/url enrichment of the dashboard
    cand={}
    try:
        for c in json.load(open('/tmp/triage_candidates.json')): cand[c['id']]=c
    except Exception: pass
    counts={'red':0,'amber':0,'blue':0,'green':0}; resolved=0; flagged=0; errs=0
    board=[]
    for j in js:
        tid=j['id']; rag=(j.get('rag') or '').lower(); reason=j.get('reason','')
        action=(j.get('action') or 'tag').lower()
        counts[rag]=counts.get(rag,0)+1
        new_tags=[f"triage-{rag}"]
        if action=='flag': new_tags.append('triage-bounce-resend')
        do_close = action=='close' and rag=='green' and not no_close
        c=cand.get(tid,{})
        rec={'id':tid,'queue':c.get('queue',''),'subject':c.get('subject',''),
             'rag':rag,'reason':reason,'action':action,'resolved':do_close,
             'age_h':c.get('age_h'),'last_from':c.get('last_from'),
             'url':f"https://{DOM}.freshdesk.com/a/tickets/{tid}"}
        board.append(rec)
        if dry:
            print(f"[DRY] #{tid} {rag.upper():5} +{new_tags}"+(" RESOLVE" if do_close else "")+f"  {reason[:70]}")
            continue
        cur=get_tags(tid)
        if cur is None: print(f"  !! #{tid} read failed",file=sys.stderr); errs+=1; continue
        merged=sorted(set(cur)|set(new_tags))
        payload={"tags":merged}
        if do_close: payload["status"]=RESOLVED; payload["responder_id"]=RESOLVE_RESPONDER
        r=put_ticket(tid,payload)
        if r.status_code!=200:
            print(f"  !! #{tid} PUT {r.status_code} {r.text[:100]}",file=sys.stderr); errs+=1; continue
        if do_close: resolved+=1
        if action=='flag': flagged+=1
        if notes and reason: add_note(tid,f"🤖 Triage: {rag.upper()} — {reason}")
        print(f"#{tid} {rag.upper():5} tags={new_tags}"+(" → RESOLVED" if do_close else ""))
        time.sleep(0.25)
    json.dump({'generated_at':now.isoformat(),'counts':counts,'resolved':resolved,
               'flagged':flagged,'total':len(js),'tickets':board},open(DASH_JSON,'w'),indent=2)
    if not dry:
        st=load_state(); st['last_run']=now.isoformat(); st['last_counts']=counts; save_state(st)
    print(f"\n{'[DRY] ' if dry else ''}applied {len(js)}  RED {counts.get('red',0)}  AMBER {counts.get('amber',0)}  BLUE {counts.get('blue',0)}  GREEN {counts.get('green',0)}  resolved {resolved}  flagged {flagged}  errors {errs}")
    print(f"dashboard -> {DASH_JSON}")

ROSTER=[  # Storage ops team (June: all muddle in; July: route by element)
    {'name':'Sage','fd':31026581073,'slack':'U08PQ0XNVN3'},
    {'name':'Emmanuel','fd':31026362357,'slack':'U08EU0KTKRB'},
    {'name':'Theo J','fd':31026525500,'slack':'U08LVMSUEHY'},
    {'name':'Shafwaan','fd':31022287751,'slack':'U04SC2UNXS8'},
]
ROSTER_FD={a['fd'] for a in ROSTER}
INTEGRATION_AGENT=31006345803
STAKES=[('chargeback',6),('ombudsman',6),('legal',6),('solicitor',6),('stolen',5),('missing',5),
        ('lost',5),('damage',5),('refund',5),('dispute',5),('unauthoris',5),('overcharg',5),('charged',4),
        ('complaint',4),('furious',4),('unhappy',4),('notice period',4),('today',4),('tomorrow',4),
        ('imminent',4),('redeliver',3),('delivery of stored',3),('collect',3),('insurance',3)]
def stakes(reason):
    s=reason.lower(); return max([w for k,w in STAKES if k in s] or [1])

def cmd_assign(per,dry):
    per=per or 20
    V=json.load(open('/tmp/triage_verdicts.json'))
    cand={c['id']:c for c in json.load(open('/tmp/triage_all_candidates.json'))}
    # worklist = reds, highest stakes first then oldest
    reds=[x for x in V if x['rag']=='red']
    reds.sort(key=lambda x:(-stakes(x['reason']), -(cand.get(x['id'],{}).get('age_h') or 0)))
    cap=per*len(ROSTER)
    queue=reds[:cap]
    # check current owners; keep tickets already held by a human, round-robin the rest
    load={a['fd']:0 for a in ROSTER}; assign_to={}; kept=[]
    for x in queue:
        r=requests.get(f"{BASE}/tickets/{x['id']}",headers=HDR,timeout=30)
        resp=r.json().get('responder_id') if r.status_code==200 else None
        if resp and resp!=INTEGRATION_AGENT:
            kept.append((x['id'],resp))
            if resp in load: load[resp]+=1
        time.sleep(0.1)
    ri=0
    for x in queue:
        if any(x['id']==k[0] for k in kept): continue
        # next agent under cap, round-robin
        for _ in range(len(ROSTER)):
            a=ROSTER[ri%len(ROSTER)]; ri+=1
            if load[a['fd']]<per: assign_to[x['id']]=a['fd']; load[a['fd']]+=1; break
    # build per-agent breakdown
    byagent={a['fd']:[] for a in ROSTER}
    for tid,fd in assign_to.items(): byagent[fd].append(tid)
    nm={a['fd']:a for a in ROSTER}
    print(f"{'[DRY] ' if dry else ''}assigning {len(assign_to)} reds across {len(ROSTER)} agents (cap {per} each); {len(kept)} already owned, kept.")
    for a in ROSTER:
        ids=byagent[a['fd']]
        print(f"\n  {a['name']} ({len(ids)}):")
        for tid in ids:
            x=next(v for v in V if v['id']==tid); c=cand.get(tid,{})
            print(f"    #{tid} [{c.get('queue','')}] {x['reason'][:90]}")
    # write back responder_id + tag
    if not dry:
        for tid,fd in assign_to.items():
            cur=get_tags(tid) or []
            put_ticket(tid,{"responder_id":fd,"tags":sorted(set(cur)|{'triage-assigned'})})
            time.sleep(0.2)
        print(f"\nassigned {len(assign_to)} tickets in Freshdesk.")
    # emit slack message text + structured assignments
    out={'per':per,'kept':kept,'assignments':{nm[fd]['name']:ids for fd,ids in byagent.items()}}
    json.dump(out,open('/tmp/triage_assignments.json','w'),indent=2)
    lines=["🎫 *Storage Freshdesk — your tickets for today* (AI-triaged, reds first)",""]
    for a in ROSTER:
        ids=byagent[a['fd']]
        tix=' '.join(f"#{t}" for t in ids)
        lines.append(f"<@{a['slack']}> — *{len(ids)}* tickets: {tix}")
    lines.append("")
    lines.append("Full worklist + reasons → https://dashboards.anyvan.com/operations/storage-freshdesk-triage")
    open('/tmp/triage_slack_assign.txt','w').write('\n'.join(lines))
    print(f"\nslack text -> /tmp/triage_slack_assign.txt")

def cmd_fetchall():
    """Pull EVERY open Storage ticket (ignores last-run state) with latest msg + direction
    -> /tmp/triage_all_candidates.json. For a full backlog triage (workflow judges these)."""
    ts=pull_open(None)
    ts.sort(key=lambda t:-rules(t)[1])
    cands=[]
    for i,t in enumerate(ts):
        rrag,rs,why=rules(t)
        direction,msg,when=last_public_msg(t['id'])
        cands.append({'id':t['id'],'queue':GROUPS.get(t.get('group_id'),''),'subject':(t.get('subject') or '')[:120],
                      'priority':PRIO.get(t.get('priority',1)),'rules_rag':rrag,'rules_score':rs,
                      'age_h':hrs(pdt(t.get('created_at'))),'idle_h':hrs(pdt(t.get('updated_at'))),
                      'last_from':direction,'last_msg':(msg or '')[:600]})
        if i%25==0: print(f"  fetched {i}/{len(ts)}",file=sys.stderr)
        time.sleep(0.15)
    json.dump(cands,open('/tmp/triage_all_candidates.json','w'),indent=2)
    print(f"fetched {len(cands)} open tickets -> /tmp/triage_all_candidates.json")

def cmd_fetch(limit,since):
    if since is None:
        since=load_state().get('last_run')
        if since: print(f"[since last run: {since}]",file=sys.stderr)
    ts=pull_open(since)
    ts.sort(key=lambda t:-rules(t)[1])   # most-urgent-first so a sample shows the interesting ones
    if limit: ts=ts[:limit]
    cands=[]
    for t in ts:
        rrag,rs,why=rules(t)
        direction,msg,when=last_public_msg(t['id'])
        cands.append({'id':t['id'],'queue':GROUPS.get(t.get('group_id'),''),'subject':(t.get('subject') or '')[:90],
                      'priority':PRIO.get(t.get('priority',1)),'rules_rag':rrag,'rules_score':rs,'rules_why':why,
                      'age_h':hrs(pdt(t.get('created_at'))),'last_from':direction,'last_msg':msg})
        time.sleep(0.2)
    json.dump(cands,open('/tmp/triage_candidates.json','w'),indent=2)
    print(f"fetched {len(cands)} candidates -> /tmp/triage_candidates.json\n")
    for c in cands:
        print(f"#{c['id']} [{c['queue']}] {c['priority']} rules={c['rules_rag']}({c['rules_score']}) last_from={c['last_from']} age={c['age_h']}h")
        print(f"   subj: {c['subject']}")
        print(f"   last: {(c['last_msg'] or '').replace(chr(10),' ')[:200]}")

ap=argparse.ArgumentParser(); ap.add_argument('mode',nargs='?',default='summary')
ap.add_argument('--limit',type=int,default=0); ap.add_argument('--since',default=None)
ap.add_argument('--dry',action='store_true'); ap.add_argument('--no-close',dest='no_close',action='store_true')
ap.add_argument('--notes',action='store_true'); ap.add_argument('--in',dest='infile',default=None)
ap.add_argument('--per',type=int,default=20)
a=ap.parse_args()
if a.mode=='summary': cmd_summary()
elif a.mode=='fetch': cmd_fetch(a.limit,a.since)
elif a.mode=='greens': cmd_greens(a.limit or 40)
elif a.mode=='fetchall': cmd_fetchall()
elif a.mode=='assign': cmd_assign(a.per,a.dry)
elif a.mode=='apply': cmd_apply(a.infile,a.dry,a.no_close,a.notes)
