"""
Google Sheets inventory tracker for the Pokemon Card eBay Lister.

Sheet columns (row 1 = headers, frozen):
  A  Date Listed   B  Card Name   C  Set        D  Number    E  Condition
  F  List Price    G  Shipping    H  eBay URL    I  Status
  J  Sold Price    K  Sold Date   L  SKU         M  Offer ID  N  Chat ID

Columns M and N are only filled for eBay listings (add_listing).
They are used by /remove so active listings survive Render redeploys.

Set GOOGLE_SHEETS_CREDENTIALS (service-account JSON as a string) and
GOOGLE_SHEETS_ID in your environment to enable this module.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

HEADERS = [
    "Date Listed", "Card Name", "Set", "Number", "Condition",
    "List Price", "Shipping", "eBay URL", "Status",
    "Sold Price", "Sold Date", "SKU", "Offer ID", "Chat ID",
]

# 1-based column indices keyed by header name
_COL = {h: i + 1 for i, h in enumerate(HEADERS)}

# Lazy module-level client — created once
_ws_cache = None


def _is_configured() -> bool:
    has_creds = bool(
        os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE") or os.getenv("GOOGLE_SHEETS_CREDENTIALS")
    )
    return has_creds and bool(os.getenv("GOOGLE_SHEETS_ID"))


def _load_creds_info() -> dict:
    """Load service account JSON from file path or inline env var."""
    path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE")
    if path:
        with open(path) as f:
            return json.load(f)
    return json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])


def _get_worksheet():
    global _ws_cache
    if _ws_cache is not None:
        return _ws_cache

    import gspread
    from google.oauth2.service_account import Credentials

    creds_info = _load_creds_info()
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(os.environ["GOOGLE_SHEETS_ID"])
    ws = sh.sheet1
    _ensure_headers(ws)
    _ws_cache = ws
    return ws


def _reset_cache() -> None:
    global _ws_cache
    _ws_cache = None


def _col_letter(n: int) -> str:
    """Convert 1-based column number to letter (1→A, 14→N, etc.)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _ensure_headers(ws) -> None:
    first_row = ws.row_values(1)
    last_col = _col_letter(len(HEADERS))
    if first_row[:len(HEADERS)] != HEADERS:
        ws.update(f"A1:{last_col}1", [HEADERS])
        ws.format(f"A1:{last_col}1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
        })
        ws.freeze(rows=1)


def _shipping_label(price: float) -> str:
    if price <= 30:
        return "Standard Envelope ($1)"
    elif price <= 100:
        return "Ground Advantage ($4)"
    else:
        return "Priority Mail ($10)"


def _find_sku_row(ws, sku: str) -> Optional[int]:
    """Return 1-based row number for the given SKU, or None."""
    col_data = ws.col_values(_COL["SKU"])
    for idx, val in enumerate(col_data):
        if val == sku:
            return idx + 1
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_inventory(card_info: dict, price: float) -> None:
    """Append a row for a card logged to inventory but NOT listed on eBay."""
    if not _is_configured():
        raise RuntimeError(
            "Google Sheets is not configured — "
            "set GOOGLE_SHEETS_CREDENTIALS and GOOGLE_SHEETS_ID."
        )
    try:
        ws = _get_worksheet()
        ws.append_row(
            [
                datetime.now().strftime("%Y-%m-%d"),
                card_info.get("card_name", ""),
                card_info.get("set_name", ""),
                card_info.get("card_number", ""),
                card_info.get("condition_label", ""),
                f"${price:.2f}",
                _shipping_label(price),
                "",          # no eBay URL
                "In Stock",  # Status
                "",          # Sold Price
                "",          # Sold Date
                "",          # SKU (no eBay listing)
            ],
            value_input_option="USER_ENTERED",
        )
        logger.info("Sheet: added inventory item '%s'", card_info.get("card_name", ""))
    except Exception:
        _reset_cache()
        raise


def add_inventory_batch(cards: list[dict]) -> int:
    """
    Append multiple inventory rows in one shot (batch photo mode).
    Raises on failure so the caller can report the error to the user.
    """
    if not _is_configured():
        raise RuntimeError(
            "Google Sheets is not configured — "
            "set GOOGLE_SHEETS_CREDENTIALS and GOOGLE_SHEETS_ID."
        )
    if not cards:
        return 0
    try:
        ws = _get_worksheet()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = []
        for card in cards:
            price = card.get("price_from_image") or 0.0
            price_str = f"${price:.2f}" if price else ""
            shipping = _shipping_label(price) if price else ""
            rows.append([
                today,
                card.get("card_name", ""),
                card.get("set_name", ""),
                card.get("card_number", ""),
                card.get("condition_label", ""),
                price_str,
                shipping,
                "",          # no eBay URL
                "In Stock",  # Status
                "",          # Sold Price
                "",          # Sold Date
                "",          # SKU
            ])
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Sheet: batch-added %d inventory rows", len(rows))
        return len(rows)
    except Exception:
        _reset_cache()
        raise


def get_in_stock() -> list[dict]:
    """Return all rows where Status = 'In Stock', newest first."""
    if not _is_configured():
        return []
    try:
        ws = _get_worksheet()
        all_rows = ws.get_all_values()
        result = []
        for i, row in enumerate(all_rows[1:], start=2):  # row 1 = header; data starts at 2
            while len(row) < len(HEADERS):
                row.append("")
            if row[_COL["Status"] - 1] == "In Stock":
                result.append({
                    "row":       i,
                    "card_name": row[_COL["Card Name"] - 1],
                    "set_name":  row[_COL["Set"] - 1],
                    "condition": row[_COL["Condition"] - 1],
                    "price":     row[_COL["List Price"] - 1],
                    "date":      row[_COL["Date Listed"] - 1],
                })
        return list(reversed(result))  # newest first
    except Exception:
        logger.exception("Sheet: failed to fetch in-stock items")
        return []


def mark_removed_row(row: int) -> None:
    """Update Status to 'Removed' for a given 1-based sheet row number."""
    if not _is_configured():
        return
    try:
        ws = _get_worksheet()
        ws.update_cell(row, _COL["Status"], "Removed")
        logger.info("Sheet: marked row %d as Removed", row)
    except Exception:
        logger.exception("Sheet: failed to mark row %d as Removed", row)


def add_listing(card_info: dict, price: float, ebay_url: str, sku: str,
                offer_id: str = "", chat_id: int = 0) -> None:
    """Append a new row for a freshly created listing."""
    if not _is_configured():
        return
    try:
        ws = _get_worksheet()
        ws.append_row(
            [
                datetime.now().strftime("%Y-%m-%d"),
                card_info.get("card_name", ""),
                card_info.get("set_name", ""),
                card_info.get("card_number", ""),
                card_info.get("condition_label", ""),
                f"${price:.2f}",
                _shipping_label(price),
                ebay_url,
                "Active",
                "",          # Sold Price
                "",          # Sold Date
                sku,
                offer_id,
                str(chat_id) if chat_id else "",
            ],
            value_input_option="USER_ENTERED",
        )
        logger.info("Sheet: added listing %s", sku)
    except Exception:
        logger.exception("Sheet: failed to add listing %s", sku)


def get_active_listings(chat_id: int) -> list[dict]:
    """Return all rows with Status='Active' for this chat, newest first.
    Used by /remove so listings survive Render redeploys.
    """
    if not _is_configured():
        return []
    try:
        ws = _get_worksheet()
        all_rows = ws.get_all_values()
        result = []
        for i, row in enumerate(all_rows[1:], start=2):
            while len(row) < len(HEADERS):
                row.append("")
            if row[_COL["Status"] - 1] != "Active":
                continue
            row_chat = row[_COL["Chat ID"] - 1].strip()
            # Include rows that match this chat_id OR have no chat_id (legacy rows)
            if row_chat and row_chat != str(chat_id):
                continue
            result.append({
                "row":       i,
                "card_name": row[_COL["Card Name"] - 1],
                "set_name":  row[_COL["Set"] - 1],
                "condition": row[_COL["Condition"] - 1],
                "price":     row[_COL["List Price"] - 1],
                "sku":       row[_COL["SKU"] - 1],
                "offer_id":  row[_COL["Offer ID"] - 1],
                "ebay_url":  row[_COL["eBay URL"] - 1],
            })
        return list(reversed(result))
    except Exception:
        logger.exception("Sheet: failed to get active listings")
        return []


def get_listing_by_sku(sku: str) -> Optional[dict]:
    """Look up an active listing by SKU. Used by the sold checker."""
    if not _is_configured():
        return None
    try:
        ws = _get_worksheet()
        all_rows = ws.get_all_values()
        for i, row in enumerate(all_rows[1:], start=2):
            while len(row) < len(HEADERS):
                row.append("")
            if row[_COL["SKU"] - 1] == sku and row[_COL["Status"] - 1] == "Active":
                return {
                    "row":       i,
                    "card_name": row[_COL["Card Name"] - 1],
                    "set_name":  row[_COL["Set"] - 1],
                    "condition": row[_COL["Condition"] - 1],
                    "price":     row[_COL["List Price"] - 1],
                    "sku":       sku,
                    "offer_id":  row[_COL["Offer ID"] - 1],
                    "chat_id":   row[_COL["Chat ID"] - 1],
                }
        return None
    except Exception:
        logger.exception("Sheet: failed to look up SKU %s", sku)
        return None


def mark_sold(sku: str, sold_price: float) -> None:
    """Update Status, Sold Price, and Sold Date for a sold listing."""
    if not _is_configured():
        return
    try:
        ws = _get_worksheet()
        row = _find_sku_row(ws, sku)
        if row is None:
            logger.warning("Sheet: SKU %s not found for mark_sold", sku)
            return
        ws.update(
            f"I{row}:K{row}",
            [["Sold", f"${sold_price:.2f}", datetime.now().strftime("%Y-%m-%d")]],
        )
        logger.info("Sheet: marked sold %s @ $%.2f", sku, sold_price)
    except Exception:
        logger.exception("Sheet: failed to mark sold %s", sku)


def mark_removed(sku: str) -> None:
    """Update Status to Removed for a delisted listing."""
    if not _is_configured():
        return
    try:
        ws = _get_worksheet()
        row = _find_sku_row(ws, sku)
        if row is None:
            logger.warning("Sheet: SKU %s not found for mark_removed", sku)
            return
        ws.update_cell(row, _COL["Status"], "Removed")
        logger.info("Sheet: marked removed %s", sku)
    except Exception:
        logger.exception("Sheet: failed to mark removed %s", sku)
