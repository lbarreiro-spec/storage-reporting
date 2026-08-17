#!/usr/bin/env python3
"""
Triage classification helpers (added 18 Jun 2026) — derive THEME + PARTNER for a ticket
from its subject + one-line reason. Keyword-based, zero-cost, no LLM (consistent with the
keyword-only call-grading policy). Used by the dashboard generator to classify the WHOLE
feed, and available to the judge step to emit a sharper `theme` per ticket going forward.

theme_of()/partner_of() take a dict-ish ticket carrying 'subject' and (optionally) 'reason'.
First match wins, so ORDER is significant — most-specific / highest-stakes first.
"""

# --- PARTNERS: storage facility / removals partners we route through. Match on subject text. ---
# (label -> list of lowercase substrings that identify them)
_PARTNERS = [
    ("Safestore",        ["safestore", "safe store", "safestorage"]),
    ("Access Self Storage", ["access self storage", "accessstorage", "access storage", "access business centre"]),
    ("Cadogan Tate",     ["cadogan tate", "cadogantate", "cadogan"]),
    ("Doree Bonner",     ["doree bonner", "dbonner"]),
    ("GB Liners",        ["gb liners", "gbliners", "aberdeen self storage"]),
    ("MJ McCarthy",      ["mj mccarthy", "mccarthy", "mjmccarthy"]),
    ("Pickfords",        ["pickfords"]),
    ("UK Storage",       ["uk storage", "ukstoragecompany"]),
    ("Britannia",        ["britannia", "bradshaw"]),
    ("easyStorage",      ["easystorage", "easy storage"]),
    ("Titan",            ["titan storage", "titan"]),
    ("Harrison & Rowley", ["harrison", "rowley", "harrisonandrowley"]),
    ("Easy Shipping",    ["easy shipping", "easyshipping"]),
]

def partner_of(t):
    s = ((t.get("subject") or "") + " " + (t.get("last_msg") or "")).lower()
    for label, keys in _PARTNERS:
        if any(k in s for k in keys):
            return label
    return "Customer (direct)"

# --- THEMES: what the ticket is ABOUT. Order = priority (first match wins). ---
_THEMES = [
    ("Delivery failure", ["undelivered", "couldn't be delivered", "couldnt be delivered", "bounce", "bad-mailbox", "mailbox", "quota-issues", "not delivered"]),
    ("Damage / loss",    ["damage", "damaged", "missing", "lost item", "broken", "scratch", "wican"]),
    ("Complaint",        ["complaint", "formally complain", "unacceptable", "misled", "misleading", "appalling", "disgrace", "ombudsman", "trading standards", "chargeback"]),
    ("Cancellation / refund", ["cancel", "cancellation", "refund", "withdraw", "no longer require"]),
    ("Billing / payment", ["invoice", "inv-", "payment", "outstanding", "overdue", "direct debit", "gocardless", "charge", "fee", "£", "advance payment", "free storage", "free week", "statement", "pay "]),
    ("Access / visit",   ["access", "visit", "access code", "padlock", "entry", "get in", "opening hours"]),
    ("E-sign / contract", ["e-sign", "esign", "sign", "terms and conditions", "t&c", "document", "signature", "authorisation", "clause"]),
    ("Booking / collection", ["booking request", "new booking", "collection", "redelivery", "date change", "date update", "unit", "move in", "move-in", "upsize", "container"]),
    ("Onboarding",       ["confirmation of goods", "goods in storage", "goods have been", "welcome", "new wican"]),
]

def theme_of(t):
    s = ((t.get("subject") or "") + " " + (t.get("reason") or "") + " " + (t.get("last_msg") or "")).lower()
    for label, keys in _THEMES:
        if any(k in s for k in keys):
            return label
    return "Other / general"


THEME_VOCAB = [name for name, _ in _THEMES] + ["Other / general"]

if __name__ == "__main__":
    import json, sys
    src = sys.argv[1] if len(sys.argv) > 1 else "/Users/scottrobinson/.anyvan/triage_feed.json"
    from collections import Counter
    d = json.load(open(src))
    ts = d.get("tickets", [])
    th = Counter(theme_of(t) for t in ts)
    pa = Counter(partner_of(t) for t in ts)
    print(f"{len(ts)} tickets")
    print("THEMES:", dict(th.most_common()))
    print("PARTNERS:", dict(pa.most_common()))
