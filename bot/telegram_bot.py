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
import sheets_client
from config import TELEGRAM_BOT_TOKEN

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

WAITING_CONDITION, WAITING_PRICE, CONFIRMING = range(3)
BATCH_WAITING_PHOTO, BATCH_CONFIRMING = range(3, 5)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Pokemon Card eBay Lister\n\n"
        "Send 1 or 2 photos of a card (front, or front + back together).\n"
        "Put a sticker with condition (NM/LP/MP/HP/DMG) and price for instant listing.\n\n"
        "Commands:\n"
        "  /confirm      — post the card on eBay + log to Google Sheet\n"
        "  /save         — log to Google Sheet only (no eBay listing)\n"
        "  /batch        — photograph 6-9 cards at once → log all to Google Sheet\n"
        "  /listings     — view your active eBay listings\n"
        "  /remove       — take an eBay listing down\n"
        "  /removestock  — remove an in-stock (non-eBay) card from inventory\n"
        "  /cancel       — cancel current operation"
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
        "Send /confirm to post on eBay, /save to log to inventory only, or /cancel to abort."
    )


# ---------------------------------------------------------------------------
# Listing conversation
# ---------------------------------------------------------------------------

async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    status = await update.message.reply_text("Analyzing your card...")

    photo_file = await update.message.photo[-1].get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())
    caption = (update.message.caption or "").strip()

    # For albums (front + back sent together): poll up to 1.5 s for the companion
    # to appear in the pre-cache, then send BOTH images to Claude so it always
    # identifies the front card regardless of which photo Telegram delivered first.
    mgid = update.message.media_group_id
    front_mid = update.message.message_id
    back_image_bytes: bytes | None = None

    if mgid:
        for _ in range(6):  # 6 × 0.25 s = 1.5 s max wait
            album: dict = context.user_data.get("_album_cache", {}).get(mgid, {})
            companions = {mid: bts for mid, bts in album.items() if mid != front_mid}
            if companions:
                back_image_bytes = next(iter(companions.values()))
                logger.info(
                    "Companion found for album %s before Claude call (%d bytes)",
                    mgid, len(back_image_bytes),
                )
                break
            await asyncio.sleep(0.25)
        if not back_image_bytes:
            logger.info("No companion found within 1.5 s for album %s — single-image analysis", mgid)

    try:
        # Pass companion so Claude sees both images and picks the front correctly
        info = await asyncio.to_thread(
            card_analyzer.analyze_card, image_bytes, caption, back_image_bytes
        )
    except Exception as exc:
        logger.exception("Card analysis failed")
        await status.edit_text(f"Couldn't analyze the card: {exc}")
        return ConversationHandler.END

    # Auto-detect multi-card batch photo — no /batch command needed
    if info.get("multi_card"):
        logger.info("multi_card detected — switching to batch analysis")
        await status.edit_text("Multiple cards detected — analyzing each one...")
        try:
            cards = await asyncio.to_thread(card_analyzer.analyze_batch, image_bytes)
        except Exception as exc:
            logger.exception("Batch analysis failed")
            await status.edit_text(f"Couldn't analyze the cards: {exc}")
            return ConversationHandler.END

        if not cards:
            await status.edit_text("No cards detected. Try again with a clearer photo.")
            return ConversationHandler.END

        context.user_data["batch_cards"] = cards
        lines = [f"Found {len(cards)} card(s):\n"]
        for i, card in enumerate(cards, 1):
            price = card.get("price_from_image")
            price_str = f"${price:.2f}" if price else "no price"
            cond = card.get("condition_label", "?")
            cond_flag = "" if card.get("condition_known") else " (?)"
            lines.append(
                f"{i}. {card.get('card_name', 'Unknown')} "
                f"({card.get('set_name', '?')} #{card.get('card_number', '?')})\n"
                f"   {cond}{cond_flag}  —  {price_str}"
            )
        lines.append(
            "\nCondition/price marked (?) were estimated from appearance.\n\n"
            "Send /save to add all to your Google Sheet, or /cancel to abort."
        )
        await status.edit_text("\n".join(lines))
        return BATCH_CONFIRMING

    # If Claude says the second image (companion) is actually the front, swap so
    # eBay always gets the front card as the primary (first) image.
    if back_image_bytes is not None and info.get("first_image_is_front") is False:
        image_bytes, back_image_bytes = back_image_bytes, image_bytes
        logger.info("Swapped front/back images based on Claude first_image_is_front=False")

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
    await asyncio.to_thread(
        sheets_client.add_listing, info, price, result["ebay_url"], result["sku"]
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


async def save_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Log card to Google Sheet only — no eBay listing created."""
    pending = context.user_data.get("pending", {})
    info = pending.get("card_info")
    price = pending.get("price")

    if not (info and price):
        await update.message.reply_text("Session expired. Please send the photo again.")
        return ConversationHandler.END

    await asyncio.to_thread(sheets_client.add_inventory, info, price)

    context.user_data.pop("pending", None)
    context.user_data.pop("_state", None)
    await update.message.reply_text(
        f"Saved to inventory ✓\n\n"
        f"{info['card_name']} ({info['set_name']})\n"
        f"Condition: {info['condition_label']}  |  Value: ${price:.2f}\n\n"
        "Added to your Google Sheet. Not listed on eBay."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("pending", None)
    context.user_data.pop("_state", None)
    await update.message.reply_text("Listing cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /batch — photograph 6-9 cards at once, log all to Google Sheet
# ---------------------------------------------------------------------------

async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Batch inventory mode\n\n"
        "Send ONE photo with up to 9 cards laid out face-up.\n\n"
        "For best accuracy, send as a FILE (not a photo):\n"
        "  📎 Tap the paperclip → File → choose your photo\n"
        "This skips Telegram's compression and gives Claude much clearer images.\n\n"
        "Tips:\n"
        "  • Lay cards face-up in good lighting\n"
        "  • Avoid overlapping cards\n"
        "  • Price/condition stickers will be read automatically\n\n"
        "Send the photo or file now, or /cancel to abort."
    )
    return BATCH_WAITING_PHOTO


async def batch_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    status = await update.message.reply_text("Analyzing all cards — this may take a few seconds...")

    # Accept both compressed photos and uncompressed file uploads
    if update.message.document:
        tg_file = await update.message.document.get_file()
    else:
        tg_file = await update.message.photo[-1].get_file()
    image_bytes = bytes(await tg_file.download_as_bytearray())

    try:
        cards = await asyncio.to_thread(card_analyzer.analyze_batch, image_bytes)
    except Exception as exc:
        logger.exception("Batch analysis failed")
        await status.edit_text(f"Couldn't analyze the photo: {exc}")
        return ConversationHandler.END

    if not cards:
        await status.edit_text(
            "No cards detected. Make sure the card fronts are clearly visible and try again."
        )
        return ConversationHandler.END

    context.user_data["batch_cards"] = cards

    lines = [f"Found {len(cards)} card(s):\n"]
    for i, card in enumerate(cards, 1):
        price = card.get("price_from_image")
        price_str = f"${price:.2f}" if price else "no price"
        cond = card.get("condition_label", "?")
        cond_flag = "" if card.get("condition_known") else " (?)"
        lines.append(
            f"{i}. {card.get('card_name', 'Unknown')} "
            f"({card.get('set_name', '?')} #{card.get('card_number', '?')})\n"
            f"   {cond}{cond_flag}  —  {price_str}"
        )

    lines.append(
        "\nCondition/price marked with (?) were estimated from the card's appearance "
        "— no sticker was visible.\n\n"
        "Send /save to add all to your Google Sheet, or /cancel to abort."
    )

    await status.edit_text("\n".join(lines))
    return BATCH_CONFIRMING


async def batch_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cards = context.user_data.pop("batch_cards", [])
    if not cards:
        await update.message.reply_text("Nothing to save — session expired.")
        return ConversationHandler.END

    count = await asyncio.to_thread(sheets_client.add_inventory_batch, cards)

    if count:
        await update.message.reply_text(
            f"Saved {count} card(s) to your Google Sheet!"
        )
    else:
        await update.message.reply_text(
            "Saved to sheet! (Google Sheets not configured — check your env vars.)"
        )
    return ConversationHandler.END


async def batch_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("batch_cards", None)
    await update.message.reply_text("Batch cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Background sold-item checker (polls eBay Orders API every 10 min)
# ---------------------------------------------------------------------------

async def _sold_checker_loop(app) -> None:
    """Background task: detect sold items and notify via Telegram + update sheet."""
    await asyncio.sleep(30)  # brief startup delay
    while True:
        try:
            sold_items = await asyncio.to_thread(ebay_client.check_for_sold_items)
            for item in sold_items:
                listing = listing_store.get_by_sku(item["sku"])
                if not listing:
                    continue
                if listing.get("sold_price"):
                    continue  # already recorded

                sold_price = item["sold_price"]
                sold_date  = item["sold_date"]
                listing_store.mark_sold(listing["id"], sold_price, sold_date)
                await asyncio.to_thread(sheets_client.mark_sold, item["sku"], sold_price)

                try:
                    await app.bot.send_message(
                        chat_id=listing["chat_id"],
                        text=(
                            f"🎉 Sold!\n\n"
                            f"{listing['card_name']} ({listing['set_name']})\n"
                            f"Condition: {listing['condition']}\n"
                            f"Listed at: ${listing['price']:.2f}  →  "
                            f"Sold for: ${sold_price:.2f}"
                        ),
                    )
                except Exception:
                    logger.exception("Failed to send sold notification")

        except Exception:
            logger.exception("Sold checker error")

        await asyncio.sleep(600)  # 10 minutes


async def _post_init(app) -> None:
    asyncio.create_task(_sold_checker_loop(app))


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


async def cmd_removestock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show in-stock (non-eBay) cards and let the user mark one as removed."""
    cards = await asyncio.to_thread(sheets_client.get_in_stock)
    if not cards:
        await update.message.reply_text(
            "No in-stock cards found in your Google Sheet.\n"
            "(Cards added via /save or /batch show up here.)"
        )
        return

    # Telegram caps inline keyboards at 100 rows; show the 50 most recent to be safe
    display = cards[:50]
    keyboard = []
    for c in display:
        label = f"{c['card_name']}"
        if c.get("set_name"):
            label += f" ({c['set_name']})"
        if c.get("price"):
            label += f" — {c['price']}"
        if c.get("condition"):
            label += f" [{c['condition']}]"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"removestock:{c['row']}")])

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="removestock:cancel")])
    note = f" (showing 50 most recent of {len(cards)})" if len(cards) > 50 else ""
    await update.message.reply_text(
        f"Select an in-stock card to remove{note}:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def removestock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    payload = query.data.split(":", 1)[1]
    if payload == "cancel":
        await query.edit_message_text("Removal cancelled.")
        return
    row = int(payload)
    await query.edit_message_text("Removing from inventory...")
    await asyncio.to_thread(sheets_client.mark_removed_row, row)
    await query.edit_message_text("Removed from inventory (status set to Removed in Google Sheet).")


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
        await asyncio.to_thread(sheets_client.mark_removed, listing["sku"])
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

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )

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
                CommandHandler("save", save_inventory),
                MessageHandler(filters.PHOTO, photo_in_state),
            ],
            BATCH_CONFIRMING: [
                CommandHandler("save", batch_save),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("save", save_inventory),
        ],
        allow_reentry=False,
        block=False,
    )

    batch_conv = ConversationHandler(
        entry_points=[CommandHandler("batch", cmd_batch)],
        states={
            BATCH_WAITING_PHOTO: [
                MessageHandler(filters.PHOTO, batch_photo_received),
                MessageHandler(filters.Document.IMAGE, batch_photo_received),
            ],
            BATCH_CONFIRMING: [
                CommandHandler("save", batch_save),
            ],
        },
        fallbacks=[CommandHandler("cancel", batch_cancel)],
        block=False,
    )

    # Group -1: pre-cache album photos before the ConversationHandler sees them
    app.add_handler(MessageHandler(filters.PHOTO, pre_album_cache), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("listings", cmd_listings))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("removestock", cmd_removestock))
    app.add_handler(CallbackQueryHandler(remove_callback, pattern=r"^remove:"))
    app.add_handler(CallbackQueryHandler(removestock_callback, pattern=r"^removestock:"))
    app.add_handler(listing_conv)
    app.add_handler(batch_conv)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
