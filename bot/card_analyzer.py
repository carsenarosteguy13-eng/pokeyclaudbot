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


_COND_ABBREV = {
    "NEW":                      "NM",
    "LIKE_NEW":                 "NM",
    "USED_EXCELLENT":           "NM",
    "USED_VERY_GOOD":           "LP",
    "USED_GOOD":                "MP",
    "USED_ACCEPTABLE":          "HP",
    "FOR_PARTS_OR_NOT_WORKING": "DMG",
}


def _build_title(info: dict) -> str:
    """Build an eBay title (≤80 chars) from card fields."""
    abbrev = _COND_ABBREV.get(info.get("condition_enum", ""), "")
    parts = ["Pokemon Card", info.get("card_name", "Unknown")]
    if info.get("card_number"):
        parts.append(info["card_number"])
    if info.get("set_name"):
        parts.append(info["set_name"])
    if info.get("rarity"):
        parts.append(info["rarity"])
    if info.get("is_holo") and "Holo" not in " ".join(parts):
        parts.append("Holo")
    if abbrev:
        parts.append(abbrev)
    title = " ".join(parts)
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]  # trim at last space
    return title


def _build_description(info: dict, caption: str = "") -> str:
    """Build an eBay HTML description from card fields."""
    title      = info.get("ebay_title", "")
    card_name  = info.get("card_name", "")
    set_name   = info.get("set_name", "")
    number     = info.get("card_number", "")
    condition  = info.get("condition_label", "")
    rarity     = info.get("rarity", "")

    lines = [f"<b>{title}</b><br><br>"]

    card_desc = card_name
    if rarity:
        card_desc += f" ({rarity})"
    set_part = ""
    if set_name and number:
        set_part = f" from the {set_name} set, card number {number}"
    elif set_name:
        set_part = f" from the {set_name} set"

    lines.append(
        f"This listing is for a <b>{condition}</b> {card_desc}{set_part}. "
        f"The card is in {condition} condition and will ship carefully in a protective sleeve."
    )

    # Include any seller notes from the caption (beyond condition/price)
    extra = caption.strip()
    for word in ["NM", "LP", "MP", "HP", "DMG", "near mint", "lightly", "moderately",
                 "heavily", "damaged"]:
        extra = extra.replace(word, "").replace(word.upper(), "")
    extra = " ".join(extra.split())
    if extra:
        lines.append(f"<br>{extra}")

    return "".join(lines)


def analyze_card(image_bytes: bytes, caption: str = "", companion_bytes: bytes | None = None) -> dict:
    """
    Identify a Pokemon card from image bytes and generate eBay listing fields.
    companion_bytes: optional second image (card back) — when provided both images
    are sent to Claude so it can correctly identify the front regardless of order.
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

    two_image_note = ""
    two_image_field = ""
    if companion_bytes:
        two_image_note = (
            "You have been sent 2 photos of the same card. "
            "One shows the FRONT (card name, HP, artwork, moves text); "
            "the other shows the BACK (standard Pokémon TCG back design with the Pokéball logo). "
            "Analyze the FRONT photo only — fill in ALL required fields from the front card. "
            "Ignore the card back entirely.\n\n"
        )
        two_image_field = (
            "\n- first_image_is_front: boolean — true if the FIRST image above shows the card "
            "front, false if the SECOND image shows the card front."
        )

    prompt = f"""{two_image_note}Analyze this Pokemon TCG card image carefully and return a single JSON object.

IMPORTANT — look for a handwritten sticker, sticky note, or label anywhere in the photo (often on the card sleeve or corner). It may show:
  • A price: digits like "35", "$15", "8.50", "35#" (treat # as $)
  • A condition abbreviation: NM, LP, MP, HP, or DMG

Condition abbreviation meanings:
  NM  = Near Mint      → condition_enum: USED_EXCELLENT
  LP  = Lightly Played → condition_enum: USED_VERY_GOOD
  MP  = Moderately Played → condition_enum: USED_GOOD
  HP  = Heavily Played → condition_enum: USED_ACCEPTABLE
  DMG = Damaged        → condition_enum: FOR_PARTS_OR_NOT_WORKING

Required fields:
- card_name: string — full card name including suffix (e.g. "Charizard VMAX", "Pikachu V")
- set_name: string — expansion/set name (e.g. "Darkness Ablaze", "Base Set")
- card_number: string — number printed on card (e.g. "020/189")
- rarity: string — rarity symbol/label (e.g. "Ultra Rare", "Holo Rare", "Common", "Secret Rare")
- is_holo: boolean
- condition_enum: string — one of: NEW, USED_EXCELLENT, USED_VERY_GOOD, USED_GOOD, USED_ACCEPTABLE, FOR_PARTS_OR_NOT_WORKING. If a condition abbreviation sticker is visible use that mapping above; otherwise use the caption hint or infer from the card's visual condition.
- condition_label: string — human-readable condition matching the enum (e.g. "Near Mint", "Lightly Played")
- condition_known: boolean — true if condition came from a visible sticker/note OR from the caption; false if guessing from image alone
- price_from_image: number or null — the numeric price from any visible sticker/note (digits only, no $ sign). If no price is visible return null.
- multi_card: boolean — true if this image contains MORE THAN ONE Pokémon card face visible (i.e. a batch/grid photo). false if it shows a single card.{two_image_field}{condition_hint}

Return ONLY the JSON object, no markdown fences or other text."""

    content: list = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": image_b64,
            },
        },
    ]
    if companion_bytes:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(companion_bytes).decode("utf-8"),
            },
        })
    content.append({"type": "text", "text": prompt})

    response = _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    # Strip accidental markdown fences if Claude adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    info = json.loads(raw.strip())

    # Build title and description in Python so condition is always accurate
    info["ebay_title"] = _build_title(info)
    info["ebay_description"] = _build_description(info, caption)
    return info


def analyze_batch(image_bytes: bytes) -> list[dict]:
    """
    Identify multiple Pokémon cards from a single photo (6-9 cards laid out together).
    Returns a list of card dicts, each with:
      card_name, set_name, card_number, rarity, condition_label,
      condition_known, price_from_image (float or None).
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """\
You are analyzing a photo containing multiple Pokémon TCG cards laid out together.

STEP 1 — COUNT: Look at the entire image carefully. Count every card visible, \
including ones near the edges or partially cut off. Remember that number.

STEP 2 — IDENTIFY EACH ONE: Starting from the top-left, work across each row \
left-to-right, then move to the next row. For EVERY card:
  a) Read the card name (including suffix: V, VMAX, VSTAR, GX, EX, TAG TEAM, etc.)
  b) Read the set name and card number at the bottom of the card
  c) Note the rarity (Ultra Rare, Holo Rare, Common, Rare, Secret Rare, etc.)
  d) Look for a handwritten sticker or label on the card or its sleeve showing:
       • A price: "35", "$15", "8.50", "35#" — treat # as $
       • A condition code: NM, LP, MP, HP, or DMG
  e) If no condition sticker: estimate from the card's appearance

Condition meanings:
  NM  = Near Mint
  LP  = Lightly Played
  MP  = Moderately Played
  HP  = Heavily Played
  DMG = Damaged

STEP 3 — VERIFY: Before writing your answer, confirm your "cards" array has \
one entry for every card you counted in Step 1. If it doesn't, go back and \
find the missing ones.

Return a single JSON object:
{
  "cards": [
    {
      "card_name": "full name including suffix",
      "set_name": "expansion set name",
      "card_number": "number on card",
      "rarity": "Ultra Rare / Holo Rare / Common / etc.",
      "condition_label": "Near Mint",
      "condition_known": true,
      "price_from_image": 25.00
    }
  ]
}

Rules:
- price_from_image must be null if no price sticker is visible (do NOT guess a price)
- condition_known must be false if you are estimating from appearance
- Include EVERY card — even partially obscured ones (best guess is fine)
- The "cards" array length MUST equal the count from Step 1

Return ONLY the JSON object, no markdown fences or other text."""

    response = _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
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
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    return data.get("cards", [])
