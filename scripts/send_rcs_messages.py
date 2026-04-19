#!/usr/bin/env python3
"""
Storage RCS — Send pending messages via Twilio
Reads rcs_jobs from Supabase where job_date=today, excluded=false, sent=false.
Sends via Twilio RCS API (SMS fallback automatic).
Updates Supabase sent=true on success.

Required GitHub Secrets:
  TWILIO_ACCOUNT_SID  — Twilio Account SID
  TWILIO_AUTH_TOKEN   — Twilio Auth Token
  TWILIO_RCS_SENDER   — RCS sender ID (e.g. rcs:MGxxxxx or the messaging service SID)
"""

import os
import sys
import requests
from datetime import date, datetime, timezone

SUPABASE_URL      = "[SUPABASE_URL_REMOVED]"
SUPABASE_ANON_KEY = "[SUPABASE_ANON_KEY_REMOVED]"
SUPABASE_HEADERS  = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
}

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_RCS_SENDER  = os.environ.get('TWILIO_RCS_SENDER')


def fetch_pending(today):
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/rcs_jobs",
        headers=SUPABASE_HEADERS,
        params={
            "job_date": f"eq.{today}",
            "excluded": "eq.false",
            "sent":     "eq.false",
            "select":   "*",
        },
    )
    resp.raise_for_status()
    return resp.json()


def send_twilio(phone, body):
    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        from_=TWILIO_RCS_SENDER,
        to=phone,
        body=body,
    )
    return msg.sid


def mark_sent(listing_id, today):
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/rcs_jobs",
        headers=SUPABASE_HEADERS,
        params={"listing_id": f"eq.{listing_id}", "job_date": f"eq.{today}"},
        json={"sent": True, "sent_at": datetime.now(timezone.utc).isoformat()},
    )


def main():
    today = str(date.today())
    print(f"Sending RCS messages for {today}")

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_RCS_SENDER]):
        print("ERROR: Missing Twilio credentials in environment")
        sys.exit(1)

    jobs = fetch_pending(today)
    print(f"Found {len(jobs)} pending jobs")

    if not jobs:
        print("Nothing to send")
        return

    sent = 0
    skipped = 0
    for job in jobs:
        name  = job.get('tp_full_name') or job.get('nickname') or '(unknown)'
        phone = job.get('phone_number', '').strip()
        tpl   = job.get('template', '').strip()

        if not phone:
            print(f"  SKIP {name} — no phone number")
            skipped += 1
            continue

        if not tpl:
            print(f"  SKIP {name} — no template")
            skipped += 1
            continue

        try:
            sid = send_twilio(phone, tpl)
            mark_sent(job['listing_id'], today)
            print(f"  SENT {name} ({phone[-4:]}) — {sid}")
            sent += 1
        except Exception as e:
            print(f"  ERROR {name}: {e}")

    print(f"\nDone — {sent} sent, {skipped} skipped")


if __name__ == '__main__':
    main()
