"""
Render deployment entry point.

Flask health server runs in a background daemon thread (Render needs HTTP).
Telegram bot runs in the MAIN thread — required by python-telegram-bot v21
because run_polling() installs signal handlers which only work on the main thread.

Start command:  python render_start.py
"""

import asyncio
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _run_flask() -> None:
    """Serve a tiny health-check endpoint in a background thread."""
    from flask import Flask
    app = Flask(__name__)

    @app.route("/")
    @app.route("/health")
    def health():
        return "OK", 200

    port = int(os.environ.get("PORT", 10000))
    # Silence Flask's startup banner and use threaded=False (single request at a time is fine)
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# Start the health server in a daemon thread FIRST so Render's health check passes
_flask_thread = threading.Thread(target=_run_flask, name="flask-health", daemon=True)
_flask_thread.start()

# Now run the bot in the main thread
if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    import telegram_bot
    telegram_bot.main()
