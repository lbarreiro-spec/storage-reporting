#!/usr/bin/env python3
"""
LIVE in-month commission CALCULATOR for the Storage support/ops team.

Pulls current month-to-date data from Snowflake, computes each agent's projected
bonus on the live engine, and writes a SELF-CONTAINED interactive HTML where QA
issues (minor/moderate/serious) can be toggled per agent -- the balance line
recalculates live in the browser.

Usage:
  python3 commission_calculator.py            # current month, MTD (refresh = re-run)
  python3 commission_calculator.py 2026-05    # any specific month (full if past)

Output: ~/Downloads/Storage Commission CALCULATOR.html
"""
import os, sys, json, datetime as dt
import snowflake.connector

ROOT = os.path.expanduser("~/Documents/storage-reporting")
OUT  = os.path.expanduser("~/Downloads/V4 Storage Commission - CALCULATOR.html")
ON_TARGET = 5500.0

# ---- target month range (MTD) ----
today = dt.date.today()
if len(sys.argv) > 1:
    y, m = map(int, sys.argv[1].split("-"))
else:
    y, m = today.year, today.month
start = dt.date(y, m, 1)
nxt   = dt.date(y + (m == 12), (m % 12) + 1, 1)
end   = min(nxt, today + dt.timedelta(days=1))          # MTD if current month
MONTH_LABEL = start.strftime("%B %Y")
ASOF = today.strftime("%-d %b %Y")
DAYS_IN = (min(end, today) - start).days + (1 if end > today else 0)
S, E = start.isoformat(), end.isoformat()

# ---- creds ----
env = {}
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"')
TOKEN = open(os.path.expanduser("~/.snowflake/connections.toml")).read().split('token = "')[1].split('"')[0]

AF = "(a.WORKERFULLNAME IN ('Shafwaan Titus','Emmanuel Nsenga','Sage') OR a.WORKERFULLNAME LIKE 'Theo J%')"
def disp(n): return "Theo" if n.startswith("Theo") else n.split()[0]

con = snowflake.connector.connect(account=env["SNOWFLAKE_ACCOUNT"], user=env["SNOWFLAKE_USER"],
        authenticator="programmatic_access_token", token=TOKEN,
        warehouse="MART_SALES_OPS_WH", role="MART_SALES_OPS_GROUP")
cur = con.cursor()
def q(sql): cur.execute(sql); return cur.fetchall()

D = {}
def slot(ag): return D.setdefault(disp(ag), dict(ib_dialled=0,ib_answered=0,wa=0.0,shift=0,active=0,csat_n=0,csat_sum=0.0,res_n=0,res_hit=0))

for ag,dl,an in q(f"""SELECT a.WORKERFULLNAME,SUM(a.DIALLED),SUM(a.ANSWERED)
    FROM MART_SALES_OPS.PRODUCTION.FACT_VOICE_ACTIVITY a
    WHERE a.VOICETYPE='Inbound' AND a.DATE>='{S}' AND a.DATE<'{E}' AND {AF} GROUP BY 1"""):
    d=slot(ag); d["ib_dialled"]+=int(dl or 0); d["ib_answered"]+=int(an or 0)

for ag,wt in q(f"""WITH pt AS (SELECT a.WORKERFULLNAME w,a.TASKID t,MIN(a.FIRSTRESPONSETIME) frt
        FROM MART_SALES_OPS.PRODUCTION.FACT_WHATSAPP_ACTIVITY a
        WHERE a.DATE>='{S}' AND a.DATE<'{E}'
          AND COALESCE(a.TYPE,'') NOT ILIKE '%outbound%' AND {AF} GROUP BY 1,2)
    SELECT w,SUM(CASE WHEN frt IS NULL THEN 0 WHEN frt<=150 THEN 1.0 WHEN frt<=250 THEN 0.9 WHEN frt<=350 THEN 0.8
        WHEN frt<=450 THEN 0.65 WHEN frt<=600 THEN 0.5 WHEN frt<=900 THEN 0.3 ELSE 0 END) FROM pt GROUP BY 1"""):
    slot(ag)["wa"]+=float(wt or 0)

for ag,active,total in q(f"""SELECT a.WORKERFULLNAME,
        SUM(a.AVAILABLE+a.ADMIN+a.OB_ACTIVITY+a.TICKETING+a.LIVE_CHAT),
        SUM(a.AVAILABLE+a.ADMIN+a.OB_ACTIVITY+a.TICKETING+a.LIVE_CHAT+a.BREAK+a.LUNCH+a.OFFLINE+a.PERSONAL+a.SYSTEM_ISSUE)
    FROM MART_SALES_OPS.PRODUCTION.FACT_AGENT_ACTIVITY a
    WHERE a.DATE>='{S}' AND a.DATE<'{E}' AND {AF} GROUP BY 1"""):
    d=slot(ag); d["active"]+=int(active or 0); d["shift"]+=int(total or 0)

for ag,_i,resp,avg,res_n,res_hit in q(f"""WITH sc AS (
        SELECT TRIM(CALLSID) CALLSID,TYPE,CASE WHEN REGEXP_LIKE(TRIM(CALLTAGS),'^[0-9]+') THEN TRY_CAST(REGEXP_SUBSTR(TRIM(CALLTAGS),'^[0-9]+') AS NUMBER)
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'one%' THEN 1 WHEN LOWER(TRIM(CALLTAGS)) LIKE 'two%' THEN 2 WHEN LOWER(TRIM(CALLTAGS)) LIKE 'three%' THEN 3
            WHEN LOWER(TRIM(CALLTAGS)) LIKE 'four%' THEN 4 WHEN LOWER(TRIM(CALLTAGS)) LIKE 'five%' THEN 5 ELSE NULL END SCORE
        FROM HARMONISED.PRODUCTION.TWILIO_EVENTS WHERE TYPE LIKE '%CSAT%' AND EVENTTYPE='task.created' AND EVENTTIMESTAMP>='{S}' AND EVENTTIMESTAMP<'{E}'),
        pc AS (SELECT CALLSID,MAX(CASE WHEN TYPE='CSAT_Interaction' AND SCORE BETWEEN 1 AND 5 THEN SCORE END) CI,
            MAX(CASE WHEN TYPE='CSAT_Resolution' AND SCORE BETWEEN 1 AND 5 THEN SCORE END) CR FROM sc GROUP BY CALLSID)
        SELECT a.WORKERFULLNAME,COUNT(*),COUNT(c.CI),ROUND(AVG(c.CI),3),COUNT(c.CR),SUM(CASE WHEN c.CR>=4 THEN 1 ELSE 0 END)
        FROM HARMONISED.PRODUCTION.TWILIO_EVENTS a LEFT JOIN pc c ON a.CUSTOMERCALLSID=c.CALLSID
        WHERE a.EVENTTYPE='task.completed' AND a.EVENTTIMESTAMP>='{S}' AND a.EVENTTIMESTAMP<'{E}' AND {AF} GROUP BY 1"""):
    d=slot(ag); d["csat_n"]+=int(resp or 0); d["csat_sum"]+=float(avg or 0)*int(resp or 0); d["res_n"]+=int(res_n or 0); d["res_hit"]+=int(res_hit or 0)

cur.close(); con.close()

agents=[]
for name in sorted(D):
    d=D[name]
    agents.append(dict(name=name,
        ar=(d["ib_answered"]/d["ib_dialled"]*100) if d["ib_dialled"] else None,
        ib_dialled=d["ib_dialled"], ib_answered=d["ib_answered"],
        wcph=((d["wa"]+d["ib_answered"])/(d["shift"]/3600.0)) if d["shift"] else 0.0,
        avail=(d["active"]/d["shift"]*100) if d["shift"] else 0.0,
        csat=(d["csat_sum"]/d["csat_n"]) if d["csat_n"] else None, csat_n=d["csat_n"],
        res=(d["res_hit"]/d["res_n"]*100) if d["res_n"] else None, res_n=d["res_n"]))

AJSON = json.dumps(agents)
print(f"{MONTH_LABEL} MTD ({DAYS_IN} days) — {len(agents)} agents")
for a in agents:
    print(f"  {a['name']:10} ans={a['ar'] and round(a['ar'],1)} wcph={a['wcph']:.2f} avail={a['avail']:.1f} csat={a['csat']} (n={a['csat_n']})")

TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Storage Commission Calculator — __MONTH__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f2;color:#1a1a1a;font-size:15px}
.page{max-width:1040px;margin:0 auto;padding:28px 20px 60px}
.header{background:#14322a;color:#fff;border-radius:12px;padding:26px 30px;margin-bottom:14px}
.header-tag{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#8fd4b4;margin-bottom:7px}
.header h1{font-size:23px;font-weight:700;margin-bottom:8px}
.header p{color:#b8e8d0;font-size:13px;line-height:1.6;max-width:740px}
.banner{background:#5a1010;color:#fff;border-radius:8px;padding:9px 15px;margin-bottom:12px;font-size:12px;font-weight:600}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.scard{background:#fff;border:1px solid #e4e4e0;border-radius:10px;padding:15px 16px}
.scard .v{font-size:25px;font-weight:800;color:#14322a;line-height:1}
.scard .l{font-size:11.5px;color:#666;margin-top:5px}
.scard.blue .v{color:#1a5c8a}.scard.green .v{color:#1a6b40}.scard.amber .v{color:#8b6000}
.agent{background:#fff;border:1px solid #e4e4e0;border-radius:12px;margin-bottom:14px;overflow:hidden}
.ahead{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;background:#f7f9f8;border-bottom:1px solid #eee}
.ahead .nm{font-size:17px;font-weight:700;color:#14322a}
.ahead .fin{text-align:right}
.ahead .fin .amt{font-size:26px;font-weight:800}
.ahead .fin .lbl{font-size:10.5px;color:#888;text-transform:uppercase;letter-spacing:.04em}
.abody{display:grid;grid-template-columns:1.35fr 1fr;gap:0}
.metrics{padding:16px 20px;border-right:1px solid #f0f0ec}
.qa{padding:16px 20px;background:#fcfcfb}
.mrow{display:flex;justify-content:space-between;font-size:13px;padding:5px 0;border-bottom:1px solid #f4f4f2}
.mrow:last-child{border-bottom:none}
.mrow .ml{color:#555}.mrow .mv{font-weight:700}
.mrow .sub{font-size:11px;color:#aaa;font-weight:400}
.sec-t{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#888;font-weight:700;margin-bottom:8px}
.qrow{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}
.qrow .qlabel{font-size:12.5px}.qrow .qlabel small{color:#999;display:block;font-size:10.5px}
.stepper{display:flex;align-items:center;gap:0}
.stepper button{width:28px;height:28px;border:1px solid #d8d8d2;background:#fff;font-size:16px;font-weight:700;cursor:pointer;color:#14322a;line-height:1}
.stepper button:hover{background:#f0f6f3}
.stepper button:first-child{border-radius:6px 0 0 6px}.stepper button:last-child{border-radius:0 6px 6px 0}
.stepper .ct{width:34px;height:28px;border-top:1px solid #d8d8d2;border-bottom:1px solid #d8d8d2;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
.balance{margin-top:12px;padding-top:12px;border-top:2px dashed #e0e0da;font-size:13px}
.balance .br{display:flex;justify-content:space-between;padding:3px 0}
.balance .br .v{font-weight:700}
.balance .br.tot{font-size:15px;border-top:1px solid #eee;margin-top:4px;padding-top:7px}
.balance .br.tot .v{color:#14322a;font-weight:800}
.qa-pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:700}
.note{font-size:11.5px;color:#888;margin-top:4px}
.card{background:#fff;border:1px solid #e4e4e0;border-radius:10px;padding:16px 20px;margin-top:14px;font-size:12.5px;color:#555;line-height:1.7}
.foot{text-align:center;font-size:11px;color:#aaa;margin-top:24px}
.reset{font-size:11px;color:#1a5c8a;cursor:pointer;text-decoration:underline;margin-left:8px}
</style></head><body><div class="page">
<div class="banner">🔒 INTERNAL — LIVE CALCULATOR. Month-to-date projection on the live engine. QA is set manually below. Not a payslip.</div>
<div class="header">
  <div class="header-tag">AnyVan Storage Operations · Commission Calculator · Live MTD</div>
  <h1>Where the team is tracking — __MONTH__</h1>
  <p>Month-to-date performance pulled from source, scored on the live engine (R5,500 on-target, R10,000 ceiling). Set each agent's QA below — minor / moderate / serious issues deduct from a clean 10/10 — and the balance line updates instantly. Re-run the script to refresh the data. <strong>As of __ASOF__ (__DAYS__ days into the month).</strong></p>
</div>
<div class="summary">
  <div class="scard blue"><div class="v" id="s-avg">—</div><div class="l">Avg projected bonus<br>(target ~R5,500)</div></div>
  <div class="scard green"><div class="v" id="s-tot">—</div><div class="l">Team total projected</div></div>
  <div class="scard amber"><div class="v" id="s-max">—</div><div class="l">Highest agent</div></div>
  <div class="scard"><div class="v" id="s-n">—</div><div class="l">Agents tracked</div></div>
</div>
<div id="agents"></div>
<div class="card">
  <strong>How to read this:</strong> each agent's <em>base</em> bonus comes from live MTD metrics (answer rate, WCPH, availability, CSAT). The <em>QA steppers</em> start clean at 10/10 — add the issues found in your reviews (minor −0.5, moderate −1.0, serious −2.0) and the QA modifier + final balance recalculate live. Figures are projections at current MTD pace, not final payouts. CSAT shows response counts (n=); where it's missing, that agent's quality runs on whatever is available and the score leans on productivity.
</div>
<div class="foot">AnyVan Storage Operations · Live Commission Calculator · __MONTH__ MTD · as of __ASOF__ · internal modelling</div>
</div>
<script>
const AGENTS = __AGENTS__;
const ON_TARGET = 5500;
function csatMult(c){if(c==null)return null;const b=[[4.7,1.2],[4.4,1.1],[4.0,0.8],[3.6,0.7],[3.2,0.65],[2.8,0.5],[2.4,0.4]];for(const[t,m]of b)if(c>=t)return m;return 0;}
function resMult(p){if(p==null)return null;const b=[[90,1.2],[85,1.1],[80,0.8],[75,0.7],[70,0.65],[60,0.5],[50,0.4]];for(const[t,m]of b)if(p>=t)return m;return 0;}
// Productivity = WCPH x Answer-Rate matrix, OR availability, whichever higher (each x50% weight).
const WCPH_ROWS=[3.50,3.00,2.50,2.00,1.50,1.00];
const AR_COLS=[90,87.5,85,82.5,80,77.5,75];
const MATRIX={3.50:[140,130,120,105,95,75,55],3.00:[130,120,105,95,85,75,65],2.50:[120,105,95,85,75,65,55],2.00:[105,95,85,75,65,55,0],1.50:[95,85,75,65,55,0,0],1.00:[85,75,65,55,0,0,0]};
function matrixMult(wcph,arp){if(arp==null)return 0;const row=WCPH_ROWS.find(w=>wcph>=w);if(row==null)return 0;const ci=AR_COLS.findIndex(c=>arp>=c);if(ci<0)return 0;return MATRIX[row][ci]/100;}
// Availability is a quiet-day safety net: lifts to PAR (1.00x) at best, never above -- being logged in can't top-score.
function availMult(p){const b=[[80,1.00],[77.5,0.90],[75,0.70],[72.5,0.65],[70,0.60]];for(const[t,m]of b)if(p>=t)return m;return 0;}
// Penalty-led QA: clean (100%) = neutral 0%; each 10% drop = -5%; floor -20%.
function qaModifier(pct){return Math.round(Math.max(-20, -(100-pct)/2)*10)/10;}
const MIN_CSAT=10;   // CSAT sub-metric only counts with >= this many responses
function score(a){
  const cm=(a.csat_n>=MIN_CSAT)?csatMult(a.csat):null;
  const rm=(a.res_n>=MIN_CSAT)?resMult(a.res):null;
  let qp=0,qw=0; if(cm!=null){qp+=cm*30;qw+=30;} if(rm!=null){qp+=rm*20;qw+=20;}
  const mxm=matrixMult(a.wcph,a.ar), avm=availMult(a.avail);
  const prod=Math.max(mxm,avm), psrc=mxm>=avm?'WCPH × answer-rate matrix':'availability';
  let sp;
  if(qw>0){ qp=qp*(50/qw); sp=qp+prod*50; }     // quality 50% + productivity 50%
  else    { sp=prod*100; qp=0; }                // no valid CSAT -> productivity fills 100%
  return {scorePct:sp, qPts:qp, base:ON_TARGET*sp/100, psrc, cm, rm, mxm, avm, prod, qOK:qw>0};
}
const ISSUES={}; // name -> {minor,mod,serious}
function fmt(n){return 'R'+Math.round(n).toLocaleString();}
function pct(v,dp=1){return v==null?'<span style="color:#bbb">no data</span>':v.toFixed(dp)+'%';}

function render(){
  const wrap=document.getElementById('agents'); wrap.innerHTML='';
  let tot=0,mx=0;
  AGENTS.forEach((a,i)=>{
    const s=score(a); const iss=ISSUES[a.name]||(ISSUES[a.name]={minor:0,mod:0,serious:0});
    let pts=10-(0.5*iss.minor+1*iss.mod+2*iss.serious); if(pts<0)pts=0;
    const qaPct=pts*10, mod=qaModifier(qaPct), fin=s.base*(1+mod/100);
    tot+=fin; if(fin>mx)mx=fin;
    const modCol = mod>0?'#1a6b40':mod<0?'#c0392b':'#666';
    const pillBg = mod>0?'#e8f9ee':mod<0?'#fdecec':'#f0f0ec';
    wrap.insertAdjacentHTML('beforeend',`
    <div class="agent">
      <div class="ahead">
        <div class="nm">${a.name}</div>
        <div class="fin"><div class="amt" style="color:${fin<3500?'#aa1a1a':fin<6500?'#1a6b40':'#2e8a54'}">${fmt(fin)}</div><div class="lbl">projected this month</div></div>
      </div>
      <div class="abody">
        <div class="metrics">
          <div class="sec-t">Live MTD metrics</div>
          <div class="mrow"><span class="ml">Answer rate</span><span class="mv">${pct(a.ar)} <span class="sub">${a.ib_answered}/${a.ib_dialled}</span></span></div>
          <div class="mrow"><span class="ml">WCPH</span><span class="mv">${a.wcph.toFixed(2)}</span></div>
          <div class="mrow"><span class="ml">Availability</span><span class="mv">${a.avail.toFixed(1)}%</span></div>
          <div class="mrow"><span class="ml">CSAT</span><span class="mv">${a.csat==null?'<span style="color:#bbb">no data</span>':a.csat.toFixed(2)} <span class="sub">n=${a.csat_n}</span></span></div>
          <div class="mrow"><span class="ml">CSAT resolved %</span><span class="mv">${a.res==null?'<span style="color:#bbb">no data</span>':a.res.toFixed(0)+'%'} <span class="sub">n=${a.res_n}</span></span></div>
          <div class="mrow"><span class="ml">Productivity score <span class="sub">50% pillar</span></span><span class="mv">${(s.prod*50).toFixed(1)} pts <span class="sub">${s.prod.toFixed(2)}× × 50</span></span></div>
          <div class="mrow"><span class="ml">&nbsp;&nbsp;↳ scored via</span><span class="mv" style="font-size:11.5px;color:#666">${s.psrc} (matrix ${s.mxm.toFixed(2)}× vs availability ${s.avm.toFixed(2)}×)</span></div>
          <div class="mrow"><span class="ml">Quality score <span class="sub">50% pillar · CSAT</span></span><span class="mv">${s.qOK?s.qPts.toFixed(1)+' pts':'<span style="color:#bbb">set aside (n&lt;10)</span>'}</span></div>
          <div class="mrow"><span class="ml">Base score</span><span class="mv" style="color:#14322a">${s.scorePct.toFixed(0)}%</span></div>
        </div>
        <div class="qa">
          <div class="sec-t">QA — issues logged this month</div>
          ${stepper(i,'minor','Minor','−0.5 · tone, small slip',iss.minor)}
          ${stepper(i,'mod','Moderate','−1.0 · wrong info, missed step',iss.mod)}
          ${stepper(i,'serious','Serious','−2.0 · wrong resolution, compliance',iss.serious)}
          <div class="balance">
            <div class="br"><span>Base bonus</span><span class="v">${fmt(s.base)}</span></div>
            <div class="br"><span>QA score</span><span class="v"><span class="qa-pill" style="background:${pillBg};color:${modCol}">${pts.toFixed(1)}/10 · ${qaPct.toFixed(0)}%</span></span></div>
            <div class="br"><span>QA modifier</span><span class="v" style="color:${modCol}">${mod>0?'+':''}${mod}%</span></div>
            <div class="br tot"><span>Final projected${(iss.minor+iss.mod+iss.serious)>0?' <span class="reset" onclick="clearI('+i+')">reset QA</span>':''}</span><span class="v">${fmt(fin)}</span></div>
          </div>
        </div>
      </div>
    </div>`);
  });
  document.getElementById('s-avg').textContent=fmt(tot/AGENTS.length);
  document.getElementById('s-tot').textContent=fmt(tot);
  document.getElementById('s-max').textContent=fmt(mx);
  document.getElementById('s-n').textContent=AGENTS.length;
}
function stepper(i,key,label,sub,val){
  return `<div class="qrow"><div class="qlabel">${label}<small>${sub}</small></div>
    <div class="stepper"><button onclick="bump(${i},'${key}',-1)">−</button><div class="ct">${val}</div><button onclick="bump(${i},'${key}',1)">+</button></div></div>`;
}
function bump(i,key,d){const a=AGENTS[i];const o=ISSUES[a.name];o[key]=Math.max(0,o[key]+d);render();}
function clearI(i){const a=AGENTS[i];ISSUES[a.name]={minor:0,mod:0,serious:0};render();}
render();
</script></body></html>"""

html = (TEMPLATE.replace("__MONTH__", MONTH_LABEL).replace("__ASOF__", ASOF)
        .replace("__DAYS__", str(DAYS_IN)).replace("__AGENTS__", AJSON))
open(OUT, "w").write(html)
print("Wrote", OUT)
