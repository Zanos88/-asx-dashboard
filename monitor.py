"""
Holder Concentration Monitor + X Sentiment Dispatcher
======================================================
Run standalone or via GitHub Actions cron job (see .github/workflows/monitor.yml).

What it does each run:
  1. Fetches top-20 holders for every tracked token via Helius RPC.
  2. Diffs against the previous snapshot; sends a Telegram alert for any
     significant changes (new wallets, exits, supply % moves ≥ threshold).
  3. Fetches X / Grok sentiment for each token and sends a digest to Telegram.
  4. Saves updated snapshots to disk so GitHub Actions can commit them back.

Environment variables:
    HELIUS_API_KEY        Helius RPC API key
    TELEGRAM_BOT_TOKEN    Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID      Target chat or channel ID
    XAI_API_KEY           xAI / Grok API key for X sentiment analysis
    MOVE_THRESHOLD_PCT    % supply change to trigger an alert (default 1.0)
    SKIP_SENTIMENT        Set to "1" to skip sentiment (e.g. on frequent runs)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
XAI_API_KEY        = os.environ.get("XAI_API_KEY", "")
SNAPSHOT_DIR       = os.path.join(os.path.dirname(__file__), "snapshots")
SKIP_SENTIMENT     = os.environ.get("SKIP_SENTIMENT", "0") == "1"

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def _load_config() -> dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load config.json (%s) — using defaults", exc)
        return {}

_cfg = _load_config()

MOVE_THRESHOLD_PCT = float(os.environ.get("MOVE_THRESHOLD_PCT", str(_cfg.get("move_threshold_pct", 1.0))))

# Token registry: symbol → mint address (sourced from config.json)
TOKENS: dict[str, str] = {
    sym: info["address"]
    for sym, info in _cfg.get("solana_tokens", {}).items()
} or {"ALON": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"}


# ── Telegram ──────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=False,
)
def send_telegram(msg: str) -> tuple[bool, str]:
    """
    Send an HTML-formatted message to Telegram via the Bot API.

    Retries up to 3 times with exponential backoff on network errors.
    Returns (False, reason) rather than raising on persistent failure.

    Args:
        msg: HTML-formatted message body (Telegram HTML subset).

    Returns:
        Tuple of (success: bool, error_description: str).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping")
        return False, "not_configured"

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=10,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.error("Telegram HTTP error %s: %s", resp.status_code, resp.text[:200])
        return False, str(exc)

    return True, ""


# ── Helius RPC ────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def fetch_holders(token_address: str) -> list[dict[str, Any]]:
    """
    Fetch the top-20 token holders via the Helius ``getTokenLargestAccounts`` RPC method.

    Retries up to 3 times with exponential backoff on transient network errors.
    Raises on persistent failure so the caller can decide whether to skip or abort.

    Args:
        token_address: Solana token mint address.

    Returns:
        List of holder dicts; each has ``address``, ``amount``, ``uiAmount`` and
        ``uiAmountString`` keys as returned by the Solana RPC specification.

    Raises:
        requests.Timeout:        If the request times out after all retries.
        requests.ConnectionError: If the host is unreachable after all retries.
        requests.HTTPError:      If the API returns a non-2xx status code.
    """
    if not HELIUS_API_KEY:
        raise ValueError("HELIUS_API_KEY is not set")

    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getTokenLargestAccounts",
        "params":  [token_address],
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()

    data = resp.json()
    if "error" in data:
        log.error("Helius RPC error for %s: %s", token_address[:8], data["error"])
        return []

    return data.get("result", {}).get("value", [])


# ── Grok / X Sentiment ────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=3, max=15),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=False,
)
def fetch_grok_sentiment(symbol: str) -> str | None:
    """
    Query the xAI / Grok API for real-time X (Twitter) sentiment on a token.

    Uses Grok's live X search via ``search_parameters`` to retrieve the latest
    community posts and synthesise a sentiment summary.

    Args:
        symbol: Token symbol to analyse (e.g. "ALON").

    Returns:
        Formatted sentiment string on success, or None if the API is unavailable
        or ``XAI_API_KEY`` is not configured.
    """
    if not XAI_API_KEY:
        log.warning("XAI_API_KEY not configured — skipping sentiment for %s", symbol)
        return None

    prompt = (
        f"Search X (Twitter) for posts about the Solana meme coin ${symbol} from the last 24 hours.\n\n"
        f"Provide a concise summary (under 300 words) covering:\n"
        f"1. Sentiment: Bullish / Neutral / Bearish with confidence %\n"
        f"2. Key narratives and themes\n"
        f"3. KOL / influencer activity\n"
        f"4. Rug, whale dump, or insider selling concerns\n"
        f"5. One-line trader verdict\n"
    )

    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": "grok-3",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a crypto trading sentiment analyst with real-time X access. "
                    "Be direct, specific, and flag risks clearly."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 600,
        "search_parameters": {
            "mode": "auto",
            "return_citations": True,
            "sources": [{"type": "x"}, {"type": "news"}],
        },
    }

    resp = requests.post(
        "https://api.x.ai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=45,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.error("Grok HTTP %s for %s: %s", resp.status_code, symbol, resp.text[:200])
        return None

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        log.error("Unexpected Grok response structure for %s: %s", symbol, exc)
        return None


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def load_snapshot(symbol: str) -> dict[str, Any] | None:
    """
    Load the most recent holder snapshot from disk for a given token.

    Args:
        symbol: Token symbol (e.g. "ALON").

    Returns:
        Snapshot dict with ``timestamp`` (ISO string) and ``holders`` (list),
        or ``None`` if no snapshot file exists or parsing fails.
    """
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not load snapshot for %s: %s", symbol, exc)
        return None


def save_snapshot(symbol: str, holders: list[dict[str, Any]]) -> None:
    """
    Persist the current holder list as a timestamped JSON snapshot.

    Args:
        symbol:  Token symbol (e.g. "ALON").
        holders: Holder list from ``fetch_holders()``.
    """
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"timestamp": datetime.utcnow().isoformat(), "holders": holders},
                fh,
                indent=2,
            )
    except OSError as exc:
        log.error("Could not save snapshot for %s: %s", symbol, exc)


# ── Comparison ────────────────────────────────────────────────────────────────

def get_amount(holder: dict[str, Any]) -> float:
    """
    Extract the decimal-adjusted token amount from a holder dict.

    Prefers ``uiAmount`` (decimal-adjusted float). Falls back to the raw
    ``amount`` string if ``uiAmount`` is absent or null.

    Args:
        holder: Single holder dict from the Helius RPC response.

    Returns:
        Float token amount.
    """
    ui = holder.get("uiAmount")
    return float(ui) if ui is not None else float(holder.get("amount", 0))


def compare_holders(
    old_holders: list[dict[str, Any]],
    new_holders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Diff two holder lists and return significant changes.

    A change is considered significant when:
    - A wallet address appears in the new list but not the old (NEW).
    - A wallet address appears in the old list but not the new (EXIT).
    - An existing wallet's percentage of total supply changes by at least
      ``MOVE_THRESHOLD_PCT`` percent (MOVE).

    Args:
        old_holders: Holder list from the previous snapshot.
        new_holders: Current holder list from the RPC.

    Returns:
        List of change dicts, each with keys:
        ``type`` (str), ``address`` (str), ``old_pct`` (float | None),
        ``new_pct`` (float | None), ``delta`` (float).
    """
    old_map   = {h["address"]: h for h in old_holders}
    new_map   = {h["address"]: h for h in new_holders}
    old_total = sum(get_amount(h) for h in old_holders) or 1.0
    new_total = sum(get_amount(h) for h in new_holders) or 1.0

    changes: list[dict[str, Any]] = []

    for addr, h in new_map.items():
        if addr not in old_map:
            pct = get_amount(h) / new_total * 100
            changes.append({"type": "NEW", "address": addr,
                            "old_pct": None, "new_pct": pct, "delta": pct})

    for addr, h in old_map.items():
        if addr not in new_map:
            pct = get_amount(h) / old_total * 100
            changes.append({"type": "EXIT", "address": addr,
                            "old_pct": pct, "new_pct": None, "delta": -pct})

    for addr in set(old_map) & set(new_map):
        old_pct = get_amount(old_map[addr]) / old_total * 100
        new_pct = get_amount(new_map[addr]) / new_total * 100
        delta   = new_pct - old_pct
        if abs(delta) >= MOVE_THRESHOLD_PCT:
            changes.append({"type": "MOVE", "address": addr,
                            "old_pct": old_pct, "new_pct": new_pct, "delta": delta})

    return changes


def format_holder_alert(
    symbol: str,
    changes: list[dict[str, Any]],
    snapshot_ts: str,
) -> str:
    """
    Build an HTML-formatted Telegram message summarising holder changes.

    Args:
        symbol:      Token symbol.
        changes:     Output of ``compare_holders()``.
        snapshot_ts: ISO timestamp string of the previous snapshot.

    Returns:
        HTML string suitable for sending via the Telegram Bot API.
    """
    lines = [
        f"🚨 <b>Holder Alert — {symbol}</b>",
        f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"vs snapshot: {snapshot_ts[:16].replace('T', ' ')} UTC\n",
    ]
    for c in changes:
        addr = f"{c['address'][:6]}...{c['address'][-4:]}"
        if c["type"] == "NEW":
            lines.append(
                f"🆕 <b>NEW</b> wallet entered top 20\n"
                f"   <code>{addr}</code> → {c['new_pct']:.2f}%"
            )
        elif c["type"] == "EXIT":
            lines.append(
                f"🚪 <b>EXIT</b> wallet left top 20\n"
                f"   <code>{addr}</code> was {c['old_pct']:.2f}%"
            )
        else:
            arrow = "📈" if c["delta"] > 0 else "📉"
            lines.append(
                f"{arrow} <b>MOVE</b>  <code>{addr}</code>\n"
                f"   {c['old_pct']:.2f}% → {c['new_pct']:.2f}% ({c['delta']:+.2f}%)"
            )
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_holder_monitor() -> None:
    """
    Check holder concentration for every token in the registry.

    For each token:
    - Fetches the current top-20 holders.
    - Diffs against the stored snapshot.
    - Sends a Telegram alert if significant changes are found.
    - Saves the updated snapshot to disk.

    A failure on any single token is logged and skipped; the loop always
    continues to the next token.
    """
    log.info("── Holder monitor starting — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    for symbol, address in TOKENS.items():
        log.info("Checking %s (%s...)", symbol, address[:8])
        try:
            current = fetch_holders(address)
        except Exception as exc:
            log.error("fetch_holders failed for %s after retries: %s — skipping", symbol, exc)
            continue

        if not current:
            log.warning("Empty holder list for %s — skipping", symbol)
            continue

        log.info("  Fetched %d holders", len(current))
        snapshot = load_snapshot(symbol)

        if snapshot:
            try:
                changes = compare_holders(snapshot["holders"], current)
            except (KeyError, TypeError) as exc:
                log.error("Snapshot comparison failed for %s: %s", symbol, exc)
                save_snapshot(symbol, current)
                continue

            if changes:
                log.info("  %d change(s) detected", len(changes))
                msg = format_holder_alert(symbol, changes, snapshot["timestamp"])
                try:
                    ok, err = send_telegram(msg)
                    if not ok:
                        log.error("Telegram delivery failed: %s", err)
                except Exception as exc:
                    log.error("Unexpected Telegram error for %s: %s", symbol, exc)
            else:
                log.info("  No significant changes")
        else:
            log.info("  No previous snapshot — creating baseline")
            try:
                send_telegram(
                    f"📸 <b>{symbol}</b> — baseline snapshot created.\n"
                    f"Tracking {len(current)} holders.\n"
                    f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
                )
            except Exception as exc:
                log.error("Failed to send baseline notification for %s: %s", symbol, exc)

        save_snapshot(symbol, current)
        log.info("  Snapshot saved → snapshots/%s_holders.json", symbol)


def run_sentiment_digest() -> None:
    """
    Fetch and broadcast X / Grok sentiment for every token in the registry.

    Each token is processed independently; a failure on one does not prevent
    the others from being dispatched.
    """
    if SKIP_SENTIMENT:
        log.info("SKIP_SENTIMENT=1 — skipping sentiment digest")
        return

    log.info("── Sentiment digest starting — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    for symbol in TOKENS:
        log.info("Fetching X sentiment for %s", symbol)
        try:
            sentiment = fetch_grok_sentiment(symbol)
            if not sentiment:
                log.warning("No sentiment returned for %s", symbol)
                continue

            msg = (
                f"𝕏 <b>X Sentiment — ${symbol}</b>\n"
                f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"{sentiment}"
            )
            ok, err = send_telegram(msg)
            if not ok:
                log.error("Failed to send sentiment for %s: %s", symbol, err)
            else:
                log.info("  Sentiment digest sent for %s", symbol)
        except Exception as exc:
            log.error("Unexpected error in sentiment for %s: %s — skipping", symbol, exc)
            continue


def run() -> None:
    """
    Entry point — run holder monitor then sentiment digest.

    Designed to be called from GitHub Actions cron or any scheduler.
    Never raises; all errors are logged.
    """
    run_holder_monitor()
    run_sentiment_digest()
    log.info("── Monitor complete")


if __name__ == "__main__":
    run()
