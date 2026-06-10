"""
Unified entry point — runs bot_commands polling + webhook FastAPI in one process.

Thread layout:
  main thread  — uvicorn serving webhook:app  (Railway binds $PORT here)
  daemon thread — bot_commands polling loop   (long-polls Telegram getUpdates)

Railway start command: python main.py
"""
from __future__ import annotations

import logging
import os
import threading

import uvicorn

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)


bot_thread: threading.Thread | None = None


def _run_bot() -> None:
    from bot_commands import main as bot_main
    log.info("Bot thread starting…")
    try:
        bot_main()
    except Exception as exc:
        log.critical("Bot thread died unexpectedly: %s", exc, exc_info=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))

    # Start the Telegram polling loop in a background daemon thread.
    # Daemon=True means it exits automatically when the main thread (uvicorn) stops.
    # bot_thread is module-level so webhook.py /health can check is_alive().
    bot_thread = threading.Thread(target=_run_bot, name="telegram-bot", daemon=True)
    bot_thread.start()
    log.info("Bot thread started (id=%s)", bot_thread.ident)

    # Run uvicorn in the main thread — Railway health-checks this port.
    log.info("Starting uvicorn on 0.0.0.0:%d", port)
    uvicorn.run(
        "webhook:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
