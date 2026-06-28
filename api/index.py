"""
Railway Webhook Receiver + API
================================
Handles real-time Helius webhook events and exposes utility endpoints.

Routes:
  POST /webhook/helius    Real-time whale detection from Helius enhanced webhooks
  POST /webhook/test      Fire a test Telegram alert (no auth required)
  POST /trigger/sentiment Manual X/Grok sentiment digest → Telegram
  GET  /api/cron          Periodic holder status + sentiment (call from any scheduler)
  GET  /health            Health check — returns config summary

Environment variables (set in Railway service → Variables):
    TELEGRAM_BOT_TOKEN      Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID        Target chat or channel ID
    HELIUS_API_KEY          Helius RPC API key
    XAI_API_KEY             xAI / Grok API key for X sentiment
    HELIUS_WEBHOOK_SECRET   Optional auth token set in Helius dashboard
    WHALE_THRESHOLD_USD     Minimum USD for whale alert (default from config.json)
    SENTIMENT_TOKENS        Comma-separated token symbols (default from config.json)
    CRON_SECRET             Optional secret to protect /api/cron endpoint
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Make the repo-root modules importable from this api/ subpackage (Vercel + local).
import sys as _sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
from supply_utils import fetch_token_supply, pct_of_supply  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHANNEL_ID   = os.environ.get("TELEGRAM_CHANNEL_ID", "")
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
_default_whale    = str(int(_cfg.get("whale_threshold_usd", 500)))
_default_symbols  = ",".join(_cfg.get("solana_tokens", {}).keys()) or "ALON"

WHALE_THRESHOLD_USD = float(os.environ.get("WHALE_THRESHOLD_USD", _default_whale))
SENTIMENT_TOKENS    = [
    t.strip() for t in os.environ.get("SENTIMENT_TOKENS", _default_symbols).split(",") if t.strip()
]

# Mint address → symbol (sourced from config.json)
TOKEN_REGISTRY: dict[str, str] = {
    info["address"]: sym
    for sym, info in _cfg.get("solana_tokens", {}).items()
} or {"8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS": "ALON"}

# Reverse: symbol → mint
SYMBOL_TO_MINT: dict[str, str] = {v: k for k, v in TOKEN_REGISTRY.items()}

# Per-run price cache (avoids redundant DexScreener calls within one webhook batch)
_price_cache: dict[str, float] = {}

app = FastAPI(
    title="ASX Dashboard API",
    description="Webhook receiver and cron dispatcher for the ASX + Solana portfolio dashboard.",
    version="2.0.0",
)


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:.2f}"


def shorten_addr(addr: str) -> str:
    return f"{addr[:8]}...{addr[-6:]}" if addr else "—"


def get_whale_severity(usd_val: float) -> tuple[str, str]:
    """Return (severity_name, emoji) based on USD value of a whale transfer."""
    if usd_val >= 10_000:
        return "CRITICAL", "🔴"
    if usd_val >= 2_000:
        return "SIGNIFICANT", "🟡"
    return "NOTABLE", "🟢"


def make_inline_keyboard(wallet_addr: str, token_mint: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "🔍 Solscan",     "url": f"https://solscan.io/account/{wallet_addr}"},
            {"text": "📊 DexScreener", "url": f"https://dexscreener.com/solana/{token_mint}"},
            {"text": "🫧 Bubblemaps",  "url": f"https://app.bubblemaps.io/sol/token/{token_mint}"},
        ]]
    }


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(
    msg: str,
    reply_markup: dict[str, Any] | None = None,
    retries: int = 3,
    *,
    chat_id: str = "",
) -> tuple[bool, str]:
    """Send an HTML-formatted message to a single Telegram chat, optionally with inline keyboard."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        log.warning("Telegram credentials not configured")
        return False, "not_configured"

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id":                  target,
        "text":                     msg,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

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


def send_alert(msg: str, reply_markup: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Send alert to channel when configured, else fall back to owner chat."""
    target = TELEGRAM_CHANNEL_ID or TELEGRAM_CHAT_ID
    if not target:
        return False, "no_targets"
    try:
        return send_telegram(msg, reply_markup, chat_id=target)
    except Exception as exc:
        return False, str(exc)


# ── DexScreener price ─────────────────────────────────────────────────────────

def get_token_price_usd(mint: str) -> float:
    """Fetch current USD price from DexScreener; cached per invocation."""
    if mint in _price_cache:
        return _price_cache[mint]
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=5,
        )
        resp.raise_for_status()
        pairs = sorted(
            resp.json().get("pairs") or [],
            key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
            reverse=True,
        )
        price = float(pairs[0].get("priceUsd", 0) or 0) if pairs else 0.0
        _price_cache[mint] = price
        return price
    except Exception as exc:
        log.warning("DexScreener price lookup failed for %s: %s", mint[:8], exc)
        return 0.0


# ── Helius RPC ────────────────────────────────────────────────────────────────

def fetch_holders(mint: str) -> list[dict[str, Any]]:
    """Fetch top-20 token holders via Helius RPC."""
    if not HELIUS_API_KEY:
        log.warning("HELIUS_API_KEY not set")
        return []
    try:
        resp = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]},
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {}).get("value", [])
    except Exception as exc:
        log.error("fetch_holders failed for %s: %s", mint[:8], exc)
        return []


# ── Webhook processing ────────────────────────────────────────────────────────

def verify_helius_signature(header_value: str, secret: str) -> bool:
    """Helius sends the raw auth token string as the Authorization header."""
    if not secret:
        return True
    return hmac.compare_digest(header_value.strip(), secret.strip())


def _transfer_direction(tx: dict[str, Any], mint: str) -> str:
    """Classify transfer as BUY / SELL / TRANSFER by inspecting swap event data."""
    swap = (tx.get("events") or {}).get("swap") or {}
    for out in swap.get("tokenOutputs", []):
        if out.get("mint") == mint:
            return "📥 BUY"
    for inp in swap.get("tokenInputs", []):
        if inp.get("mint") == mint:
            return "📤 SELL"
    return "🔀 TRANSFER"


def format_whale_alert(
    symbol: str,
    mint: str,
    emoji: str,
    direction: str,
    from_addr: str,
    to_addr: str,
    amount: float,
    price: float,
    usd_val: float,
    tx_type: str,
    time_str: str,
    sig: str,
) -> str:
    """Build quant-style HTML alert string matching monitor.py format."""
    sev_name, sev_icon = get_whale_severity(usd_val)
    usd_display  = fmt_usd(usd_val)
    price_str    = f"${price:.6f}".rstrip("0").rstrip(".") if price else "N/A"
    from_short   = shorten_addr(from_addr)
    to_short     = shorten_addr(to_addr)
    solscan_from = f"https://solscan.io/account/{from_addr}" if from_addr else "#"
    solscan_to   = f"https://solscan.io/account/{to_addr}"   if to_addr   else "#"
    dex_url      = f"https://dexscreener.com/solana/{mint}"
    bubbles_url  = f"https://app.bubblemaps.io/sol/token/{mint}"
    contract_url = f"https://solscan.io/token/{mint}"

    lines = [
        f"{sev_icon} <b>{symbol} {emoji} — WHALE {direction}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"💰 {usd_display}  ({amount:,.0f} tokens)",
        f"💵 Price: {price_str}",
        f"🔀 <a href=\"{solscan_from}\">{from_short}</a> → <a href=\"{solscan_to}\">{to_short}</a>",
        f"🕐 {time_str}  |  {tx_type}",
        f'📜 <a href="{contract_url}">{mint[:8]}...{mint[-4:]}</a>',
        "━━━━━━━━━━━━━━━━━━━━━━",
        f'🔗 <a href="{solscan_from}">Solscan</a> | '
        f'<a href="{dex_url}">DexScreener</a> | '
        f'<a href="{bubbles_url}">Bubblemaps</a>',
    ]
    return "\n".join(lines)


def process_transaction(tx: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """
    Parse one Helius enhanced transaction.

    Returns list of (alert_text, reply_markup) tuples for any whale transfers
    that exceed the configured threshold. Parse errors per transfer are logged
    and skipped — never raised.
    """
    results:  list[tuple[str, dict[str, Any]]] = []
    sig      = tx.get("signature", "unknown")
    tx_type  = tx.get("type", "UNKNOWN")
    ts       = tx.get("timestamp", 0)
    time_str = datetime.utcfromtimestamp(ts).strftime("%H:%M:%S UTC") if ts else "—"

    log.info("Processing tx %s... type=%s ts=%s", sig[:16], tx_type, time_str)

    for tt in tx.get("tokenTransfers", []):
        try:
            mint = tt.get("mint", "")
            if mint not in TOKEN_REGISTRY:
                continue

            symbol    = TOKEN_REGISTRY[mint]
            token_cfg = _cfg.get("solana_tokens", {}).get(symbol, {})
            emoji     = token_cfg.get("emoji", "🪙")
            amount    = float(tt.get("tokenAmount", 0) or 0)
            from_addr = tt.get("fromUserAccount", "") or ""
            to_addr   = tt.get("toUserAccount", "")   or ""
            direction = _transfer_direction(tx, mint)

            price = get_token_price_usd(mint)
            # Never alert on a defaulted price. get_token_price_usd returns 0.0 on
            # failure; a $0 USD value built on that is meaningless, so skip + log
            # rather than fall through to a token-count gate and emit a "~$0" whale.
            if not price:
                log.warning(
                    "  Skip %s %s: price unavailable — not alerting on a defaulted $0 value",
                    symbol, direction,
                )
                continue
            usd_val = round(amount * price, 2)
            if usd_val < WHALE_THRESHOLD_USD:
                log.info(
                    "  Skip %s %s: $%.2f < threshold $%.0f",
                    symbol, direction, usd_val, WHALE_THRESHOLD_USD,
                )
                continue

            log.info(
                "  🐳 WHALE %s %s %s — %,.0f tokens  $%.2f",
                symbol, direction, sig[:12], amount, usd_val,
            )

            alert = format_whale_alert(
                symbol, mint, emoji, direction,
                from_addr, to_addr, amount, price, usd_val,
                tx_type, time_str, sig,
            )
            # Use the from_addr for the wallet Solscan link in the keyboard
            wallet_for_kb = from_addr or to_addr
            kb = make_inline_keyboard(wallet_for_kb, mint)
            results.append((alert, kb))

        except (ValueError, TypeError, KeyError) as exc:
            log.warning("  Skipping malformed transfer in tx %s: %s", sig[:12], exc)
            continue

    return results


# ── Grok / X Sentiment ────────────────────────────────────────────────────────

def fetch_grok_sentiment(symbol: str, retries: int = 2) -> str | None:
    if not XAI_API_KEY:
        log.warning("XAI_API_KEY not set — skipping sentiment for %s", symbol)
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


# ── Holder status ─────────────────────────────────────────────────────────────

def send_holder_status(symbol: str, mint: str) -> None:
    holders = fetch_holders(mint)
    if not holders:
        log.warning("No holder data for %s — skipping status", symbol)
        return

    # Percent-of-supply uses TRUE circulating supply, never the sum of the top-20
    # holders (which inflates every figure). If supply can't be fetched, skip the
    # alert — never emit a %-of-supply number built on a degraded denominator.
    total_supply = fetch_token_supply(mint, HELIUS_API_KEY)
    if total_supply is None:
        log.error("Holder status for %s skipped — true supply unavailable", symbol)
        return

    top3_amt = sum(float(h.get("uiAmount") or h.get("amount", 0)) for h in holders[:3])
    top3     = pct_of_supply(top3_amt, total_supply)
    if top3 is None:
        log.error("Holder status for %s skipped — supply invalid", symbol)
        return
    flag = "🔴" if top3 > 50 else "🟡" if top3 > 30 else "🟢"

    lines = [
        f"👥 <b>Holder Status — {symbol}</b>",
        f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"{flag} Top 3 concentration: {top3:.1f}%\n",
    ]
    for i, h in enumerate(holders[:10], 1):
        amt = float(h.get("uiAmount") or h.get("amount", 0))
        pct = pct_of_supply(amt, total_supply) or 0.0
        lines.append(f"{i:2d}. <code>{shorten_addr(h.get('address', ''))}</code>  {pct:.2f}%")

    ok, err = send_alert("\n".join(lines))
    if not ok:
        log.error("Failed to send holder status for %s: %s", symbol, err)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    """Health check — returns config summary so you can verify Railway is live."""
    return JSONResponse({
        "status":              "ok",
        "tokens_monitored":    list(TOKEN_REGISTRY.values()),
        "whale_threshold_usd": WHALE_THRESHOLD_USD,
        "timestamp":           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


@app.post("/webhook/helius")
async def helius_webhook(request: Request) -> JSONResponse:
    """
    Accept POST requests from Helius enhanced webhook.

    Always returns 200 so Helius does not retry on parse errors.
    Returns 401 only on an explicit bad signature (intentional rejection).
    """
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    body    = await request.body()
    log.info("Helius webhook received at %s — %d bytes", now_str, len(body))

    # Signature check
    signature = request.headers.get("authorization", "")
    if not HELIUS_WEBHOOK_SECRET:
        log.error("HELIUS_WEBHOOK_SECRET not configured — rejecting webhook POST")
        return JSONResponse({"error": "auth_not_configured"}, status_code=401)
    if not verify_helius_signature(signature, HELIUS_WEBHOOK_SECRET):
        log.warning("Rejected webhook — invalid auth token")
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    # Parse — return 200 even on bad JSON to stop Helius retry spam
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON in webhook body: %s", exc)
        return JSONResponse({"received": 0, "alerts_sent": 0, "error": "invalid_json"})

    transactions: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
    log.info("Processing %d transaction(s)", len(transactions))

    alerts_sent = 0
    for tx in transactions:
        try:
            for alert_text, kb in process_transaction(tx):
                ok, err = send_alert(alert_text, reply_markup=kb)
                if ok:
                    alerts_sent += 1
                else:
                    log.error("Telegram delivery failed: %s", err)
        except Exception as exc:
            log.error("Unexpected error processing tx %s: %s", tx.get("signature", "?")[:12], exc)
            continue  # never crash on a single transaction

    return JSONResponse({"received": len(transactions), "alerts_sent": alerts_sent})


@app.post("/webhook/test")
async def webhook_test(request: Request) -> JSONResponse:
    """
    Send a test whale alert to Telegram.

    Body: {"token": "ALON", "amount_usd": 1500}
    No auth required. Used to verify Telegram connectivity and alert formatting.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    symbol     = str(body.get("token", "ALON")).upper()
    amount_usd = float(body.get("amount_usd", 1000))
    mint       = SYMBOL_TO_MINT.get(symbol, list(SYMBOL_TO_MINT.values())[0] if SYMBOL_TO_MINT else "")
    token_cfg  = _cfg.get("solana_tokens", {}).get(symbol, {})
    emoji      = token_cfg.get("emoji", "🪙")
    price      = get_token_price_usd(mint) if mint else 0.0
    amount     = (amount_usd / price) if price else amount_usd
    time_str   = datetime.utcnow().strftime("%H:%M:%S UTC")

    alert = format_whale_alert(
        symbol, mint or "TEST", emoji,
        "📥 BUY",
        "TestFromWallet1111111111111111111111111111",
        "TestToWallet22222222222222222222222222222222",
        amount, price, amount_usd,
        "TEST", time_str, "TEST_SIG_1234567890",
    )

    kb = make_inline_keyboard(
        "TestFromWallet1111111111111111111111111111",
        mint or "TEST",
    )

    ok, err = send_telegram(alert, reply_markup=kb)
    log.info("Test alert for %s $%.0f — sent=%s", symbol, amount_usd, ok)

    return JSONResponse({
        "sent":      ok,
        "error":     err or None,
        "message":   alert,
        "symbol":    symbol,
        "amount_usd": amount_usd,
    })


@app.post("/trigger/sentiment")
async def trigger_sentiment(request: Request) -> JSONResponse:
    """Manually trigger X sentiment digest. Protected by HELIUS_WEBHOOK_SECRET if set."""
    if HELIUS_WEBHOOK_SECRET:
        key = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(key, HELIUS_WEBHOOK_SECRET):
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)

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
                ok, err = send_alert(msg)
                results[symbol] = "sent" if ok else f"telegram_error: {err}"
            else:
                results[symbol] = "no_sentiment_returned"
        except Exception as exc:
            log.error("Sentiment error for %s: %s", symbol, exc)
            results[symbol] = f"error: {exc}"

    return JSONResponse({"status": "complete", "results": results})


@app.get("/api/cron")
async def cron(request: Request) -> JSONResponse:
    """Periodic holder status + sentiment. Call from any scheduler."""
    if CRON_SECRET:
        provided = request.headers.get("x-cron-secret", "")
        if not hmac.compare_digest(provided, CRON_SECRET):
            return JSONResponse({"error": "invalid_cron_secret"}, status_code=401)

    log.info("Cron triggered — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    dispatched: list[str] = []

    for symbol in SENTIMENT_TOKENS:
        mint = SYMBOL_TO_MINT.get(symbol)

        if mint:
            try:
                send_holder_status(symbol, mint)
                dispatched.append(f"{symbol}:holders")
            except Exception as exc:
                log.error("Holder status failed for %s: %s", symbol, exc)

        try:
            sentiment = fetch_grok_sentiment(symbol)
            if sentiment:
                msg = (
                    f"𝕏 <b>X Sentiment — ${symbol}</b>\n"
                    f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                    f"{sentiment}"
                )
                ok, err = send_alert(msg)
                if ok:
                    dispatched.append(f"{symbol}:sentiment")
                else:
                    log.error("Sentiment Telegram failed for %s: %s", symbol, err)
        except Exception as exc:
            log.error("Sentiment error for %s: %s — skipping", symbol, exc)
            continue

    return JSONResponse({"cron": "complete", "dispatched": dispatched})
