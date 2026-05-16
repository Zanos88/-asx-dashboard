"""
Vercel Serverless API
=====================
Handles three responsibilities:

  POST /webhook/helius        Real-time whale detection from Helius webhooks
  POST /trigger/sentiment     Manual X/Grok sentiment digest → Telegram
  GET  /api/cron              Vercel cron (every 6h) — holder status + sentiment
  GET  /health                Health check

Environment variables (set in Vercel dashboard → Settings → Environment Variables):
    TELEGRAM_BOT_TOKEN      Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID        Target chat or channel ID
    HELIUS_API_KEY          Helius RPC API key
    XAI_API_KEY             xAI / Grok API key for X sentiment
    HELIUS_WEBHOOK_SECRET   Optional HMAC secret set in Helius dashboard
    WHALE_THRESHOLD_USD     Minimum USD for whale alert (default 10000)
    SENTIMENT_TOKENS        Comma-separated token symbols (default ALON)
    CRON_SECRET             Optional secret to protect /api/cron endpoint
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_API_KEY        = os.environ.get("HELIUS_API_KEY", "")
XAI_API_KEY           = os.environ.get("XAI_API_KEY", "")
HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "")
CRON_SECRET           = os.environ.get("CRON_SECRET", "")
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")

def _load_config() -> dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load config.json (%s) — using defaults", exc)
        return {}

_cfg = _load_config()
_default_whale = str(int(_cfg.get("whale_threshold_usd", 10000)))
_default_symbols = ",".join(_cfg.get("solana_tokens", {}).keys()) or "ALON"

WHALE_THRESHOLD_USD   = float(os.environ.get("WHALE_THRESHOLD_USD", _default_whale))
SENTIMENT_TOKENS      = [t.strip() for t in os.environ.get("SENTIMENT_TOKENS", _default_symbols).split(",") if t.strip()]

# Mint address → symbol (sourced from config.json)
TOKEN_REGISTRY: dict[str, str] = {
    info["address"]: sym
    for sym, info in _cfg.get("solana_tokens", {}).items()
} or {"8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS": "ALON"}

# Reverse lookup: symbol → mint
SYMBOL_TO_MINT: dict[str, str] = {v: k for k, v in TOKEN_REGISTRY.items()}

app = FastAPI(
    title="Portfolio API",
    description="Webhook receiver and cron dispatcher for the ASX + Solana portfolio dashboard.",
    version="1.0.0",
)


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    """Format a float as a compact USD string (e.g. $1.23M, $456K)."""
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:.4f}"


def shorten_addr(addr: str) -> str:
    """Shorten a Solana wallet address to XXXX...XXXX form."""
    return f"{addr[:6]}...{addr[-4:]}" if addr else "—"


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(msg: str, retries: int = 3) -> tuple[bool, str]:
    """
    Send an HTML-formatted message to Telegram via the Bot API.

    Retries with exponential backoff on transient network errors.

    Args:
        msg:     HTML-formatted message body.
        retries: Maximum number of attempts.

    Returns:
        Tuple of (success: bool, error_description: str).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not configured")
        return False, "not_configured"

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
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
            time.sleep(2 ** attempt)

    return False, f"Failed after {retries} attempts"


# ── Helius RPC ────────────────────────────────────────────────────────────────

def fetch_holders(mint: str, retries: int = 3) -> list[dict[str, Any]]:
    """
    Fetch the top-20 token holders via Helius getTokenLargestAccounts RPC.

    Args:
        mint:    Solana token mint address.
        retries: Maximum number of retry attempts.

    Returns:
        List of holder dicts, or empty list on failure.
    """
    if not HELIUS_API_KEY:
        log.warning("HELIUS_API_KEY not set")
        return []

    url     = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]}

    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                log.error("Helius RPC error for %s: %s", mint[:8], data["error"])
                return []
            return data.get("result", {}).get("value", [])
        except requests.Timeout:
            log.warning("Helius timeout for %s (attempt %d/%d)", mint[:8], attempt + 1, retries)
        except requests.HTTPError as exc:
            log.error("Helius HTTP error for %s: %s", mint[:8], exc)
            return []
        except (requests.ConnectionError, ValueError) as exc:
            log.warning("Helius error for %s (attempt %d/%d): %s", mint[:8], attempt + 1, retries, exc)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    return []


# ── Grok / X Sentiment ────────────────────────────────────────────────────────

def fetch_grok_sentiment(symbol: str, retries: int = 2) -> str | None:
    """
    Query the xAI Grok API for real-time X (Twitter) sentiment on a token.

    Uses Grok's live search to retrieve current X posts and synthesise a
    sentiment summary. Capped at 2 retries to respect Vercel's timeout limits.

    Args:
        symbol:  Token symbol (e.g. "ALON").
        retries: Maximum number of retry attempts.

    Returns:
        Formatted sentiment string, or None on failure.
    """
    if not XAI_API_KEY:
        log.warning("XAI_API_KEY not configured — skipping sentiment for %s", symbol)
        return None

    prompt = (
        f"Search X (Twitter) for posts about the Solana meme coin ${symbol} from the last 24 hours.\n\n"
        f"Provide a concise summary (under 250 words) covering:\n"
        f"1. Sentiment: Bullish / Neutral / Bearish with confidence %\n"
        f"2. Key narratives or themes\n"
        f"3. KOL / influencer activity\n"
        f"4. Rug, whale dump, or insider concerns\n"
        f"5. One-line trader verdict"
    )
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "model": "grok-3",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a crypto sentiment analyst with real-time X access. "
                    "Be concise, specific, and flag risks clearly."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
        "search_parameters": {
            "mode": "auto",
            "return_citations": True,
            "sources": [{"type": "x"}, {"type": "news"}],
        },
    }

    for attempt in range(retries):
        try:
            resp = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json=body,
                timeout=40,
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
            time.sleep(3)

    return None


# ── Holder status (no diff — Vercel has no persistent storage) ────────────────

def send_holder_status(symbol: str, mint: str) -> None:
    """
    Fetch current top-10 holders and send a status Telegram message.

    Because Vercel functions are stateless, this sends the raw current state
    rather than a diff. GitHub Actions handles diff-based alerting with snapshots.

    Args:
        symbol: Token symbol.
        mint:   Token mint address.
    """
    holders = fetch_holders(mint)
    if not holders:
        log.warning("No holder data for %s — skipping status", symbol)
        return

    total = sum(
        float(h.get("uiAmount") or h.get("amount", 0))
        for h in holders
    ) or 1.0

    top3 = sum(
        float(h.get("uiAmount") or h.get("amount", 0))
        for h in holders[:3]
    ) / total * 100

    concentration_flag = "🔴" if top3 > 50 else "🟡" if top3 > 30 else "🟢"
    lines = [
        f"👥 <b>Holder Status — {symbol}</b>",
        f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"{concentration_flag} Top 3 concentration: {top3:.1f}%\n",
    ]
    for i, h in enumerate(holders[:10], 1):
        amt = float(h.get("uiAmount") or h.get("amount", 0))
        pct = amt / total * 100
        lines.append(f"{i:2d}. <code>{shorten_addr(h.get('address',''))}</code>  {pct:.2f}%")

    ok, err = send_telegram("\n".join(lines))
    if not ok:
        log.error("Failed to send holder status for %s: %s", symbol, err)


# ── Webhook processing ────────────────────────────────────────────────────────

def verify_helius_signature(header_value: str, secret: str) -> bool:
    """
    Verify the Authorization header on an incoming Helius webhook request.

    Helius sends the raw auth token string you configured in the dashboard
    as the ``Authorization`` header value — it is NOT an HMAC digest.

    Args:
        header_value: Value of the ``Authorization`` header from Helius.
        secret:       Auth token configured in the Helius webhook dashboard.

    Returns:
        True if valid, or if no secret is configured (verification skipped).
    """
    if not secret:
        return True
    return hmac.compare_digest(header_value.strip(), secret.strip())


# Module-level price cache — lives for the duration of one Vercel function invocation
_price_cache: dict[str, float] = {}


def get_token_price_usd(mint: str) -> float:
    """
    Fetch the current USD price for a token from DexScreener.

    Results are cached in ``_price_cache`` for the lifetime of the invocation.
    Returns 0.0 on any failure so callers can fall back gracefully.

    Args:
        mint: Solana token mint address.

    Returns:
        Price in USD as a float, or 0.0 if unavailable.
    """
    if mint in _price_cache:
        return _price_cache[mint]
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=4,
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        # Use highest-liquidity pair
        pairs = sorted(
            pairs,
            key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
            reverse=True,
        )
        price = float(pairs[0].get("priceUsd", 0) or 0) if pairs else 0.0
        _price_cache[mint] = price
        return price
    except Exception as exc:
        log.warning("DexScreener price lookup failed for %s: %s", mint[:8], exc)
        return 0.0


def _transfer_direction(tx: dict[str, Any], mint: str) -> str:
    """
    Determine transfer direction from Helius swap event data.

    Checks ``events.swap.tokenInputs`` and ``tokenOutputs`` to classify the
    transfer as BUY or SELL relative to the tracked token. Falls back to
    TRANSFER for non-swap transactions.

    Args:
        tx:   Full Helius enhanced transaction dict.
        mint: Token mint address to check direction for.

    Returns:
        One of ``"📥 BUY"``, ``"📤 SELL"``, or ``"🔀 TRANSFER"``.
    """
    swap = (tx.get("events") or {}).get("swap") or {}
    for out in swap.get("tokenOutputs", []):
        if out.get("mint") == mint:
            return "📥 BUY"
    for inp in swap.get("tokenInputs", []):
        if inp.get("mint") == mint:
            return "📤 SELL"
    return "🔀 TRANSFER"


def process_transaction(tx: dict[str, Any]) -> list[str]:
    """
    Parse a Helius enhanced transaction and return Telegram alert strings for
    any token transfers that exceed the whale threshold.

    USD value is estimated via DexScreener price lookup. If DexScreener is
    unavailable, falls back to token count vs ``min_holder_change_tokens``
    from config.json.

    Individual transfer parse errors are logged and skipped rather than raised.

    Args:
        tx: Raw enhanced transaction dict from a Helius webhook payload.

    Returns:
        List of HTML-formatted alert strings (empty if no whale activity).
    """
    alerts:   list[str] = []
    sig      = tx.get("signature", "unknown")
    tx_type  = tx.get("type", "UNKNOWN")
    ts       = tx.get("timestamp", 0)
    time_str = datetime.utcfromtimestamp(ts).strftime("%H:%M:%S UTC") if ts else "—"

    for tt in tx.get("tokenTransfers", []):
        try:
            mint = tt.get("mint", "")

            # Only alert on tokens we actively track
            if mint not in TOKEN_REGISTRY:
                continue

            symbol    = TOKEN_REGISTRY[mint]
            token_cfg = _cfg.get("solana_tokens", {}).get(symbol, {})
            emoji     = token_cfg.get("emoji", "🪙")
            amount    = float(tt.get("tokenAmount", 0) or 0)

            # USD value via DexScreener; fall back to token-count threshold
            price   = get_token_price_usd(mint)
            usd_val = round(amount * price, 2) if price else 0.0

            if price:
                if usd_val < WHALE_THRESHOLD_USD:
                    continue
            else:
                min_tokens = float(_cfg.get("min_holder_change_tokens", 1000))
                if amount < min_tokens:
                    continue

            from_addr = tt.get("fromUserAccount", "")
            to_addr   = tt.get("toUserAccount", "")
            direction = _transfer_direction(tx, mint)

            usd_str   = f"\n💵 {fmt_usd(usd_val)}" if usd_val else ""
            price_str = f"  @${price:.6f}" if price else ""

            alerts.append(
                f"🐳 <b>WHALE {direction} — {symbol} {emoji}</b>\n"
                f"📊 {amount:,.0f} tokens{price_str}{usd_str}\n"
                f"🔀 <code>{shorten_addr(from_addr)}</code> → <code>{shorten_addr(to_addr)}</code>\n"
                f"🕐 {time_str}  |  {tx_type}\n"
                f"🔗 <code>{sig[:20]}...</code>"
            )
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Skipping malformed transfer in tx %s: %s", sig[:12], exc)
            continue

    return alerts


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    """Railway / Vercel health check — returns 200 when the service is live."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/webhook/helius")
async def helius_webhook(request: Request) -> JSONResponse:
    """
    Accept POST requests from the Helius webhook service.

    Validates the optional HMAC signature, parses each transaction for whale
    transfers above the configured threshold, and dispatches Telegram alerts.
    Always returns 200 to prevent Helius from retrying valid deliveries.

    Args:
        request: Incoming FastAPI Request with Helius payload.

    Returns:
        JSON with counts of transactions received and alerts sent.
    """
    body = await request.body()

    signature = request.headers.get("authorization", "")
    if HELIUS_WEBHOOK_SECRET and not verify_helius_signature(signature, HELIUS_WEBHOOK_SECRET):
        log.warning("Rejected webhook — invalid HMAC signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    transactions: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
    log.info("Received %d transaction(s)", len(transactions))

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

    Requires an ``x-api-key`` header matching ``HELIUS_WEBHOOK_SECRET`` when set.
    Safe to call from GitHub Actions, cron services, or manually via curl.

    Args:
        request: Incoming FastAPI Request.

    Returns:
        JSON confirming which tokens were processed.
    """
    if HELIUS_WEBHOOK_SECRET:
        key = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(key, HELIUS_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid API key")

    results: dict[str, str] = {}
    for symbol in SENTIMENT_TOKENS:
        try:
            sentiment = fetch_grok_sentiment(symbol)
            if sentiment:
                msg = (
                    f"𝕏 <b>X Sentiment — ${symbol}</b>\n"
                    f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                    f"{sentiment}"
                )
                ok, err = send_telegram(msg)
                results[symbol] = "sent" if ok else f"telegram_error: {err}"
            else:
                results[symbol] = "no_sentiment_returned"
        except Exception as exc:
            log.error("Sentiment error for %s: %s", symbol, exc)
            results[symbol] = f"error: {exc}"
            continue

    return JSONResponse({"status": "complete", "results": results})


@app.get("/api/cron")
async def cron(request: Request) -> JSONResponse:
    """
    Vercel cron endpoint — runs every 6 hours automatically.

    Sends:
    1. Current holder status (top-10 wallets with % supply) for each token.
    2. X/Grok sentiment digest for each token.

    Note: Because Vercel functions are stateless, this sends the current raw
    state rather than diffs. Use GitHub Actions (monitor.py) for diff-based
    alerts with snapshot persistence.

    Optionally protected by ``CRON_SECRET`` via the ``x-cron-secret`` header.

    Args:
        request: Incoming FastAPI Request (Vercel cron call).

    Returns:
        JSON summarising what was dispatched.
    """
    # Optional protection — set CRON_SECRET in Vercel env vars
    if CRON_SECRET:
        provided = request.headers.get("x-cron-secret", "")
        if not hmac.compare_digest(provided, CRON_SECRET):
            raise HTTPException(status_code=401, detail="Invalid cron secret")

    log.info("Cron triggered — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    dispatched: list[str] = []

    for symbol in SENTIMENT_TOKENS:
        mint = SYMBOL_TO_MINT.get(symbol)

        # Holder status
        if mint:
            try:
                send_holder_status(symbol, mint)
                dispatched.append(f"{symbol}:holders")
            except Exception as exc:
                log.error("Holder status failed for %s: %s", symbol, exc)

        # X Sentiment
        try:
            sentiment = fetch_grok_sentiment(symbol)
            if sentiment:
                msg = (
                    f"𝕏 <b>X Sentiment — ${symbol}</b>\n"
                    f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                    f"{sentiment}"
                )
                ok, err = send_telegram(msg)
                if ok:
                    dispatched.append(f"{symbol}:sentiment")
                else:
                    log.error("Sentiment Telegram failed for %s: %s", symbol, err)
        except Exception as exc:
            log.error("Sentiment error for %s: %s — skipping", symbol, exc)
            continue  # never crash on a single token

    return JSONResponse({"cron": "complete", "dispatched": dispatched})
