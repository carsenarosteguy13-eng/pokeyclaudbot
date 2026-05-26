"""
Render deployment entry point.

Starts a minimal Flask health server (required by Render's free web tier)
alongside the Telegram polling bot in a background thread.

Start command:  gunicorn -w 1 render_start:flask_app
"""

import asyncio
import os
import sys
import threading
from pathlib import Path

# Ensure the bot package directory is on the path
sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask

flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health():
    return "OK", 200


def _run_bot() -> None:
    """Run the Telegram bot in its own asyncio event loop (blocking)."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    import telegram_bot
    telegram_bot.main()


# Start the bot thread when this module is first imported.
# gunicorn imports the module once (single worker -w 1), so this runs exactly once.
_bot_thread = threading.Thread(target=_run_bot, name="telegram-bot", daemon=True)
_bot_thread.start()


if __name__ == "__main__":
    # Local fallback: run Flask dev server
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)
