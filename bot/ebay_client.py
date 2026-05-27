import base64
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from config import EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN, EBAY_BASE_URL, SELLER_ZIP_CODE

logger = logging.getLogger(__name__)

POKEMON_CATEGORY_ID = "183454"  # Pokémon TCG Individual Cards

_token_cache: dict = {"token": None, "expires_at": 0.0}
_fulfillment_token_cache: dict = {"token": None, "expires_at": 0.0}
_policies_cache: dict | None = None       # payment + return policy IDs
_fulfillment_tiers: dict | None = None    # {"envelope"|"ground"|"priority"|"default": policy_id}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_access_token() -> str:
    """Access token for sell.inventory + sell.account (listing operations)."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    creds = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        f"{EBAY_BASE_URL}/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": EBAY_REFRESH_TOKEN,
            "scope": (
                "https://api.ebay.com/oauth/api_scope/sell.inventory "
                "https://api.ebay.com/oauth/api_scope/sell.account.readonly"
            ),
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data["expires_in"]
    return _token_cache["token"]


def _get_fulfillment_token() -> str | None:
    """
    Access token for sell.fulfillment.readonly (sold-order polling).
    Returns None if the refresh token doesn't have this scope yet —
    caller should re-run get_refresh_token.py to enable it.
    """
    if _fulfillment_token_cache["token"] and time.time() < _fulfillment_token_cache["expires_at"] - 60:
        return _fulfillment_token_cache["token"]

    creds = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    try:
        r = requests.post(
            f"{EBAY_BASE_URL}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": EBAY_REFRESH_TOKEN,
                "scope": "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
            },
            timeout=15,
        )
        if r.status_code == 400:
            logger.warning(
                "sell.fulfillment.readonly not in refresh token — sold detection disabled. "
                "Re-run get_refresh_token.py to enable automatic sold notifications."
            )
            return None
        r.raise_for_status()
    except requests.HTTPError:
        return None

    data = r.json()
    _fulfillment_token_cache["token"] = data["access_token"]
    _fulfillment_token_cache["expires_at"] = time.time() + data["expires_in"]
    return _fulfillment_token_cache["token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }


# ---------------------------------------------------------------------------
# Shipping-tier fulfillment policy selection
# ---------------------------------------------------------------------------

# Price thresholds for shipping tiers
_TIER_ENVELOPE_MAX = 30.0   # $0–$30  → eBay Standard Envelope
_TIER_GROUND_MAX   = 100.0  # $30–$100 → USPS Ground Advantage
#                            # $100+   → USPS Priority Mail

# Keywords used to identify policies by name or service code
_TIER_KEYWORDS = {
    "envelope": ["envelope", "first class", "firstclass", "ese", "standard env"],
    "ground":   ["ground advantage", "groundadvantage", "ground"],
    "priority": ["priority"],
}


def _classify_policy(policy: dict) -> str | None:
    """Return 'envelope', 'ground', or 'priority' — or None if unclassifiable."""
    name = policy.get("name", "").lower()
    for tier, kws in _TIER_KEYWORDS.items():
        if any(k in name for k in kws):
            return tier

    # Fall back to service-code inspection
    for opt in policy.get("shippingOptions", []):
        for svc in opt.get("shippingServices", []):
            code = svc.get("shippingServiceCode", "").lower()
            if any(k in code for k in _TIER_KEYWORDS["envelope"]):
                return "envelope"
            if any(k in code for k in _TIER_KEYWORDS["ground"]):
                return "ground"
            if "priority" in code:
                return "priority"
    return None


def _load_fulfillment_tiers() -> dict:
    """
    Fetch all fulfillment policies and classify into shipping tiers.
    Returns a dict like:
      {"envelope": "...", "ground": "...", "priority": "...", "default": "..."}
    Any tier not found maps to the "default" (first) policy.
    """
    global _fulfillment_tiers
    if _fulfillment_tiers:
        return _fulfillment_tiers

    r = requests.get(
        f"{EBAY_BASE_URL}/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    policies = r.json().get("fulfillmentPolicies", [])
    if not policies:
        raise RuntimeError(
            "No fulfillment policies found on your eBay account. "
            "Create one at seller.ebay.com."
        )

    result: dict = {"default": policies[0]["fulfillmentPolicyId"]}
    for p in policies:
        tier = _classify_policy(p)
        if tier and tier not in result:
            result[tier] = p["fulfillmentPolicyId"]
            logger.info("Fulfillment tier '%s' → policy '%s' (%s)",
                        tier, p["fulfillmentPolicyId"], p.get("name", ""))

    if len(result) == 1:
        logger.warning(
            "Could not classify fulfillment policies into tiers by name/service code. "
            "All tiers will use the default policy. "
            "Rename your eBay fulfillment policies to include 'Envelope', 'Ground', "
            "and 'Priority' so the bot can select automatically."
        )
    _fulfillment_tiers = result
    return result


def _fulfillment_policy_id_for_price(price: float) -> str:
    tiers = _load_fulfillment_tiers()
    if price <= _TIER_ENVELOPE_MAX:
        return tiers.get("envelope") or tiers["default"]
    elif price <= _TIER_GROUND_MAX:
        return tiers.get("ground") or tiers["default"]
    else:
        return tiers.get("priority") or tiers["default"]


# ---------------------------------------------------------------------------
# Account policies (payment / return only — fulfillment handled above)
# ---------------------------------------------------------------------------

def _get_policies() -> dict:
    global _policies_cache
    if _policies_cache:
        return _policies_cache

    base = f"{EBAY_BASE_URL}/sell/account/v1"
    specs = [
        ("payment_policy", "paymentPolicies", "paymentPolicyId"),
        ("return_policy",  "returnPolicies",  "returnPolicyId"),
    ]

    result: dict = {}
    for endpoint, resp_key, id_field in specs:
        r = requests.get(
            f"{base}/{endpoint}?marketplace_id=EBAY_US",
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get(resp_key, [])
        if not items:
            raise RuntimeError(
                f"No {endpoint.replace('_', ' ')} found on your eBay account. "
                "Create one at seller.ebay.com before listing."
            )
        result[endpoint] = items[0][id_field]

    _policies_cache = result
    return result


# ---------------------------------------------------------------------------
# Merchant location
# ---------------------------------------------------------------------------

def _ensure_location() -> str:
    r = requests.get(
        f"{EBAY_BASE_URL}/sell/inventory/v1/location",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    locations = r.json().get("locations", [])
    if locations:
        return locations[0]["merchantLocationKey"]

    key = "main-location"
    r = requests.post(
        f"{EBAY_BASE_URL}/sell/inventory/v1/location/{key}",
        headers=_headers(),
        json={
            "location": {
                "address": {"country": "US", "postalCode": SELLER_ZIP_CODE}
            },
            "locationTypes": ["WAREHOUSE"],
            "name": "Main Location",
            "merchantLocationStatus": "ENABLED",
        },
        timeout=15,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Failed to create eBay location: {r.status_code} {r.text}")
    return key


# ---------------------------------------------------------------------------
# Listing lifecycle
# ---------------------------------------------------------------------------

def create_listing(card_info: dict, image_urls: list, price: float) -> dict:
    """
    Create and publish an eBay listing.
    image_urls: list of public HTTPS image URLs (first = front, second = back).
    Returns dict with keys: sku, offer_id, listing_id, ebay_url.
    """
    sku = f"POKE-{uuid.uuid4().hex[:12].upper()}"
    location_key = _ensure_location()
    policies = _get_policies()
    fulfillment_policy_id = _fulfillment_policy_id_for_price(price)

    # Category 183454 (Pokémon TCG Individual Cards) uses special condition IDs:
    #   2750 = Graded   3000 = Used (generic)   4000 = Ungraded
    # All our ungraded listings use 4000 ("Ungraded") = USED_VERY_GOOD enum.
    # Condition 4000 REQUIRES conditionDescriptor "Card Condition" (ID 40001).
    inventory_condition = "USED_VERY_GOOD"  # maps to condition ID 4000 = Ungraded

    _condition_enum = card_info.get("condition_enum", "USED_VERY_GOOD")
    _descriptor_value_map = {
        "NEW":                      "400010",  # Near mint or better
        "LIKE_NEW":                 "400010",
        "USED_EXCELLENT":           "400010",
        "USED_VERY_GOOD":           "400015",  # Lightly played
        "USED_GOOD":                "400016",  # Moderately played
        "USED_ACCEPTABLE":          "400017",  # Heavily played
        "FOR_PARTS_OR_NOT_WORKING": "400017",
    }
    descriptor_value_id = _descriptor_value_map.get(_condition_enum, "400015")
    condition_descriptors = [{"name": "40001", "values": [descriptor_value_id]}]

    _card_condition_aspect = {
        "NEW":                       "Near Mint or Better",
        "LIKE_NEW":                  "Near Mint or Better",
        "USED_EXCELLENT":            "Near Mint or Better",
        "USED_VERY_GOOD":            "Lightly Played",
        "USED_GOOD":                 "Moderately Played",
        "USED_ACCEPTABLE":           "Heavily Played",
        "FOR_PARTS_OR_NOT_WORKING":  "Damaged",
    }
    card_condition = _card_condition_aspect.get(_condition_enum, "Lightly Played")

    aspects: dict = {
        "Card Name": [card_info["card_name"]],
        "Game": ["Pokémon"],
        "Card Condition": [card_condition],
    }
    if card_info.get("set_name"):
        aspects["Set"] = [card_info["set_name"]]
    if card_info.get("card_number"):
        aspects["Card Number"] = [card_info["card_number"]]
    if card_info.get("rarity"):
        aspects["Rarity"] = [card_info["rarity"]]

    inv_body = {
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
        "condition": inventory_condition,
        "conditionDescriptors": condition_descriptors,
        "conditionDescription": card_info.get("condition_label", ""),
        "product": {
            "title": card_info["ebay_title"][:80],
            "description": card_info["ebay_description"],
            "imageUrls": image_urls,
            "aspects": aspects,
        },
    }
    logger.info("eBay inventory PUT: condition=%s descriptor=%s fulfillment=%s",
                inventory_condition, condition_descriptors, fulfillment_policy_id)
    r = requests.put(
        f"{EBAY_BASE_URL}/sell/inventory/v1/inventory_item/{sku}",
        headers=_headers(),
        json=inv_body,
        timeout=20,
    )
    logger.info("eBay inventory PUT response: %s %s", r.status_code, r.text[:500] if r.text else "")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"eBay inventory item error: {r.status_code} {r.text}")

    r = requests.post(
        f"{EBAY_BASE_URL}/sell/inventory/v1/offer",
        headers=_headers(),
        json={
            "sku": sku,
            "marketplaceId": "EBAY_US",
            "format": "FIXED_PRICE",
            "availableQuantity": 1,
            "categoryId": POKEMON_CATEGORY_ID,
            "listingDescription": card_info["ebay_description"],
            "listingPolicies": {
                "fulfillmentPolicyId": fulfillment_policy_id,
                "paymentPolicyId":     policies["payment_policy"],
                "returnPolicyId":      policies["return_policy"],
            },
            "merchantLocationKey": location_key,
            "pricingSummary": {
                "price": {"value": f"{price:.2f}", "currency": "USD"}
            },
        },
        timeout=20,
    )
    if r.status_code != 201:
        raise RuntimeError(f"eBay offer error: {r.status_code} {r.text}")
    offer_id = r.json()["offerId"]

    r = requests.post(
        f"{EBAY_BASE_URL}/sell/inventory/v1/offer/{offer_id}/publish/",
        headers=_headers(),
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"eBay publish error: {r.status_code} {r.text}")
    listing_id = r.json()["listingId"]

    return {
        "sku": sku,
        "offer_id": offer_id,
        "listing_id": listing_id,
        "ebay_url": f"https://www.ebay.com/itm/{listing_id}",
    }


def end_listing(offer_id: str) -> None:
    """Withdraw a live listing by its offer ID."""
    r = requests.post(
        f"{EBAY_BASE_URL}/sell/inventory/v1/offer/{offer_id}/withdraw",
        headers=_headers(),
        timeout=15,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"eBay withdraw error: {r.status_code} {r.text}")


# ---------------------------------------------------------------------------
# Sold-item detection (polls eBay Orders API)
# ---------------------------------------------------------------------------

def check_for_sold_items(lookback_hours: int = 25) -> list[dict]:
    """
    Return a list of recently sold items whose SKU starts with 'POKE-'.
    Each dict has: sku, order_id, sold_price (float), sold_date (str YYYY-MM-DD).
    Returns [] silently if the fulfillment scope is unavailable.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    token = _get_fulfillment_token()
    if token is None:
        return []

    try:
        r = requests.get(
            f"{EBAY_BASE_URL}/sell/fulfillment/v1/order",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={"filter": f"creationdate:[{cutoff_str}..]", "limit": 50},
            timeout=20,
        )
    except Exception as exc:
        logger.warning("Order poll request failed: %s", exc)
        return []

    if r.status_code in (401, 403):
        logger.warning(
            "sell.fulfillment.readonly scope unavailable — sold detection disabled. "
            "Re-run get_refresh_token.py to enable it."
        )
        return []
    if not r.ok:
        logger.warning("Order poll failed: %d %s", r.status_code, r.text[:300])
        return []

    sold: list[dict] = []
    for order in r.json().get("orders", []):
        for item in order.get("lineItems", []):
            sku = item.get("sku", "")
            if not sku.startswith("POKE-"):
                continue
            raw_price = item.get("lineItemCost", {}).get("value", "0")
            raw_date = order.get("creationDate", "")[:10]  # "YYYY-MM-DD"
            sold.append({
                "sku": sku,
                "order_id": order["orderId"],
                "sold_price": float(raw_price),
                "sold_date": raw_date,
            })

    if sold:
        logger.info("Order poll found %d sold item(s)", len(sold))
    return sold
