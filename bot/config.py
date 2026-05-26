import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
EBAY_CLIENT_ID = _require("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = _require("EBAY_CLIENT_SECRET")
EBAY_REFRESH_TOKEN = _require("EBAY_REFRESH_TOKEN")
IMGBB_API_KEY = _require("IMGBB_API_KEY")
SELLER_ZIP_CODE = os.getenv("SELLER_ZIP_CODE", "10001")
EBAY_BASE_URL = "https://api.ebay.com"
