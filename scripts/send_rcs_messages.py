#!/usr/bin/env python3
"""
Storage RCS — Send messages via Twilio
Reads the job list from the JOBS_JSON environment variable (set by workflow input).
Each job: { listing_id, tp_full_name, phone_number, job_type, template }

Required GitHub Secrets:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_RCS_SENDER  — RCS sender ID from your Twilio account
"""

import json
import os
import sys

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_RCS_SENDER  = os.environ.get('TWILIO_RCS_SENDER')
JOBS_JSON          = os.environ.get('JOBS_JSON', '[]')


def main():
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_RCS_SENDER]):
        print("ERROR: Missing Twilio credentials")
        sys.exit(1)

    try:
        jobs = json.loads(JOBS_JSON)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JOBS_JSON: {e}")
        sys.exit(1)

    if not jobs:
        print("No jobs to send")
        return

    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    sent = skipped = errors = 0
    for job in jobs:
        name  = job.get('tp_full_name') or job.get('nickname') or '(unknown)'
        phone = (job.get('phone_number') or '').strip()
        body  = (job.get('template') or '').strip()

        if not phone:
            print(f"  SKIP {name} — no phone number")
            skipped += 1
            continue
        if not body:
            print(f"  SKIP {name} — no template")
            skipped += 1
            continue

        try:
            msg = client.messages.create(
                from_=TWILIO_RCS_SENDER,
                to=phone,
                body=body,
            )
            print(f"  SENT {name} ({phone[-4:]}) → {msg.sid}")
            sent += 1
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            errors += 1

    print(f"\nDone — {sent} sent, {skipped} skipped, {errors} errors")
    if errors:
        sys.exit(1)


if __name__ == '__main__':
    main()
