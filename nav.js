/* ============================================================================
 * AnyVan Storage Reporting — SHARED NAVIGATION
 * Single source of truth for the hub cards + every board's sidebar.
 * Hosted at https://robbosd.github.io/storage-reporting/nav.js
 *
 * Each storage board loads this once, before </body>:
 *   <script src="https://robbosd.github.io/storage-reporting/nav.js"></script>
 * and it will:
 *   - rebuild the left sidebar (<nav class="sidebar">) from AV_STORAGE_BOARDS, OR
 *   - fill the hub card grid (<div class="grid" id="av-hub-grid"></div>) from it.
 *
 * To add / remove / rename a board, or fix a dropped card: EDIT THIS FILE ONLY.
 * Cards can no longer be lost by republishing a board from a stale copy.
 * ==========================================================================*/
(function () {
  // Canonical board registry. Order = sidebar order = hub card order.
  // hero:true  -> the hub renders this as the big hero (kept hardcoded in the hub), so it is NOT added to the card grid.
  // sidebar:false -> excluded from the left sidebar.
  // hubCard:false -> excluded from the hub card grid (still shows in the sidebar).
  // restricted:[emails] -> card + sidebar link only render for these signed-in emails (gate is the CARD's visibility; the board has its own data gate too).
  // badge: 'live' | 'manual' | 'deck' | 'tool' | 'guide'
  var AV_STORAGE_BOARDS = [
    { path:'/operations/storage-mtd',                 label:'MTD Revenue',      icon:'💷', badge:'live',   hero:true,
      blurb:'Month-to-date invoiced & paid revenue, YoY (actual + forecast), transport AV fee, sq ft & customer flow, fees and pipeline — the headline board for the month.' },
    { path:'/operations/storage-monthly',             label:'Monthly Trends',   icon:'📈', badge:'live',
      blurb:'Year-on-year monthly trend charts across revenue, profit and volume metrics.' },
    { path:'/operations/storage-weekly',              label:'Weekly KPI',       icon:'🗓️', badge:'manual',
      blurb:'Weekly KPI matrix across Website, Leads, Sales, BAU & Zoho. Hand-maintained — click ✎ Edit to update each week.' },
    { path:'/operations/storage-hubspot-pipeline',    label:'HubSpot Pipeline', icon:'🔶', badge:'live',
      blurb:'AVC-UK-STORAGE pipeline — lead volume, funnel, win rate, owner leaderboard, uncontacted backlog and speed-to-lead conversion.' },
    { path:'/operations/storage-connect',             label:'Connect & Conversion', icon:'🔗', badge:'live',
      blurb:'Sales effectiveness — per-agent outbound reach & connect rate (v1 proxy from voice activity), building toward a Lead→Onboarded→Signed→Paid conversion funnel.' },
    { path:'/operations/storage-sales-leaderboard',   label:'Sales Leaderboard',icon:'🏆', badge:'live',
      blurb:'Storage sales ranking by agent — bookings & revenue, updated live from the team sheet.' },
    { path:'/operations/storage-sales-performance',   label:'Sales Performance',icon:'🧑‍💼', badge:'live',
      blurb:'Bookings per day per person from Zoho CRM — forecast revenue (tenure × agreed £/wk), booked sq ft, APP attach, booking quality and conversion (HubSpot leads → sale), with a RAG leaderboard.' },
    // storage-commission: PUBLIC. The HUB CARD is injected inline by the hub HTML (so hubCard:false here
    // to avoid a duplicate). This entry provides the SIDEBAR link, now ungated. The board's own data gate
    // (renderRestricted) still protects the actual commission figures regardless of who sees the link.
    { path:'/operations/storage-commission',          label:'Sales Commission', icon:'💰', badge:'tool', hubCard:false,
      blurb:'Submit storage sales commissions and track live per-rep & per-month payouts — tenure × invoice-cap matrix plus the AnyVan-fee share. Replaces the old Slack form.' },
    { path:'/operations/storage-calculator',          label:'Storage Pricing',  icon:'🧮', badge:'tool',
      blurb:'Agent pricing tool — Sq Ft / M² sizing plus price-match for Access, Non-Access & BlueSpace (Spain): full price, discount ladder and a 15% margin floor. Live from the price sheet.' },
    { path:'/operations/storage-placement-map',       label:'Placement Map',    icon:'🗺️', badge:'tool',
      blurb:'Find the best storage site for any postcode — pins sized by sell priority, with each site’s address, opening hours, pricing and sales notes. Live from the master storage map.' },
    { path:'/operations/storage-voice-activity',      label:'Voice Activity',   icon:'🎙️', badge:'live',
      blurb:'Inbound + outbound dials, answer rate, talk time & AHT per agent. Month selector + ops/sales toggle.' },
    { path:'/operations/storage-daily-activity',      label:'Daily Activity',   icon:'📊', badge:'live',
      blurb:'Per-agent daily activity — online, available, break, admin, OB activity & more, with ops/sales toggle.' },
    { path:'/operations/storage-whatsapp',            label:'WhatsApp',         icon:'💬', badge:'live',
      blurb:'Inbound/outbound chats, wait & response times, engagement and reply rate per agent.' },
    { path:'/operations/storage-freshdesk',           label:'Freshdesk',        icon:'🎫', badge:'live',
      blurb:'Daily ticket KPIs — in/resolved/backlog, SLA %, resolve times, by queue and by agent.' },
    { path:'/operations/storage-freshdesk-triage',    label:'Freshdesk Triage', icon:'🚦', badge:'tool',
      blurb:'AI triage of open Storage tickets — 🔴/🟠/🔵/🟢 with a one-line reason, reds first. Standing backlog + ageing, auto-resolves greens, flags delivery failures, assigns reds to the team.' },
    { path:'/operations/storage-csat',                label:'CSAT & NPS',       icon:'⭐', badge:'live',
      blurb:'Weekly CSAT per Ops agent and NPS (collection / redelivery / combined) with promoters & detractors.' },
    { path:'/operations/storage-call-grading',        label:'Call Grading',     icon:'📞', badge:'live',
      blurb:'Weekly storage-mention-rate heatmap per agent, by Inbound / Outbound / Lead Gen pod.' },
    { path:'/operations/storage-debt',                label:'Debt Intelligence',icon:'📉', badge:'live',
      blurb:'Overdue balance, recovery, debt segmentation, customer risk tiers, collection curve & cohorts.' },
    { path:'/operations/storage-weekly-presentations',label:'Weekly Decks',     icon:'🗂️', badge:'deck',
      blurb:'Every weekly Storage presentation, archived as a branded deck — slides, commentary and type per screenshot.' },
    { path:'/operations/storage-team',                label:'Ops Team & Roles', icon:'👥', badge:'guide',
      blurb:'Who does what on the Storage team — every owner\'s activities, cadence, SLA and escalation in one place. The day-to-day operating manual for the four-person team.' },
    { path:'/operations/storage-guides',              label:'How-To Guides',    icon:'📚', badge:'guide', hubCard:false,
      blurb:'Step-by-step team guides for running the day-to-day in Zoho — starting with one-click customer onboarding.' },
    { path:'/operations/sophie-ai-performance',       label:'Sophie (AI Agent)', icon:'🤖', badge:'live', hubCard:false,
      blurb:'Everything on the Sophie AI agent in one board — cross-channel containment & escalation, what is avoidable, voice hang-ups and the WhatsApp deep-dive. Tabs: Overview · Escalations · Voice hang-ups · WhatsApp.' }
  ];

  // expose for debugging / reuse
  window.AV_STORAGE_BOARDS = AV_STORAGE_BOARDS;

  function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function curPath(){ return location.pathname.replace(/\/+$/,''); }

  // Signed-in email for restricted-card gating. Default '' = restricted cards hidden (safe).
  var SIGNED_IN = '';
  function allowed(b){
    if(!b.restricted) return true;
    return SIGNED_IN && b.restricted.indexOf(SIGNED_IN) !== -1;
  }
  // Resolve the signed-in email to gate the restricted SIDEBAR link. Mirrors the exact pattern the hub
  // already uses successfully: getCurrentUser first, and only if empty fall back to ensureAuthenticated
  // then retry. NO explicit init() — av-dashboard.js auto-inits, and a second init() can clear the
  // cached user. Any failure resolves to '' -> restricted links stay hidden (safe default).
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

  function renderSidebar(){
    var sb = document.querySelector('nav.sidebar') || document.querySelector('.sidebar');
    if(!sb) return false;
    var here = curPath();
    // wipe any hardcoded / drifted links
    var links = sb.querySelectorAll('a.nav-link');
    for(var i=0;i<links.length;i++) links[i].parentNode.removeChild(links[i]);
    var html = AV_STORAGE_BOARDS.filter(function(b){ return b.sidebar !== false && allowed(b); }).map(function(b){
      var active = (here === b.path.replace(/\/+$/,''));
      return '<a class="nav-link'+(active?' active':'')+'" href="'+b.path+'">'+esc(b.label)+'</a>';
    }).join('');
    var bottom = sb.querySelector('.sidebar-bottom');
    if(bottom){ bottom.insertAdjacentHTML('beforebegin', html); }
    else {
      sb.insertAdjacentHTML('beforeend', html);
      sb.insertAdjacentHTML('beforeend','<div class="sidebar-bottom"><a class="sidebar-home" href="/operations/storage">&larr; All boards (Hub)</a></div>');
    }
    return true;
  }

  function renderHub(){
    var grid = document.getElementById('av-hub-grid');
    if(!grid) return false;
    var here = curPath();
    grid.innerHTML = AV_STORAGE_BOARDS.filter(function(b){ return !b.hero && b.hubCard !== false && allowed(b); }).map(function(b){
      var badge = b.badge || 'live';
      var btxt  = badge==='deck' ? 'Decks' : badge==='manual' ? 'Manual' : badge==='tool' ? 'Tool' : badge==='guide' ? 'Guides' : 'Live';
      var self  = (here === b.path.replace(/\/+$/,''));
      return '<a class="card live'+(self?' current':'')+'" href="'+b.path+'">'+
        '<div class="top"><div class="ico">'+b.icon+'</div><span class="badge '+badge+'">'+btxt+'</span></div>'+
        '<h2>'+esc(b.label)+'</h2><p>'+esc(b.blurb)+'</p>'+
        '<div class="arrow">Open &rarr;</div></a>';
    }).join('');
    return true;
  }

  function go(){
    try{ renderSidebar(); renderHub(); }catch(e){ if(window.console) console.warn('[av-nav]', e); }
    // Resolve signed-in user, then re-render the SIDEBAR ONLY so restricted links appear for allowed users.
    // Deliberately NOT re-rendering the hub grid: that uses innerHTML and would wipe inline-injected cards
    // (the hub's private Ops Comms card + the inline commission card). The hub card is handled inline.
    getSignedInEmail().then(function(email){
      if(!email || email === SIGNED_IN) return;
      SIGNED_IN = email;
      try{ renderSidebar(); }catch(e){ if(window.console) console.warn('[av-nav]', e); }
    });
  }
  if(document.readyState !== 'loading') go();
  else document.addEventListener('DOMContentLoaded', go);
})();
