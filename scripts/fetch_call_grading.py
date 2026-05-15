#!/usr/bin/env python3
"""
AnyVan Storage — Call Grading (storage mention rate per agent)

Pulls Jiminny call transcripts from Snowflake (read-only), finds every agent
line that contains "storage" (or "store"/"warehous"), and counts how often
each agent mentioned storage on a pitch-eligible call.

Eligibility filter (see _eligible_call_ids_cte) excludes voicemails, transfers,
calls under 2 minutes, and foreign-market teams — so the denominator only
counts calls that could plausibly contain a storage pitch.

By default, classification is **keyword-only**: a call counts as "pitched" if
the agent's transcript contains any storage/store/warehous mention. Pass
--use-llm to ask Claude Haiku 4.5 to filter to genuine pitches (more accurate,
needs ANTHROPIC_API_KEY).

Snowflake is read-only — classifications are held in memory and the JSON output
is the only persisted artifact. Every run re-classifies fresh.

Output:
  - JSON file ~/Documents/storage-reporting/data/call_grading_sections.json
    matching the SECTIONS shape in reports/call-grading.html

Usage:
  python3 fetch_call_grading.py                       # default: trailing 19 weeks, keyword-only
  python3 fetch_call_grading.py --weeks 8             # most recent 8 weeks only
  python3 fetch_call_grading.py --start 2026-05-04    # one week starting 4 May
  python3 fetch_call_grading.py --use-llm             # add Claude Haiku quality filter
  python3 fetch_call_grading.py --dry-run             # query but don't write JSON

Env vars required: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER
                   ANTHROPIC_API_KEY only needed with --use-llm
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
import snowflake.connector

# ─── CONFIG ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
OUTPUT_JSON = DATA_DIR / "call_grading_sections.json"

# Load .env from repo root if present (matches other fetch_*.py)
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SNOWFLAKE_ACCOUNT = os.environ["SNOWFLAKE_ACCOUNT"]
SNOWFLAKE_USER = os.environ["SNOWFLAKE_USER"]
SNOWFLAKE_WH = "MART_SALES_OPS_WH"
SNOWFLAKE_ROLE = "MART_SALES_OPS_GROUP"
SNOWFLAKE_DB = "CONFORMED"
SNOWFLAKE_SCHEMA = "DEVELOPMENT"
PER_LINE_TABLE = f"{SNOWFLAKE_DB}.{SNOWFLAKE_SCHEMA}.STORAGE_CALL_PITCHES_LLM"
PER_CALL_TABLE = f"{SNOWFLAKE_DB}.{SNOWFLAKE_SCHEMA}.STORAGE_CALL_PITCHES_LLM_CALLS"

CLAUDE_MODEL = "claude-haiku-4-5"
CLAUDE_MAX_TOKENS = 8
CLAUDE_CONCURRENCY = 10

# Jiminny ANYVAN_USER_TEAMNAME → (display label, group, section id used in HTML)
TEAM_MAP = {
    "Inbound Sales - Cape Town":       ("IB – Liam (Cape Town)", "inbound",  "ib-liam"),
    "Inbound Sales - Liam":            ("IB – Liam (Cape Town)", "inbound",  "ib-liam"),
    "Inbound Sales Cape Town - Liam":  ("IB – Liam (Cape Town)", "inbound",  "ib-liam"),
    "Inbound Sales Cape Town - Jordan":("IB – Liam (Cape Town)", "inbound",  "ib-liam"),
    "Inbound Sales Cape Town - Kyle":  ("IB – Liam (Cape Town)", "inbound",  "ib-liam"),
    "Inbound Sales - Brian":           ("IB – Brian",            "inbound",  "ib-brian"),
    "Outbound Sales - Kyle":           ("OB – Kyle",             "outbound", "ob-kyle"),
    "Outbound Sales - Alex":           ("OB – Alex",             "outbound", "ob-alex"),
    "Lead Generation - Damien":        ("LG – Damien",           "lg",       "lg-damien"),
    "Lead Gen - Damien":               ("LG – Damien",           "lg",       "lg-damien"),
}

SECTION_ORDER = ["ib-liam", "ib-brian", "ob-kyle", "ob-alex", "lg-damien"]

# Agents excluded from the report (ex-employees, managers not on the bookings target, etc.).
# Names must match ANYVAN_USER_NAME exactly as it comes from Jiminny.
EXCLUDED_AGENTS = {
    # IB – Liam (Cape Town)
    "Andrea Henniker", "Aneeqah Abdol", "Chad P", "Daniel M", "Hlumela Nodunyelwa",
    "Kyle Marquard", "M Shai", "Mo Isaacs", "Tashlyn Hass", "Tashwille Hawkins",
    # IB – Brian
    "Habeeb K", "Matthew Kershaw", "Nafis M",
    # LG – Damien
    "Cameron D", "Connor N", "Deon H", "Harry Valentine", "Luke O",
    "Nick L", "Prosper Mubata", "Vinny Pastor", "William August",
}

# ─── ELIGIBILITY FILTER (for both numerator and denominator) ───────────────────
# A call only counts toward the pitch-rate denominator if it represents a real
# sales conversation. We exclude foreign-market teams, calls that never reached
# voicemail-passthrough or transfer-passthrough conversations, and anything
# under 2 minutes (analysis of the 60–120s band showed it is dominated by
# quick declines, cancellations and callbacks — not pitchable conversations).
ELIGIBLE_TEAMS = (
    "Lead Generation - Damien",
    "Lead Gen - Damien",
    "Inbound Sales Cape Town - Liam",
    "Inbound Sales Cape Town - Jordan",
    "Inbound Sales Cape Town - Kyle",
    "Inbound Sales - Cape Town",
    "Inbound Sales - Liam",
    "Inbound Sales - Brian",
    "Outbound Sales - Kyle",
    "Outbound Sales - Alex",
)
ELIGIBLE_MIN_DURATION_SECONDS = 120
_VOICEMAIL_SQL = (
    "LOWER(t.TRANSCRIPT) LIKE '%leave a message%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%after the tone%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%not available%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%record your message%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%on another line%'"
)
_TRANSFER_SQL = (
    "LOWER(t.TRANSCRIPT) LIKE '%transfer you%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%get you over to%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%get you through to%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%put you through%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%let me check if he%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%hold on the line%' "
    "OR LOWER(t.TRANSCRIPT) LIKE '%stay on the line%'"
)


def _eligible_call_ids_cte(week_start: date, week_end_exclusive: date) -> str:
    """Returns a SQL CTE producing EVENT_IDs that pass the eligibility filter
    for a date window. Inject after the WITH keyword in any query that needs
    to restrict to pitchable calls.
    """
    teams_in = ", ".join(f"'{t}'" for t in ELIGIBLE_TEAMS)
    return f"""
        eligible_events AS (
            SELECT m.EVENT_ID
            FROM HARMONISED.PRODUCTION.JIMINNY_CALL_METADATA m
            JOIN HARMONISED.PRODUCTION.JIMINNY_CALL_TRANSCRIPT t USING (EVENT_ID)
            WHERE TRY_TO_TIMESTAMP(m.ACTUAL_START_TIME) >= '{week_start.isoformat()}'::DATE
              AND TRY_TO_TIMESTAMP(m.ACTUAL_START_TIME) <  '{week_end_exclusive.isoformat()}'::DATE
              AND m.STATUS = 'completed'
              AND m.DURATION_SECONDS >= {ELIGIBLE_MIN_DURATION_SECONDS}
              AND m.ANYVAN_USER_NAME IS NOT NULL
              AND m.ANYVAN_USER_TEAMNAME IN ({teams_in})
            GROUP BY m.EVENT_ID
            HAVING NOT BOOLOR_AGG({_VOICEMAIL_SQL})
               AND NOT BOOLOR_AGG({_TRANSFER_SQL})
        )
    """

# ─── PROMPT (long, deliberately, to clear the 4096-token caching threshold on Haiku) ───

CLASSIFY_SYSTEM = """You are classifying snippets from sales call transcripts at AnyVan, a UK home-removals
company that also sells self-storage.

Your only job is to answer one question:

  Is the AGENT in this snippet mentioning AnyVan's own storage service as something
  the customer could use — either right now, or as a future option?

Answer with exactly one word: "yes" or "no". No punctuation, no explanation, no extra words.

────────────────────────────────────────────────────────────────────────────────
WHAT COUNTS AS YES
────────────────────────────────────────────────────────────────────────────────

The agent is OFFERING AnyVan storage to the customer as a service. This includes:
- Pitching storage as part of the current job ("we do offer storage from as little as £23.99 a week")
- Asking whether the customer needs storage ("did you need any items taken into storage?")
- Mentioning AnyVan storage as a future option ("if you need it down the line we can flip it into a storage booking")
- Naming AnyVan storage as available in the customer's area ("we've got storage in and around your area")
- Describing AnyVan's storage pricing or offering, even in response to a customer question, IF the agent is
  framing it as something the customer could use
- Confirming the customer's storage move that AnyVan would do (e.g. delivery TO a storage facility AnyVan is
  taking them to, as part of an offered storage booking)

────────────────────────────────────────────────────────────────────────────────
WHAT COUNTS AS NO
────────────────────────────────────────────────────────────────────────────────

- The CUSTOMER is talking about their own existing storage situation ("my stuff is in storage at the moment")
- The agent is merely acknowledging or asking about the customer's existing third-party storage situation
  ("when does the storage need to be emptied?", "is the storage unit ground floor?")
- The agent is discussing logistics around an existing third-party storage facility (Storage King, Big
  Yellow, Safestore, Access, etc.) that AnyVan is NOT offering — just moving items in or out of
- Generic in-transit references like "we'll put it in storage at the depot overnight" — that's transit
  warehousing, not AnyVan's self-storage product
- The agent is reading boilerplate confirmation text that happens to contain the word storage but isn't
  an offer (e.g. confirming a booking that is purely a removal)
- Casual / unrelated use of the word "storage" (e.g. "phone storage", "extra storage on a vehicle")

────────────────────────────────────────────────────────────────────────────────
EXAMPLES — YES
────────────────────────────────────────────────────────────────────────────────

EX1
AGENT: "Cool. So we do offer storage from as little as £23.99 a week. Did you need any items taken in storage?"
Answer: yes

EX2
AGENT: "We've got storage in and around your area, so we can do that for you short-term, long-term, whatever you need."
Answer: yes

EX3
AGENT: "If you need it, I could — like I said to you, I can flip it into our storage at the drop of a hat. You just let me know."
Answer: yes

EX4
AGENT: "Let me know if you do need it. We can switch it into a storage booking at the drop of a hat."
Answer: yes

EX5
AGENT: "So, um, if you need storage, we can do storage for you."
Answer: yes

EX6
CUSTOMER: "Okay, so what are the storage prices?"
AGENT: "Yeah so storage prices is done in square feet, but we work in cubic meters. Roughly twenty pounds a week for a small unit."
Answer: yes

EX7
AGENT: "Cool. So, um, do you require any storage? We do offer storage from as little as £23.99 a week."
Answer: yes

EX8
AGENT: "If your dates change and you need somewhere to keep your stuff in between, we've got our own storage we can move you into."
Answer: yes

EX9
AGENT: "Do you need any items put into storage, or is everything going straight to the new place?"
Answer: yes

EX10
AGENT: "Just so you know, if anything changes with the buyer's timeline, we can put your belongings into our storage and redeliver when you're ready."
Answer: yes

EX11
AGENT: "Our storage starts from about twenty pounds a week — happy to put a quote together if it's useful."
Answer: yes

EX12
AGENT: "If you ever need it down the line, we offer storage too — same team, same drivers, just gets warehoused with us until you want it back."
Answer: yes

────────────────────────────────────────────────────────────────────────────────
EXAMPLES — NO
────────────────────────────────────────────────────────────────────────────────

EX13
CUSTOMER: "All of our stuff is currently in storage in Lyth."
Answer: no
(Customer describing their own situation. Agent isn't pitching anything.)

EX14
AGENT: "Normally I recommend a video survey for a move of this distance, but with your stuff being in storage, that's going to be quite hard."
Answer: no
(Acknowledging the customer's existing storage; not offering AnyVan storage.)

EX15
AGENT: "As for do you need to be there, it's a storage unit, so generally no, depending on how well you organize it."
Answer: no
(Discussing the customer's own existing storage unit.)

EX16
AGENT: "All right, we're still delivering to Storage King then. No worries."
Answer: no
(Confirming a delivery to a third-party storage facility, not offering AnyVan storage.)

EX17
CUSTOMER: "No, I've got storage already."
Answer: no
(Customer is the one talking. Even if the agent had pitched in a previous line, this specific snippet is the customer answering.)

EX18
AGENT: "Sure, we can drop everything at the storage unit on the way through."
Answer: no
(Logistics around the customer's existing storage — they already have a unit elsewhere.)

EX19
AGENT: "If we need to, we can leave it in the van overnight or put it in storage at the depot until the morning."
Answer: no
(Transit warehousing language, not AnyVan's self-storage product.)

EX20
AGENT: "Yeah, I'll need a bit more phone storage to send that file over."
Answer: no
(Unrelated use of the word storage.)

EX21
AGENT: "Did you say the storage facility is ground floor or upstairs?"
Answer: no
(Asking about the customer's existing third-party storage facility.)

EX22
AGENT: "It looks like one of our drivers picked up from a Big Yellow Storage on a similar route last week."
Answer: no
(Naming a competitor; not offering AnyVan storage.)

EX23
AGENT: "Yeah, that's fine, we'll drop straight into the storage King unit when we arrive."
Answer: no
(Storage King is a third-party facility, not AnyVan storage.)

EX24
CUSTOMER: "We were going to put some stuff in storage but decided not to."
Answer: no
(Customer talking about their own decision; the agent line is missing or unrelated.)

────────────────────────────────────────────────────────────────────────────────
EDGE CASES
────────────────────────────────────────────────────────────────────────────────

- If both customer and agent speak and ONLY the customer mentions storage in a way that
  is the AnyVan offer: answer no (the agent has to be the one making the offer).
- If the agent says "we offer storage" but is clearly listing services unrelated to the
  customer's current job and not inviting them to use it: still answer yes (it counts as
  raising AnyVan storage as something the customer could take up).
- When in genuine doubt between yes and no, lean no — we want to count clear offers,
  not generous interpretations of ambiguous lines.

Answer with exactly one word: "yes" or "no".
"""


# ─── DATES ─────────────────────────────────────────────────────────────────────

def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_label(monday: date) -> str:
    return monday.strftime("%-d %b")


def resolve_weeks(args):
    if args.start:
        start = monday_of(date.fromisoformat(args.start))
        return [start]
    n = args.weeks if args.weeks else 19
    # Trailing N weeks ending at *this* week (Monday of today's week). Current
    # week is partial but the board has always shown it, so we include it.
    today_mon = monday_of(date.today())
    return [today_mon - timedelta(days=7 * i) for i in range(n - 1, -1, -1)]


# ─── SNOWFLAKE ─────────────────────────────────────────────────────────────────

def get_sf_token():
    toml = Path("~/.snowflake/connections.toml").expanduser().read_text()
    return toml.split('token = "')[1].split('"')[0]


def sf_connect():
    return snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        authenticator="programmatic_access_token",
        token=get_sf_token(),
        warehouse=SNOWFLAKE_WH,
        role=SNOWFLAKE_ROLE,
        database=SNOWFLAKE_DB,
        schema=SNOWFLAKE_SCHEMA,
    )


def _table_exists(cur, fqn: str) -> bool:
    db, schema, name = fqn.split(".")
    cur.execute(
        f"SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{name}'"
    )
    return (cur.fetchone() or [0])[0] > 0


def ensure_tables(cur):
    if _table_exists(cur, PER_LINE_TABLE) and _table_exists(cur, PER_CALL_TABLE):
        return
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {PER_LINE_TABLE} (
            EVENT_ID         VARCHAR,
            LINE_STARTSAT    FLOAT,
            LINE_ENDSAT      FLOAT,
            AGENT_NAME       VARCHAR,
            AGENT_EMAIL      VARCHAR,
            TEAM_NAME        VARCHAR,
            CALL_DATE        DATE,
            WEEK_START       DATE,
            LINE_TEXT        VARCHAR,
            CONTEXT_SNIPPET  VARCHAR,
            CLASSIFICATION   BOOLEAN,
            MODEL            VARCHAR,
            CLASSIFIED_AT    TIMESTAMP_TZ,
            CONSTRAINT PK_PITCH PRIMARY KEY (EVENT_ID, LINE_STARTSAT)
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {PER_CALL_TABLE} (
            EVENT_ID         VARCHAR PRIMARY KEY,
            AGENT_NAME       VARCHAR,
            AGENT_EMAIL      VARCHAR,
            TEAM_NAME        VARCHAR,
            CALL_DATE        DATE,
            WEEK_START       DATE,
            CANDIDATE_LINES  INT,
            POSITIVE_LINES   INT,
            IS_PITCH         BOOLEAN,
            UPDATED_AT       TIMESTAMP_TZ
        )
    """)


def fetch_candidate_lines(cur, week_start: date, week_end_exclusive: date):
    """Pull every agent-spoken line containing 'storage', 'store' or 'warehous'
    (case-insensitive) in [week_start, week_end_exclusive), plus 1 line of
    context on either side from the same call. The LLM stage filters out
    irrelevant matches like 'in store for you' or 'phone storage'."""
    sql = f"""
        WITH {_eligible_call_ids_cte(week_start, week_end_exclusive)},
        agent_calls AS (
            SELECT
                m.EVENT_ID,
                m.ANYVAN_USER_NAME       AS agent_name,
                m.ANYVAN_USER_EMAIL      AS agent_email,
                m.ANYVAN_USER_TEAMNAME   AS team_name,
                TRY_TO_TIMESTAMP(m.ACTUAL_START_TIME)::DATE AS call_date
            FROM HARMONISED.PRODUCTION.JIMINNY_CALL_METADATA m
            JOIN eligible_events e USING (EVENT_ID)
            WHERE m.ANYVAN_USER_NAME IS NOT NULL
        ),
        all_lines AS (
            SELECT
                ac.*,
                t.PARTICIPANTNAME,
                t.ISORGANIZER,
                t.STARTSAT,
                t.ENDSAT,
                t.TRANSCRIPT,
                LAG(t.TRANSCRIPT)      OVER (PARTITION BY ac.EVENT_ID ORDER BY t.STARTSAT) AS prev_text,
                LAG(t.PARTICIPANTNAME) OVER (PARTITION BY ac.EVENT_ID ORDER BY t.STARTSAT) AS prev_speaker,
                LEAD(t.TRANSCRIPT)      OVER (PARTITION BY ac.EVENT_ID ORDER BY t.STARTSAT) AS next_text,
                LEAD(t.PARTICIPANTNAME) OVER (PARTITION BY ac.EVENT_ID ORDER BY t.STARTSAT) AS next_speaker
            FROM agent_calls ac
            JOIN HARMONISED.PRODUCTION.JIMINNY_CALL_TRANSCRIPT t USING (EVENT_ID)
        )
        SELECT
            EVENT_ID,
            agent_name,
            agent_email,
            team_name,
            call_date,
            STARTSAT,
            ENDSAT,
            TRANSCRIPT,
            prev_speaker, prev_text,
            next_speaker, next_text
        FROM all_lines
        WHERE ISORGANIZER = TRUE
          AND PARTICIPANTNAME = agent_name
          AND (TRANSCRIPT ILIKE '%storage%'
               OR TRANSCRIPT ILIKE '%store%'
               OR TRANSCRIPT ILIKE '%warehous%')
        ORDER BY EVENT_ID, STARTSAT
    """
    cur.execute(sql)
    cols = [c[0].lower() for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_total_calls_per_agent_week(cur, week_start: date, week_end_exclusive: date):
    """Denominator: eligible (pitchable) Jiminny calls per agent for the week.
    Eligible = UK sales/LG team, completed status, >=120s, no voicemail or
    transfer markers in the transcript. See _eligible_call_ids_cte for the
    canonical filter set."""
    sql = f"""
        WITH {_eligible_call_ids_cte(week_start, week_end_exclusive)}
        SELECT
            m.ANYVAN_USER_EMAIL,
            m.ANYVAN_USER_NAME,
            m.ANYVAN_USER_TEAMNAME,
            COUNT(*) AS calls
        FROM HARMONISED.PRODUCTION.JIMINNY_CALL_METADATA m
        JOIN eligible_events e USING (EVENT_ID)
        GROUP BY 1, 2, 3
    """
    cur.execute(sql)
    return cur.fetchall()


def fetch_existing_classifications(cur, week_start: date, week_end_exclusive: date):
    """Returns set of (event_id, line_startsat) already classified."""
    cur.execute(f"""
        SELECT EVENT_ID, LINE_STARTSAT
        FROM {PER_LINE_TABLE}
        WHERE CALL_DATE >= '{week_start.isoformat()}'::DATE
          AND CALL_DATE <  '{week_end_exclusive.isoformat()}'::DATE
    """)
    return {(r[0], float(r[1])) for r in cur.fetchall()}


# ─── CLASSIFICATION ────────────────────────────────────────────────────────────

def build_snippet(row) -> str:
    parts = []
    if row.get("prev_text"):
        speaker = "AGENT" if row.get("prev_speaker") == row["agent_name"] else "CUSTOMER"
        parts.append(f'{speaker}: "{row["prev_text"].strip()}"')
    parts.append(f'AGENT: "{row["transcript"].strip()}"')
    if row.get("next_text"):
        speaker = "AGENT" if row.get("next_speaker") == row["agent_name"] else "CUSTOMER"
        parts.append(f'{speaker}: "{row["next_text"].strip()}"')
    return "\n".join(parts)


def classify_one(client: anthropic.Anthropic, snippet: str) -> bool:
    """Single classification call. Returns True for yes, False for no."""
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": CLASSIFY_SYSTEM,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"Snippet:\n\n{snippet}\n\nAnswer (yes or no):",
            }
        ],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip().lower()
    return text.startswith("y")


def classify_lines(client, rows, dry_run=False):
    """Returns dict keyed by (event_id, startsat) → (classification, snippet)."""
    out = {}
    snippets = {(r["event_id"], float(r["startsat"])): build_snippet(r) for r in rows}

    cache_audit = {"hits": 0, "writes": 0, "uncached": 0}

    if dry_run:
        # Classify just the first 5 to verify the prompt works end-to-end
        sample_keys = list(snippets.keys())[:5]
        sample_snips = {k: snippets[k] for k in sample_keys}
        snippets = sample_snips

    def _worker(key_and_snippet):
        key, snippet = key_and_snippet
        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    system=[
                        {
                            "type": "text",
                            "text": CLASSIFY_SYSTEM,
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                    messages=[
                        {
                            "role": "user",
                            "content": f"Snippet:\n\n{snippet}\n\nAnswer (yes or no):",
                        }
                    ],
                )
                text = next((b.text for b in resp.content if b.type == "text"), "").strip().lower()
                return key, snippet, text.startswith("y"), resp.usage
            except anthropic.RateLimitError:
                time.sleep(2 ** attempt)
            except anthropic.APIError as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        return key, snippet, False, None

    total = len(snippets)
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CLAUDE_CONCURRENCY) as ex:
        futures = [ex.submit(_worker, item) for item in snippets.items()]
        for fut in as_completed(futures):
            key, snippet, label, usage = fut.result()
            out[key] = (label, snippet)
            if usage is not None:
                cache_audit["hits"]   += getattr(usage, "cache_read_input_tokens", 0) or 0
                cache_audit["writes"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
                cache_audit["uncached"] += getattr(usage, "input_tokens", 0) or 0
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(f"   classified {done}/{total}  ({rate:.1f}/s, ETA {eta:.0f}s)")

    print(
        f"   cache audit — read: {cache_audit['hits']:,} tok  "
        f"write: {cache_audit['writes']:,} tok  "
        f"uncached: {cache_audit['uncached']:,} tok"
    )
    if cache_audit["hits"] == 0 and total > 1:
        print("   ⚠ WARNING: zero cache reads. Prompt prefix may be below the Haiku 4.5 4096-token threshold.")

    return out


# ─── PERSISTENCE ───────────────────────────────────────────────────────────────

def write_per_line(cur, rows, classifications):
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    to_insert = []
    for r in rows:
        key = (r["event_id"], float(r["startsat"]))
        if key not in classifications:
            continue
        label, snippet = classifications[key]
        team_name = r.get("team_name") or ""
        mon = monday_of(r["call_date"])
        to_insert.append((
            r["event_id"],
            float(r["startsat"]),
            float(r["endsat"]) if r["endsat"] is not None else None,
            r["agent_name"],
            r["agent_email"],
            team_name,
            r["call_date"].isoformat(),
            mon.isoformat(),
            r["transcript"][:8000],
            snippet[:8000],
            label,
            CLAUDE_MODEL,
            now,
        ))

    # MERGE so re-runs upsert rather than duplicate
    cur.executemany(
        f"""
        MERGE INTO {PER_LINE_TABLE} t USING (
            SELECT
                %s::VARCHAR AS event_id, %s::FLOAT AS line_startsat, %s::FLOAT AS line_endsat,
                %s::VARCHAR AS agent_name, %s::VARCHAR AS agent_email, %s::VARCHAR AS team_name,
                %s::DATE AS call_date, %s::DATE AS week_start,
                %s::VARCHAR AS line_text, %s::VARCHAR AS context_snippet,
                %s::BOOLEAN AS classification, %s::VARCHAR AS model, %s::TIMESTAMP_TZ AS classified_at
        ) s
        ON t.EVENT_ID = s.event_id AND t.LINE_STARTSAT = s.line_startsat
        WHEN MATCHED THEN UPDATE SET
            classification = s.classification, model = s.model, classified_at = s.classified_at,
            context_snippet = s.context_snippet
        WHEN NOT MATCHED THEN INSERT (
            EVENT_ID, LINE_STARTSAT, LINE_ENDSAT, AGENT_NAME, AGENT_EMAIL, TEAM_NAME,
            CALL_DATE, WEEK_START, LINE_TEXT, CONTEXT_SNIPPET, CLASSIFICATION, MODEL, CLASSIFIED_AT
        ) VALUES (
            s.event_id, s.line_startsat, s.line_endsat, s.agent_name, s.agent_email, s.team_name,
            s.call_date, s.week_start, s.line_text, s.context_snippet, s.classification, s.model, s.classified_at
        )
        """,
        to_insert,
    )


def recompute_per_call(cur, week_start: date, week_end_exclusive: date):
    """Recompute STORAGE_CALL_PITCHES_LLM_CALLS for the window from the per-line table."""
    cur.execute(f"""
        MERGE INTO {PER_CALL_TABLE} t
        USING (
            SELECT
                EVENT_ID,
                ANY_VALUE(AGENT_NAME)  AS agent_name,
                ANY_VALUE(AGENT_EMAIL) AS agent_email,
                ANY_VALUE(TEAM_NAME)   AS team_name,
                ANY_VALUE(CALL_DATE)   AS call_date,
                ANY_VALUE(WEEK_START)  AS week_start,
                COUNT(*)                                AS candidate_lines,
                SUM(CASE WHEN CLASSIFICATION THEN 1 ELSE 0 END) AS positive_lines,
                BOOLOR_AGG(CLASSIFICATION)              AS is_pitch
            FROM {PER_LINE_TABLE}
            WHERE WEEK_START >= '{week_start.isoformat()}'::DATE
              AND WEEK_START <  '{week_end_exclusive.isoformat()}'::DATE
            GROUP BY EVENT_ID
        ) s
        ON t.EVENT_ID = s.EVENT_ID
        WHEN MATCHED THEN UPDATE SET
            AGENT_NAME = s.agent_name, AGENT_EMAIL = s.agent_email, TEAM_NAME = s.team_name,
            CALL_DATE = s.call_date, WEEK_START = s.week_start,
            CANDIDATE_LINES = s.candidate_lines, POSITIVE_LINES = s.positive_lines,
            IS_PITCH = s.is_pitch, UPDATED_AT = CURRENT_TIMESTAMP()
        WHEN NOT MATCHED THEN INSERT (
            EVENT_ID, AGENT_NAME, AGENT_EMAIL, TEAM_NAME, CALL_DATE, WEEK_START,
            CANDIDATE_LINES, POSITIVE_LINES, IS_PITCH, UPDATED_AT
        ) VALUES (
            s.EVENT_ID, s.agent_name, s.agent_email, s.team_name, s.call_date, s.week_start,
            s.candidate_lines, s.positive_lines, s.is_pitch, CURRENT_TIMESTAMP()
        )
    """)


# ─── AGGREGATE → JSON ──────────────────────────────────────────────────────────

def build_sections_json(cur, weeks, call_results):
    """Returns a dict with WEEKS and SECTIONS arrays matching call-grading.html shape.

    call_results: in-memory dict of {event_id: {is_pitch, agent_name, agent_email,
        team_name, call_date, week_start}} aggregated from this run's classifications.
        Replaces the previous Snowflake-backed per-call rollup so the script no
        longer needs write access to Snowflake.
    """
    week_starts = weeks
    week_labels = [week_label(w) for w in week_starts]
    earliest = week_starts[0]
    latest_exc = week_starts[-1] + timedelta(days=7)

    # Per-agent-week pitch counts — derived in-memory from this run's classifications
    pitched = defaultdict(int)
    for info in call_results.values():
        if info["is_pitch"]:
            pitched[(info["agent_name"], info["week_start"])] += 1
    pitched = dict(pitched)

    # Per-agent-week denominator (eligible / pitchable Jiminny calls only).
    # Eligibility filter matches the numerator in fetch_candidate_lines so the
    # pitch-rate denominator is internally consistent.
    cur.execute(f"""
        WITH {_eligible_call_ids_cte(earliest, latest_exc)}
        SELECT
            m.ANYVAN_USER_NAME,
            m.ANYVAN_USER_EMAIL,
            m.ANYVAN_USER_TEAMNAME,
            DATE_TRUNC('week', TRY_TO_TIMESTAMP(m.ACTUAL_START_TIME))::DATE AS week_start,
            COUNT(*) AS total_calls
        FROM HARMONISED.PRODUCTION.JIMINNY_CALL_METADATA m
        JOIN eligible_events e USING (EVENT_ID)
        GROUP BY 1, 2, 3, 4
    """)
    totals_rows = cur.fetchall()

    # Build agent registry: (name, email, team) — pick the most common team for each agent
    agent_team_votes = defaultdict(lambda: defaultdict(int))
    for name, email, team, _wk, calls in totals_rows:
        agent_team_votes[(name, email)][team] = agent_team_votes[(name, email)].get(team, 0) + int(calls)

    agent_section = {}
    unknown_teams = set()
    for (name, email), team_votes in agent_team_votes.items():
        if name in EXCLUDED_AGENTS:
            continue
        top_team = max(team_votes.items(), key=lambda kv: kv[1])[0]
        if top_team in TEAM_MAP:
            agent_section[(name, email)] = (top_team, TEAM_MAP[top_team])
        else:
            unknown_teams.add(top_team)

    if unknown_teams:
        print(f"   ⚠ unmapped Jiminny teams (agents dropped): {sorted(unknown_teams)}")

    # totals per (agent, week) — SUM across sub-teams in case an agent appears
    # under multiple team-leader sub-teams in the same week (e.g. Cape Town team
    # leader changes). Bug fix: prior version overwrote, causing >100% rates.
    totals = defaultdict(int)
    for name, _email, _team, wk, calls in totals_rows:
        totals[(name, wk)] += int(calls)

    # Build SECTIONS
    section_buckets = {sid: {"label": None, "group": None, "agents": []} for sid in SECTION_ORDER}

    # Sort agents alphabetically by display name within their section
    by_section = defaultdict(list)
    for (name, email), (jiminny_team, (label, group, sid)) in agent_section.items():
        if sid not in section_buckets:
            continue
        section_buckets[sid]["label"] = label
        section_buckets[sid]["group"] = group
        by_section[sid].append((name, email))

    sections_array = []
    for sid in SECTION_ORDER:
        bucket = section_buckets[sid]
        if bucket["label"] is None:
            continue
        agents_arr = []
        for name, email in sorted(by_section[sid], key=lambda x: x[0]):
            r = []
            for wk in week_starts:
                tot = totals.get((name, wk), 0)
                if tot == 0:
                    r.append(None)
                else:
                    p = pitched.get((name, wk), 0)
                    r.append(round(p * 100 / tot, 1))
            agents_arr.append({"n": name, "r": r})
        sections_array.append({
            "id": sid,
            "group": bucket["group"],
            "label": bucket["label"],
            "agents": agents_arr,
        })

    return {
        "WEEKS": week_labels,
        "SECTIONS": sections_array,
        "_meta": {
            "model": CLAUDE_MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "week_starts": [w.isoformat() for w in week_starts],
        },
    }


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, help="Number of trailing weeks to process (default 19).")
    parser.add_argument("--start", type=str, help="ISO date — process the single week starting this Monday.")
    parser.add_argument("--dry-run", action="store_true", help="Classify only 5 lines per week, do not write the JSON output.")
    parser.add_argument("--use-llm", action="store_true", help="Use Claude Haiku to classify each storage mention (more accurate; needs ANTHROPIC_API_KEY). Default is keyword-only.")
    args = parser.parse_args()
    # Default is keyword-only per project policy (see feedback memory).
    args.no_llm = not args.use_llm
    if args.use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠  --use-llm passed but ANTHROPIC_API_KEY not set — falling back to keyword-only.")
        args.no_llm = True

    weeks = resolve_weeks(args)
    print(f"\n🚀 fetch_call_grading.py — {len(weeks)} week(s): {weeks[0]} → {weeks[-1]}")
    print(f"   model={CLAUDE_MODEL}  concurrency={CLAUDE_CONCURRENCY}  dry_run={args.dry_run}")
    print(f"   Snowflake: READ-ONLY (writes disabled by policy)\n")

    if not args.dry_run:
        DATA_DIR.mkdir(exist_ok=True)

    client = None if args.no_llm else anthropic.Anthropic()
    conn = sf_connect()
    cur = conn.cursor()

    # Per-call rollup accumulated in-memory across all weeks. Replaces the
    # previous Snowflake-persisted per-line/per-call tables — see
    # feedback_snowflake_writes.md memory: Snowflake is read-only for this
    # script. Every run re-classifies fresh.
    call_results = {}

    for wk in weeks:
        wk_end = wk + timedelta(days=7)
        print(f"── week of {wk} ──")
        rows = fetch_candidate_lines(cur, wk, wk_end)
        print(f"   {len(rows)} candidate lines found")

        if not rows:
            continue

        if args.no_llm:
            # Keyword-only: every candidate line (agent said "storage"/"store"/"warehous")
            # is treated as a pitch. Less accurate than the LLM grader but does not
            # need an API key.
            classifications = {(r["event_id"], float(r["startsat"])): (True, "") for r in rows}
            print(f"   ✓ keyword-flagged {len(classifications)} lines (no LLM)")
        else:
            classifications = classify_lines(client, rows, dry_run=args.dry_run)
            print(f"   ✓ classified {len(classifications)} lines")

        # Roll up per-call: a call is_pitch=True if any of its classified lines
        # came back as a real pitch.
        for r in rows:
            key = (r["event_id"], float(r["startsat"]))
            label_tuple = classifications.get(key)
            if label_tuple is None:
                continue
            label = label_tuple[0] if isinstance(label_tuple, tuple) else bool(label_tuple)
            eid = r["event_id"]
            if eid not in call_results:
                call_results[eid] = {
                    "is_pitch":    False,
                    "agent_name":  r["agent_name"],
                    "agent_email": r["agent_email"],
                    "team_name":   r.get("team_name") or "",
                    "call_date":   r["call_date"],
                    "week_start":  monday_of(r["call_date"]),
                }
            if label:
                call_results[eid]["is_pitch"] = True

    if not args.dry_run:
        print(f"\n📊 Building SECTIONS JSON from {len(call_results)} classified calls...")
        payload = build_sections_json(cur, weeks, call_results)
        OUTPUT_JSON.write_text(json.dumps(payload, indent=2))
        print(f"   wrote {OUTPUT_JSON}")
        print(f"   {len(payload['SECTIONS'])} sections, {sum(len(s['agents']) for s in payload['SECTIONS'])} agents")

    cur.close()
    conn.close()
    print("\n✅ Done.\n")


if __name__ == "__main__":
    main()
