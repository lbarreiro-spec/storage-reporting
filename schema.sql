-- AnyVan Storage Reporting — Supabase Schema
-- Run this in the Supabase SQL Editor

-- Monthly revenue, invoices, customers, sq ft
create table if not exists mtd_monthly (
  label              text primary key,   -- 'YYYY-MM'
  invoiced_revenue   numeric,
  paid_revenue       numeric,
  invoice_count      integer,
  paid_count         integer,
  unique_customers   integer,
  promo_pct          numeric,
  writeoff_count     integer,
  writeoff_value     numeric,
  sqft_booked        numeric,
  sqft_entering      numeric,
  sqft_exiting       numeric,
  customers_booked   integer,
  customers_entering integer,
  customers_exiting  integer,
  updated_at         timestamptz default now()
);

-- Monthly transport (collections + redeliveries)
create table if not exists mtd_transport_monthly (
  label             text primary key,   -- 'YYYY-MM'
  coll_jobs         integer,
  coll_completed    integer,
  coll_av_fee       numeric,
  redel_jobs        integer,
  redel_completed   integer,
  redel_av_fee      numeric,
  updated_at        timestamptz default now()
);

-- Monthly fees (overdue admin, early release)
create table if not exists mtd_fees_monthly (
  label                 text primary key,   -- 'YYYY-MM'
  overdue_admin_count   integer,
  overdue_admin_total   numeric,
  early_release_count   integer,
  early_release_total   numeric,
  updated_at            timestamptz default now()
);

-- YoY snapshot for current month (single row, id always 1)
create table if not exists mtd_yoy (
  id                              integer primary key default 1,
  period                          text,
  as_of                           text,
  days_elapsed                    integer,
  days_in_month                   integer,
  cy_invoiced                     numeric,
  py_invoiced_same_days           numeric,
  invoiced_actual_yoy_pct         numeric,
  cy_invoiced_forecast            numeric,
  py_invoiced_full_month          numeric,
  invoiced_forecast_yoy_pct       numeric,
  cy_paid                         numeric,
  py_paid_same_days               numeric,
  paid_actual_yoy_pct             numeric,
  cy_paid_forecast                numeric,
  py_paid_full_month              numeric,
  paid_forecast_yoy_pct           numeric,
  cy_unique_customers             integer,
  py_unique_customers_same_days   integer,
  customers_actual_yoy_pct        numeric,
  cy_customers_forecast           integer,
  py_customers_full_month         integer,
  customers_forecast_yoy_pct      numeric,
  coll_av_fee                     numeric,
  coll_av_fee_prior_year          numeric,
  coll_yoy_pct                    numeric,
  redel_av_fee                    numeric,
  redel_av_fee_prior_year         numeric,
  redel_yoy_pct                   numeric,
  updated_at                      timestamptz default now()
);

-- Weekly KPI — legacy data seeded from Google Sheet, future data from live sources
create table if not exists weekly_kpi (
  week_commencing  date primary key,  -- Monday of each week, e.g. 2025-01-06
  invoice_revenue  numeric,
  cash_collected   numeric,
  transport_net_rev numeric,
  total_sales      integer,           -- new storage deals booked
  total_leads      integer,
  bau_leads        integer,           -- BAU website leads only
  inbound_calls    integer,
  answer_rate      numeric,           -- % e.g. 92.5
  tickets_raised   integer,
  tickets_resolved integer,
  csat             numeric,           -- /5 scale
  updated_at       timestamptz default now()
);

-- Daily agent activity
create table if not exists daily_activity (
  date            date not null,
  agent           text not null,
  team            text not null,
  available       integer,
  break_time      integer,
  lunch           integer,
  admin           integer,
  ob_activity     integer,
  offline         integer,
  personal        integer,
  system_issue    integer,
  ticketing       integer,
  live_chat       integer,
  online_duration integer,
  first_login     text,
  last_logout     text,
  primary key (date, agent)
);

-- Weekly CSAT per ops agent (Shafwaan, Shaun, Emmanuel, Theo)
create table if not exists csat_weekly (
  week_commencing  date not null,
  agent            text not null,
  interactions     integer,
  responses        integer,
  avg_csat         numeric,     -- /5 scale
  resolution_pct   numeric,     -- 0-100
  primary key (week_commencing, agent)
);

-- Weekly NPS (collection / redelivery / combined)
create table if not exists nps_weekly (
  week_commencing  date not null,
  type             text not null,   -- 'collection', 'redelivery', 'combined'
  score            integer,         -- -100 to +100
  responses        integer,
  promoters        integer,
  passives         integer,
  detractors       integer,
  primary key (week_commencing, type)
);

-- Daily voice activity (IB + OB, seconds stored as integers)
create table if not exists voice_daily (
  date            date not null,
  agent           text not null,
  team            text not null,
  ib_dialled      integer,
  ib_answered     integer,
  ib_talktime     integer,   -- seconds
  ib_aht          integer,   -- seconds
  ob_dialled      integer,
  ob_answered     integer,
  ob_talktime     integer,   -- seconds
  ob_aht          integer,   -- seconds
  primary key (date, agent)
);

-- Daily WhatsApp activity (IB + OB + engagement, seconds stored as integers)
create table if not exists whatsapp_daily (
  date                   date not null,
  agent                  text not null,
  team                   text not null,
  ib_chats               integer,
  ib_avg_wait            integer,   -- seconds
  ib_avg_first_response  integer,   -- seconds
  ib_avg_wrap            integer,   -- seconds
  ib_talk_time           integer,   -- seconds
  ob_chats               integer,
  ob_talk_time           integer,   -- seconds
  eng_convos_initiated   integer,
  eng_messages_sent      integer,
  eng_customers_replied  integer,
  primary key (date, agent)
);

-- Daily invoiced revenue for current month vs prior year (for chart)
create table if not exists mtd_daily_revenue (
  label       text    not null,   -- 'YYYY-MM'
  day         integer not null,   -- 1–31
  day_name    text,               -- 'Mon', 'Tue', etc.
  cy_revenue  numeric,            -- current year daily invoiced
  py_revenue  numeric,            -- prior year daily invoiced
  updated_at  timestamptz default now(),
  primary key (label, day)
);
alter table mtd_daily_revenue disable row level security;

-- RLS: disabled — open read access (no auth in reporting app)
alter table mtd_monthly           disable row level security;
alter table mtd_transport_monthly  disable row level security;
alter table mtd_fees_monthly       disable row level security;
alter table mtd_yoy                disable row level security;
alter table weekly_kpi             disable row level security;
alter table daily_activity         disable row level security;
alter table voice_daily            disable row level security;
alter table whatsapp_daily         disable row level security;

-- ClearPass invoice automation outcomes (one row per invoice processed)
create table if not exists clearpass_invoices (
  id           bigserial primary key,
  processed_at timestamptz not null default now(),
  month        text not null,   -- 'YYYY-MM'
  supplier     text not null,   -- parser slug e.g. 'safestore', 'uk_storage'
  outcome      text not null    -- 'approved', 'manual', 'rejected'
);
create index if not exists clearpass_invoices_month_idx    on clearpass_invoices (month);
create index if not exists clearpass_invoices_supplier_idx on clearpass_invoices (supplier);
alter table clearpass_invoices disable row level security;
