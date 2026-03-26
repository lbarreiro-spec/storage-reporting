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

-- RLS: disabled — open read access (no auth in reporting app)
alter table mtd_monthly          disable row level security;
alter table mtd_transport_monthly disable row level security;
alter table mtd_fees_monthly     disable row level security;
alter table mtd_yoy              disable row level security;
