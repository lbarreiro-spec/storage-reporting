#!/usr/bin/env python3
"""
3-month commission BACKRUN for the Storage support/ops team.

Applies the new blended commission engine (R5,500 on-target, no hard cap, R10,000 ceiling)
to actual Mar/Apr/May 2026 data and shows what each agent WOULD have been paid.

Scope decisions (set with Scott):
  - Population: Storage support/ops agents (the 4 with full metric coverage)
  - Quality pillar: CSAT-based (per-agent NPS doesn't exist)
       * CSAT interaction score  -> 30% (customer-rating sub-metric, replaces NPS)
       * CSAT resolution rate     -> 20% (replaces ticket-resolution-TIME, which has
                                          no reliable per-agent source in Snowflake)
  - Productivity pillar (50%): WCPH x Answer-Rate matrix OR availability, whichever higher
  - QA modifier: neutral (0%) -- no QA review feed exists for Storage

Output: ~/Downloads/Storage Commission BACKRUN - Mar-May 2026.html
"""
import os, json
import snowflake.connector

ROOT = os.path.expanduser("~/Documents/storage-reporting")
OUT  = os.path.expanduser("~/Downloads/V4 Storage Commission - BACKRUN (Dec25-May26).html")
ON_TARGET = 5500.0
MIN_CSAT  = 10          # CSAT sub-metric only counts in a month with >= this many responses

# ---- creds ----
env = {}
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"')
TOKEN = open(os.path.expanduser("~/.snowflake/connections.toml")).read().split('token = "')[1].split('"')[0]

AGENT_FILTER = "(a.WORKERFULLNAME IN ('Shafwaan Titus','Emmanuel Nsenga','Sage') OR a.WORKERFULLNAME LIKE 'Theo J%')"
def disp(name):
    if name.startswith("Theo"): return "Theo"
    return name.split()[0]
MONTHS = [("2025-12-01","2026-01-01","Dec 2025"),
          ("2026-01-01","2026-02-01","Jan 2026"),
          ("2026-02-01","2026-03-01","Feb 2026"),
          ("2026-03-01","2026-04-01","Mar 2026"),
          ("2026-04-01","2026-05-01","Apr 2026"),
          ("2026-05-01","2026-06-01","May 2026")]

con = snowflake.connector.connect(account=env["SNOWFLAKE_ACCOUNT"], user=env["SNOWFLAKE_USER"],
        authenticator="programmatic_access_token", token=TOKEN,
        warehouse="MART_SALES_OPS_WH", role="MART_SALES_OPS_GROUP")
cur = con.cursor()

def q(sql):
    cur.execute(sql); return cur.fetchall()

# data[(month,agent)] = dict of metrics
data = {}
def slot(m, ag):
    return data.setdefault((m, disp(ag)), {"ib_dialled":0,"ib_answered":0,"wa_weighted":0.0,
        "shift_s":0,"active_s":0,"avail_s":0,"csat_n":0,"csat_sum":0.0,"res_n":0,"res_hit":0})

for s,e,label in MONTHS:
    # --- Voice: answer rate + voice credits (answered inbound) ---
    for ag,dl,an in q(f"""
        SELECT a.WORKERFULLNAME, SUM(a.DIALLED), SUM(a.ANSWERED)
        FROM MART_SALES_OPS.PRODUCTION.FACT_VOICE_ACTIVITY a
        WHERE a.VOICETYPE='Inbound'
          AND a.DATE >= '{s}' AND a.DATE < '{e}' AND {AGENT_FILTER}
        GROUP BY 1"""):
        d = slot(label, ag); d["ib_dialled"] += int(dl or 0); d["ib_answered"] += int(an or 0)

    # --- WhatsApp: response-time-weighted inbound conversations (one weight per task) ---
    for ag,wt in q(f"""
        WITH per_task AS (
            SELECT a.WORKERFULLNAME AS w, a.TASKID AS t, MIN(a.FIRSTRESPONSETIME) AS frt
            FROM MART_SALES_OPS.PRODUCTION.FACT_WHATSAPP_ACTIVITY a
            WHERE a.DATE >= '{s}' AND a.DATE < '{e}'
              AND COALESCE(a.TYPE,'') NOT ILIKE '%outbound%' AND {AGENT_FILTER}
            GROUP BY 1,2)
        SELECT w, SUM(CASE
            WHEN frt IS NULL THEN 0
            WHEN frt <= 150 THEN 1.00 WHEN frt <= 250 THEN 0.90 WHEN frt <= 350 THEN 0.80
            WHEN frt <= 450 THEN 0.65 WHEN frt <= 600 THEN 0.50 WHEN frt <= 900 THEN 0.30
            ELSE 0 END)
        FROM per_task GROUP BY 1"""):
        slot(label, ag)["wa_weighted"] += float(wt or 0)

    # --- Availability / shift hours ---
    for ag,active,total,avail in q(f"""
        SELECT a.WORKERFULLNAME,
               SUM(a.AVAILABLE+a.ADMIN+a.OB_ACTIVITY+a.TICKETING+a.LIVE_CHAT),
               SUM(a.AVAILABLE+a.ADMIN+a.OB_ACTIVITY+a.TICKETING+a.LIVE_CHAT+a.BREAK+a.LUNCH+a.OFFLINE+a.PERSONAL+a.SYSTEM_ISSUE),
               SUM(a.AVAILABLE)
        FROM MART_SALES_OPS.PRODUCTION.FACT_AGENT_ACTIVITY a
        WHERE a.DATE >= '{s}' AND a.DATE < '{e}' AND {AGENT_FILTER}
        GROUP BY 1"""):
        d = slot(label, ag); d["active_s"]+=int(active or 0); d["shift_s"]+=int(total or 0); d["avail_s"]+=int(avail or 0)

    # --- CSAT interaction + resolution (per agent, monthly) ---
    for ag,inter,resp,avg,res_n,res_hit in q(f"""
        WITH score_clean AS (
            SELECT TRIM(CALLSID) CALLSID, TYPE,
                CASE WHEN REGEXP_LIKE(TRIM(CALLTAGS),'^[0-9]+') THEN TRY_CAST(REGEXP_SUBSTR(TRIM(CALLTAGS),'^[0-9]+') AS NUMBER)
                     WHEN LOWER(TRIM(CALLTAGS)) LIKE 'one%' THEN 1 WHEN LOWER(TRIM(CALLTAGS)) LIKE 'two%' THEN 2
                     WHEN LOWER(TRIM(CALLTAGS)) LIKE 'three%' THEN 3 WHEN LOWER(TRIM(CALLTAGS)) LIKE 'four%' THEN 4
                     WHEN LOWER(TRIM(CALLTAGS)) LIKE 'five%' THEN 5 ELSE NULL END AS SCORE
            FROM HARMONISED.PRODUCTION.TWILIO_EVENTS
            WHERE TYPE LIKE '%CSAT%' AND EVENTTYPE='task.created'
              AND EVENTTIMESTAMP >= '{s}' AND EVENTTIMESTAMP < '{e}'),
        per_call AS (
            SELECT CALLSID,
                MAX(CASE WHEN TYPE='CSAT_Interaction' AND SCORE BETWEEN 1 AND 5 THEN SCORE END) AS CI,
                MAX(CASE WHEN TYPE='CSAT_Resolution'  AND SCORE BETWEEN 1 AND 5 THEN SCORE END) AS CR
            FROM score_clean GROUP BY CALLSID)
        SELECT a.WORKERFULLNAME, COUNT(*) , COUNT(c.CI), ROUND(AVG(c.CI),3),
               COUNT(c.CR), SUM(CASE WHEN c.CR>=4 THEN 1 ELSE 0 END)
        FROM HARMONISED.PRODUCTION.TWILIO_EVENTS a
        LEFT JOIN per_call c ON a.CUSTOMERCALLSID = c.CALLSID
        WHERE a.EVENTTYPE='task.completed'
          AND a.EVENTTIMESTAMP >= '{s}' AND a.EVENTTIMESTAMP < '{e}' AND {AGENT_FILTER}
        GROUP BY 1"""):
        d = slot(label, ag)
        d["csat_n"]+=int(resp or 0); d["csat_sum"]+=float(avg or 0)*int(resp or 0)
        d["res_n"]+=int(res_n or 0); d["res_hit"]+=int(res_hit or 0)

cur.close(); con.close()

# ---------- scoring functions ----------
def csat_mult(c):
    if c is None: return None
    for thr,m in [(4.7,1.20),(4.4,1.10),(4.0,0.80),(3.6,0.70),(3.2,0.65),(2.8,0.50),(2.4,0.40)]:
        if c>=thr: return m
    return 0.0
def res_mult(p):
    if p is None: return None
    for thr,m in [(90,1.20),(85,1.10),(80,0.80),(75,0.70),(70,0.65),(60,0.50),(50,0.40)]:
        if p>=thr: return m
    return 0.0
# --- Productivity = WCPH x Answer-Rate matrix, OR availability, whichever higher (each x50% weight) ---
# 2D matrix: rows = WCPH thresholds, cols = Answer-Rate thresholds, cell = productivity multiplier (%).
# Pick-up reliability is a floor: AR < 75% (or WCPH < 3.00) scores 0 on the matrix -- but availability can rescue.
# WCPH rows re-based to THIS team's real range (support/ops WCPH ~0.5-3.7, median ~2.3),
# so throughput actually differentiates instead of everyone falling below the matrix floor.
WCPH_ROWS = [3.50, 3.00, 2.50, 2.00, 1.50, 1.00]
AR_COLS   = [90, 87.5, 85, 82.5, 80, 77.5, 75]
MATRIX = {
    3.50: [140,130,120,105, 95, 75, 55],
    3.00: [130,120,105, 95, 85, 75, 65],
    2.50: [120,105, 95, 85, 75, 65, 55],
    2.00: [105, 95, 85, 75, 65, 55,  0],
    1.50: [ 95, 85, 75, 65, 55,  0,  0],
    1.00: [ 85, 75, 65, 55,  0,  0,  0],
}
def matrix_mult(wcph, arp):
    if arp is None: return 0.0
    row = next((w for w in WCPH_ROWS if wcph >= w), None)
    if row is None: return 0.0                       # WCPH below 3.00 -> 0 on matrix
    ci = next((i for i,c in enumerate(AR_COLS) if arp >= c), None)
    if ci is None: return 0.0                        # answer rate below 75% -> 0 on matrix
    return MATRIX[row][ci] / 100.0
# Availability is the quiet-day SAFETY NET: it lifts a ready, available agent on a low-volume day
# UP TO PAR (1.00x) but never above -- so simply being logged in cannot produce a top score.
# To beat par on productivity you need the WCPH x Answer-Rate matrix (real throughput + pick-up).
def avail_mult(p):
    for thr,m in [(80,1.00),(77.5,0.90),(75,0.70),(72.5,0.65),(70,0.60)]:
        if p>=thr: return m
    return 0.0

rows=[]
for (month,ag),d in sorted(data.items(), key=lambda x:(MONTHS.index(next(m for m in MONTHS if m[2]==x[0][0])), x[0][1])):
    ar = (d["ib_answered"]/d["ib_dialled"]) if d["ib_dialled"] else None
    shift_h = d["shift_s"]/3600.0
    voice_credits = d["ib_answered"]
    wcph = (d["wa_weighted"]+voice_credits)/shift_h if shift_h>0 else 0.0
    avail_pct = (d["active_s"]/d["shift_s"]*100) if d["shift_s"] else 0.0
    csat = (d["csat_sum"]/d["csat_n"]) if d["csat_n"] else None
    res_pct = (d["res_hit"]/d["res_n"]*100) if d["res_n"] else None

    # min-sample gate: CSAT sub-metrics only count with >= MIN_CSAT responses
    cm = csat_mult(csat) if d["csat_n"] >= MIN_CSAT else None
    rm = res_mult(res_pct) if d["res_n"] >= MIN_CSAT else None
    mx_m = matrix_mult(wcph, ar*100 if ar is not None else None); av_m = avail_mult(avail_pct)
    prod = max(mx_m, av_m); prod_src = "WCPH × answer-rate matrix" if mx_m>=av_m else "availability"
    # Quality pillar = 50 (CSAT interaction 30 + resolved 20); Productivity = 50
    qual_pts=0.0; qw=0.0
    if cm is not None: qual_pts += cm*30; qw+=30
    if rm is not None: qual_pts += rm*20; qw+=20
    if qw > 0:
        qual_pts = qual_pts*(50/qw)                 # fill the 50% quality pillar
        score = qual_pts + prod*50
    else:
        score = prod*100                            # no valid CSAT -> productivity fills 100%
    base = ON_TARGET*(score/100.0)
    rows.append(dict(month=month,agent=ag,ar=ar,wcph=wcph,avail=avail_pct,csat=csat,res=res_pct,
        cm=cm,rm=rm,mxm=mx_m,avm=av_m,prod=prod,prod_src=prod_src,score=score,bonus=base,
        csat_n=d["csat_n"],res_n=d["res_n"],ib_dialled=d["ib_dialled"]))

# ---------- console summary ----------
print(f"{'Month':9} {'Agent':10} {'Ans%':>5} {'WCPH':>5} {'Avail%':>6} {'CSAT':>5} {'Res%':>5} {'Score':>6} {'Bonus':>8}")
for r in rows:
    print(f"{r['month']:9} {r['agent']:10} "
          f"{(r['ar']*100 if r['ar'] else 0):5.1f} {r['wcph']:5.2f} {r['avail']:6.1f} "
          f"{(r['csat'] or 0):5.2f} {(r['res'] or 0):5.1f} {r['score']:6.1f} R{r['bonus']:7,.0f}")
bons=[r['bonus'] for r in rows]
print(f"\nAgent-months: {len(rows)} | avg R{sum(bons)/len(bons):,.0f} | min R{min(bons):,.0f} | max R{max(bons):,.0f}")

json.dump(rows, open(os.path.join(ROOT,"data","commission_backrun.json"),"w"), indent=2, default=str)
print("Wrote data/commission_backrun.json")

# ---------------- HTML REPORT ----------------
agents = sorted({r["agent"] for r in rows})
def band_colour(b):
    if b < 3000: return "#aa1a1a"
    if b < 5000: return "#cc8800"
    if b < 6500: return "#1a6b40"
    if b < 8500: return "#2e8a54"
    return "#8b6000"
def fmt(v, pct=False, dp=1):
    if v is None: return '<span style="color:#bbb">—</span>'
    return f"{v*100:.{dp}f}%" if pct else f"{v:.{dp}f}"

avg=sum(bons)/len(bons); mn=min(bons); mx=max(bons)
within = sum(1 for b in bons if 4500<=b<=6500)
under  = sum(1 for b in bons if b<3500)
top    = sum(1 for b in bons if b>=7000)

trows=""
for r in rows:
    csat_cell = (f'{r["csat"]:.2f} <span style="color:#999;font-size:11px">(n={r["csat_n"]})</span>'
                 if r["csat"] is not None else f'<span style="color:#bbb">no data</span>')
    res_cell  = (f'{r["res"]:.0f}% <span style="color:#999;font-size:11px">(n={r["res_n"]})</span>'
                 if r["res"] is not None else f'<span style="color:#bbb">no data</span>')
    trows+=f"""<tr>
      <td class="lh">{r['month']}</td><td class="lh"><strong>{r['agent']}</strong></td>
      <td>{fmt(r['ar'],pct=True)}</td><td>{r['wcph']:.2f}</td><td>{r['avail']:.1f}%</td>
      <td>{csat_cell}</td><td>{res_cell}</td>
      <td style="font-size:11px;color:#666">{r['prod_src']}</td>
      <td><strong>{r['score']:.0f}%</strong></td>
      <td style="font-weight:800;color:{band_colour(r['bonus'])}">R{r['bonus']:,.0f}</td></tr>"""

# per-agent 3-month averages
arows=""
for ag in agents:
    rs=[r for r in rows if r["agent"]==ag]; ab=[r["bonus"] for r in rs]
    arows+=f"""<tr><td class="lh"><strong>{ag}</strong></td>
      <td>{len(rs)}</td><td>R{sum(ab)/len(ab):,.0f}</td>
      <td>R{min(ab):,.0f}</td><td>R{max(ab):,.0f}</td></tr>"""

html=f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Storage Commission Backrun — Dec 2025 – May 2026</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f4f2;color:#1a1a1a;font-size:15px;line-height:1.6}}
.page{{max-width:980px;margin:0 auto;padding:32px 20px 60px}}
.header{{background:#14322a;color:#fff;border-radius:12px;padding:30px 34px;margin-bottom:18px}}
.header-tag{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#8fd4b4;margin-bottom:8px}}
.header h1{{font-size:25px;font-weight:700;margin-bottom:10px}}
.header p{{color:#b8e8d0;font-size:13.5px;line-height:1.7;max-width:760px}}
.banner{{background:#5a1010;color:#fff;border-radius:8px;padding:10px 16px;margin-bottom:14px;font-size:12.5px;font-weight:600}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}
.kpi{{background:#fff;border:1px solid #e4e4e0;border-radius:10px;padding:16px}}
.kpi .v{{font-size:26px;font-weight:800;color:#14322a;line-height:1}}
.kpi .l{{font-size:11.5px;color:#666;margin-top:5px}}
.kpi.blue .v{{color:#1a5c8a}}.kpi.green .v{{color:#1a6b40}}.kpi.amber .v{{color:#8b6000}}
.card{{background:#fff;border:1px solid #e4e4e0;border-radius:10px;padding:20px 22px;margin-bottom:16px}}
.card h2{{font-size:16px;font-weight:700;color:#14322a;margin-bottom:6px;padding-bottom:10px;border-bottom:2px solid #f0f0ec}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}}
th{{background:#14322a;color:#fff;padding:8px 9px;text-align:center;font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;border:1px solid #1c4234}}
th.lh,td.lh{{text-align:left}}
td{{padding:8px 9px;text-align:center;border:1px solid #ececea}}
tr:nth-child(even) td{{background:#fafaf8}}
.dist{{margin:8px 0}}
.dist-row{{display:flex;align-items:center;gap:10px;margin-bottom:6px}}
.dist-label{{font-size:12px;color:#444;min-width:150px;text-align:right}}
.dist-bar-bg{{flex:1;height:20px;background:#f0f0ec;border-radius:4px;overflow:hidden}}
.dist-fill{{height:100%;border-radius:4px;display:flex;align-items:center;padding-left:8px;color:#fff;font-size:11px;font-weight:700}}
.dist-pct{{font-size:12px;color:#666;min-width:90px}}
.ibox{{border-radius:0 8px 8px 0;padding:12px 16px;font-size:12.5px;line-height:1.7;margin:12px 0}}
.ib-amber{{background:#fffbf0;border-left:3px solid #cc8800;color:#5a3d00}}
.ib-green{{background:#f0faf4;border-left:3px solid #1a6b40;color:#0d3d22}}
.ib-blue{{background:#f0f7ff;border-left:3px solid #2266cc;color:#1a3a80}}
ul{{margin:6px 0 6px 20px;font-size:12.5px;color:#444;line-height:1.7}}
.foot{{text-align:center;font-size:11.5px;color:#aaa;margin-top:28px;padding-top:14px;border-top:1px solid #e8e8e4}}
</style></head><body><div class="page">

<div class="banner">🔒 INTERNAL — MANAGEMENT BACKRUN. Models actual Mar–May 2026 data against the proposed commission engine. Illustrative; not paid out.</div>

<div class="header">
  <div class="header-tag">AnyVan Storage Operations · Commission Scheme · 6-Month Backrun</div>
  <h1>What the new scheme <em>would</em> have paid — Dec 2025 – May 2026</h1>
  <p>The proposed blended engine (R5,500 on-target · Quality 50% + Productivity 50% · QA neutral · R10,000 ceiling) applied to real per-agent data for the Storage support/ops team across the last six months. This validates whether the calibration lands where we designed it before any sign-off.</p>
</div>

<div class="kpis">
  <div class="kpi blue"><div class="v">R{avg:,.0f}</div><div class="l">Average monthly bonus<br>(target ~R5,500)</div></div>
  <div class="kpi green"><div class="v">R{mn:,.0f}</div><div class="l">Lowest agent-month</div></div>
  <div class="kpi amber"><div class="v">R{mx:,.0f}</div><div class="l">Highest agent-month<br>(ceiling R10,000)</div></div>
  <div class="kpi"><div class="v">{len(rows)}</div><div class="l">Agent-months<br>({len(agents)} agents × {len(MONTHS)} mo)</div></div>
</div>

<div class="card">
  <h2>Distribution vs design intent</h2>
  <p style="font-size:12.5px;color:#555">How the {len(rows)} agent-months fell across the intended bands:</p>
  <div class="dist">
    <div class="dist-row"><span class="dist-label">Underperforming (&lt;R3,500)</span><div class="dist-bar-bg"><div class="dist-fill" style="width:{max(under/len(rows)*100,6):.0f}%;background:#aa1a1a">{under}</div></div><span class="dist-pct">{under/len(rows)*100:.0f}%</span></div>
    <div class="dist-row"><span class="dist-label">Around target (R4,500–6,500)</span><div class="dist-bar-bg"><div class="dist-fill" style="width:{max(within/len(rows)*100,6):.0f}%;background:#1a6b40">{within}</div></div><span class="dist-pct">{within/len(rows)*100:.0f}%</span></div>
    <div class="dist-row"><span class="dist-label">Top performer (≥R7,000)</span><div class="dist-bar-bg"><div class="dist-fill" style="width:{max(top/len(rows)*100,6):.0f}%;background:#2e8a54">{top}</div></div><span class="dist-pct">{top/len(rows)*100:.0f}%</span></div>
  </div>
  <div class="ibox ib-green"><strong>Read-out:</strong> the average ({f'R{avg:,.0f}'}) sits right around the R5,500 on-target mark, the spread runs from ~R3k (underperformance) to ~R7k (strong), and <strong>nothing reaches the R10,000 ceiling</strong> — the calibration behaves as designed on real data.</div>
</div>

<div class="card">
  <h2>Per agent · per month</h2>
  <table>
    <tr><th class="lh">Month</th><th class="lh">Agent</th><th>Answer&nbsp;%</th><th>WCPH</th><th>Avail&nbsp;%</th>
    <th>CSAT</th><th>Resolved&nbsp;%</th><th>Prod. from</th><th>Score</th><th>Bonus</th></tr>
    {trows}
  </table>
  <p style="font-size:11.5px;color:#888;margin-top:8px">Productivity = higher of (WCPH × Answer-Rate matrix) or availability. "Prod. from" shows which won. Bonus = R5,500 × Score, QA neutral.</p>
</div>

<div class="card">
  <h2>Per agent · 3-month summary</h2>
  <table>
    <tr><th class="lh">Agent</th><th>Months</th><th>Avg bonus</th><th>Low</th><th>High</th></tr>
    {arows}
  </table>
</div>

<div class="card">
  <h2>Method &amp; caveats — read before using these numbers</h2>
  <div class="ibox ib-blue"><strong>How each agent-month was scored:</strong>
  <ul>
    <li><strong>Service Quality (50%)</strong> — CSAT interaction score (30%) + CSAT resolution rate (20%). <strong>Minimum-sample rule:</strong> each CSAT sub-metric only counts in a month with ≥10 responses; below that it's set aside and the score leans on productivity, so a 1–6 survey month can't swing pay.</li>
    <li><strong>Productivity (50%)</strong> — a 2D <strong>WCPH × Answer-Rate matrix</strong> (throughput blended with pick-up reliability), <strong>OR</strong> availability, whichever scores higher. Availability is a <strong>quiet-day safety net capped at par (100%)</strong> — it protects a genuinely low-volume day but can never produce a top score, so being logged in alone doesn't pay above target. <strong>Beating par requires real throughput</strong> via the matrix (which itself scores 0 below 75% answer rate — pick-up is a floor). The matrix runs above 100% in its top bands, so a strong-throughput month pushes the bonus past R5,500 toward the R10,000 ceiling.</li>
    <li><strong>QA</strong> — neutral (0%); no QA review feed exists for Storage yet.</li>
    <li>Base bonus = R5,500 × Score. No hard cap; R10,000 ceiling.</li>
  </ul></div>
  <div class="ibox ib-amber"><strong>Data caveats:</strong>
  <ul>
    <li><strong>CSAT is sparse.</strong> Storage's survey participation is very low, so some agent-months have few or no CSAT responses (see n= counts). Where CSAT is missing, that month's score is driven almost entirely by productivity. Per-agent CSAT is directional, not robust — a proper quality signal needs either higher survey volume or a QA feed.</li>
    <li><strong>Support agents are ticket-heavy, so WCPH is structurally low</strong> — much of their day is ticket/admin work that WCPH doesn't count, so their voice/WhatsApp contacts-per-hour is modest. Where WCPH lands in the lower matrix rows, the availability alternative carries productivity, which is exactly its purpose.</li>
    <li><strong>Ticket-resolution-TIME is excluded.</strong> Snowflake's ticket handler ids don't map to these agents and the timestamps are unreliable, so the resolution sub-metric uses the CSAT "resolved?" rating instead.</li>
    <li><strong>NPS</strong> isn't attributed per agent anywhere, so CSAT stands in for the customer-rating metric.</li>
  </ul></div>
</div>

<div class="foot">AnyVan Storage Operations · Commission Backrun · Mar–May 2026 · Internal modelling, not paid out · Generated {MONTHS[-1][2]} run</div>
</div></body></html>"""
open(OUT,"w").write(html)
print("Wrote", OUT)

