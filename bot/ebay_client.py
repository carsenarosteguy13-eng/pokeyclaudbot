import base64
import os
import time
import uuid
import requests
from config import EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_REFRESH_TOKEN, EBAY_BASE_URL, SELLER_ZIP_CODE

POKEMON_CATEGORY_ID = "183454"  # Pokémon TCG Individual Cards

_token_cache: dict = {"token": None, "expires_at": 0.0}
_policies_cache: dict | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_access_token() -> str:
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


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }


# ---------------------------------------------------------------------------
# Account policies (fulfillment / payment / return)
# ---------------------------------------------------------------------------

def _get_policies() -> dict:
    global _policies_cache
    if _policies_cache:
        return _policies_cache

    base = f"{EBAY_BASE_URL}/sell/account/v1"
    specs = [
        ("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId"),
        ("payment_policy",     "paymentPolicies",     "paymentPolicyId"),
        ("return_policy",      "returnPolicies",       "returnPolicyId"),
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

def create_listing(card_info: dict, image_url: str, price: float) -> dict:
    """
    Create and publish an eBay listing.
    Returns dict with keys: sku, offer_id, listing_id, ebay_url.
    """
    sku = f"POKE-{uuid.uuid4().hex[:12].upper()}"
    location_key = _ensure_location()
    policies = _get_policies()

    # Build aspects for better search visibility
    aspects: dict = {
        "Card Name": [card_info["card_name"]],
        "Game": ["Pokémon"],
    }
    if card_info.get("set_name"):
        aspects["Set"] = [card_info["set_name"]]
    if card_info.get("card_number"):
        aspects["Card Number"] = [card_info["card_number"]]
    if card_info.get("rarity"):
        aspects["Rarity"] = [card_info["rarity"]]

    # Inventory item
    r = requests.put(
        f"{EBAY_BASE_URL}/sell/inventory/v1/inventory_item/{sku}",
        headers=_headers(),
        json={
            "availability": {"shipToLocationAvailability": {"quantity": 1}},
            "condition": card_info["condition_enum"],
            "conditionDescription": card_info.get("condition_label", ""),
            "product": {
                "title": card_info["ebay_title"][:80],
                "description": card_info["ebay_description"],
                "imageUrls": [image_url],
                "aspects": aspects,
            },
        },
        timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"eBay inventory item error: {r.status_code} {r.text}")

    # Offer
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
                "fulfillmentPolicyId": policies["fulfillment_policy"],
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

    # Publish
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
