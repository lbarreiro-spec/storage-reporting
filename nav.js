/* ============================================================================
 * AnyVan Storage Reporting — SHARED NAVIGATION  (section-aware, v2)
 * Single source of truth for: the hub SECTION TILES, each SECTION PAGE's board
 * grid, and every board's left SIDEBAR.
 * Hosted at https://robbosd.github.io/storage-reporting/nav.js
 *
 * Each storage board / hub / section page loads this once, before </body>:
 *   <script src="https://robbosd.github.io/storage-reporting/nav.js"></script>
 * and it will, depending on which containers the page has:
 *   - #av-hub-sections            -> render the 7 SECTION TILES (the hub)
 *   - #av-section-grid[data-section=N] -> render that section's BOARD CARDS
 *   - nav.sidebar                 -> rebuild the left sidebar (grouped by section)
 *   - #av-hub-grid (legacy)       -> flat board-card grid (back-compat)
 *
 * TO ADD / MOVE / RENAME A BOARD: edit AV_STORAGE_BOARDS below (set its
 * `section`). To rename a section or change its colour: edit AV_STORAGE_SECTIONS.
 * EDIT THIS FILE ONLY — pages carry a baked fallback that this overwrites, so a
 * board can never be lost by republishing a page from a stale copy.
 * ==========================================================================*/
(function () {
  // ---- Section registry (order = hub order = sidebar group order) ----
  var AV_STORAGE_SECTIONS = [
    { id:1, path:'/operations/storage-topline',      label:'Top line reporting',      icon:'💷', accent:'#106799', tint:'#eaf6fd',
      blurb:'The headline numbers for the month and the recurring reporting cadence — revenue, trends, weekly KPIs and the weekly deck.' },
    { id:2, path:'/operations/storage-soldiers',     label:'Soldiers of Storage',     icon:'🎖️', accent:'#b45309', tint:'#fef3c7',
      blurb:'The Storage Sales Team — pipeline, performance, commission and the gamified Arena that drives them.' },
    { id:3, path:'/operations/storage-heroes',       label:"Storage Op's (Hero's)",   icon:'🦸', accent:'#15803d', tint:'#dcfce7',
      blurb:'The service & ops team keeping customers happy and accounts clean — tickets, triage, satisfaction, debt, roles and comms.' },
    { id:4, path:'/operations/storage-team-hub',     label:'Storage Team',            icon:'🧑‍💻', accent:'#41a5dd', tint:'#eaf6fd',
      blurb:'Team activity across every channel — voice, daily availability and WhatsApp, per agent.' },
    { id:5, path:'/operations/storage-sales-tools',  label:'Storage Sales tools',     icon:'🧰', accent:'#6d28d9', tint:'#ede9fe',
      blurb:'The day-to-day tools the sales floor runs on — placement, pricing, leaderboard and call grading.' },
    { id:6, path:'/operations/storage-wider-ops',    label:'Wider Ops',               icon:'🌐', accent:'#075985', tint:'#e0f2fe',
      blurb:'Whole-team boards beyond Storage — the full UK contact centre, Operations weekly KPIs and the Sophie AI agent.' },
    { id:7, path:'/operations/storage-analysis-hub', label:'Analytics & other reports', icon:'📚', accent:'#4338ca', tint:'#e0e7ff',
      blurb:'One-off deep-dives, the analysis library and the how-to guides for running the day-to-day.' }
  ];

  // ---- Board registry. `section` = which section it belongs to. Order within a
  // section = display order. `headline:true` = star badge. `wider:true` = whole-team
  // (blue left rule). `priv:true`+`restricted` = private card, gated by email. ----
  var AV_STORAGE_BOARDS = [
    // §1 Top line reporting
    { path:'/operations/storage-mtd',                 label:'MTD Revenue',        icon:'💷', badge:'live', section:1, headline:true,
      blurb:'Month-to-date invoiced & paid revenue, YoY (actual + forecast), transport AV fee, sq ft & customer flow, fees and pipeline — the headline board for the month.' },
    { path:'/operations/storage-monthly',             label:'Monthly Trends',     icon:'📈', badge:'live', section:1,
      blurb:'Year-on-year monthly trend charts across revenue, profit and volume metrics.' },
    { path:'/operations/storage-weekly',              label:'Weekly KPI',         icon:'🗓️', badge:'manual', section:1,
      blurb:'Weekly KPI matrix across Website, Leads, Sales, BAU & Zoho. Hand-maintained — click ✎ Edit to update each week.' },
    { path:'/operations/storage-weekly-presentations',label:'Weekly Decks',       icon:'🗂️', badge:'deck', section:1,
      blurb:'Every weekly Storage presentation, archived as a branded deck — slides, commentary and type per screenshot.' },

    // §2 Soldiers of Storage (Storage Sales Team)
    { path:'/operations/storage-hubspot-pipeline',    label:'HubSpot Pipeline',   icon:'🔶', badge:'live', section:2,
      blurb:'AVC-UK-STORAGE pipeline — lead volume, funnel, win rate, owner leaderboard, uncontacted backlog and speed-to-lead conversion.' },
    { path:'/operations/storage-commission',          label:'Sales Commission',   icon:'💰', badge:'tool', section:2,
      blurb:'Submit storage sales commissions and track live per-rep & per-month payouts — tenure × invoice-cap matrix plus the AnyVan-fee share. Replaces the old Slack form.' },
    { path:'/operations/storage-connect',             label:'Connect & Conversion', icon:'🔗', badge:'live', section:2,
      blurb:'Sales effectiveness — per-agent outbound reach & connect rate (v1 proxy from voice activity), building toward a Lead→Onboarded→Signed→Paid conversion funnel.' },
    { path:'/operations/storage-sales-performance',   label:'Sales Performance',  icon:'🧑‍💼', badge:'live', section:2,
      blurb:'Bookings per day per person from Zoho CRM — forecast revenue (tenure × agreed £/wk), booked sq ft, APP attach, booking quality and conversion (HubSpot leads → sale), with a RAG leaderboard.' },
    { path:'/operations/storage-sales-arena',         label:'Sales Arena',        icon:'🏟️', badge:'live', section:2,
      blurb:'Gamified incentives arcade for the Storage sales team — concurrent games & leagues (sq ft, forecast £, conversion, speed-to-lead, speed-to-deal) with live Zoho data, RAG pace-to-target, podiums and rank movement.' },
    { path:'/operations/storage-sales-prizes',        label:'Sales Prizes',       icon:'🎁', badge:'tool', section:2,
      blurb:'Rewards store for the Sales Arena — spend earned coins on prizes by category, track balances and stock, and submit redemptions for manager approval.' },

    // §3 Storage Op's (Hero's)
    { path:'/operations/storage-freshdesk',           label:'Freshdesk',          icon:'🎫', badge:'live', section:3,
      blurb:'Daily ticket KPIs — in/resolved/backlog, SLA %, resolve times, by queue and by agent.' },
    { path:'/operations/storage-freshdesk-triage',    label:'Freshdesk Triage',   icon:'🚦', badge:'tool', section:3,
      blurb:'AI triage of open Storage tickets — 🔴/🟠/🔵/🟢 with a one-line reason, reds first. Standing backlog + ageing, auto-resolves greens, flags delivery failures, assigns reds to the team.' },
    { path:'/operations/storage-csat',                label:'CSAT & NPS',         icon:'⭐', badge:'live', section:3,
      blurb:'Weekly CSAT per Ops agent and NPS (collection / redelivery / combined) with promoters & detractors.' },
    { path:'/operations/storage-debt',                label:'Debt Intelligence',  icon:'📉', badge:'live', section:3,
      blurb:'Overdue balance, recovery, debt segmentation, customer risk tiers, collection curve & cohorts.' },
    { path:'/operations/storage-team',                label:'Ops Team & Roles',   icon:'👥', badge:'guide', section:3,
      blurb:'Who does what on the Storage team — every owner\'s activities, cadence, SLA and escalation in one place. The day-to-day operating manual for the team.' },
    { path:'/operations/ops-comms',                   label:'Ops Comms — Commission', icon:'🔒', badge:'priv', section:3, priv:true, restricted:['scott@anyvan.com'],
      blurb:'Storage ops commission calculator — Productivity / CSAT / Ticket Resolution, QA steppers & payout ladder. Visible only to you.' },

    // §4 Storage Team
    { path:'/operations/storage-voice-activity',      label:'Voice Activity',     icon:'🎙️', badge:'live', section:4,
      blurb:'Inbound + outbound dials, answer rate, talk time & AHT per agent. Month selector + ops/sales toggle.' },
    { path:'/operations/storage-daily-activity',      label:'Daily Activity',     icon:'📊', badge:'live', section:4,
      blurb:'Per-agent daily activity — online, available, break, admin, OB activity & more, with ops/sales toggle.' },
    { path:'/operations/storage-whatsapp',            label:'WhatsApp',           icon:'💬', badge:'live', section:4,
      blurb:'Inbound/outbound chats, wait & response times, engagement and reply rate per agent.' },

    // §5 Storage Sales tools
    { path:'/operations/storage-placement-map',       label:'Placement Map',      icon:'🗺️', badge:'tool', section:5,
      blurb:'Find the best storage site for any postcode — pins sized by sell priority, with each site’s address, opening hours, pricing and sales notes. Live from the master storage map.' },
    { path:'/operations/storage-calculator',          label:'Storage Pricing',    icon:'🧮', badge:'tool', section:5,
      blurb:'Agent pricing tool — Sq Ft / M² sizing plus price-match for Access, Non-Access & BlueSpace (Spain): full price, discount ladder and a 15% margin floor. Live from the price sheet.' },
    { path:'/operations/storage-sales-leaderboard',   label:'Sales Leaderboard',  icon:'🏆', badge:'live', section:5,
      blurb:'Storage sales ranking by agent — bookings & revenue, updated live from the team sheet.' },
    { path:'/operations/storage-call-grading',        label:'Call Grading',       icon:'📞', badge:'live', section:5,
      blurb:'Weekly storage-mention-rate heatmap per agent, by Inbound / Outbound / Lead Gen pod.' },

    // §6 Wider Ops (whole-team boards)
    { path:'/operations/cs-uk-contact-centre',        label:'CS Contact Centre',  icon:'🎧', badge:'live', section:6, wider:true,
      blurb:'The whole UK Customer Service team (not just Storage): true answer rate incl. queue abandons, abandoned/missed, call volume, CSAT (+ per agent), per-agent×hour heatmap, availability, utilisation & AI deflection. Voice / WhatsApp / Chat toggle.' },
    { path:'/operations/ops-weekly',                  label:'Operations — Weekly KPIs', icon:'📋', badge:'live', section:6, wider:true,
      blurb:'The whole Operations team (all departments, not just Storage): the weekly KPI matrix across Allocations, Spend, CS Health and more — editable cells with WoW variance, targets, per-metric definitions, source links & comment threads.' },
    { path:'/operations/sophie-ai-performance',       label:'Sophie (AI Agent)',  icon:'🤖', badge:'live', section:6, wider:true,
      blurb:'Everything on the Sophie AI agent in one board — cross-channel containment & escalation, what is avoidable, voice hang-ups and the WhatsApp deep-dive. Tabs: Overview · Escalations · Voice hang-ups · WhatsApp.' },

    // §7 Analytics & other reports
    { path:'/operations/storage-artifacts',           label:'Analysis Library',   icon:'📚', badge:'lib', section:7,
      blurb:'One-off investigations & deep-dives for Storage & Removals — saved interactive reports, catalogued and filterable. Starts with the transport fee vs TTV audit.' },
    { path:'/operations/storage-guides',              label:'How-To Guides',      icon:'📚', badge:'guide', section:7,
      blurb:'Step-by-step team guides for running the day-to-day in Zoho — starting with one-click customer onboarding.' }
  ];

  // expose for debugging / reuse
  window.AV_STORAGE_BOARDS = AV_STORAGE_BOARDS;
  window.AV_STORAGE_SECTIONS = AV_STORAGE_SECTIONS;

  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function curPath(){ return location.pathname.replace(/\/+$/,''); }
  function samePath(a,b){ return a.replace(/\/+$/,'') === b.replace(/\/+$/,''); }
  function badgeText(b){
    if(b.headline) return 'Headline';
    var m={deck:'Decks',manual:'Manual',tool:'Tool',guide:'Guides',lib:'Library',priv:'Private · Scott'};
    return m[b.badge] || 'Live';
  }
  function badgeClass(b){ return b.headline ? 'star' : (b.badge || 'live'); }
  function boardsIn(id){ return AV_STORAGE_BOARDS.filter(function(b){ return b.section===id && allowed(b); }); }

  // ---- restricted-card gating ----
  var SIGNED_IN = '';
  function allowed(b){
    if(!b.restricted) return true;
    return SIGNED_IN && b.restricted.indexOf(SIGNED_IN) !== -1;
  }
  function getSignedInEmail(){
    if(!window.AVDashboard || !AVDashboard.getCurrentUser) return Promise.resolve('');
    try{
      var email = function(u){ return ((u && u.email) || '').toLowerCase(); };
      return Promise.resolve(AVDashboard.getCurrentUser()).then(function(u){
        if(email(u)) return email(u);
        if(!AVDashboard.ensureAuthenticated) return '';
        return Promise.resolve(AVDashboard.ensureAuthenticated())
          .then(function(){ return Promise.resolve(AVDashboard.getCurrentUser()); })
          .then(email).catch(function(){ return ''; });
      }).catch(function(){ return ''; });
    }catch(e){ return Promise.resolve(''); }
  }

  // ---- board card (section pages + legacy hub grid) ----
  function cardHTML(b, here){
    var extra = b.priv ? ' priv-card' : (b.wider ? ' cs-card' : '');
    var self  = samePath(here, b.path) ? ' current' : '';
    return '<a class="card live'+extra+self+'" href="'+b.path+'">'+
      '<div class="top"><div class="ico">'+b.icon+'</div><span class="badge '+badgeClass(b)+'">'+badgeText(b)+'</span></div>'+
      '<h2>'+esc(b.label)+'</h2><p>'+esc(b.blurb)+'</p>'+
      '<div class="arrow">Open &rarr;</div></a>';
  }

  // ---- HUB: 7 section tiles ----
  function renderHubSections(){
    var host = document.getElementById('av-hub-sections');
    if(!host) return false;
    host.innerHTML = AV_STORAGE_SECTIONS.map(function(s){
      var list = boardsIn(s.id);
      var chips = list.map(function(b){ return '<span class="chip">'+esc(b.label)+'</span>'; }).join('');
      return '<a class="sec" href="'+s.path+'" style="--accent:'+s.accent+';--tint:'+s.tint+'">'+
        '<div class="sec-inner">'+
          '<div class="top"><div class="ico">'+s.icon+'</div><span class="num">0'+s.id+'</span></div>'+
          '<h2>'+esc(s.label)+'</h2><p>'+esc(s.blurb)+'</p>'+
          '<div class="chips">'+chips+'<span class="cnt">'+list.length+' boards</span></div>'+
          '<div class="go">Open section &rarr;</div>'+
        '</div></a>';
    }).join('');
    return true;
  }

  // ---- SECTION PAGE: board cards for one section ----
  function renderSection(){
    var grid = document.getElementById('av-section-grid');
    if(!grid) return false;
    var id = parseInt(grid.getAttribute('data-section'), 10);
    if(!id) return false;
    var here = curPath();
    grid.innerHTML = boardsIn(id).map(function(b){ return cardHTML(b, here); }).join('');
    return true;
  }

  // ---- LEGACY flat hub grid (#av-hub-grid) ----
  function renderHubGrid(){
    var grid = document.getElementById('av-hub-grid');
    if(!grid) return false;
    var here = curPath();
    grid.innerHTML = AV_STORAGE_BOARDS.filter(function(b){ return !b.headline && allowed(b); })
      .map(function(b){ return cardHTML(b, here); }).join('');
    return true;
  }

  // ---- SIDEBAR: grouped by section ----
  function renderSidebar(){
    var sb = document.querySelector('nav.sidebar') || document.querySelector('.sidebar');
    if(!sb) return false;
    var here = curPath();
    // wipe any hardcoded / previously-rendered links + group headers
    var old = sb.querySelectorAll('a.nav-link, .nav-group');
    for(var i=0;i<old.length;i++) old[i].parentNode.removeChild(old[i]);
    var html = '';
    AV_STORAGE_SECTIONS.forEach(function(s){
      var list = boardsIn(s.id);
      if(!list.length) return;
      html += '<div class="nav-group" style="padding:14px 24px 5px;font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:rgba(255,255,255,0.38)">'+esc(s.label)+'</div>';
      html += list.map(function(b){
        var active = samePath(here, b.path) ? ' active' : '';
        return '<a class="nav-link'+active+'" href="'+b.path+'">'+esc(b.label)+'</a>';
      }).join('');
    });
    var bottom = sb.querySelector('.sidebar-bottom');
    if(bottom){ bottom.insertAdjacentHTML('beforebegin', html); }
    else {
      sb.insertAdjacentHTML('beforeend', html);
      sb.insertAdjacentHTML('beforeend','<div class="sidebar-bottom"><a class="sidebar-home" href="/operations/storage">&larr; All boards (Hub)</a></div>');
    }
    return true;
  }

  function renderAll(){
    try{ renderSidebar(); renderHubSections(); renderSection(); renderHubGrid(); }
    catch(e){ if(window.console) console.warn('[av-nav]', e); }
  }

  function go(){
    renderAll();
    // Resolve signed-in user, then re-render everything so restricted boards
    // (e.g. Ops Comms) appear for allowed users. Safe now: no page relies on
    // inline-injected cards — the registry owns every card.
    getSignedInEmail().then(function(email){
      if(!email || email === SIGNED_IN) return;
      SIGNED_IN = email;
      renderAll();
    });
  }
  if(document.readyState !== 'loading') go();
  else document.addEventListener('DOMContentLoaded', go);
})();
