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
  [{"id":123,"rag":"red|amber|blue|green","lane":"log|reply|action|none",
    "reason":"one line","note_draft":"...","action":"tag|close|flag"}]
    rag    = severity (drives assignment + Slack).
    lane   = what to DO (orthogonal to severity); drives the private-note body shape:
             log    = customer stated a fact, nothing to change -> note + AUTO-CLOSE (safe, internal)
             reply  = customer asked something we can answer -> note_draft = the answer; agent SENDS (never auto-sent)
             action = change request / complaint / legal / damage -> HUMAN-ONLY; note = "action needed" + prep.
                      Grades on merits, NOT always-red.
             none   = system msg, bounce, pure ack, or ball-in-their-court (blue)
    action = write-back mechanism: tag = add triage-<rag> only
             close = resolve (greens + log-lane facts); suppressed by --no-close
             flag  = add triage-bounce-resend (delivery failure, do NOT close)
    note_draft (optional) = drafted reply (reply lane) or suggested handling (action lane);
                            written into the PRIVATE note only, never sent.

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
# Prefer the triage BOT agent's own key (so notes/tags/resolves are authored by the
# "AnyVan Freshdesk API - DO NOT DELETE" bot, not the human whose key this otherwise is).
# Falls back to the generic key if the bot key isn't configured.
API=(os.environ.get('FRESHDESK_TRIAGE_API_KEY') or cfg.get('FRESHDESK_TRIAGE_API_KEY')
     or os.environ.get('FRESHDESK_API_KEY') or cfg.get('FRESHDESK_API_KEY'))
DOM=os.environ.get('FRESHDESK_DOMAIN') or cfg.get('FRESHDESK_DOMAIN')
assert API and DOM, "missing FRESHDESK creds"
BASE=f"https://{DOM}.freshdesk.com/api/v2"
HDR={"Authorization":"Basic "+base64.b64encode(f"{API}:X".encode()).decode(),"Content-Type":"application/json"}

# Pooled session + retry: reuse connections (avoids local ephemeral-port exhaustion on big
# --all sweeps) and retry transient failures. The per-ticket reads have no other 429 handling,
# so without this a throttled sweep silently returns empty bodies and the judge sees nothing.
SESS=requests.Session()
SESS.mount('https://',requests.adapters.HTTPAdapter(pool_connections=8,pool_maxsize=8))
def rget(url,tries=6,**kw):
    """GET with connection-error + 429 retry (backoff). Returns the final Response, or None
    if every attempt errored at the socket level."""
    if not url.startswith('http'): url=f"{BASE}{url}"
    kw.setdefault('headers',HDR); kw.setdefault('timeout',30)
    r=None
    for k in range(tries):
        try:
            r=SESS.get(url,**kw)
        except requests.exceptions.RequestException:
            time.sleep(2+k); continue
        if r.status_code==429:
            time.sleep(int(r.headers.get('Retry-After','3'))+1); continue
        return r
    return r

GROUPS={31000116715:"Storage",31000118020:"Storage Complaints",31000117596:"Storage Payments",
        31000117121:"Storage Support",31000117989:"Storage Warehouse",31000119031:"Storage Whs Redeliveries"}
OPEN_STATUSES=(2,3,10,11)   # this instance's open/pending states (1 is invalid; 4/5 = resolved/closed)
PRIO={1:"Low",2:"Medium",3:"High",4:"Urgent"}
RESOLVED=4   # auto-close target: Resolved (reversible) rather than Closed(5)
# This instance requires a responder before a ticket can be resolved. Attribute
# auto-resolves to the integration agent (not a human) so real agents' KPI stats stay clean.
RESOLVE_RESPONDER=31006345803   # "AnyVan Freshdesk API - DO NOT DELETE" (tech-api+freshdesk@anyvan.com)
STATE_FILE=os.path.expanduser('~/.anyvan/freshdesk_triage_state.json')
# Per-ticket "last public message timestamp we've already seen", so we can tell a GENUINE new
# customer reply from a ticket that's merely still-open since the last run. Without this, an
# --all run would re-note every open ticket every day. {ticket_id(str): last_msg_ts}.
SEEN_FILE=os.path.expanduser('~/.anyvan/freshdesk_triage_seen.json')
DASH_JSON='/tmp/triage_dashboard.json'
now=datetime.now(timezone.utc)

def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {}
def save_state(d):
    json.dump(d,open(STATE_FILE,'w'),indent=2)
def load_seen():
    try: return json.load(open(SEEN_FILE))
    except Exception: return None   # None = never run before (baseline; don't burst-note)
def save_seen(d):
    json.dump(d,open(SEEN_FILE,'w'),indent=2)
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
        r=rget(f"{BASE}/search/tickets",params={"query":'"'+q+'"',"page":page})
        if r is None: break
        if r.status_code!=200:
            if page==1: print("search HTTP",r.status_code,r.text[:120],file=sys.stderr)
            break
        res=r.json().get('results',[]); out+=res
        if len(res)<30: break
        page+=1; time.sleep(0.35)
    return out

def pull_open(since=None):
    # Freshdesk's updated_at filter is DATE-granular (YYYY-MM-DD) — strip any time/tz.
    # Use the day BEFORE last_run so the run's own day is re-included (overlap is idempotent;
    # re-judging an unchanged ticket just re-writes the same tag), avoiding a same-day-tail miss.
    since_date=None
    if since:
        d=pdt(since) or pdt(since+'T00:00:00')
        try:
            from datetime import timedelta
            since_date=(d - timedelta(days=1)).date().isoformat()
        except Exception:
            since_date=str(since)[:10]
    seen={}
    for gid in GROUPS:
        for st in OPEN_STATUSES:
            q=f"group_id:{gid} AND status:{st}"
            if since_date: q+=f" AND updated_at:>'{since_date}'"
            for t in search(q): seen[t['id']]=t
            time.sleep(0.25)
    return list(seen.values())

def pull_open_list(days_back=4, max_pages=80):
    """AUTHORITATIVE discovery via the LIST endpoint (/tickets?updated_since). The Lucene
    /search index is eventually-consistent and silently drops recent tickets; the list
    endpoint is consistent. Returns OPEN Storage-group tickets updated in the last `days_back`
    days. Union this with search so nothing slips through untriaged/unassigned."""
    from datetime import timedelta
    since=(now - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
    out=[]; page=1
    while page<=max_pages:
        # NEWEST-first: if company volume ever exceeds the page cap, we drop the OLDEST end
        # (which search reliably covers) and never the recent untriaged tickets we must catch.
        r=rget(f"{BASE}/tickets",
                       params={"updated_since":since,"order_by":"updated_at","order_type":"desc","per_page":100,"page":page},timeout=30)
        if r is None: break
        if r.status_code!=200:
            if page==1: print("list HTTP",r.status_code,r.text[:120],file=sys.stderr)
            break
        batch=r.json()
        if not isinstance(batch,list) or not batch: break
        out+=[t for t in batch if t.get('group_id') in GROUPS and t.get('status') in OPEN_STATUSES]
        if len(batch)<100: break
        page+=1; time.sleep(0.3)
    return out

def last_public_msg(tid):
    """Return (direction, text, when) of the latest public (non-note) message; falls back to description."""
    try:
        r=rget(f"{BASE}/tickets/{tid}",params={"include":"conversations"})
        if r is None or r.status_code!=200: return (None,'',None)
        t=r.json(); convs=[c for c in (t.get('conversations') or []) if not c.get('private')]
        if convs:
            c=convs[-1]
            return ('customer' if c.get('incoming') else 'us',
                    (c.get('body_text') or '')[:600], c.get('created_at'))
        return ('customer',(t.get('description_text') or '')[:600], t.get('created_at'))
    except Exception:
        return (None,'',None)

def full_thread(tid, max_chars=6000, max_msgs=25):
    """ALL public messages oldest->newest, '[customer]/[us]' prefixed, capped. Used for
    likely-reds (rules-proxy) so the judge sees the whole conversation, not just the latest
    message. Same single API call as last_public_msg. Returns (last_direction, thread_text, last_msg_ts)."""
    try:
        r=rget(f"{BASE}/tickets/{tid}",params={"include":"conversations"})
        if r is None or r.status_code!=200: return (None,'',None)
        t=r.json(); seq=[]; last_ts=t.get('created_at')
        desc=(t.get('description_text') or '').strip()
        if desc: seq.append(('customer',desc))
        for c in (t.get('conversations') or []):
            if c.get('private'): continue
            txt=(c.get('body_text') or '').strip()
            if txt:
                seq.append(('customer' if c.get('incoming') else 'us',txt))
                if c.get('created_at'): last_ts=c.get('created_at')   # newest public msg ts
        if not seq: return ('customer','',last_ts)
        last_dir=seq[-1][0]; seq=seq[-max_msgs:]
        parts=[f"[{d}] {txt[:1500]}" for d,txt in seq]
        return (last_dir, ('\n---\n'.join(parts))[:max_chars], last_ts)
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
    r=rget(f"{BASE}/tickets/{tid}")
    if r is None or r.status_code!=200: return None
    return r.json().get('tags') or []

def get_tags_status(tid):
    """Live (tags, status). Used to re-verify a ticket is still OPEN immediately before
    writing — closes the search-index-staleness race where a ticket resolved/closed by an
    agent between fetch and write could otherwise receive a write (and trip any reopen-on-
    update automation). Returns (None,None) on read failure."""
    r=rget(f"{BASE}/tickets/{tid}")
    if r is None or r.status_code!=200: return (None,None)
    j=r.json(); return (j.get('tags') or [], j.get('status'))

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

def build_note(j):
    """Lane-shaped PRIVATE note body. REPLY/ACTION drafts are for a human to read & send —
    nothing here is ever emailed to the customer (zero-email governance)."""
    rag=(j.get('rag') or '').upper(); lane=(j.get('lane') or 'none').lower()
    reason=j.get('reason',''); draft=(j.get('note_draft') or '').strip()
    if lane=='log':
        b=f"🤖 Triage [LOG] — customer stated a fact, nothing to change. Ticket auto-resolved.\n{reason}"
        if draft: b+=f"\n\nLogged: {draft}"
        return b
    if lane=='reply':
        b=f"🤖 Triage [REPLY] — draft answer for an agent to review & SEND (not sent automatically):\n\n{draft or '(no draft supplied)'}"
        if reason: b+=f"\n\n— why: {reason}"
        return b
    if lane=='action':
        b=f"🤖 Triage [ACTION — human only] — {reason}"
        if draft: b+=f"\n\nSuggested handling / draft:\n{draft}"
        return b
    return f"🤖 Triage: {rag} — {reason}"

def cmd_apply(infile,dry,no_close,notes):
    infile=infile or '/tmp/triage_judgements.json'
    js=json.load(open(infile))
    # index candidates (if present) for queue/subject/url enrichment of the dashboard
    cand={}
    try:
        for c in json.load(open('/tmp/triage_candidates.json')): cand[c['id']]=c
    except Exception: pass
    counts={'red':0,'amber':0,'blue':0,'green':0}; lanes={'log':0,'reply':0,'action':0,'none':0}
    resolved=0; flagged=0; errs=0; skipped=0; noted=0
    # Note policy: NEW ticket (unseen) in an actionable lane -> note; a GENUINE new customer reply
    # (last_msg_ts advanced since we last saw it) -> note (any lane). First run ever = baseline:
    # record timestamps WITHOUT noting, so we don't dump a note on every already-open ticket.
    seen=load_seen(); baseline=(seen is None)
    if seen is None: seen={}
    board=[]
    for j in js:
        tid=j['id']; rag=(j.get('rag') or '').lower(); reason=j.get('reason','')
        action=(j.get('action') or 'tag').lower(); lane=(j.get('lane') or 'none').lower()
        counts[rag]=counts.get(rag,0)+1; lanes[lane]=lanes.get(lane,0)+1
        new_tags=[f"triage-{rag}"]
        if action=='flag': new_tags.append('triage-bounce-resend')
        if lane in ('log','reply','action'): new_tags.append(f"triage-{lane}")
        # close on an explicit close action for greens, OR a log-lane fact (auto-close after note)
        do_close = (not no_close) and action=='close' and (rag=='green' or lane=='log')
        c=cand.get(tid,{})
        rec={'id':tid,'queue':c.get('queue',''),'subject':c.get('subject',''),
             'rag':rag,'lane':lane,'reason':reason,'action':action,'resolved':do_close,
             'age_h':c.get('age_h'),'idle_h':c.get('idle_h'),'last_from':c.get('last_from'),
             'url':f"https://{DOM}.freshdesk.com/a/tickets/{tid}"}
        board.append(rec)
        if dry:
            print(f"[DRY] #{tid} {rag.upper():5} {lane:6} +{new_tags}"+(" RESOLVE" if do_close else "")+f"  {reason[:60]}")
            continue
        cur,cur_status=get_tags_status(tid)
        if cur is None: print(f"  !! #{tid} read failed",file=sys.stderr); errs+=1; continue
        # Skip anything no longer open (agent resolved/closed it after our fetch — search index lag).
        # Writing to a resolved/closed ticket risks reopening it via account automation; never do it.
        if cur_status not in OPEN_STATUSES:
            print(f"  ~~ #{tid} now status {cur_status} (not open) — skipped, no write",file=sys.stderr); skipped+=1; continue
        # Strip stale RAG + lane tags so a re-judged ticket carries only its CURRENT grade/lane
        # (keeps triage-assigned, triage-bounce-resend and all non-triage tags).
        RAG_TAGS={'triage-red','triage-amber','triage-blue','triage-green'}
        LANE_TAGS={'triage-log','triage-reply','triage-action'}
        merged=sorted((set(cur)-RAG_TAGS-LANE_TAGS)|set(new_tags))
        payload={"tags":merged}
        if do_close: payload["status"]=RESOLVED; payload["responder_id"]=RESOLVE_RESPONDER
        r=put_ticket(tid,payload)
        if r.status_code!=200:
            print(f"  !! #{tid} PUT {r.status_code} {r.text[:100]}",file=sys.stderr); errs+=1; continue
        if do_close: resolved+=1
        if action=='flag': flagged+=1
        # --- note gating ---
        sid=str(tid); lts=c.get('last_msg_ts'); prior_ts=seen.get(sid)
        is_new_ticket=sid not in seen
        new_reply=(not is_new_ticket) and (c.get('last_from')=='customer') and lts and prior_ts and (lts>prior_ts)
        want_note=(is_new_ticket and lane in ('log','reply','action')) or new_reply
        why_note='new' if (is_new_ticket and want_note) else ('reply' if new_reply else '')
        if notes and not baseline and want_note and (reason or j.get('note_draft')):
            add_note(tid,build_note(j)); noted+=1
        seen[sid]=lts or prior_ts   # advance the marker (keep last-known if this fetch lacked a ts)
        print(f"#{tid} {rag.upper():5} {lane:6} tags={new_tags}"+(" → RESOLVED" if do_close else "")+(f"  📝note({why_note})" if (notes and not baseline and want_note) else ""))
        time.sleep(0.25)
    json.dump({'generated_at':now.isoformat(),'counts':counts,'lanes':lanes,'resolved':resolved,
               'flagged':flagged,'total':len(js),'tickets':board},open(DASH_JSON,'w'),indent=2)
    if not dry:
        st=load_state(); st['last_run']=now.isoformat(); st['last_counts']=counts; save_state(st)
        save_seen(seen)
        if baseline: print(f"[seen baseline seeded: {len(seen)} tickets recorded — notes start from the NEXT run]",file=sys.stderr)
    print(f"\n{'[DRY] ' if dry else ''}applied {len(js)}  RED {counts.get('red',0)}  AMBER {counts.get('amber',0)}  BLUE {counts.get('blue',0)}  GREEN {counts.get('green',0)}  resolved {resolved}  flagged {flagged}  notes {noted}  skipped(not-open) {skipped}  errors {errs}")
    print(f"   lanes: log {lanes['log']}  reply {lanes['reply']}  action {lanes['action']}  none {lanes['none']}")
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

def cmd_assign(per,dry,allscope=False):
    per=per or 20
    # Read THIS run's judgements + candidates (the canonical files written by judge/fetch).
    # Fall back to the legacy workflow filenames only if the current ones are absent.
    def _load(primary,legacy):
        for p in (primary,legacy):
            if os.path.exists(p): return json.load(open(p))
        raise FileNotFoundError(f"{primary} (or {legacy})")
    V=_load('/tmp/triage_judgements.json','/tmp/triage_verdicts.json')
    cand={c['id']:c for c in _load('/tmp/triage_candidates.json','/tmp/triage_all_candidates.json')}
    # worklist: reds only (default) or ALL open tickets (--all). Sort reds→amber→blue→green,
    # then highest-stakes, then oldest, so high-priority work distributes first/evenly.
    rank={'red':0,'amber':1,'blue':2,'green':3}
    if allscope:
        queue=sorted(V,key=lambda x:(rank.get(x.get('rag'),9),-stakes(x.get('reason','')),-(cand.get(x['id'],{}).get('age_h') or 0)))
        per=10**6   # no per-agent cap: every open ticket gets an owner; round-robin = even split
    else:
        reds=[x for x in V if x['rag']=='red']
        reds.sort(key=lambda x:(-stakes(x['reason']), -(cand.get(x['id'],{}).get('age_h') or 0)))
        queue=reds[:per*len(ROSTER)]
    # check current owners; keep tickets already held by a human, round-robin the rest
    load={a['fd']:0 for a in ROSTER}; assign_to={}; kept=[]; gone=set()
    for x in queue:
        r=rget(f"{BASE}/tickets/{x['id']}")
        j=r.json() if (r is not None and r.status_code==200) else {}
        # Skip anything resolved/closed since fetch — never assign/write to a non-open ticket.
        if j.get('status') not in OPEN_STATUSES:
            gone.add(x['id']); time.sleep(0.1); continue
        resp=j.get('responder_id')
        if resp and resp!=INTEGRATION_AGENT:
            kept.append((x['id'],resp))
            if resp in load: load[resp]+=1
        time.sleep(0.1)
    queue=[x for x in queue if x['id'] not in gone]
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
    _lbl='tickets (ALL open)' if allscope else 'reds'
    _cap='no cap — every open ticket gets an owner' if allscope else f'cap {per} each'
    print(f"{'[DRY] ' if dry else ''}assigning {len(assign_to)} {_lbl} across {len(ROSTER)} agents ({_cap}); {len(kept)} already owned, kept.")
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
    # high-level run summary (counts) from the apply step's dashboard JSON
    try:
        d=json.load(open(DASH_JSON)); c=d.get('counts',{})
        red,amb,blu,grn=c.get('red',0),c.get('amber',0),c.get('blue',0),c.get('green',0)
        res,flg,tot=d.get('resolved',0),d.get('flagged',0),d.get('total',0)
    except Exception:
        red=amb=blu=grn=res=flg=tot=0
    URL="https://dashboards.anyvan.com/operations/storage-freshdesk-triage"
    lines=[]
    lines.append("🎫 *Storage triage has just run* — here's the latest sweep 👇")
    lines.append("")
    lines.append(f"📥 *{tot} tickets triaged this run*")
    lines.append(f"   🔴 *{red}* urgent  ·  🟠 *{amb}* can-wait  ·  🔵 *{blu}* awaiting customer  ·  🟢 *{grn}* no-reply")
    lines.append(f"   ✅ *{res}* auto-resolved (signed docs & system notifications)  ·  📨 *{flg}* delivery failures flagged to resend")
    lines.append("")
    # ⚠️ Escalations — high-stakes reds (legal/ombudsman/chargeback/etc.) called out by # so they can't blend into the red count.
    esc=[x for x in V if x.get('rag')=='red' and stakes(x.get('reason','')) >= 5]
    esc.sort(key=lambda x:-stakes(x.get('reason','')))
    if esc:
        lines.append("⚠️ *ESCALATIONS — high-stakes, eyes on these first:*")
        for x in esc[:12]:
            c=cand.get(x['id'],{})
            url=f"https://{DOM}.freshdesk.com/a/tickets/{x['id']}"
            lines.append(f"   • <{url}|#{x['id']}> [{c.get('queue','')}] — {(x.get('reason') or '')[:90]}")
        if len(esc)>12: lines.append(f"   …and *{len(esc)-12}* more")
        lines.append("")
    lines.append("🧑‍💻 *New tickets added to your queue* — open Freshdesk → *\"Tickets assigned to me\"*:")
    any_new=False
    for a in ROSTER:
        n=len(byagent[a['fd']])
        if n: any_new=True
        lines.append(f"   • <@{a['slack']}> — {'*'+str(n)+'* new' if n else 'up to date ✅'}")
    if not any_new:
        lines.append("   _(no new allocations — everyone's at capacity; please clear current queues first)_")
    lines.append("")
    lines.append("Reds first please — clear or note your queue by end of day. 💪")
    lines.append(f"📋 Full board → {URL}")
    open('/tmp/triage_slack_assign.txt','w').write('\n'.join(lines))
    print(f"\nslack text -> /tmp/triage_slack_assign.txt")

class ThrottledFD:
    """Polite, rate-limit-aware Freshdesk caller for BULK jobs on the SHARED account bucket.
    Robustness, not guesswork:
      • Self-cap — never exceeds `our_budget` calls/min (a small slice of the 3000/min account
        limit), so a bulk job is always a minority of shared usage.
      • Reserve-aware — reads X-RateLimit-Remaining off every response; if the ACCOUNT-WIDE
        remaining falls below `reserve` (the company is busy), it fully YIELDS until the bucket
        refills — never starves other teams.
      • 429-safe — on 429 it sleeps exactly Retry-After (+buffer) and RETRIES the same call;
        never skips, never hammers (hammering while throttled extends the penalty).
    Reusable for any bulk Freshdesk operation."""
    def __init__(self, base, hdr, our_budget=300, reserve=1000):
        self.base=base; self.hdr=hdr; self.min_interval=60.0/max(1,our_budget)
        self.reserve=reserve; self.last=0.0; self.remaining=None; self.calls=0
    def request(self, method, path, **kw):
        while True:
            gap=time.time()-self.last
            if gap < self.min_interval: time.sleep(self.min_interval-gap)   # self-cap pacing
            if self.remaining is not None and self.remaining < self.reserve: # account busy → yield
                print(f"  [throttle] account remaining {self.remaining} < {self.reserve} — yielding 30s for refill",file=sys.stderr)
                time.sleep(30); self.remaining=None
            r=requests.request(method,f"{self.base}{path}",headers=self.hdr,timeout=30,**kw)
            self.last=time.time(); self.calls+=1
            rem=r.headers.get('X-RateLimit-Remaining')
            if rem is not None:
                try: self.remaining=int(rem)
                except Exception: pass
            if r.status_code==429:
                ra=int(r.headers.get('Retry-After','30'))+2
                print(f"  [throttle] 429 — sleeping {ra}s, retrying same call",file=sys.stderr); time.sleep(ra); continue
            return r

def cmd_assignall(go):
    """Distribute ALL open tickets evenly across the roster, skipping any already owned by a human.
    DRY (default) plans off the LOCAL feed — ZERO API calls. --go REFRESHES the live open-list
    (search; so only genuinely-open + unowned tickets are assigned, no stale/resolved ones), then
    writes responders via ThrottledFD (≤300/min, yields if account busy, 429-retries, resumable)."""
    name2fd={a['name']:a['fd'] for a in ROSTER}; fd2name={a['fd']:a['name'] for a in ROSTER}
    roster_names={a['name'] for a in ROSTER}; rank={'red':0,'blue':1,'amber':2,'green':3}
    feed=json.load(open(os.path.expanduser('~/.anyvan/triage_feed.json')))
    feed_by_id={t['id']:t for t in feed.get('tickets',[])}
    def m(t): return t.get('idle_h') if t.get('idle_h') is not None else (t.get('age_h') or 0)
    load={a['name']:0 for a in ROSTER}; pool=[]; skipped=0
    if go:
        # LIVE refresh: source of truth for what's OPEN + who currently owns it (search-based, 429-safe)
        print("refreshing LIVE open-list (search) so only genuinely-open + unowned tickets get assigned…",file=sys.stderr)
        for t in pull_open(None):
            resp=t.get('responder_id')
            if resp in fd2name: load[fd2name[resp]]+=1                       # already a roster agent's
            elif resp and resp not in (INTEGRATION_AGENT,RESOLVE_RESPONDER): skipped+=1   # another human's — leave
            else:                                                            # unowned/integration → distribute
                ft=feed_by_id.get(t['id'],{})
                pool.append({'id':t['id'],'rag':ft.get('rag','amber'),'idle_h':hrs(pdt(t.get('updated_at')))})
    else:
        for t in [x for x in feed.get('tickets',[]) if not x.get('resolved')]:
            o=(t.get('owner') or '')
            if o in roster_names: load[o]+=1
            elif o: skipped+=1
            else: pool.append({'id':t['id'],'rag':t.get('rag','amber'),'idle_h':m(t)})
    pool.sort(key=lambda t:(rank.get(t.get('rag'),9), -(t.get('idle_h') or 0)))   # reds first, oldest first
    assign={}
    for t in pool:
        a=min(load,key=lambda n:load[n]); assign[t['id']]=a; load[a]+=1   # greedy → lowest load
    print(f"{'[GO] ' if go else '[DRY] '}distributing {len(pool)} unowned across {len(ROSTER)} agents; {skipped} already owned by a human (left).")
    for a in ROSTER: print(f"  {a['name']:9} → {load[a['name']]} total")
    if not go:
        print("\n(DRY — zero API calls. `assignall --go` refreshes live + writes, throttled.)"); return
    # durable resume state (NOT /tmp) so a 429/stop/crash picks up where it left off
    state_p=os.path.expanduser('~/.anyvan/triage_assignall_done.json')
    done=set(json.load(open(state_p))) if os.path.exists(state_p) else set()
    todo=[(tid,nm) for tid,nm in assign.items() if tid not in done]
    fd=ThrottledFD(BASE,HDR,our_budget=300,reserve=1000)   # ≤300/min, yields if account busy
    print(f"writing {len(todo)} (responder-only, throttled ≤300/min, auto-yields + 429-retries)…")
    n=0
    for tid,name in todo:
        r=fd.request('PUT',f"/tickets/{tid}",data=json.dumps({"responder_id":name2fd[name]}))
        if r.status_code!=200:
            print(f"  !! #{tid} {r.status_code} {r.text[:80]}",file=sys.stderr); continue
        done.add(tid); n+=1
        if n%25==0: json.dump(list(done),open(state_p,'w')); print(f"  …{n}/{len(todo)} (acct remaining {fd.remaining})",file=sys.stderr)
    json.dump(list(done),open(state_p,'w'))
    print(f"assigned {n} this pass — {len(done)}/{len(assign)} done. ({fd.calls} API calls made.)")

def cmd_fetchall():
    """Pull EVERY open Storage ticket (ignores last-run state) with latest msg + direction
    -> /tmp/triage_all_candidates.json. For a full backlog triage (workflow judges these)."""
    ts=pull_open(None)
    ts.sort(key=lambda t:-rules(t)[1])
    cands=[]
    for i,t in enumerate(ts):
        rrag,rs,why=rules(t)
        if rrag=='RED':
            direction,msg,lts=full_thread(t['id']); read='full'
        else:
            direction,msg,lts=last_public_msg(t['id']); msg=(msg or '')[:600]; read='last'
        cands.append({'id':t['id'],'queue':GROUPS.get(t.get('group_id'),''),'subject':(t.get('subject') or '')[:120],
                      'priority':PRIO.get(t.get('priority',1)),'rules_rag':rrag,'rules_score':rs,
                      'age_h':hrs(pdt(t.get('created_at'))),'idle_h':hrs(pdt(t.get('updated_at'))),
                      'last_from':direction,'read':read,'last_msg':msg,'last_msg_ts':lts})
        if i%25==0: print(f"  fetched {i}/{len(ts)}",file=sys.stderr)
        time.sleep(0.15)
    json.dump(cands,open('/tmp/triage_all_candidates.json','w'),indent=2)
    print(f"fetched {len(cands)} open tickets -> /tmp/triage_all_candidates.json")

def cmd_fetch(limit,allflag,fetch_days=4):
    # INCREMENTAL = tickets with no triage-<rag> tag yet (genuinely new/untriaged).
    # We key off the tag, NOT updated_at: our own tag writes bump updated_at, so a
    # time-based filter re-pulls everything we touched. --all re-judges the whole open set.
    RAG_TAGS={'triage-red','triage-amber','triage-blue','triage-green'}
    # BULLETPROOF discovery: union search (group-targeted) with the AUTHORITATIVE list endpoint
    # (catches tickets the eventually-consistent search index drops). No ticket left untriaged.
    seen={t['id']:t for t in pull_open(None)}
    n_search=len(seen); added=0
    for t in pull_open_list(days_back=fetch_days):
        if t['id'] not in seen: seen[t['id']]=t; added+=1
    ts=list(seen.values())
    print(f"[discovery: {n_search} via search + {added} extra via list = {len(ts)} open]",file=sys.stderr)
    if not allflag:
        before=len(ts)
        ts=[t for t in ts if not (set(t.get('tags') or []) & RAG_TAGS)]
        print(f"[incremental: {len(ts)} untriaged of {before} open]",file=sys.stderr)
    ts.sort(key=lambda t:-rules(t)[1])   # most-urgent-first so a sample shows the interesting ones
    if limit: ts=ts[:limit]
    cands=[]
    for t in ts:
        rrag,rs,why=rules(t)
        # rules-proxy: likely-reds get the FULL thread; everything else just the latest message.
        if rrag=='RED':
            direction,msg,lts=full_thread(t['id']); read='full'
        else:
            direction,msg,lts=last_public_msg(t['id']); read='last'
        cands.append({'id':t['id'],'queue':GROUPS.get(t.get('group_id'),''),'subject':(t.get('subject') or '')[:90],
                      'priority':PRIO.get(t.get('priority',1)),'rules_rag':rrag,'rules_score':rs,'rules_why':why,
                      'age_h':hrs(pdt(t.get('created_at'))),'idle_h':hrs(pdt(t.get('updated_at'))),
                      'last_from':direction,'read':read,'last_msg':msg,'last_msg_ts':lts})
        time.sleep(0.2)
    json.dump(cands,open('/tmp/triage_candidates.json','w'),indent=2)
    print(f"fetched {len(cands)} candidates -> /tmp/triage_candidates.json\n")
    for c in cands:
        print(f"#{c['id']} [{c['queue']}] {c['priority']} rules={c['rules_rag']}({c['rules_score']}) last_from={c['last_from']} age={c['age_h']}h")
        print(f"   subj: {c['subject']}")
        print(f"   last: {(c['last_msg'] or '').replace(chr(10),' ')[:200]}")

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('mode',nargs='?',default='summary')
    ap.add_argument('--limit',type=int,default=0); ap.add_argument('--since',default=None)
    ap.add_argument('--dry',action='store_true'); ap.add_argument('--no-close',dest='no_close',action='store_true')
    # Notes ON by default (leave triage context for the agent). --no-notes to suppress; --notes kept as a no-op alias.
    ap.add_argument('--notes',action='store_true'); ap.add_argument('--no-notes',dest='no_notes',action='store_true')
    ap.add_argument('--in',dest='infile',default=None)
    ap.add_argument('--per',type=int,default=20); ap.add_argument('--all',dest='allflag',action='store_true')
    ap.add_argument('--go',action='store_true'); ap.add_argument('--days',type=int,default=4)
    a=ap.parse_args()
    if a.mode=='summary': cmd_summary()
    elif a.mode=='fetch': cmd_fetch(a.limit,a.allflag,a.days)
    elif a.mode=='greens': cmd_greens(a.limit or 40)
    elif a.mode=='fetchall': cmd_fetchall()
    elif a.mode=='assign': cmd_assign(a.per,a.dry,allscope=a.allflag)
    elif a.mode=='assignall': cmd_assignall(a.go)
    elif a.mode=='apply': cmd_apply(a.infile,a.dry,a.no_close,notes=(not a.no_notes))
