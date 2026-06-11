"""
Helius Webhook Receiver + Telegram Command Bot
===============================================
Deploy as a **separate** Railway service from the same repo.

Receives real-time transaction notifications from Helius and forwards:
  - Whale transfer alerts to Telegram
  - Scheduled X / Grok sentiment digests to Telegram

Also runs an async Telegram bot polling loop (via FastAPI lifespan) that
lets you control the monitor from Telegram:
  /status                — current thresholds and tracked tokens
  /snapshot              — live top-10 holders for all tracked tokens
  /holders SYM           — live top-20 holders for one token
  /coins                 — list tracked coins with addresses
  /addtoken SYM ADDR     — add a coin to tracking (alias: /addcoin)
  /removetoken SYM       — remove a coin from tracking (alias: /removecoin)
  /threshold <usd>       — set whale_threshold_usd (e.g. /threshold 2000)
  /movethreshold <pct>   — set move_threshold_pct (e.g. /movethreshold 0.005)
  /mintokens <n>         — set min_holder_change_tokens (0 to disable)
  /related               — recent cross-coin wallet overlaps from Supabase

Config changes written via commands are stored in Supabase bot_config and
take effect on the next monitor.py cron run (within 15 minutes).

Environment variables (set in Railway service):
    TELEGRAM_BOT_TOKEN      Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID        Target chat or channel ID
    HELIUS_WEBHOOK_SECRET   Optional HMAC secret set in Helius dashboard
    WHALE_THRESHOLD_USD     Minimum USD value to trigger whale alert (default 10000)
    XAI_API_KEY             xAI / Grok API key for X sentiment analysis
    SENTIMENT_TOKENS        Comma-separated list of token symbols to analyse (default ALON)
    SUPABASE_URL            Supabase project URL
    SUPABASE_SERVICE_KEY    Supabase service-role key (bypasses RLS)

Railway start command:
    uvicorn webhook:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
TELEGRAM_CHANNEL_ID   = os.environ.get("TELEGRAM_CHANNEL_ID", "")
HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "")
WHALE_THRESHOLD_USD   = float(os.environ.get("WHALE_THRESHOLD_USD", "10000"))
XAI_API_KEY           = os.environ.get("XAI_API_KEY", "")
SENTIMENT_TOKENS      = [t.strip() for t in os.environ.get("SENTIMENT_TOKENS", "ALON").split(",")]
SUPABASE_URL          = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY          = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)
HELIUS_API_KEY        = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC_URL        = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if os.environ.get("HELIUS_API_KEY") else "https://api.mainnet-beta.solana.com"

# Mint address → symbol mapping
TOKEN_REGISTRY: dict[str, str] = {
    "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS": "ALON",
    "5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2": "TROLL",
}

# Telegram bot polling offset (module-level to survive between loop iterations)
_tg_offset: int = 0


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _get_supabase():
    """Return a supabase Client or None if not configured."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as exc:
        log.error("Supabase client init failed: %s", exc)
        return None


def _bot_config_get(key: str) -> str | None:
    sb = _get_supabase()
    if not sb:
        return None
    try:
        res = sb.table("bot_config").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except Exception as exc:
        log.warning("bot_config get '%s' failed: %s", key, exc)
        return None


def _bot_config_set(key: str, value: str) -> bool:
    sb = _get_supabase()
    if not sb:
        return False
    try:
        existing = sb.table("bot_config").select("key").eq("key", key).execute()
        now = datetime.now(timezone.utc).isoformat()
        if existing.data:
            sb.table("bot_config").update({"value": value, "updated_at": now}).eq("key", key).execute()
        else:
            sb.table("bot_config").insert({"key": key, "value": value, "updated_at": now}).execute()
        return True
    except Exception as exc:
        log.error("bot_config set '%s' failed: %s", key, exc)
        return False


def _get_tracked_tokens() -> dict[str, str]:
    """Return tracked tokens from bot_config, falling back to TOKEN_REGISTRY values."""
    raw = _bot_config_get("tracked_tokens")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    # Derive from TOKEN_REGISTRY as fallback
    return {sym: addr for addr, sym in TOKEN_REGISTRY.items()}


def _set_tracked_tokens(tokens: dict[str, str]) -> bool:
    return _bot_config_set("tracked_tokens", json.dumps(tokens))


# ── Solana RPC helpers (for bot commands) ────────────────────────────────────

def fetch_holders_sync(mint: str, top_n: int = 20) -> list[dict]:
    """Fetch top-N token holders via Helius (or fallback) RPC. Never raises."""
    urls = [HELIUS_RPC_URL, "https://api.mainnet-beta.solana.com"]
    for rpc_url in urls:
        try:
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
                      "params": [mint]},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            accounts = data.get("result", {}).get("value", [])
            total = sum(float(a.get("uiAmount", 0) or 0) for a in accounts)
            holders = []
            for i, acc in enumerate(accounts[:top_n]):
                amt = float(acc.get("uiAmount", 0) or 0)
                pct = (amt / total * 100) if total else 0
                holders.append({
                    "rank":    i + 1,
                    "address": acc.get("address", ""),
                    "amount":  amt,
                    "pct":     pct,
                })
            return holders
        except Exception as exc:
            log.warning("fetch_holders_sync failed on %s for %s: %s",
                        rpc_url[:40], mint[:8], exc)
    return []


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:.4f}"


def shorten_addr(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr else "—"


# ── Telegram sync (for webhook alerts) ───────────────────────────────────────

def send_telegram(msg: str, retries: int = 3, *, chat_id: str = "") -> tuple[bool, str]:
    """Send HTML-formatted message to a single Telegram chat with exponential backoff."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        log.warning("Telegram credentials not configured — skipping alert")
        return False, "not_configured"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": msg, "parse_mode": "HTML",
               "disable_web_page_preview": True}

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


def send_alert(msg: str, reply_markup: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Send alert to channel when configured, else fall back to owner chat."""
    target = TELEGRAM_CHANNEL_ID or TELEGRAM_CHAT_ID
    if not target:
        return False, "no_targets"
    try:
        return send_telegram(msg, chat_id=target)
    except Exception as exc:
        return False, str(exc)


# ── Telegram async (for bot command polling) ──────────────────────────────────

async def _tg_send_async(client: httpx.AsyncClient, chat_id: str | int, text: str) -> None:
    """Send a plain-text reply from the bot (async)."""
    try:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as exc:
        log.error("Async Telegram send failed: %s", exc)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_status(client: httpx.AsyncClient, chat_id: int) -> None:
    move_pct  = _bot_config_get("move_threshold_pct") or "0.01"
    mintokens = _bot_config_get("min_holder_change_tokens") or "0"
    whale_usd = _bot_config_get("whale_threshold_usd") or str(int(WHALE_THRESHOLD_USD))
    tokens    = _get_tracked_tokens()
    coin_list = ", ".join(tokens.keys()) or "(none)"
    text = (
        f"📊 <b>Monitor Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Move threshold: {float(move_pct):.4f}%\n"
        f"🐳 Whale threshold: ${float(whale_usd):,.0f} USD\n"
        f"🪙 Min tokens: {mintokens}\n"
        f"🔍 Tracked: {coin_list}\n"
        f"⏱ Cron: every 15 min\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Changes take effect within 15 min."
    )
    await _tg_send_async(client, chat_id, text)


async def cmd_threshold(client: httpx.AsyncClient, chat_id: int, args: str) -> None:
    """Set whale_threshold_usd (minimum USD for real-time whale alerts)."""
    if not args:
        await _tg_send_async(client, chat_id, "Usage: /threshold <usd>  e.g. /threshold 2000")
        return
    try:
        val = float(args.strip())
        if val < 0:
            raise ValueError("negative")
    except ValueError:
        await _tg_send_async(client, chat_id, f"❌ Invalid value: {args!r} — must be a positive number like 2000")
        return
    old = _bot_config_get("whale_threshold_usd") or str(int(WHALE_THRESHOLD_USD))
    if _bot_config_set("whale_threshold_usd", str(val)):
        await _tg_send_async(
            client, chat_id,
            f"✅ Whale threshold updated: ${float(old):,.0f} → ${val:,.0f} USD\n"
            f"Takes effect on next run (within 15 min)."
        )
    else:
        await _tg_send_async(client, chat_id, "❌ Failed to update whale threshold (Supabase error — check Railway logs).")


async def cmd_movethreshold(client: httpx.AsyncClient, chat_id: int, args: str) -> None:
    """Set move_threshold_pct (minimum % supply move for holder change alerts)."""
    if not args:
        await _tg_send_async(client, chat_id, "Usage: /movethreshold <pct>  e.g. /movethreshold 0.005")
        return
    try:
        val = float(args.strip())
        if val < 0:
            raise ValueError("negative")
    except ValueError:
        await _tg_send_async(client, chat_id, f"❌ Invalid value: {args!r} — must be a positive number like 0.01")
        return
    old = _bot_config_get("move_threshold_pct") or "0.01"
    if _bot_config_set("move_threshold_pct", str(val)):
        await _tg_send_async(
            client, chat_id,
            f"✅ Move threshold updated: {float(old):.4f}% → {val:.4f}%\n"
            f"Takes effect on next run (within 15 min)."
        )
    else:
        await _tg_send_async(client, chat_id, "❌ Failed to update move threshold (Supabase error — check Railway logs).")


async def cmd_mintokens(client: httpx.AsyncClient, chat_id: int, args: str) -> None:
    if not args:
        await _tg_send_async(client, chat_id, "Usage: /mintokens <n>  e.g. /mintokens 0  (0 disables this gate)")
        return
    try:
        val = int(float(args.strip()))
    except ValueError:
        await _tg_send_async(client, chat_id, f"❌ Invalid value: {args!r} — must be an integer")
        return
    old = _bot_config_get("min_holder_change_tokens") or "?"
    if _bot_config_set("min_holder_change_tokens", str(val)):
        await _tg_send_async(
            client, chat_id,
            f"✅ Min tokens updated: {old} → {val}\nTakes effect on next run (within 15 min)."
        )
    else:
        await _tg_send_async(client, chat_id, "❌ Failed to update (Supabase error — check Railway logs).")


async def cmd_snapshot(client: httpx.AsyncClient, chat_id: int) -> None:
    """Show current top-10 holders for all tracked tokens."""
    tokens = _get_tracked_tokens()
    if not tokens:
        await _tg_send_async(client, chat_id, "No coins currently tracked.")
        return
    await _tg_send_async(client, chat_id, f"⏳ Fetching holders for {', '.join(tokens.keys())}…")
    for sym, mint in tokens.items():
        holders = fetch_holders_sync(mint, top_n=10)
        if not holders:
            await _tg_send_async(client, chat_id, f"❌ Could not fetch holders for {sym}.")
            continue
        lines = [f"📊 <b>{sym} — Top 10 Holders</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
        for h in holders:
            addr = h["address"]
            short = f"{addr[:6]}…{addr[-4:]}"
            lines.append(
                f"#{h['rank']:>2}  {h['pct']:.2f}%  "
                f"<code>{short}</code>  "
                f"({h['amount']:,.0f})"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        await _tg_send_async(client, chat_id, "\n".join(lines))


async def cmd_holders(client: httpx.AsyncClient, chat_id: int, args: str) -> None:
    """Show top-20 holders for a specific token symbol."""
    sym = args.strip().upper()
    if not sym:
        await _tg_send_async(client, chat_id, "Usage: /holders SYMBOL  e.g. /holders ALON")
        return
    tokens = _get_tracked_tokens()
    mint = tokens.get(sym)
    if not mint:
        known = ", ".join(tokens.keys()) or "(none)"
        await _tg_send_async(client, chat_id, f"❌ {sym} not tracked. Known: {known}")
        return
    await _tg_send_async(client, chat_id, f"⏳ Fetching top-20 holders for {sym}…")
    holders = fetch_holders_sync(mint, top_n=20)
    if not holders:
        await _tg_send_async(client, chat_id, f"❌ Could not fetch holders for {sym}.")
        return
    lines = [f"📊 <b>{sym} — Top 20 Holders</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    for h in holders:
        addr = h["address"]
        short = f"{addr[:6]}…{addr[-4:]}"
        lines.append(
            f"#{h['rank']:>2}  {h['pct']:.2f}%  "
            f"<code>{short}</code>  "
            f"({h['amount']:,.0f})"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    await _tg_send_async(client, chat_id, "\n".join(lines))


async def cmd_coins(client: httpx.AsyncClient, chat_id: int) -> None:
    tokens = _get_tracked_tokens()
    if not tokens:
        await _tg_send_async(client, chat_id, "No coins currently tracked.")
        return
    lines = ["🔍 <b>Tracked Coins</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    for sym, addr in tokens.items():
        short = f"{addr[:8]}...{addr[-4:]}"
        lines.append(f"<b>{sym}</b>  <code>{short}</code>")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Use /addcoin SYM ADDR or /removecoin SYM to modify."
    ]
    await _tg_send_async(client, chat_id, "\n".join(lines))


async def cmd_addcoin(client: httpx.AsyncClient, chat_id: int, args: str) -> None:
    parts = args.strip().split()
    if len(parts) != 2:
        await _tg_send_async(client, chat_id,
            "Usage: /addcoin SYMBOL MINT_ADDRESS\ne.g. /addcoin BONK DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
        return
    sym, addr = parts[0].upper(), parts[1]
    if len(addr) < 32:
        await _tg_send_async(client, chat_id, "❌ Address looks too short — check the mint address.")
        return
    tokens = _get_tracked_tokens()
    tokens[sym] = addr
    if _set_tracked_tokens(tokens):
        await _tg_send_async(client, chat_id,
            f"✅ Added <b>{sym}</b> (<code>{addr[:8]}...{addr[-4:]}</code>)\n"
            f"Now tracking: {', '.join(tokens.keys())}\nTakes effect within 15 min.")
    else:
        await _tg_send_async(client, chat_id, "❌ Failed to update tracked_tokens in Supabase.")


async def cmd_removecoin(client: httpx.AsyncClient, chat_id: int, args: str) -> None:
    sym = args.strip().upper()
    if not sym:
        await _tg_send_async(client, chat_id, "Usage: /removecoin SYMBOL  e.g. /removecoin TROLL")
        return
    tokens = _get_tracked_tokens()
    if sym not in tokens:
        await _tg_send_async(client, chat_id, f"❌ {sym} is not in the tracked list. Use /coins to see current list.")
        return
    del tokens[sym]
    if _set_tracked_tokens(tokens):
        remaining = ", ".join(tokens.keys()) or "(none)"
        await _tg_send_async(client, chat_id,
            f"✅ Removed <b>{sym}</b>.\nNow tracking: {remaining}\nTakes effect within 15 min.")
    else:
        await _tg_send_async(client, chat_id, "❌ Failed to update tracked_tokens in Supabase.")


async def cmd_related(client: httpx.AsyncClient, chat_id: int) -> None:
    sb = _get_supabase()
    if not sb:
        await _tg_send_async(client, chat_id, "❌ Supabase not configured — cannot fetch relationships.")
        return
    try:
        res = (
            sb.table("wallet_relationships")
            .select("wallet_address,coin_a,coin_a_pct,coin_b,coin_b_pct,detected_at")
            .order("detected_at", desc=True)
            .limit(20)
            .execute()
        )
        rows = res.data or []
    except Exception as exc:
        await _tg_send_async(client, chat_id, f"❌ Supabase query failed: {exc}")
        return

    if not rows:
        await _tg_send_async(client, chat_id, "No cross-coin wallet relationships recorded yet.")
        return

    # Deduplicate by wallet_address, show most recent per wallet
    seen: set[str] = set()
    lines = ["🕸 <b>Cross-coin Wallet Overlaps</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    count = 0
    for row in rows:
        addr = row["wallet_address"]
        if addr in seen:
            continue
        seen.add(addr)
        short = f"{addr[:8]}...{addr[-6:]}"
        lines.append(
            f"<code>{short}</code>\n"
            f"  {row['coin_a']}: {row['coin_a_pct']:.2f}%  |  {row['coin_b']}: {row['coin_b_pct']:.2f}%"
        )
        count += 1
        if count >= 8:
            break
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    await _tg_send_async(client, chat_id, "\n".join(lines))


# ── Telegram command dispatcher ───────────────────────────────────────────────

async def handle_command(client: httpx.AsyncClient, update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    text    = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")
    if not text or not chat_id:
        return
    if not text.startswith("/"):
        return

    # Only respond to the configured chat
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        log.info("Ignored command from unauthorized chat %s", chat_id)
        return

    parts   = text.split(None, 1)
    command = parts[0].split("@")[0].lower()  # strip @botname suffix
    args    = parts[1] if len(parts) > 1 else ""

    log.info("Bot command: %s %r from chat %s", command, args, chat_id)

    if command == "/status":
        await cmd_status(client, chat_id)
    elif command == "/threshold":
        await cmd_threshold(client, chat_id, args)
    elif command == "/movethreshold":
        await cmd_movethreshold(client, chat_id, args)
    elif command == "/mintokens":
        await cmd_mintokens(client, chat_id, args)
    elif command == "/snapshot":
        await cmd_snapshot(client, chat_id)
    elif command == "/holders":
        await cmd_holders(client, chat_id, args)
    elif command == "/coins":
        await cmd_coins(client, chat_id)
    elif command in ("/addcoin", "/addtoken"):
        await cmd_addcoin(client, chat_id, args)
    elif command in ("/removecoin", "/removetoken"):
        await cmd_removecoin(client, chat_id, args)
    elif command == "/related":
        await cmd_related(client, chat_id)
    elif command in ("/start", "/help"):
        await _tg_send_async(client, chat_id, (
            "🤖 <b>Monitor Bot Commands</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "/status — config + thresholds\n"
            "/snapshot — top-10 holders for all coins\n"
            "/holders SYM — top-20 holders for one coin\n"
            "/coins — list tracked coins\n"
            "/addtoken SYM ADDR — add a coin to tracking\n"
            "/removetoken SYM — remove a coin from tracking\n"
            "/threshold &lt;usd&gt; — whale alert min USD (e.g. 2000)\n"
            "/movethreshold &lt;pct&gt; — holder move alert % (e.g. 0.005)\n"
            "/mintokens &lt;n&gt; — min token gate (0 to disable)\n"
            "/related — cross-coin whale overlaps\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Config changes take effect within 15 min."
        ))
    else:
        await _tg_send_async(client, chat_id,
            f"Unknown command: {command}\nSend /help for the list of available commands.")


# ── Telegram long-poll loop ───────────────────────────────────────────────────

async def telegram_command_loop() -> None:
    """Poll Telegram for bot updates and dispatch commands. Runs forever."""
    global _tg_offset
    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — bot command loop not started")
        return

    log.info("Telegram command bot polling started")
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                resp = await client.get(
                    f"{base_url}/getUpdates",
                    params={
                        "offset":          _tg_offset,
                        "timeout":         30,
                        "allowed_updates": ["message"],
                    },
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
                for upd in updates:
                    _tg_offset = upd["update_id"] + 1
                    try:
                        await handle_command(client, upd)
                    except Exception as exc:
                        log.error("Command handler error for update %s: %s", upd.get("update_id"), exc)
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                pass  # long-poll timeout is normal — loop immediately
            except Exception as exc:
                log.error("Bot polling error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)


# ── FastAPI lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Telegram command polling is handled by bot_commands.py (via main.py daemon thread).
    # webhook.py handles only Helius webhooks and HTTP endpoints.
    yield


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Portfolio Webhook Receiver",
    description="Receives Helius webhooks, forwards whale alerts, and runs a Telegram command bot.",
    version="2.0.0",
    lifespan=lifespan,
)


# ── Grok / X Sentiment ────────────────────────────────────────────────────────

def fetch_grok_sentiment(symbol: str, retries: int = 3) -> str | None:
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
            ok, err = send_alert(msg)
            if not ok:
                log.error("Failed to send sentiment digest for %s: %s", symbol, err)
            else:
                log.info("Sentiment digest sent for %s", symbol)
        except Exception as exc:
            log.error("Unexpected error in sentiment digest for %s: %s", symbol, exc)
            continue


# ── Helius webhook processing ─────────────────────────────────────────────────

def verify_helius_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return True
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def process_transaction(tx: dict[str, Any]) -> list[str]:
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
                f"📤 From: {shorten_addr(from_addr)}\n"
                f"📋 <code>{from_addr or '—'}</code>\n"
                f"📥 To:   {shorten_addr(to_addr)}\n"
                f"📋 <code>{to_addr or '—'}</code>\n"
                f"🕐 {time_str}  |  {tx_type}\n"
                f"🔗 <code>{sig}...</code>"
            )
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Skipping malformed token transfer in tx %s: %s", sig, exc)
            continue

    return alerts


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    import sys
    _main = sys.modules.get("main") or sys.modules.get("__main__")
    bot_thread = getattr(_main, "bot_thread", None) if _main else None
    bot_alive = bot_thread is not None and bot_thread.is_alive()
    payload = {
        "status": "ok" if bot_alive else "degraded",
        "bot_thread": "alive" if bot_alive else "dead",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
        "supabase_configured": bool(SUPABASE_URL),
    }
    status_code = 200 if bot_alive else 503
    return JSONResponse(payload, status_code=status_code)


@app.post("/webhook/helius")
async def helius_webhook(request: Request) -> JSONResponse:
    body = await request.body()

    signature = request.headers.get("authorization", "")
    if not HELIUS_WEBHOOK_SECRET:
        log.error("HELIUS_WEBHOOK_SECRET not configured — rejecting webhook POST")
        raise HTTPException(status_code=401, detail="Webhook auth not configured")
    if not verify_helius_signature(body, signature, HELIUS_WEBHOOK_SECRET):
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
                ok, err = send_alert(alert)
                if ok:
                    alerts_sent += 1
                else:
                    log.error("Telegram delivery failed: %s", err)
        except Exception as exc:
            log.error("Unexpected error processing tx %s: %s", tx.get("signature", "?")[:12], exc)
            continue

    return JSONResponse({"received": len(transactions), "alerts_sent": alerts_sent})


@app.post("/trigger/sentiment")
async def trigger_sentiment(request: Request) -> JSONResponse:
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
