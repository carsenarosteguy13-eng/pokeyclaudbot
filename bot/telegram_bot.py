"""
Pokemon Card eBay Lister — Telegram Bot

Conversation flow:
  1. User sends 1 or 2 photos together (album = front + back).
     Telegram delivers album photos as sequential updates; the back photo
     is captured when it arrives in whatever state we're in.
  2. Optional sticker with condition (NM/LP/MP/HP/DMG) and price → skip those steps
  3. Bot asks for condition if not supplied
  4. Bot asks for price if not supplied
  5. /confirm → publish eBay listing
  6. /remove → withdraw a live listing
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import card_analyzer
import ebay_client
import image_uploader
import listing_store
from config import TELEGRAM_BOT_TOKEN

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

WAITING_CONDITION, WAITING_PRICE, CONFIRMING = range(3)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Pokemon Card eBay Lister\n\n"
        "Send 1 or 2 photos of a Pokemon card (front, or front + back together).\n"
        "Put a sticker on the card with condition (NM/LP/MP/HP/DMG) and price for instant listing.\n\n"
        "Commands:\n"
        "  /listings — view your active eBay listings\n"
        "  /remove   — take a listing down\n"
        "  /cancel   — cancel a listing in progress"
    )


# ---------------------------------------------------------------------------
# Group -1 handler: pre-cache all album photos before ConversationHandler runs
# ---------------------------------------------------------------------------

async def pre_album_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs in handler group -1 (before the ConversationHandler).
    For every photo that has a media_group_id, download and cache the bytes
    keyed by (media_group_id, message_id).  photo_received will harvest the
    companion photo from this cache after the slow Claude API call finishes.
    """
    if not update.message or not update.message.photo:
        return
    mgid = update.message.media_group_id
    if not mgid:
        return
    mid = update.message.message_id
    cache: dict = context.user_data.setdefault("_album_cache", {})
    if mgid in cache and mid in cache[mgid]:
        return  # already cached
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = bytes(await photo_file.download_as_bytearray())
    cache.setdefault(mgid, {})[mid] = photo_bytes
    logger.info("Pre-cached album photo mgid=%s msg_id=%s (%d bytes)",
                mgid, mid, len(photo_bytes))


def _preview_text(info: dict, price: float, has_back: bool) -> str:
    return (
        "Ready to list:\n\n"
        f"Card:      {info['card_name']} ({info['set_name']})\n"
        f"Number:    {info['card_number']}\n"
        f"Condition: {info['condition_label']}\n"
        f"Price:     ${price:.2f}\n"
        f"Photos:    {'front + back' if has_back else 'front only'}\n"
        f"Title:     {info['ebay_title']}\n\n"
        "Send /confirm to post on eBay, or /cancel to abort."
    )


# ---------------------------------------------------------------------------
# Listing conversation
# ---------------------------------------------------------------------------

async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    status = await update.message.reply_text("Analyzing your card...")

    photo_file = await update.message.photo[-1].get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())
    caption = (update.message.caption or "").strip()

    try:
        info = await asyncio.to_thread(card_analyzer.analyze_card, image_bytes, caption)
    except Exception as exc:
        logger.exception("Card analysis failed")
        await status.edit_text(f"Couldn't analyze the card: {exc}")
        return ConversationHandler.END

    # After the slow Claude call, check the pre-cache for a companion back photo
    # (pre_album_cache in group -1 runs before us and downloads all album photos).
    back_image_bytes: bytes | None = None
    mgid = update.message.media_group_id
    front_mid = update.message.message_id
    if mgid:
        album: dict = context.user_data.get("_album_cache", {}).get(mgid, {})
        for mid, bts in album.items():
            if mid != front_mid:
                back_image_bytes = bts
                logger.info("Harvested back photo from pre-cache mgid=%s msg_id=%s", mgid, mid)
                break

    context.user_data["pending"] = {
        "card_info": info,
        "image_bytes": image_bytes,
        "back_image_bytes": back_image_bytes,
        "_media_group_id": mgid,
    }

    card_line = (
        f"{info['card_name']} — {info['set_name']} "
        f"#{info['card_number']} ({info['rarity']})"
    )
    detected_parts = []
    if info.get("condition_known"):
        detected_parts.append(f"Condition: {info['condition_label']}")
    price_img = info.get("price_from_image")
    if price_img:
        detected_parts.append(f"Price: ${float(price_img):.2f}")
    detected_line = (" — detected: " + ", ".join(detected_parts)) if detected_parts else ""

    if price_img:
        context.user_data["pending"]["price"] = float(price_img)

    if not info.get("condition_known"):
        await status.edit_text(
            f"Card identified: {card_line}{detected_line}\n\n"
            "What's the condition?\n"
            "Near Mint / Lightly Played / Moderately Played / Heavily Played / Poor"
        )
        return WAITING_CONDITION

    if price_img:
        await status.edit_text(
            f"Card identified: {card_line}{detected_line}\n\n"
            + _preview_text(info, float(price_img), back_image_bytes is not None)
        )
        return CONFIRMING

    await status.edit_text(
        f"Card identified: {card_line}{detected_line}\n"
        f"Condition: {info['condition_label']}\n"
        f"Title: {info['ebay_title']}\n\n"
        "What price would you like? (USD, e.g. 15.00)"
    )
    return WAITING_PRICE


async def photo_in_state(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int = WAITING_CONDITION) -> int:
    """
    Called when a photo arrives in WAITING_CONDITION / WAITING_PRICE / CONFIRMING.
    Treat every new photo as starting a fresh card listing.
    """
    return await photo_received(update, context)


async def condition_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    condition_text = update.message.text.strip()
    enum, label = card_analyzer.parse_condition(condition_text)

    pending = context.user_data.get("pending", {})
    info = pending.get("card_info", {})
    info["condition_enum"] = enum
    info["condition_label"] = label
    context.user_data["pending"]["card_info"] = info

    price_img = info.get("price_from_image")
    if price_img:
        context.user_data["pending"]["price"] = float(price_img)

    if price_img:
        has_back = pending.get("back_image_bytes") is not None
        await update.message.reply_text(
            f"Condition set to: {label}\n\n"
            + _preview_text(info, float(price_img), has_back)
        )
        context.user_data["_state"] = CONFIRMING
        return CONFIRMING

    card_line = f"{info['card_name']} — {info['set_name']} #{info['card_number']}"
    await update.message.reply_text(
        f"Condition set to: {label}\n\n"
        f"Card: {card_line}\n"
        f"Title: {info['ebay_title']}\n\n"
        "What price would you like? (USD, e.g. 15.00)"
    )
    context.user_data["_state"] = WAITING_PRICE
    return WAITING_PRICE


async def price_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().lstrip("$").strip()
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Please enter a valid price (e.g. 15.00 or $15)")
        return WAITING_PRICE

    pending = context.user_data.get("pending", {})
    info = pending.get("card_info", {})
    context.user_data["pending"]["price"] = price
    has_back = pending.get("back_image_bytes") is not None

    await update.message.reply_text(_preview_text(info, price, has_back))
    context.user_data["_state"] = CONFIRMING
    return CONFIRMING


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pending = context.user_data.get("pending", {})
    info = pending.get("card_info")
    image_bytes = pending.get("image_bytes")
    back_image_bytes = pending.get("back_image_bytes")
    price = pending.get("price")

    if not (info and image_bytes and price):
        await update.message.reply_text("Session expired. Please send the photo again.")
        return ConversationHandler.END

    status = await update.message.reply_text("Uploading images and creating eBay listing...")

    try:
        image_url = await asyncio.to_thread(image_uploader.upload_image, image_bytes)
        image_urls = [image_url]
        if back_image_bytes:
            back_url = await asyncio.to_thread(image_uploader.upload_image, back_image_bytes)
            image_urls.append(back_url)
        result = await asyncio.to_thread(ebay_client.create_listing, info, image_urls, price)
    except Exception as exc:
        logger.exception("Listing creation failed")
        await status.edit_text(f"Failed to create listing: {exc}")
        return ConversationHandler.END

    listing_store.save(
        chat_id=update.effective_chat.id,
        card_name=info["card_name"],
        set_name=info.get("set_name", ""),
        sku=result["sku"],
        offer_id=result["offer_id"],
        listing_id=result["listing_id"],
        price=price,
        condition=info.get("condition_label", ""),
        ebay_url=result["ebay_url"],
    )

    context.user_data.pop("pending", None)
    context.user_data.pop("_state", None)
    await status.edit_text(
        f"Listed!\n\n"
        f"{info['card_name']} ({info['set_name']})\n"
        f"Condition: {info['condition_label']}  |  Price: ${price:.2f}\n\n"
        f"{result['ebay_url']}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pending", None)
    context.user_data.pop("_state", None)
    await update.message.reply_text("Listing cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /listings
# ---------------------------------------------------------------------------

async def cmd_listings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = listing_store.get_all(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no active listings.")
        return
    lines = ["Your active listings:\n"]
    for r in rows:
        lines.append(
            f"• {r['card_name']} ({r['set_name']}) — ${r['price']:.2f} [{r['condition']}]\n"
            f"  {r['ebay_url']}"
        )
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /remove
# ---------------------------------------------------------------------------

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = listing_store.get_all(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no active listings to remove.")
        return
    keyboard = [
        [InlineKeyboardButton(
            f"{r['card_name']} — ${r['price']:.2f}",
            callback_data=f"remove:{r['id']}",
        )]
        for r in rows
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="remove:cancel")])
    await update.message.reply_text(
        "Select a listing to remove from eBay:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    payload = query.data.split(":", 1)[1]
    if payload == "cancel":
        await query.edit_message_text("Removal cancelled.")
        return
    db_id = int(payload)
    listing = listing_store.get_by_id(db_id)
    if not listing:
        await query.edit_message_text("Listing not found (already removed?).")
        return
    await query.edit_message_text(f"Removing {listing['card_name']}...")
    try:
        await asyncio.to_thread(ebay_client.end_listing, listing["offer_id"])
        listing_store.delete(db_id)
        await query.edit_message_text(
            f"Removed: {listing['card_name']} ({listing['set_name']})\n"
            f"The eBay listing has been ended."
        )
    except Exception as exc:
        logger.exception("Failed to remove listing")
        await query.edit_message_text(f"Failed to remove listing: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    listing_store.init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    listing_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, photo_received)],
        states={
            WAITING_CONDITION: [
                MessageHandler(filters.PHOTO, photo_in_state),
                MessageHandler(filters.TEXT & ~filters.COMMAND, condition_received),
            ],
            WAITING_PRICE: [
                MessageHandler(filters.PHOTO, photo_in_state),
                MessageHandler(filters.TEXT & ~filters.COMMAND, price_received),
            ],
            CONFIRMING: [
                CommandHandler("confirm", confirm),
                MessageHandler(filters.PHOTO, photo_in_state),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=False,
        block=False,
    )

    # Group -1: pre-cache album photos before the ConversationHandler sees them
    app.add_handler(MessageHandler(filters.PHOTO, pre_album_cache), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("listings", cmd_listings))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(remove_callback, pattern=r"^remove:"))
    app.add_handler(listing_conv)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
