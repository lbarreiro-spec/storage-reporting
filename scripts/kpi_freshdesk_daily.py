"""
Freshdesk Daily KPI — Storage Team
Writes yesterday's data to JSON.

Metrics:
  Overview  : Tickets In, Resolved, Backlog, FR SLA%, Res SLA%, Avg/p90 resolve
  By Queue  : In, Resolved, Backlog, FR SLA% per Storage queue
  By Agent  : Assigned, Resolved, Backlog, FR SLA%, Avg/p90 resolve per agent

Usage:
  python3 kpi_freshdesk_daily.py           # writes yesterday's data
  python3 kpi_freshdesk_daily.py 2026-03-04  # writes data for a specific date
"""

import sys, os, json, time, base64, urllib.request, urllib.parse, urllib.error
from datetime import date, datetime, timedelta
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
# Env vars take priority (GitHub Actions), fall back to local config file
API_KEY = os.environ.get('FRESHDESK_API_KEY')
DOMAIN  = os.environ.get('FRESHDESK_DOMAIN')

if not API_KEY or not DOMAIN:
    config = {}
    for _cp in [os.path.expanduser('~/.anyvan/config.txt'), os.path.expanduser('~/Documents/anyvan-kpi/.env')]:
        if os.path.exists(_cp):
            with open(_cp) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if '=' in _line and not _line.startswith('#'):
                        _k, _v = _line.split('=', 1)
                        config[_k.strip()] = _v.strip()
            break
    API_KEY = API_KEY or config.get('FRESHDESK_API_KEY')
    DOMAIN  = DOMAIN  or config.get('FRESHDESK_DOMAIN')

if not API_KEY or not DOMAIN:
    raise RuntimeError('FRESHDESK_API_KEY and FRESHDESK_DOMAIN must be set')

BASE_URL = f"https://{DOMAIN}.freshdesk.com/api/v2"
AUTH     = base64.b64encode(f"{API_KEY}:X".encode()).decode()
FD_HDR   = {"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"}

STORAGE_GROUPS = {
    31000116715: "Storage",
    31000118020: "Storage Complaints",
    31000117596: "Storage Payments",
    31000117121: "Storage Support",
    31000117989: "Storage Warehouse",
    31000119031: "Storage Warehouse Redeliveries",
}

TRACKED_AGENTS = ['Shaun', 'Emmanuel', 'Theo', 'Shafwaan']
AGENT_NAME_FRAGMENTS = {
    'Shaun':    'Shaun G',
    'Emmanuel': 'Emmanuel',
    'Theo':     'Theo',
    'Shafwaan': 'Shafwaan',
}

ROW = {
    'date':         3,
    'in':           5,
    'resolved':     6,
    'backlog':      7,
    'fr_sla':       8,
    'res_sla':      9,
    'avg_resolve':  10,
    'p90_resolve':  11,
    'Storage Support':                  {'in':13,'resolved':14,'backlog':15,'fr_sla':16},
    'Storage Warehouse':                {'in':17,'resolved':18,'backlog':19,'fr_sla':20},
    'Storage':                          {'in':21,'resolved':22,'backlog':23,'fr_sla':24},
    'Storage Whs. Redeliveries':        {'in':25,'resolved':26,'backlog':27,'fr_sla':28},
    'Storage Complaints':               {'in':29,'resolved':30,'backlog':31,'fr_sla':32},
    'Shaun':    {'assigned':34,'resolved':35,'backlog':36,'fr_sla':37,'avg':38,'p90':39},
    'Emmanuel': {'assigned':40,'resolved':41,'backlog':42,'fr_sla':43,'avg':44,'p90':45},
    'Theo':     {'assigned':46,'resolved':47,'backlog':48,'fr_sla':49,'avg':50,'p90':51},
    'Shafwaan': {'assigned':52,'resolved':53,'backlog':54,'fr_sla':55,'avg':56,'p90':57},
    'quality': {
        'Shaun':    {'replied':59,'note_only':60,'nothing':61,'reply_rate':62,'fast':63},
        'Emmanuel': {'replied':64,'note_only':65,'nothing':66,'reply_rate':67,'fast':68},
        'Theo':     {'replied':69,'note_only':70,'nothing':71,'reply_rate':72,'fast':73},
        'Shafwaan': {'replied':74,'note_only':75,'nothing':76,'reply_rate':77,'fast':78},
    },
}

QUEUE_SHEET_KEY = {
    'Storage Support':                  'Storage Support',
    'Storage Warehouse':                'Storage Warehouse',
    'Storage':                          'Storage',
    'Storage Warehouse Redeliveries':   'Storage Whs. Redeliveries',
    'Storage Complaints':               'Storage Complaints',
}

# ── Date target ───────────────────────────────────────────────────────────────
if len(sys.argv) == 2:
    target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
else:
    target_date = date.today() - timedelta(days=1)

day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
day_end   = day_start + timedelta(days=1)
date_str  = target_date.strftime("%d/%m/%Y")

print(f"\nFreshdesk Daily KPI — {target_date}")

# ── API helpers ───────────────────────────────────────────────────────────────
def fd_get(path, params=None, retries=5):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=FD_HDR)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 60))
                print(f"  [rate limit] waiting {wait}s…", end="\r")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Failed: {path}")

def fd_search(query, retries=5):
    results = []
    page = 1
    while True:
        try:
            data = fd_get("/search/tickets", {"query": f'"{query}"', "page": page})
        except urllib.error.HTTPError as e:
            if e.code == 400:
                break
            raise
        batch = data.get("results", []) if isinstance(data, dict) else []
        results.extend(batch)
        if len(batch) < 30 or page >= 10:
            break
        page += 1
        time.sleep(0.8)
    return results

def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ") if s else None

def mins_to_hrs(m):
    return round(m / 60, 1) if m is not None else None

def avg(lst):
    return sum(lst) / len(lst) if lst else None

def pct(lst, p):
    if not lst:
        return None
    s = sorted(lst)
    return s[min(int(len(s) * p / 100), len(s) - 1)]

# ── 1. Fetch agents ───────────────────────────────────────────────────────────
print("Loading agents…")
all_agents = {}
page = 1
while True:
    batch = fd_get("/agents", {"per_page": 50, "page": page})
    if not batch:
        break
    for a in batch:
        all_agents[a["id"]] = a["contact"]["name"]
    if len(batch) < 50:
        break
    page += 1
    time.sleep(0.5)

agent_ids = {}
for aid, name in all_agents.items():
    for first in TRACKED_AGENTS:
        fragment = AGENT_NAME_FRAGMENTS[first]
        if name.startswith(fragment):
            agent_ids[first] = aid
            break

print(f"  Tracked agents: { {k: all_agents[v] for k,v in agent_ids.items()} }")
time.sleep(1)

# ── 2. Fetch yesterday's tickets ──────────────────────────────────────────────
print(f"Fetching tickets for {target_date}…")

since_str  = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
target_str = target_date.strftime("%Y-%m-%d")
after_str  = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

created_tickets  = []
resolved_tickets = []
seen_created  = set()
seen_resolved = set()

for gid, gname in STORAGE_GROUPS.items():
    q = f"group_id:{gid} AND created_at:>'{since_str}' AND created_at:<'{after_str}'"
    batch = fd_search(q)
    for t in batch:
        if t["id"] not in seen_created:
            seen_created.add(t["id"])
            created_tickets.append(t)
    time.sleep(0.8)

    two_weeks = (target_date - timedelta(days=14)).strftime("%Y-%m-%d")
    q2 = f"group_id:{gid} AND created_at:>'{two_weeks}' AND status:4"
    batch2 = fd_search(q2)
    for t in batch2:
        updated = parse_dt(t.get("updated_at"))
        if updated and day_start <= updated < day_end and t["id"] not in seen_resolved:
            seen_resolved.add(t["id"])
            resolved_tickets.append(t)
    time.sleep(0.8)

print(f"  {len(created_tickets)} tickets in, {len(resolved_tickets)} resolved.")
time.sleep(1)

# ── 3. Fetch backlog ──────────────────────────────────────────────────────────
print("Fetching backlog…")
backlog_tickets = []
seen_backlog = set()
for gid in STORAGE_GROUPS:
    for status_code in [1, 2]:
        batch = fd_search(f"group_id:{gid} AND status:{status_code}")
        for t in batch:
            if t["id"] not in seen_backlog:
                seen_backlog.add(t["id"])
                backlog_tickets.append(t)
        time.sleep(0.5)

print(f"  {len(backlog_tickets)} open/pending tickets.")

# ── 3b. Fetch conversations ───────────────────────────────────────────────────
print(f"Fetching conversations for {len(resolved_tickets)} resolved tickets…")
conv_cache = {}
for i, t in enumerate(resolved_tickets):
    tid = t["id"]
    try:
        convs = fd_get(f"/tickets/{tid}/conversations")
        outbound = [c for c in convs if not c.get("private") and not c.get("incoming")]
        notes    = [c for c in convs if c.get("private")]
        conv_cache[tid] = {
            "replied":   len(outbound) > 0,
            "note_only": len(outbound) == 0 and len(notes) > 0,
            "nothing":   len(convs) == 0,
        }
    except Exception:
        conv_cache[tid] = {"replied": False, "note_only": False, "nothing": True}
    if i > 0 and i % 10 == 0:
        print(f"  {i}/{len(resolved_tickets)} done…", end="\r")
    time.sleep(0.5)
print(f"  Conversations loaded for {len(conv_cache)} tickets.          ")

# ── 4. Compute metrics ────────────────────────────────────────────────────────
def get_queue_name(t):
    return STORAGE_GROUPS.get(t.get("group_id"), "Other")

def resolve_time_hrs(t):
    stats   = t.get("stats", {}) or {}
    created = parse_dt(t["created_at"])
    resolved = parse_dt(stats.get("resolved_at")) or parse_dt(stats.get("closed_at")) or parse_dt(t.get("updated_at"))
    if not created or not resolved:
        return None
    mins = (resolved - created).total_seconds() / 60
    return mins_to_hrs(mins) if mins >= 0 else None

def fr_sla_pct(tickets):
    met = sum(1 for t in tickets if t.get("fr_escalated") is False)
    brc = sum(1 for t in tickets if t.get("fr_escalated") is True)
    total = met + brc
    return round(met / total * 100, 1) if total else None

def res_sla_pct(tickets):
    met = sum(1 for t in tickets if t.get("is_escalated") is False)
    brc = sum(1 for t in tickets if t.get("is_escalated") is True)
    total = met + brc
    return round(met / total * 100, 1) if total else None

resolve_hrs = [h for t in resolved_tickets if (h := resolve_time_hrs(t)) is not None]

overall = {
    'in':        len(created_tickets),
    'resolved':  len(resolved_tickets),
    'backlog':   sum(1 for t in backlog_tickets if t.get("group_id") in STORAGE_GROUPS),
    'fr_sla':    fr_sla_pct(created_tickets),
    'res_sla':   res_sla_pct(resolved_tickets),
    'avg':       round(avg(resolve_hrs), 1) if resolve_hrs else None,
    'p90':       round(pct(resolve_hrs, 90), 1) if resolve_hrs else None,
}

queue_in       = defaultdict(list)
queue_resolved = defaultdict(list)
queue_backlog  = defaultdict(list)

for t in created_tickets:
    queue_in[get_queue_name(t)].append(t)
for t in resolved_tickets:
    queue_resolved[get_queue_name(t)].append(t)
for t in backlog_tickets:
    queue_backlog[get_queue_name(t)].append(t)

queue_metrics = {}
for qname in QUEUE_SHEET_KEY:
    inn  = queue_in.get(qname, [])
    res  = queue_resolved.get(qname, [])
    blg  = queue_backlog.get(qname, [])
    queue_metrics[qname] = {
        'in':       len(inn),
        'resolved': len(res),
        'backlog':  len(blg),
        'fr_sla':   fr_sla_pct(inn),
    }

agent_in       = defaultdict(list)
agent_resolved = defaultdict(list)
agent_backlog  = defaultdict(list)

for t in created_tickets:
    aid = t.get("responder_id")
    for first, fid in agent_ids.items():
        if aid == fid:
            agent_in[first].append(t)

for t in resolved_tickets:
    aid = t.get("responder_id")
    for first, fid in agent_ids.items():
        if aid == fid:
            agent_resolved[first].append(t)

for t in backlog_tickets:
    aid = t.get("responder_id")
    for first, fid in agent_ids.items():
        if aid == fid:
            agent_backlog[first].append(t)

agent_metrics = {}
for first in TRACKED_AGENTS:
    inn  = agent_in.get(first, [])
    res  = agent_resolved.get(first, [])
    blg  = agent_backlog.get(first, [])
    hrs  = [h for t in res if (h := resolve_time_hrs(t)) is not None]
    agent_metrics[first] = {
        'assigned': len(inn),
        'resolved': len(res),
        'backlog':  len(blg),
        'fr_sla':   fr_sla_pct(inn),
        'avg':      round(avg(hrs), 1) if hrs else None,
        'p90':      round(pct(hrs, 90), 1) if hrs else None,
    }

quality_metrics = {}
for first in TRACKED_AGENTS:
    fid  = agent_ids.get(first)
    mine = [t for t in resolved_tickets if t.get("responder_id") == fid] if fid else []
    replied = note_only = nothing = fast = 0
    for t in mine:
        c       = conv_cache.get(t["id"], {"replied": False, "note_only": False, "nothing": True})
        created = parse_dt(t["created_at"])
        updated = parse_dt(t.get("updated_at"))
        if c["replied"]:
            replied += 1
        elif c["note_only"]:
            note_only += 1
        else:
            nothing += 1
        if created and updated and (updated - created).total_seconds() < 1800:
            fast += 1
    total    = len(mine)
    reply_rt = round(replied / total * 100, 1) if total else None
    quality_metrics[first] = {
        'replied':    replied,
        'note_only':  note_only,
        'nothing':    nothing,
        'reply_rate': reply_rt,
        'fast':       fast,
        'total':      total,
    }

# ── Print summary ─────────────────────────────────────────────────────────────
print(f"\n  {'Metric':<30} {'Value':>8}")
print(f"  {'─'*30} {'─'*8}")
print(f"  {'Tickets In':<30} {overall['in']:>8}")
print(f"  {'Tickets Resolved':<30} {overall['resolved']:>8}")
print(f"  {'Backlog':<30} {overall['backlog']:>8}")
print(f"  {'FR SLA %':<30} {str(overall['fr_sla'])+'%' if overall['fr_sla'] else '—':>8}")
print(f"  {'Res SLA %':<30} {str(overall['res_sla'])+'%' if overall['res_sla'] else '—':>8}")
print(f"  {'Avg Resolve (hrs)':<30} {str(overall['avg']) if overall['avg'] else '—':>8}")
print(f"  {'p90 Resolve (hrs)':<30} {str(overall['p90']) if overall['p90'] else '—':>8}")

print(f"\n  By Queue:")
for q, m in queue_metrics.items():
    print(f"    {q:<35}  In:{m['in']:>3}  Res:{m['resolved']:>3}  Blg:{m['backlog']:>3}  FR:{str(m['fr_sla'])+'%' if m['fr_sla'] else '—':>6}")

print(f"\n  By Agent:")
for a, m in agent_metrics.items():
    print(f"    {a:<12}  Assgn:{m['assigned']:>3}  Res:{m['resolved']:>3}  Blg:{m['backlog']:>3}  FR:{str(m['fr_sla'])+'%' if m['fr_sla'] else '—':>6}  Avg:{str(m['avg'])+'h' if m['avg'] else '—':>6}  p90:{str(m['p90'])+'h' if m['p90'] else '—':>6}")

print(f"\n  Response Quality (of tickets resolved yesterday):")
print(f"  {'Agent':<12} {'Closed':>6} {'Replied':>8} {'Note only':>10} {'Nothing':>8} {'Reply%':>7} {'Fast<30m':>9}")
print(f"  {'─'*12} {'─'*6} {'─'*8} {'─'*10} {'─'*8} {'─'*7} {'─'*9}")
for a, q in quality_metrics.items():
    rr = f"{q['reply_rate']}%" if q['reply_rate'] is not None else '—'
    print(f"  {a:<12} {q['total']:>6} {q['replied']:>8} {q['note_only']:>10} {q['nothing']:>8} {rr:>7} {q['fast']:>9}")

# ── 5. Write JSON ─────────────────────────────────────────────────────────────
import os as _os

_AGENT_DISPLAY = {
    'Shaun':    'Shaun Gae',
    'Emmanuel': 'Emmanuel Nsenga',
    'Theo':     'Theo J',
    'Shafwaan': 'Shafwaan Titus',
}

_QUEUE_DISPLAY = {
    'Storage Support':               'Storage Support',
    'Storage Warehouse':             'Storage Warehouse',
    'Storage':                       'Storage',
    'Storage Warehouse Redeliveries':'Whs. Redeliveries',
    'Storage Complaints':            'Storage Complaints',
}

JSON_PATH = os.environ.get('OUTPUT_FILE') or _os.path.expanduser('~/Documents/anyvan-kpi/data/freshdesk_data.json')
_fd_store = {}
if _os.path.exists(JSON_PATH):
    with open(JSON_PATH) as _ff:
        _fd_store = json.load(_ff)

_date_key = target_date.strftime('%Y-%m-%d')

_queues_list = []
for _qname in ['Storage Support', 'Storage Warehouse', 'Storage',
               'Storage Warehouse Redeliveries', 'Storage Complaints']:
    _qm = queue_metrics.get(_qname, {'in': 0, 'resolved': 0, 'backlog': 0, 'fr_sla': None})
    _queues_list.append({
        'in':  _qm['in'],
        'res': _qm['resolved'],
        'blg': _qm['backlog'],
        'fr':  _qm['fr_sla'] if _qm['fr_sla'] is not None else 0,
    })

_agents_list = []
for _first in TRACKED_AGENTS:
    _am  = agent_metrics.get(_first, {'assigned': 0, 'resolved': 0, 'backlog': 0, 'fr_sla': None, 'avg': None, 'p90': None})
    _qm2 = quality_metrics.get(_first, {'replied': 0, 'note_only': 0, 'nothing': 0, 'reply_rate': None, 'fast': 0})
    _rr  = (str(round(_qm2['reply_rate'])) + '%') if _qm2['reply_rate'] is not None else '—'
    _agents_list.append({
        'assigned':  _am['assigned'],
        'resolved':  _am['resolved'],
        'backlog':   _am['backlog'],
        'fr':        _am['fr_sla'] if _am['fr_sla'] is not None else 0,
        'avg':       (str(_am['avg']) + 'h') if _am['avg'] is not None else '—',
        'p90':       (str(_am['p90']) + 'h') if _am['p90'] is not None else '—',
        'replied':   _qm2['replied'],
        'note_only': _qm2['note_only'],
        'nothing':   _qm2['nothing'],
        'reply_rate':_rr,
        'fast':      _qm2['fast'],
    })

_fd_store[_date_key] = {
    'inn':      overall['in'],
    'resolved': overall['resolved'],
    'backlog':  overall['backlog'],
    'fr_sla':   overall['fr_sla'] if overall['fr_sla'] is not None else 0,
    'res_sla':  overall['res_sla'] if overall['res_sla'] is not None else 0,
    'avg_r':    (str(overall['avg']) + 'h') if overall['avg'] is not None else '—',
    'p90':      (str(overall['p90']) + 'h') if overall['p90'] is not None else '—',
    'queues':   _queues_list,
    'agents':   _agents_list,
}

with open(JSON_PATH, 'w') as _ff:
    json.dump(_fd_store, _ff, indent=2)
print(f"JSON|{JSON_PATH}")
print(f"DATE|{date_str}")
