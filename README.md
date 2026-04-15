# Storage Reporting

Live dashboard: https://robbosd.github.io/storage-reporting/index.html

## Claude Code Trigger

To refresh all data in one go, type this in Claude Code:

```
/rundata
```

This triggers all GitHub Actions workflows in this repo (Voice, WhatsApp, Daily Activity, CSAT, MTD, Monthly, Weekly, Debt, Freshdesk) and reports back when done.

## Workflows

| Workflow | Script | Schedule |
|---|---|---|
| Fetch Voice Daily | `scripts/fetch_voice.py` | Mon–Fri ~7am UTC |
| Fetch Daily Activity | `scripts/fetch_daily_activity.py` | Mon–Fri ~7am UTC |
| Fetch WhatsApp Daily | `scripts/fetch_whatsapp.py` | Mon–Fri ~7am UTC |
| Fetch CSAT & NPS Weekly | `scripts/fetch_csat.py` | Mon–Fri ~7am UTC |
| Fetch MTD Data | `scripts/fetch_mtd.py` | Mon–Fri ~7am UTC |
| Fetch Monthly Trends | `scripts/fetch_monthly.py` | Mon–Fri ~7am UTC |
| Fetch Weekly KPI | `scripts/fetch_weekly.py` | Mon–Fri ~7am UTC |
| Fetch Debt Data | `scripts/kpi_debt.py` | Mon–Fri ~7am UTC |
| Refresh Freshdesk Data | `scripts/kpi_freshdesk_daily.py` | Mon–Fri ~7am UTC |

All workflows also support `workflow_dispatch` for manual runs (triggered by `/rundata`).

## Architecture

Snowflake / Zoho / Freshdesk → Python scripts → Supabase → GitHub Pages dashboard
