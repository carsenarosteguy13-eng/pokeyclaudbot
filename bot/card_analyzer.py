import base64
import json
import anthropic
from config import ANTHROPIC_API_KEY

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Maps user-supplied condition text to eBay's ConditionEnum + display label
_CONDITION_KEYWORDS: list[tuple[list[str], str, str]] = [
    (["sealed", "psa", "bgs", "cgc", "graded"], "NEW", "Sealed/Graded"),
    (["mint", "m/m", "gem"], "NEW", "Mint"),
    (["near mint", "nm/m", "nm"], "USED_EXCELLENT", "Near Mint"),
    (["lightly played", "lightly", "excellent", " lp"], "USED_VERY_GOOD", "Lightly Played"),
    (["moderately played", "moderately", " mp", "good"], "USED_GOOD", "Moderately Played"),
    (["heavily played", "heavily", " hp", "poor"], "USED_ACCEPTABLE", "Heavily Played"),
    (["damaged", "for parts", "crease", "water"], "FOR_PARTS_OR_NOT_WORKING", "Damaged"),
]

def parse_condition(text: str) -> tuple[str, str]:
    """Return (condition_enum, condition_label) from free-text condition."""
    lower = text.lower()
    for keywords, enum, label in _CONDITION_KEYWORDS:
        if any(k in lower for k in keywords):
            return enum, label
    return "USED_GOOD", "Moderately Played"


def analyze_card(image_bytes: bytes, caption: str = "") -> dict:
    """
    Identify a Pokemon card from image bytes and generate eBay listing fields.
    Returns a dict with: card_name, set_name, card_number, rarity, is_holo,
    ebay_title (≤80 chars), ebay_description (HTML ok), condition_enum,
    condition_label, condition_known.
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    condition_hint = (
        f"\nThe seller describes the condition as: \"{caption}\""
        if caption.strip()
        else ""
    )

    prompt = f"""Analyze this Pokemon TCG card image carefully and return a single JSON object.

Required fields:
- card_name: string — full card name including suffix (e.g. "Charizard VMAX", "Pikachu V")
- set_name: string — expansion/set name (e.g. "Darkness Ablaze", "Base Set")
- card_number: string — number printed on card (e.g. "020/189")
- rarity: string — rarity symbol/label (e.g. "Ultra Rare", "Holo Rare", "Common", "Secret Rare")
- is_holo: boolean
- ebay_title: string — eBay listing title, MAXIMUM 80 CHARACTERS, include "Pokemon Card", card name, set, number, rarity. Example: "Pokemon Card Charizard VMAX 020/189 Darkness Ablaze Ultra Rare Holo NM"
- ebay_description: string — 3-4 sentence eBay listing description, may use basic HTML tags (<b>, <br>, <ul>). Mention the card name, set, number, condition, and that it ships in a protective sleeve.
- condition_enum: string — one of: NEW, USED_EXCELLENT, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE, FOR_PARTS_OR_NOT_WORKING. Use USED_EXCELLENT for Near Mint, USED_VERY_GOOD for Lightly Played, USED_GOOD for Moderately Played, USED_ACCEPTABLE for Heavily Played.
- condition_label: string — human-readable condition matching the enum (e.g. "Near Mint", "Lightly Played")
- condition_known: boolean — true if condition was stated in the caption, false if you are guessing from image alone{condition_hint}

Return ONLY the JSON object, no markdown fences or other text."""

    response = _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences if Claude adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
