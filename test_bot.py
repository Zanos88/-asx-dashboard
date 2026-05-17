"""
Smoke-test suite for bot_commands.py.

Sends real Telegram commands and verifies the bot responds correctly.
Requires a running bot (Railway or local `python main.py`) and:
  TELEGRAM_BOT_TOKEN  — bot token from @BotFather
  TELEGRAM_CHAT_ID    — the chat ID authorized in bot_commands.py

Usage:
  python test_bot.py
  python test_bot.py --timeout 15   # seconds per command (default 12)

Exit code 0 = all pass, 1 = any failure.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE      = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send(text: str) -> int:
    """Send a message and return its message_id."""
    r = requests.post(f"{BASE}/sendMessage", json={
        "chat_id": CHAT_ID, "text": text,
    }, timeout=10)
    r.raise_for_status()
    return r.json()["result"]["message_id"]


def _poll_reply(after_id: int, timeout: int) -> str | None:
    """
    Long-poll getUpdates until a bot reply appears after `after_id`,
    returning its text. Returns None if nothing arrives within `timeout` seconds.
    """
    deadline = time.monotonic() + timeout
    offset   = 0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        poll_secs = min(5, max(1, int(remaining)))
        r = requests.get(f"{BASE}/getUpdates", params={
            "offset": offset, "timeout": poll_secs, "allowed_updates": ["message"],
        }, timeout=poll_secs + 5)
        r.raise_for_status()
        for upd in r.json().get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            if msg.get("message_id", 0) > after_id and msg.get("from", {}).get("is_bot"):
                return msg.get("text") or ""
    return None


def run_test(label: str, command: str, expect: str, timeout: int) -> bool:
    """Send `command`, wait for a bot reply, check it starts with `expect`."""
    print(f"  {label} ... ", end="", flush=True)
    try:
        sent_id = _send(command)
        reply   = _poll_reply(sent_id, timeout)
        if reply is None:
            print(f"FAIL (no reply within {timeout}s)")
            return False
        if not reply.startswith(expect):
            snippet = reply[:80].replace("\n", " ")
            print(f"FAIL (unexpected reply: {snippet!r})")
            return False
        elapsed = timeout  # we don't track exact time but it's within timeout
        print(f"PASS")
        return True
    except Exception as exc:
        print(f"ERROR ({exc})")
        return False


# ── Test cases ────────────────────────────────────────────────────────────────

TESTS = [
    # (label, command_text, expected_reply_prefix)
    ("/ping",           "/ping",          "🟢 Bot online"),
    ("/status",         "/status",        "📊"),
    ("/snapshot",       "/snapshot",      "📸"),
    ("/help",           "/help",          "📋"),
    ("/holders ALON",   "/holders ALON",  "⏳"),   # bot replies "fetching…" first
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=12, help="Seconds to wait per command")
    args = parser.parse_args()

    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    print(f"\nBot smoke tests (timeout={args.timeout}s per command)\n{'─'*46}")

    results = [
        run_test(label, cmd, expect, args.timeout)
        for label, cmd, expect in TESTS
    ]

    passed = sum(results)
    total  = len(results)
    print(f"\n{'─'*46}")
    print(f"{'✅ All' if passed == total else '❌'} {passed}/{total} passed\n")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
