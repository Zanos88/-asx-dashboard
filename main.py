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
import time

import uvicorn

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)


bot_thread: threading.Thread | None = None


def _run_bot() -> None:
    from bot_commands import main as bot_main
    # Retry loop — handles transient Telegram Conflict errors that occur during
    # Railway blue-green deploys when two instances briefly overlap.
    attempt = 0
    while True:
        attempt += 1
        log.info("Bot polling attempt %d starting…", attempt)
        try:
            bot_main()
            # bot_main() returned normally (should not happen in production)
            log.warning("Bot polling loop exited cleanly — restarting in 5s")
            time.sleep(5)
        except Exception as exc:
            msg = str(exc)
            if "Conflict" in msg or "terminated by other getUpdates" in msg:
                # Old instance is still alive; wait for it to be killed then retry.
                wait = min(10 * attempt, 60)
                log.warning(
                    "Telegram Conflict detected (two instances overlap) — "
                    "waiting %ds then retrying (attempt %d)", wait, attempt
                )
                time.sleep(wait)
            else:
                log.critical("Bot thread crashed: %s", exc, exc_info=True)
                # Back off before restart to avoid tight crash loops
                time.sleep(min(15 * attempt, 120))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))

    # Start the Telegram polling loop in a background daemon thread.
    # Daemon=True means it exits automatically when the main thread (uvicorn) stops.
    # bot_thread is module-level so webhook.py /health can check is_alive().
    bot_thread = threading.Thread(target=_run_bot, name="telegram-bot", daemon=True)
    bot_thread.start()
    log.info("Bot thread started (id=%s)", bot_thread.ident)

    # Alias __main__ as "main" so that `import main` in webhook.py gets this
    # module object (where bot_thread was set) rather than a fresh import.
    import sys
    sys.modules["main"] = sys.modules["__main__"]

    # Run uvicorn in the main thread — Railway health-checks this port.
    log.info("Starting uvicorn on 0.0.0.0:%d", port)
    uvicorn.run(
        "webhook:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
