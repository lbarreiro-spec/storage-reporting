"""
Zoho CRM Connector — customer matching for Storage Freshdesk triage.

VENDORED 17 Aug 2026 from the ClearPass repo (anyvan/connectors/zoho.py), where it
is shared with the supplier-invoice validator. It was previously OUTSIDE this repo,
which meant the triage handover shipped without the CRM enrichment layer at all.

If ClearPass and triage both end up inside AnyVan infrastructure, promote this to a
shared internal library rather than maintaining two copies. Until then, a change in
either copy must be mirrored.

Credentials are passed to the constructor — nothing is hardcoded here, which is why
this file is safe in a public repo. The knowledge pack and contract PDF are NOT.

Original header follows.

ClearPass - Zoho CRM Connector
Looks up deals to validate invoices against CRM stage.
Primary lookup: Listing_ID (7-digit AV number)
Fallback lookup: customer/deal name from invoice title
"""

import logging
import requests
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)

ZOHO_API_BASE = "https://www.zohoapis.eu/crm/v3"
ZOHO_AUTH_URL = "https://accounts.zoho.eu/oauth/v2/token"

# (connect, read) seconds — without this, a stalled Zoho socket hangs the whole
# batch indefinitely (CLOSE_WAIT). Callers treat a raised error as "no match".
REQUEST_TIMEOUT = (10, 30)

DEAL_FIELDS = (
    "Deal_Name,Stage,Listing_ID,Order_Id,Warehouse_Name1,"
    "Unit_Numbers,Agreed_price_per_week,In_Store_Date,"
    "Out_Store_Date,Confirmed_Redelivery_Date,Storage_Center,Account_Name,"
    "Email,Phone"
)


class ZohoConnector:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token = None

    def _get_access_token(self) -> str:
        resp = requests.post(ZOHO_AUTH_URL, params={
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise ValueError(f"Failed to get Zoho access token: {data}")
        self._access_token = token
        logger.info("Zoho CRM access token refreshed")
        return token

    def _headers(self) -> dict:
        if not self._access_token:
            self._get_access_token()
        return {"Authorization": f"Zoho-oauthtoken {self._access_token}"}

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f"{ZOHO_API_BASE}/{endpoint}"
        for attempt in range(2):
            try:
                resp = requests.get(url, headers=self._headers(), params=params,
                                    timeout=REQUEST_TIMEOUT)
                if resp.status_code == 401:
                    self._get_access_token()
                    resp = requests.get(url, headers=self._headers(), params=params,
                                        timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt == 0:
                    logger.warning(f"Zoho request timed out/failed ({endpoint}), retrying once: {e}")
                    continue
                raise

    def find_deal_by_listing_id(self, listing_id: str) -> Optional[dict]:
        """
        PRIMARY lookup — search Zoho by Listing_ID (7-digit AV number, AV prefix stripped).
        Returns the deal dict or None.
        """
        try:
            data = self._get("Deals/search", params={
                "criteria": f"(Listing_ID:equals:{listing_id})",
                "fields": DEAL_FIELDS,
            })
            deals = data.get("data", [])
            if deals:
                d = deals[0]
                logger.info(f"Listing_ID {listing_id} → {d.get('Deal_Name')} [{d.get('Stage')}]")
                return d
            logger.warning(f"No deal found for Listing_ID: {listing_id}")
            return None
        except Exception as e:
            logger.warning(f"Listing_ID lookup failed ({listing_id}): {e}")
            return None

    def find_deal_by_customer_name(self, customer_name: str) -> Optional[dict]:
        """
        FALLBACK lookup — search by customer name when no AV listing ID present.
        Trek format is 'Surname, Firstname' — Zoho Deal_Name is 'Firstname Surname'.
        Tries multiple candidates to maximise match rate.
        Commas in names break Zoho search — always clean before querying.
        """
        import re as _re

        candidates = []
        name = customer_name.strip()

        if ',' in name:
            parts = [p.strip() for p in name.split(',', 1)]
            surname, firstname = parts[0], parts[1]
            candidates.append(f"{firstname} {surname}")  # Zoho format: Firstname Surname
            candidates.append(surname)                    # Surname only
            candidates.append(firstname)                  # Firstname only
        else:
            candidates.append(name)
            words = name.split()
            if len(words) > 1:
                candidates.append(words[-1])   # Last word (surname)
                candidates.append(words[0])    # First word

        for candidate in candidates:
            if len(candidate) < 3:
                continue
            try:
                # Use 'word' param — Zoho free-text search on Deal_Name
                # 'contains' operator returns 400; 'word' works correctly
                data = self._get("Deals/search", params={
                    "word": candidate,
                    "fields": DEAL_FIELDS,
                })
                deals = data.get("data", [])
                if deals:
                    in_store = [d for d in deals if d.get("Stage") == "In Store"]
                    result = in_store[0] if in_store else deals[0]
                    logger.info(f"Name '{customer_name}' → '{candidate}' → {result.get('Deal_Name')} [{result.get('Stage')}]")
                    return result
            except Exception as e:
                logger.debug(f"Name search failed for '{candidate}': {e}")
                continue

        logger.warning(f"No CRM match found for: {customer_name}")
        return None

    def find_deals_by_email(self, email: str) -> List[dict]:
        """
        Email lookup — Deals carry the customer Email directly. Returns ALL matching
        deals (a repeat customer can have several). Stronger key than name.
        """
        email = (email or "").strip()
        if "@" not in email:
            return []
        try:
            data = self._get("Deals/search", params={
                "criteria": f"(Email:equals:{email})",
                "fields": DEAL_FIELDS,
            })
            return data.get("data", []) or []
        except Exception as e:
            logger.debug(f"Email lookup failed ({email}): {e}")
            return []

    def find_deals_by_word(self, term: str) -> List[dict]:
        """Free-text search across the whole deal record — catches a reference (e.g. the
        REDELIVERY leg ref) that lives on the deal but isn't the Listing_ID field.
        Round-trip storage customers have 2 AV refs (in-store + redelivery)."""
        try:
            data = self._get("Deals/search", params={"word": str(term), "fields": DEAL_FIELDS})
            return data.get("data", []) or []
        except Exception as e:
            logger.debug(f"Word search failed for '{term}': {e}")
            return []

    def find_deals_by_name(self, customer_name: str) -> List[dict]:
        """
        Name lookup returning ALL candidates from the first hitting name-variant
        (so the caller can detect ambiguity). Weak key — never auto-trust alone.
        """
        candidates = []
        name = (customer_name or "").strip()
        if ',' in name:
            parts = [p.strip() for p in name.split(',', 1)]
            surname, firstname = parts[0], parts[1]
            candidates += [f"{firstname} {surname}", surname, firstname]
        else:
            candidates.append(name)
            words = name.split()
            if len(words) > 1:
                candidates += [words[-1], words[0]]
        for candidate in candidates:
            if len(candidate) < 3:
                continue
            try:
                data = self._get("Deals/search", params={"word": candidate, "fields": DEAL_FIELDS})
                deals = data.get("data", []) or []
                if deals:
                    return deals
            except Exception as e:
                logger.debug(f"Name search failed for '{candidate}': {e}")
                continue
        return []

    @staticmethod
    def _name_agrees(deal: dict, name: str) -> bool:
        """Loose token check: do the deal's name and the supplied name share a surname-ish token?"""
        dn = (deal.get("Deal_Name") or "").lower()
        toks = [t for t in name.lower().replace(',', ' ').split() if len(t) >= 3]
        return any(t in dn for t in toks)

    def match_customer(self, listing_id: str = None, email: str = None,
                       name: str = None) -> dict:
        """
        Confidence-gated customer match (policy locked 9 Jun 2026).
        Returns {deal, confidence, method, candidates}.
        - listing-id match (optionally name-confirmed) -> HIGH   (auto-write OK)
        - email match                                  -> MEDIUM (auto-write OK)
        - name-only, single hit                        -> LOW    (human-gate)
        - name, multiple hits                           -> AMBIGUOUS (human-gate)
        - nothing                                       -> NONE
        """
        # 1. Listing ID — strongest. If the exact Listing_ID misses, the ref may be the
        # REDELIVERY leg (round-trip customers have 2 AV refs) — fall back to a free-text
        # search that finds the ref wherever it sits on the deal record.
        if listing_id:
            lid = str(listing_id).lstrip("AVav")
            d = self.find_deal_by_listing_id(lid)
            if d:
                method = "listing-id + name agree" if (name and self._name_agrees(d, name)) else "listing-id"
                return {"deal": d, "confidence": "high", "method": method, "candidates": 1}
            hits = self.find_deals_by_word(lid)
            if len(hits) == 1:
                return {"deal": hits[0], "confidence": "medium",
                        "method": "listing-ref word match (likely redelivery leg)", "candidates": 1}
            if len(hits) > 1:
                return {"deal": None, "confidence": "ambiguous",
                        "method": "listing-ref word (multiple matches)", "candidates": len(hits)}
        # 2. Email — strong
        if email:
            deals = self.find_deals_by_email(email)
            if deals:
                in_store = [x for x in deals if x.get("Stage") == "In Store"]
                pick = in_store[0] if in_store else deals[0]
                method = "email" if len(deals) == 1 else "email (multiple deals → prefer In Store)"
                return {"deal": pick, "confidence": "medium", "method": method, "candidates": len(deals)}
        # 3. Name — weak, gate it
        if name:
            deals = self.find_deals_by_name(name)
            if len(deals) == 1:
                return {"deal": deals[0], "confidence": "low", "method": "name-only", "candidates": 1}
            if len(deals) > 1:
                return {"deal": None, "confidence": "ambiguous", "method": "name (multiple matches)", "candidates": len(deals)}
        return {"deal": None, "confidence": "none", "method": None, "candidates": 0}

    def find_deals_by_supplier(self, supplier_name: str) -> List[dict]:
        """All deals for a given storage supplier (warehouse company)."""
        try:
            data = self._get("Deals", params={
                "criteria": f"(Account_Name:equals:{supplier_name})",
                "fields": DEAL_FIELDS,
            })
            deals = data.get("data", [])
            logger.info(f"Found {len(deals)} deals for supplier: {supplier_name}")
            return deals
        except Exception as e:
            logger.warning(f"Supplier lookup failed for '{supplier_name}': {e}")
            return []

    def find_active_storage_deals(self, supplier_name: str) -> List[dict]:
        """Returns only 'In Store' deals for a given supplier."""
        deals = self.find_deals_by_supplier(supplier_name)
        active = [d for d in deals if d.get("Stage") == "In Store"]
        logger.info(f"Active 'In Store' deals for '{supplier_name}': {len(active)}")
        return active

    def health_check(self) -> bool:
        try:
            self._get("Deals", params={"fields": "Deal_Name", "$per_page": 1})
            logger.info("Zoho CRM health check: OK")
            return True
        except Exception as e:
            logger.error(f"Zoho CRM health check failed: {e}")
            return False


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import load_config
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = load_config(required=["ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_CRM_REFRESH_TOKEN"])
    zoho = ZohoConnector(cfg["ZOHO_CLIENT_ID"], cfg["ZOHO_CLIENT_SECRET"], cfg["ZOHO_CRM_REFRESH_TOKEN"])

    print("\n--- Zoho CRM Health Check ---")
    if zoho.health_check():
        print("✅ Connected")

        # Test primary lookup
        print("\n--- Primary: Listing_ID lookup ---")
        deal = zoho.find_deal_by_listing_id("9298324")
        if deal:
            print(f"  ✅ {deal.get('Deal_Name')} | {deal.get('Stage')} | {deal.get('Warehouse_Name1')}")

        # Test fallback lookup
        print("\n--- Fallback: Customer name lookup ---")
        deal2 = zoho.find_deal_by_customer_name("SMW Financial")
        if deal2:
            print(f"  ✅ {deal2.get('Deal_Name')} | {deal2.get('Stage')}")
    else:
        print("❌ Connection failed")
