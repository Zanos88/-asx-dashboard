"""
Helius Webhook Receiver
=======================
Deploy as a **separate** Railway service from the same repo.

Receives real-time transaction notifications from Helius and forwards:
  - Whale transfer alerts to Telegram
  - Scheduled X / Grok sentiment digests to Telegram

Environment variables (set in Railway service):
    TELEGRAM_BOT_TOKEN      Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID        Target chat or channel ID
    HELIUS_WEBHOOK_SECRET   Optional HMAC secret set in Helius dashboard
    WHALE_THRESHOLD_USD     Minimum USD value to trigger whale alert (default 10000)
    XAI_API_KEY             xAI / Grok API key for X sentiment analysis
    SENTIMENT_TOKENS        Comma-separated list of token symbols to analyse (default ALON)

Railway start command:
    uvicorn webhook:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "")
WHALE_THRESHOLD_USD   = float(os.environ.get("WHALE_THRESHOLD_USD", "10000"))
XAI_API_KEY           = os.environ.get("XAI_API_KEY", "")
SENTIMENT_TOKENS      = [t.strip() for t in os.environ.get("SENTIMENT_TOKENS", "ALON").split(",")]

# Mint address → symbol mapping — extend as you add tokens
TOKEN_REGISTRY: dict[str, str] = {
    "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS": "ALON",
}

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Portfolio Webhook Receiver",
    description="Receives Helius webhooks and forwards whale + sentiment alerts to Telegram.",
    version="1.0.0",
)


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    """Format a float as a compact USD string (e.g. $1.23M)."""
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:.4f}"


def shorten_addr(addr: str) -> str:
    """Shorten a Solana wallet address for display."""
    return f"{addr[:6]}...{addr[-4:]}" if addr else "—"


# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(msg: str, retries: int = 3) -> tuple[bool, str]:
    """
    Send an HTML-formatted message to Telegram via the Bot API.

    Retries up to ``retries`` times with exponential backoff on transient errors.

    Args:
        msg:     HTML-formatted message body.
        retries: Maximum number of attempts before giving up.

    Returns:
        Tuple of (success: bool, error_description: str).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not configured — skipping alert")
        return False, "not_configured"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}

    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True, ""
        except requests.Timeout:
            log.warning("Telegram timeout (attempt %d/%d)", attempt + 1, retries)
        except requests.HTTPError as exc:
            log.error("Telegram HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
            return False, str(exc)
        except requests.ConnectionError as exc:
            log.warning("Telegram connection error (attempt %d/%d): %s", attempt + 1, retries, exc)

        if attempt < retries - 1:
            import time
            time.sleep(2 ** attempt)

    return False, f"Failed after {retries} attempts"


# ── Grok / X Sentiment ────────────────────────────────────────────────────────

def fetch_grok_sentiment(symbol: str, retries: int = 3) -> str | None:
    """
    Query the xAI / Grok API for real-time X sentiment on a given token.

    Uses Grok's live X search to retrieve the latest community sentiment.
    Retries with exponential backoff on transient network errors.

    Args:
        symbol:  Token symbol to analyse (e.g. "ALON").
        retries: Maximum number of retry attempts.

    Returns:
        Formatted sentiment string, or None on failure.
    """
    if not XAI_API_KEY:
        log.warning("XAI_API_KEY not set — skipping sentiment for %s", symbol)
        return None

    prompt = (
        f"Search X (Twitter) for posts about the Solana meme coin ${symbol} from the last 24 hours.\n\n"
        f"Provide a concise summary covering:\n"
        f"1. Sentiment: Bullish / Neutral / Bearish (with % confidence)\n"
        f"2. Key narratives or themes\n"
        f"3. KOL / influencer activity (bullish or bearish)\n"
        f"4. Rug/whale/dump concerns raised\n"
        f"5. One-line verdict for a speculative trader\n\n"
        f"Keep response under 300 words. Be direct and specific."
    )

    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": "grok-3",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a crypto trading sentiment analyst with real-time access to X (Twitter). "
                    "Be concise, specific, and flag risks clearly."
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

    import time
    for attempt in range(retries):
        try:
            resp = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=45,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.Timeout:
            log.warning("Grok timeout for %s (attempt %d/%d)", symbol, attempt + 1, retries)
        except requests.HTTPError as exc:
            log.error("Grok HTTP %s for %s: %s", exc.response.status_code, symbol, exc.response.text[:200])
            return None
        except (requests.ConnectionError, KeyError, ValueError) as exc:
            log.warning("Grok error for %s (attempt %d/%d): %s", symbol, attempt + 1, retries, exc)

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    log.error("Grok sentiment failed for %s after %d attempts", symbol, retries)
    return None


def send_sentiment_digest() -> None:
    """
    Fetch X sentiment for all configured tokens and send a digest to Telegram.

    Processes tokens sequentially; a failure on one token does not prevent
    the others from being analysed.
    """
    log.info("Sending X sentiment digest for tokens: %s", SENTIMENT_TOKENS)
    for symbol in SENTIMENT_TOKENS:
        try:
            sentiment = fetch_grok_sentiment(symbol)
            if not sentiment:
                continue

            msg = (
                f"𝕏 <b>X Sentiment Digest — ${symbol}</b>\n"
                f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"{sentiment}"
            )
            ok, err = send_telegram(msg)
            if not ok:
                log.error("Failed to send sentiment digest for %s: %s", symbol, err)
            else:
                log.info("Sentiment digest sent for %s", symbol)
        except Exception as exc:
            log.error("Unexpected error in sentiment digest for %s: %s", symbol, exc)
            continue  # never crash on a single token


# ── Helius webhook processing ─────────────────────────────────────────────────

def verify_helius_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify the HMAC-SHA256 signature on an incoming Helius webhook request.

    Args:
        body:      Raw request body bytes.
        signature: Value of the ``authorization`` header from Helius.
        secret:    Shared secret configured in the Helius dashboard.

    Returns:
        True if the signature is valid or no secret is configured.
    """
    if not secret:
        return True
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def process_transaction(tx: dict[str, Any]) -> list[str]:
    """
    Parse a single Helius transaction and return Telegram alert strings for
    any token transfers that exceed the whale threshold.

    A failure to parse any individual transfer is logged and skipped so the
    rest of the transaction continues to be processed.

    Args:
        tx: Raw transaction dict from a Helius webhook payload.

    Returns:
        List of HTML-formatted alert strings (empty if no whale activity).
    """
    alerts: list[str] = []
    sig      = tx.get("signature", "unknown")[:12]
    tx_type  = tx.get("type", "UNKNOWN")
    ts       = tx.get("timestamp", 0)
    time_str = datetime.utcfromtimestamp(ts).strftime("%H:%M:%S UTC") if ts else "—"

    for tt in tx.get("tokenTransfers", []):
        try:
            mint      = tt.get("mint", "")
            symbol    = TOKEN_REGISTRY.get(mint, f"{mint[:8]}...")
            amount    = float(tt.get("tokenAmount", 0) or 0)
            usd_val   = float(tt.get("tokenAmountUsd", 0) or 0)

            if usd_val < WHALE_THRESHOLD_USD:
                continue

            from_addr = tt.get("fromUserAccount", "")
            to_addr   = tt.get("toUserAccount", "")
            direction = "📤 SELL" if not to_addr else "📥 BUY"

            alerts.append(
                f"🐳 <b>WHALE {direction} — {symbol}</b>\n"
                f"💰 {fmt_usd(usd_val)}\n"
                f"📊 {amount:,.0f} tokens\n"
                f"🔀 {shorten_addr(from_addr)} → {shorten_addr(to_addr)}\n"
                f"🕐 {time_str}  |  {tx_type}\n"
                f"🔗 <code>{sig}...</code>"
            )
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Skipping malformed token transfer in tx %s: %s", sig, exc)
            continue

    return alerts


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    """Railway health check endpoint. Returns 200 when the service is running."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/webhook/helius")
async def helius_webhook(request: Request) -> JSONResponse:
    """
    Accept POST requests from the Helius webhook service.

    Validates the optional HMAC signature, parses each transaction for whale
    transfers, and dispatches Telegram alerts. Returns 200 even if Telegram
    delivery fails so Helius does not retry unnecessarily.

    Args:
        request: FastAPI Request object containing the raw Helius payload.

    Returns:
        JSON with counts of received transactions and alerts sent.
    """
    body = await request.body()

    # Optional signature verification
    signature = request.headers.get("authorization", "")
    if HELIUS_WEBHOOK_SECRET and not verify_helius_signature(body, signature, HELIUS_WEBHOOK_SECRET):
        log.warning("Rejected webhook — invalid signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON in webhook body: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    transactions: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
    log.info("Received %d transaction(s) from Helius", len(transactions))

    alerts_sent = 0
    for tx in transactions:
        try:
            for alert in process_transaction(tx):
                ok, err = send_telegram(alert)
                if ok:
                    alerts_sent += 1
                else:
                    log.error("Telegram delivery failed: %s", err)
        except Exception as exc:
            log.error("Unexpected error processing tx %s: %s", tx.get("signature", "?")[:12], exc)
            continue  # never crash on a single transaction

    return JSONResponse({"received": len(transactions), "alerts_sent": alerts_sent})


@app.post("/trigger/sentiment")
async def trigger_sentiment(request: Request) -> JSONResponse:
    """
    Manually trigger an X sentiment digest for all configured tokens.

    This endpoint can be called from GitHub Actions or any scheduler.
    Requires a ``x-api-key`` header matching ``HELIUS_WEBHOOK_SECRET`` when set.

    Returns:
        JSON confirming the digest was dispatched.
    """
    if HELIUS_WEBHOOK_SECRET:
        key = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(key, HELIUS_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid API key")

    send_sentiment_digest()
    return JSONResponse({"status": "dispatched", "tokens": SENTIMENT_TOKENS})


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    log.info("Starting webhook receiver on port %d", port)
    uvicorn.run("webhook:app", host="0.0.0.0", port=port, reload=False)
