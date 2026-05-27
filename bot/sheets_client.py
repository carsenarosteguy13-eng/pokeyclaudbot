"""
Google Sheets inventory tracker for the Pokemon Card eBay Lister.

Sheet columns (row 1 = headers, frozen):
  A  Date Listed   B  Card Name   C  Set        D  Number    E  Condition
  F  List Price    G  Shipping    H  eBay URL    I  Status
  J  Sold Price    K  Sold Date   L  SKU

Set GOOGLE_SHEETS_CREDENTIALS (service-account JSON as a string) and
GOOGLE_SHEETS_ID in your environment to enable this module.
All public functions silently no-op when credentials are absent.
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
    "Sold Price", "Sold Date", "SKU",
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


def _ensure_headers(ws) -> None:
    first_row = ws.row_values(1)
    if first_row != HEADERS:
        ws.update("A1:L1", [HEADERS])
        ws.format("A1:L1", {
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

def add_listing(card_info: dict, price: float, ebay_url: str, sku: str) -> None:
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
                "",
                "",
                sku,
            ],
            value_input_option="USER_ENTERED",
        )
        logger.info("Sheet: added listing %s", sku)
    except Exception:
        logger.exception("Sheet: failed to add listing %s", sku)


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
