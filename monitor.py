"""
Holder Concentration Monitor
=============================
Run standalone or via GitHub Actions cron job (see .github/workflows/monitor.yml).

Environment variables:
    HELIUS_API_KEY            Helius RPC API key
    TELEGRAM_BOT_TOKEN        Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID          Target chat or channel ID
    SUPABASE_URL              Supabase project URL (https://xxx.supabase.co)
    SUPABASE_SERVICE_ROLE_KEY Supabase service-role key (bypasses RLS)
    ANTHROPIC_API_KEY         Anthropic API key for AI interpretation
    MOVE_THRESHOLD_PCT        % supply change to trigger an alert (overridden by bot_config)
    MIN_HOLDER_CHANGE_TOKENS  raw token amount threshold (overridden by bot_config)
"""

from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from supabase import Client, create_client
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
HELIUS_API_KEY      = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SNAPSHOT_DIR       = os.path.join(os.path.dirname(__file__), "snapshots")

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_config() -> dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load config.json (%s) — using defaults", exc)
        return {}


_cfg = _load_config()

MOVE_THRESHOLD_PCT = float(
    os.environ.get("MOVE_THRESHOLD_PCT", str(_cfg.get("move_threshold_pct", 1.0)))
)
MIN_HOLDER_CHANGE_TOKENS = float(
    os.environ.get("MIN_HOLDER_CHANGE_TOKENS", str(_cfg.get("min_holder_change_tokens", 0)))
)

PUBLIC_SOLANA_RPC = _cfg.get("helius_fallback_rpc", "https://api.mainnet-beta.solana.com")

TOKENS: dict[str, str] = {
    sym: info["address"]
    for sym, info in _cfg.get("solana_tokens", {}).items()
} or {"ALON": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"}

# Alert rate limiting
RATE_LIMIT_SECS    = 300
MAX_ALERTS_PER_RUN = 20

# Wallet intelligence (FIX 3)
WALLET_INTEL_CACHE_SECS    = 600   # 10 min
WALLET_INTEL_MIN_DELTA_PCT = 0.1   # only fetch for moves >= 0.1%
PRICE_CACHE_SECS           = 60    # 60 sec price cache

MAJOR_TOKEN_MINTS: dict[str, str] = {
    "BONK":   "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JUP":    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "HANTA":  "2tXpgu2DLTsPUf9zFmuZmA4xrYxXKBTpVq9wAM7hzs9y",
    "PUMP":   "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
}
WSOL_MINT = "So11111111111111111111111111111111111111112"

# ── Module-level run state ────────────────────────────────────────────────────
DRY_RUN: bool                                    = False  # set via --dry-run
_ai_cache: dict[str, tuple[str, float]]          = {}
_coord_ai_cache: dict[str, tuple[str, float]]    = {}
_alert_timestamps: dict[str, float]              = {}
_wallet_intel_cache: dict[str, tuple[dict, float]] = {}
_price_cache: dict[str, tuple[dict, float]]      = {}
_hourly_alert_count: int                         = 0
_hourly_flows: list[dict[str, Any]]              = []


# ── Supabase client ───────────────────────────────────────────────────────────

def init_supabase() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("Supabase not configured — set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")
        return None
    try:
        client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        client.table("wallet_snapshots").select("id").limit(1).execute()
        log.info("✅ Supabase connection OK — %s", SUPABASE_URL)
        return client
    except Exception as exc:
        log.error("❌ Supabase connection FAILED: %s", exc)
        return None


_supabase: Client | None = init_supabase()


def _load_bot_config_from_supabase() -> None:
    """Override config.json values with live settings written by the Telegram bot."""
    global MOVE_THRESHOLD_PCT, MIN_HOLDER_CHANGE_TOKENS, TOKENS
    if _supabase is None:
        return
    try:
        result = _supabase.table("bot_config").select("key,value").execute()
        cfg = {row["key"]: row["value"] for row in (result.data or [])}

        if "move_threshold_pct" in cfg:
            MOVE_THRESHOLD_PCT = float(cfg["move_threshold_pct"])
            log.info("bot_config override: move_threshold_pct=%.4f%%", MOVE_THRESHOLD_PCT)

        if "min_holder_change_tokens" in cfg:
            MIN_HOLDER_CHANGE_TOKENS = float(cfg["min_holder_change_tokens"])
            log.info("bot_config override: min_holder_change_tokens=%.0f", MIN_HOLDER_CHANGE_TOKENS)

        if "tracked_tokens" in cfg:
            try:
                tracked = json.loads(cfg["tracked_tokens"])
                if isinstance(tracked, dict) and tracked:
                    TOKENS.clear()
                    TOKENS.update(tracked)
                    log.info("bot_config override: tracked_tokens=%s", list(TOKENS.keys()))
            except json.JSONDecodeError:
                pass
    except Exception as exc:
        log.warning("Failed to load bot_config from Supabase: %s", exc)


# ── Supabase writers ──────────────────────────────────────────────────────────

def write_snapshot_to_supabase(symbol: str, token_address: str, holders: list[dict]) -> None:
    if DRY_RUN:
        log.info("[DRY RUN] Supabase wallet_snapshots write skipped (%s)", symbol)
        return
    if _supabase is None:
        return
    total        = sum(get_amount(h) for h in holders) or 1.0
    top10_pct    = sum(get_amount(h) / total * 100 for h in holders[:10])
    holder_count = len(holders)
    captured_at  = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "token_address": token_address, "token_symbol": symbol, "symbol": symbol,
            "wallet_address": h["address"], "rank": rank,
            "balance": get_amount(h),
            "pct_supply": round(get_amount(h) / total * 100, 6),
            "captured_at": captured_at, "holder_count": holder_count,
            "top10_pct": round(top10_pct, 4),
        }
        for rank, h in enumerate(holders, 1)
    ]
    try:
        _supabase.table("wallet_snapshots").insert(rows).execute()
        log.info("  ✅ %d rows → wallet_snapshots (%s)", len(rows), symbol)
    except Exception as exc:
        log.error("  ❌ wallet_snapshots insert failed for %s: %s", symbol, exc)


def write_alert_to_supabase(symbol: str, token_address: str, change: dict, telegram_sent: bool) -> None:
    if DRY_RUN:
        log.info("[DRY RUN] Supabase whale_alerts write skipped (%s)", symbol)
        return
    if _supabase is None:
        return
    now         = datetime.now(timezone.utc).isoformat()
    token_delta = change.get("tokens_delta") or change.get("token_delta") or 0
    delta       = change.get("delta", 0)
    flow_type   = (
        "entry" if change["type"] == "NEW"
        else "exit" if change["type"] == "EXIT"
        else "buy"  if delta > 0
        else "sell"
    )
    try:
        _supabase.table("whale_alerts").insert({
            "token_address": token_address, "token_symbol": symbol, "symbol": symbol,
            "wallet_address": change["address"], "change_type": change["type"],
            "old_pct": change.get("old_pct"), "new_pct": change.get("new_pct"),
            "delta_pct": round(delta, 6), "token_delta": token_delta,
            "trigger": change.get("trigger", "pct"),
            "alerted_at": now, "telegram_sent": telegram_sent,
        }).execute()
    except Exception as exc:
        log.error("  ❌ whale_alerts insert failed: %s", exc)

    try:
        _supabase.table("wallet_flow_changes").insert({
            "token_address": token_address, "token_symbol": symbol, "symbol": symbol,
            "wallet_address": change["address"],
            "prev_balance": change.get("old_tokens"), "new_balance": change.get("new_tokens"),
            "change_amount": token_delta if delta >= 0 else -token_delta,
            "change_pct": round(delta, 6), "flow_type": flow_type, "detected_at": now,
        }).execute()
    except Exception as exc:
        log.error("  ❌ wallet_flow_changes insert failed: %s", exc)


def get_wallet_first_seen(address: str, symbol: str, rank: int) -> dict[str, Any]:
    default = {"age_days": None, "is_new_wallet": False}
    if _supabase is None:
        return default
    try:
        result = _supabase.table("wallet_metadata").select("first_seen").eq("address", address).execute()
        now = datetime.now(timezone.utc)
        if result.data:
            first_seen = datetime.fromisoformat(result.data[0]["first_seen"].replace("Z", "+00:00"))
            age_days   = (now - first_seen).days
            return {"age_days": age_days, "is_new_wallet": age_days < 1}
        else:
            _supabase.table("wallet_metadata").insert({
                "address": address, "symbol": symbol,
                "first_seen": now.isoformat(), "first_seen_rank": rank,
            }).execute()
            return {"age_days": 0, "is_new_wallet": True}
    except Exception as exc:
        log.warning("wallet_metadata lookup failed for %s: %s", address[:8], exc)
        return default


def persist_wallet_relationships(cross_holdings: dict[str, dict[str, float]]) -> None:
    if _supabase is None:
        return
    multi = {addr: h for addr, h in cross_holdings.items() if len(h) >= 2}
    if not multi:
        return
    rows = []
    for addr, holdings in multi.items():
        sym_list = sorted(holdings.keys())
        for i in range(len(sym_list)):
            for j in range(i + 1, len(sym_list)):
                rows.append({
                    "wallet_address": addr,
                    "coin_a": sym_list[i], "coin_a_pct": round(holdings[sym_list[i]], 4),
                    "coin_b": sym_list[j], "coin_b_pct": round(holdings[sym_list[j]], 4),
                })
    if rows:
        try:
            _supabase.table("wallet_relationships").insert(rows).execute()
            log.info("✅ %d wallet relationship row(s) written", len(rows))
        except Exception as exc:
            log.error("❌ wallet_relationships insert failed: %s", exc)

# ── Supabase writers ──────────────────────────────────────────────────────────

def write_snapshot_to_supabase(symbol: str, token_address: str, holders: list[dict]) -> None:
    if DRY_RUN:
        log.info("[DRY RUN] Supabase wallet_snapshots write skipped (%s)", symbol)
        return
    if _supabase is None:
        return
    total        = sum(get_amount(h) for h in holders) or 1.0
    top10_pct    = sum(get_amount(h) / total * 100 for h in holders[:10])
    holder_count = len(holders)
    captured_at  = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "token_address": token_address, "token_symbol": symbol, "symbol": symbol,
            "wallet_address": h["address"], "rank": rank,
            "balance": get_amount(h),
            "pct_supply": round(get_amount(h) / total * 100, 6),
            "captured_at": captured_at, "holder_count": holder_count,
            "top10_pct": round(top10_pct, 4),
        }
        for rank, h in enumerate(holders, 1)
    ]
    try:
        _supabase.table("wallet_snapshots").insert(rows).execute()
        log.info("  ✅ %d rows → wallet_snapshots (%s)", len(rows), symbol)
    except Exception as exc:
        log.error("  ❌ wallet_snapshots insert failed for %s: %s", symbol, exc)


def write_alert_to_supabase(symbol: str, token_address: str, change: dict, telegram_sent: bool) -> None:
    if DRY_RUN:
        log.info("[DRY RUN] Supabase whale_alerts write skipped (%s)", symbol)
        return
    if _supabase is None:
        return
    now         = datetime.now(timezone.utc).isoformat()
    token_delta = change.get("tokens_delta") or change.get("token_delta") or 0
    delta       = change.get("delta", 0)
    flow_type   = (
        "entry" if change["type"] == "NEW"
        else "exit" if change["type"] == "EXIT"
        else "buy"  if delta > 0
        else "sell"
    )
    try:
        _supabase.table("whale_alerts").insert({
            "token_address": token_address, "token_symbol": symbol, "symbol": symbol,
            "wallet_address": change["address"], "change_type": change["type"],
            "old_pct": change.get("old_pct"), "new_pct": change.get("new_pct"),
            "delta_pct": round(delta, 6), "token_delta": token_delta,
            "trigger": change.get("trigger", "pct"),
            "alerted_at": now, "telegram_sent": telegram_sent,
        }).execute()
    except Exception as exc:
        log.error("  ❌ whale_alerts insert failed: %s", exc)

    try:
        _supabase.table("wallet_flow_changes").insert({
            "token_address": token_address, "token_symbol": symbol, "symbol": symbol,
            "wallet_address": change["address"],
            "prev_balance": change.get("old_tokens"), "new_balance": change.get("new_tokens"),
            "change_amount": token_delta if delta >= 0 else -token_delta,
            "change_pct": round(delta, 6), "flow_type": flow_type, "detected_at": now,
        }).execute()
    except Exception as exc:
        log.error("  ❌ wallet_flow_changes insert failed: %s", exc)


def get_wallet_first_seen(address: str, symbol: str, rank: int) -> dict[str, Any]:
    default = {"age_days": None, "is_new_wallet": False}
    if _supabase is None:
        return default
    try:
        result = _supabase.table("wallet_metadata").select("first_seen").eq("address", address).execute()
        now = datetime.now(timezone.utc)
        if result.data:
            first_seen = datetime.fromisoformat(result.data[0]["first_seen"].replace("Z", "+00:00"))
            age_days   = (now - first_seen).days
            return {"age_days": age_days, "is_new_wallet": age_days < 1}
        else:
            _supabase.table("wallet_metadata").insert({
                "address": address, "symbol": symbol,
                "first_seen": now.isoformat(), "first_seen_rank": rank,
            }).execute()
            return {"age_days": 0, "is_new_wallet": True}
    except Exception as exc:
        log.warning("wallet_metadata lookup failed for %s: %s", address[:8], exc)
        return default


# ── Telegram ──────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=False,
)
def send_telegram(msg: str, reply_markup: dict[str, Any] | None = None, *, chat_id: str = "") -> tuple[bool, str]:
    if DRY_RUN:
        log.info("[DRY RUN] Telegram skipped: %s", msg[:80])
        return True, "dry-run"
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        log.warning("Telegram not configured — missing token or chat ID")
        return False, "not_configured"
    payload: dict[str, Any] = {
        "chat_id": target, "text": msg,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json=payload, timeout=10,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.error("Telegram HTTP error %s: %s", resp.status_code, resp.text[:200])
        return False, str(exc)
    return True, ""


def send_alert(msg: str, reply_markup: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Send alert to channel when configured, else fall back to owner chat."""
    target = TELEGRAM_CHANNEL_ID or TELEGRAM_CHAT_ID
    if not target:
        return False, "no_targets"
    try:
        result = send_telegram(msg, reply_markup, chat_id=target)
        return result if result else (False, "retry_exhausted")
    except Exception as exc:
        return False, str(exc)


def make_inline_keyboard(wallet_address: str, token_address: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "📊 DexScreener", "url": f"https://dexscreener.com/solana/{token_address}"},
            {"text": "🔍 Solscan",     "url": f"https://solscan.io/account/{wallet_address}"},
            {"text": "🫧 Bubblemaps",  "url": f"https://app.bubblemaps.io/sol/token/{token_address}"},
        ]]
    }


# ── Rate limiting ─────────────────────────────────────────────────────────────

def is_rate_limited(address: str) -> bool:
    global _hourly_alert_count
    if _hourly_alert_count >= MAX_ALERTS_PER_RUN:
        return True
    return (time.time() - _alert_timestamps.get(address, 0.0)) < RATE_LIMIT_SECS


def record_alert_sent(address: str) -> None:
    global _hourly_alert_count
    _alert_timestamps[address] = time.time()
    _hourly_alert_count += 1


# ── Severity ──────────────────────────────────────────────────────────────────

def get_severity(change: dict[str, Any]) -> tuple[str, str]:
    abs_delta = abs(change["delta"])
    if change["type"] in ("NEW", "EXIT") or abs_delta >= 1.0:
        return "CRITICAL", "🔴"
    elif abs_delta >= 0.5:
        return "SIGNIFICANT", "🟡"
    return "NOTABLE", "🟢"


# ── DexScreener price (FIX 4 — cached, includes 24h change) ─────────────────

def fetch_price_context(token_address: str) -> dict[str, Any]:
    cached = _price_cache.get(token_address)
    if cached and (time.time() - cached[1]) < PRICE_CACHE_SECS:
        return cached[0]
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=10,
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return {}
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        pc   = best.get("priceChange") or {}
        result = {
            "price":      float(best.get("priceUsd") or 0),
            "change_1h":  float(pc.get("h1")  or 0),
            "change_24h": float(pc.get("h24") or 0),
        }
        _price_cache[token_address] = (result, time.time())
        return result
    except Exception as exc:
        log.warning("DexScreener fetch failed for %s: %s", token_address[:8], exc)
        return {}


# ── Wallet intelligence (FIX 3) ───────────────────────────────────────────────

def fetch_wallet_intel(wallet_address: str, current_symbol: str) -> dict[str, Any]:
    """
    Fetch all SPL token holdings + SOL balance for a wallet.
    Checks for other monitored tokens and major Solana tokens.
    Cached for WALLET_INTEL_CACHE_SECS. Never raises.
    """
    cached = _wallet_intel_cache.get(wallet_address)
    if cached and (time.time() - cached[1]) < WALLET_INTEL_CACHE_SECS:
        return cached[0]

    result: dict[str, Any] = {
        "other_monitored": {},  # sym -> {"amount": float, "usd": float, "rank": int|None}
        "major_tokens":    {},  # sym -> {"amount": float, "usd": float}
        "sol_balance":     None,
        "sol_usd":         None,
        "total_usd_est":   None,
    }

    mints_of_interest: dict[str, str] = {}
    for sym, addr in TOKENS.items():
        if sym != current_symbol:
            mints_of_interest[addr] = sym
    for sym, addr in MAJOR_TOKEN_MINTS.items():
        mints_of_interest[addr] = sym

    rpc_url = (
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        if HELIUS_API_KEY else PUBLIC_SOLANA_RPC
    )

    try:
        spl_payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "getTokenAccountsByOwner",
            "params":  [
                wallet_address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"},
            ],
        }
        resp = requests.post(rpc_url, json=spl_payload, timeout=10)
        if resp.status_code == 403 and rpc_url != PUBLIC_SOLANA_RPC:
            log.warning("  Wallet intel: Helius 403 for %s — using public RPC", wallet_address[:8])
            resp = requests.post(PUBLIC_SOLANA_RPC, json=spl_payload, timeout=10)
        resp.raise_for_status()

        accounts  = (resp.json().get("result") or {}).get("value") or []
        total_usd = 0.0

        for acc in accounts:
            try:
                info   = acc["account"]["data"]["parsed"]["info"]
                mint   = info["mint"]
                amount = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
            except (KeyError, TypeError, ValueError):
                continue

            if mint not in mints_of_interest or amount <= 0:
                continue

            sym   = mints_of_interest[mint]
            price = fetch_price_context(mint).get("price") or 0.0
            usd   = round(amount * price, 2)
            total_usd += usd
            entry = {"amount": amount, "usd": usd}

            if sym in TOKENS and sym != current_symbol:
                result["other_monitored"][sym] = entry
            else:
                result["major_tokens"][sym] = entry

        # Rank lookup for other monitored tokens (uses local disk snapshot — fast, no RPC)
        for sym, entry in result["other_monitored"].items():
            snap = load_snapshot(sym)
            if snap:
                for i, h in enumerate(snap.get("holders", []), 1):
                    if h.get("address") == wallet_address:
                        entry["rank"] = i
                        break

        # SOL balance (native)
        sol_resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 2,
            "method":  "getBalance",
            "params":  [wallet_address],
        }, timeout=5)
        if sol_resp.ok:
            lamports = (sol_resp.json().get("result") or {}).get("value") or 0
            result["sol_balance"] = lamports / 1e9
            if result["sol_balance"] and result["sol_balance"] >= 0.1:
                sol_price = fetch_price_context(WSOL_MINT).get("price") or 0.0
                sol_usd = round(result["sol_balance"] * sol_price, 2)
                result["sol_usd"] = sol_usd
                total_usd += sol_usd

        if total_usd > 0:
            result["total_usd_est"] = round(total_usd, 2)

    except Exception as exc:
        log.warning("fetch_wallet_intel failed for %s: %s", wallet_address[:8], exc)

    _wallet_intel_cache[wallet_address] = (result, time.time())
    return result


# ── AI interpretation ─────────────────────────────────────────────────────────

def get_ai_interpretation(symbol: str, severity: str, change_type: str, delta: float, rank: int) -> str:
    cache_key = f"{symbol}:{severity}:{change_type}:{delta:.1f}:{rank}"
    cached = _ai_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < 300:
        return cached[0]
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            f"Give a single concise sentence of quant interpretation for this on-chain event: "
            f"{symbol} #{rank} holder {change_type} with {delta:+.2f}% supply change "
            f"(severity: {severity}). Focus on market implications for traders."
        )
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        interp = msg.content[0].text.strip()
        _ai_cache[cache_key] = (interp, time.time())
        return interp
    except Exception as exc:
        log.warning("AI interpretation failed: %s", exc)
        return ""


def get_coordinated_move_ai(
    token: str,
    direction: str,
    n_wallets: int,
    total_pct: float,
    price_change_1h: float | None,
) -> str:
    """Claude interpretation for coordinated moves (3+ wallets). Cached 30 min."""
    cache_key = f"coord:{token}:{direction}:{n_wallets}:{total_pct:.1f}"
    cached = _coord_ai_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < 1800:
        return cached[0]
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        price_ctx = ""
        if price_change_1h is not None:
            price_ctx = f", while price is {'up' if price_change_1h > 0 else 'down'} {abs(price_change_1h):.1f}% in the last hour"
        prompt = (
            f"In one sentence, what does it mean when {n_wallets} of the top-20 holders of "
            f"{token} {direction} simultaneously, moving {total_pct:.2f}% of supply in total{price_ctx}?"
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        interp = msg.content[0].text.strip()
        _coord_ai_cache[cache_key] = (interp, time.time())
        return interp
    except Exception as exc:
        log.warning("Coordinated move AI failed: %s", exc)
        return ""


# ── Helius RPC with public fallback ───────────────────────────────────────────

def fetch_holders(token_address: str) -> list[dict[str, Any]]:
    """Fetch top-20 holders; falls back to public Solana RPC on 403."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method":  "getTokenLargestAccounts",
        "params":  [token_address],
    }
    endpoints: list[tuple[str, str]] = []
    if HELIUS_API_KEY:
        endpoints.append(("Helius", f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"))
    endpoints.append(("Solana-public", PUBLIC_SOLANA_RPC))

    last_exc: Exception = RuntimeError("no endpoints configured")
    for name, url in endpoints:
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 403:
                    log.warning("  RPC %s → 403 for %s — trying fallback", name, token_address[:8])
                    break
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    log.error("  RPC %s error for %s: %s", name, token_address[:8], data["error"])
                    break
                holders = data.get("result", {}).get("value", [])
                log.info("  RPC endpoint used: %s  (%d holders)", name, len(holders))
                return holders
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                log.warning("  RPC %s attempt %d failed for %s: %s", name, attempt + 1, token_address[:8], exc)
                if attempt == 0:
                    time.sleep(2)
            except requests.HTTPError as exc:
                last_exc = exc
                log.warning("  RPC %s HTTP error for %s: %s — trying fallback", name, token_address[:8], exc)
                break

    raise RuntimeError(f"All RPC endpoints failed for {token_address[:8]}: {last_exc}")


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def load_snapshot(symbol: str) -> dict[str, Any] | None:
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
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "holders": holders}, fh, indent=2)
    except OSError as exc:
        log.error("Could not save snapshot for %s: %s", symbol, exc)


# ── Holder comparison ─────────────────────────────────────────────────────────

def get_amount(holder: dict[str, Any]) -> float:
    ui = holder.get("uiAmount")
    return float(ui) if ui is not None else float(holder.get("amount", 0))


def compare_holders(old_holders: list[dict], new_holders: list[dict]) -> list[dict[str, Any]]:
    old_map      = {h["address"]: h for h in old_holders}
    new_map      = {h["address"]: h for h in new_holders}
    old_total    = sum(get_amount(h) for h in old_holders) or 1.0
    new_total    = sum(get_amount(h) for h in new_holders) or 1.0
    old_rank_map = {h["address"]: i + 1 for i, h in enumerate(old_holders)}
    new_rank_map = {h["address"]: i + 1 for i, h in enumerate(new_holders)}
    changes: list[dict[str, Any]] = []

    for addr, h in new_map.items():
        if addr not in old_map:
            tokens = get_amount(h)
            changes.append({
                "type": "NEW", "address": addr,
                "old_pct": None, "new_pct": tokens / new_total * 100,
                "delta": tokens / new_total * 100,
                "old_rank": None, "new_rank": new_rank_map.get(addr),
                "tokens_delta": tokens, "old_tokens": 0.0, "new_tokens": tokens,
                "trigger": "entry",
            })

    for addr, h in old_map.items():
        if addr not in new_map:
            tokens = get_amount(h)
            changes.append({
                "type": "EXIT", "address": addr,
                "old_pct": tokens / old_total * 100, "new_pct": None,
                "delta": -(tokens / old_total * 100),
                "old_rank": old_rank_map.get(addr), "new_rank": None,
                "tokens_delta": tokens, "old_tokens": tokens, "new_tokens": 0.0,
                "trigger": "exit",
            })

    for addr in set(old_map) & set(new_map):
        old_amt     = get_amount(old_map[addr])
        new_amt     = get_amount(new_map[addr])
        old_pct     = old_amt / old_total * 100
        new_pct     = new_amt / new_total * 100
        delta       = new_pct - old_pct
        token_delta = abs(new_amt - old_amt)
        pct_trig    = abs(delta) >= MOVE_THRESHOLD_PCT
        tok_trig    = MIN_HOLDER_CHANGE_TOKENS > 0 and token_delta >= MIN_HOLDER_CHANGE_TOKENS
        if pct_trig or tok_trig:
            changes.append({
                "type": "MOVE", "address": addr,
                "old_pct": old_pct, "new_pct": new_pct, "delta": delta,
                "old_rank": old_rank_map.get(addr), "new_rank": new_rank_map.get(addr),
                "tokens_delta": token_delta, "old_tokens": old_amt, "new_tokens": new_amt,
                "trigger": "pct+tokens" if (pct_trig and tok_trig) else ("pct" if pct_trig else "tokens"),
            })

    return changes


# ── Cross-coin intelligence ───────────────────────────────────────────────────

def build_cross_holdings(all_current: dict[str, list[dict]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for symbol, holders in all_current.items():
        total = sum(get_amount(h) for h in holders) or 1.0
        for h in holders:
            addr = h["address"]
            pct  = get_amount(h) / total * 100
            result.setdefault(addr, {})[symbol] = round(pct, 4)
    return result


def send_cross_coin_digest(
    cross_holdings: dict[str, dict[str, float]],
    all_price_ctx: dict[str, dict[str, Any]],
    token_emojis: dict[str, str],
) -> None:
    multi = {
        addr: h for addr, h in cross_holdings.items()
        if len(h) >= 2 and sum(h.values()) >= 0.5
    }
    if not multi:
        return

    lines = [
        f"🕸 <b>Cross-coin Whales — {datetime.now(timezone.utc).strftime('%H:%M UTC')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for addr, holdings in sorted(multi.items(), key=lambda kv: -sum(kv[1].values()))[:10]:
        short     = f"{addr[:8]}...{addr[-6:]}"
        total_pct = sum(holdings.values())
        lines.append(f"<code>{short}</code>  (combined {total_pct:.2f}%)")
        lines.append(f"📋 <code>{addr}</code>")
        for sym, pct in sorted(holdings.items(), key=lambda kv: -kv[1]):
            emoji = token_emojis.get(sym, "🔹")
            lines.append(f"  {emoji} {sym}: {pct:.2f}%")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    ok, err = send_alert("\n".join(lines).strip())
    if ok:
        log.info("Cross-coin digest sent (%d wallets)", len(multi))
    else:
        log.error("Cross-coin digest failed: %s", err)


def detect_coordinated_moves(
    all_run_changes: dict[str, list[dict[str, Any]]],
    all_addresses: dict[str, str],
    all_price_ctx: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Detect 2+ wallets moving same direction; AI interpretation for 3+ (FIX 5)."""
    for symbol, changes in all_run_changes.items():
        buyers  = [c for c in changes if c["type"] == "MOVE" and c["delta"] > 0]
        sellers = [c for c in changes if c["type"] == "MOVE" and c["delta"] < 0]

        for direction_label, group, dir_key in (
            ("BUYING 🟢", buyers, "buy"),
            ("SELLING 🔴", sellers, "sell"),
        ):
            if len(group) < 2:
                continue

            total_delta   = sum(abs(c["delta"]) for c in group)
            token_address = all_addresses.get(symbol, "")
            price_ctx     = (all_price_ctx or {}).get(symbol, {})
            price_1h      = price_ctx.get("change_1h")

            icon  = "🟢" if "BUYING" in direction_label else "🔴"
            lines = [
                f"⚡ <b>COORDINATED MOVE — {symbol}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"{icon} {len(group)} wallets {direction_label} simultaneously",
            ]
            for c in sorted(group, key=lambda x: -abs(x["delta"]))[:5]:
                rank  = c.get("new_rank") or c.get("old_rank") or "?"
                addr  = c["address"]
                short = f"{addr[:8]}...{addr[-6:]}"
                lines.append(f"  #{rank} <code>{short}</code>  {c['delta']:+.3f}%")
                lines.append(f"       📋 <code>{addr}</code>")  # FIX 1 — full address

            lines += [
                f"🏆 Net: {total_delta:.3f}% supply moved",
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            ]

            # FIX 5 — AI for 3+ wallets
            if len(group) >= 3:
                ai = get_coordinated_move_ai(symbol, dir_key, len(group), total_delta, price_1h)
                if ai:
                    lines.append(f"🤖 <i>{ai}</i>")

            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            if token_address:
                lines.append(
                    f'🔗 <a href="https://dexscreener.com/solana/{token_address}">DexScreener</a>'
                )

            ok, err = send_alert("\n".join(lines))
            if ok:
                log.info("Coordinated move alert sent for %s (%d wallets)", symbol, len(group))
            else:
                log.error("Coordinated move alert failed: %s", err)


# ── Alert formatting (FIX 1 + FIX 3 + FIX 4) ─────────────────────────────────

def format_quant_alert(
    symbol: str,
    token_address: str,
    change: dict[str, Any],
    price_ctx: dict[str, Any],
    wallet_info: dict[str, Any],
    ai_interp: str,
    cross_coin: dict[str, float] | None = None,
    wallet_intel: dict[str, Any] | None = None,
) -> str:
    addr         = change["address"]
    short_addr   = f"{addr[:8]}...{addr[-6:]}"
    new_rank     = change.get("new_rank")
    old_rank     = change.get("old_rank")
    change_type  = change["type"]
    delta        = change["delta"]
    tokens_delta = change.get("tokens_delta", 0.0)
    severity_name, severity_icon = get_severity(change)

    # Label
    if change_type == "NEW" and (change.get("new_pct") or 0) >= 0.5:
        label = "🆕 NEW WHALE ENTRY"
    elif change_type == "EXIT" and (change.get("old_pct") or 0) >= 1.0:
        label = f"🚪 MAJOR EXIT — former #{old_rank} holder"
    elif change_type == "NEW":
        label = "NEW ENTRY"
    elif change_type == "EXIT":
        label = "EXIT"
    else:
        label = (
            "CRITICAL MOVE"     if severity_name == "CRITICAL"
            else "SIGNIFICANT MOVE" if severity_name == "SIGNIFICANT"
            else "NOTABLE MOVE"
        )

    # Rank
    if new_rank and old_rank:
        rank_str = (
            f"#{new_rank} Holder (↑ from #{old_rank})" if new_rank < old_rank
            else f"#{new_rank} Holder (↓ from #{old_rank})" if new_rank > old_rank
            else f"#{new_rank} Holder"
        )
    elif new_rank:
        rank_str = f"#{new_rank} Holder (NEW)"
    elif old_rank:
        rank_str = f"Former #{old_rank} Holder (EXITED)"
    else:
        rank_str = "Unknown Rank"

    # Supply
    if change_type == "NEW":
        supply_str, sign = f"0% → {change['new_pct']:.3f}% (+{change['new_pct']:.3f}%)", "+"
    elif change_type == "EXIT":
        supply_str, sign = f"{change['old_pct']:.3f}% → 0% (-{change['old_pct']:.3f}%)", "-"
    else:
        sign       = "+" if delta > 0 else ""
        supply_str = f"{change['old_pct']:.3f}% → {change['new_pct']:.3f}% ({sign}{delta:.3f}%)"

    # Token / USD
    price   = price_ctx.get("price") or 0.0
    usd_val = tokens_delta * price
    usd_str = f"~${usd_val:,.0f} USD" if usd_val >= 1 else f"~${usd_val:.4f} USD"
    tokens_str = f"{sign}{tokens_delta:,.0f} ({usd_str})"

    # Price (FIX 4 — 1h + 24h)
    if price:
        price_str   = f"${price:.6f}".rstrip("0").rstrip(".")
        change_1h   = price_ctx.get("change_1h")
        change_24h  = price_ctx.get("change_24h")
        parts = [price_str]
        if change_1h is not None:
            parts.append(f"1h: {change_1h:+.1f}%")
        if change_24h is not None:
            parts.append(f"24h: {change_24h:+.1f}%")
        price_line = " | ".join(parts)
    else:
        price_line = "N/A"

    # Wallet age
    age_days      = wallet_info.get("age_days")
    is_new_wallet = wallet_info.get("is_new_wallet", False)
    age_str       = (
        "🚨 NEW WALLET (&lt; 24h)" if is_new_wallet
        else f"{age_days} day{'s' if age_days != 1 else ''}" if age_days is not None
        else "Unknown"
    )
    new_flag = " 🚨" if is_new_wallet and change_type == "NEW" else ""

    solscan_url    = f"https://solscan.io/account/{addr}"
    dex_url        = f"https://dexscreener.com/solana/{token_address}"
    bubblemaps_url = f"https://app.bubblemaps.io/sol/token/{token_address}"
    contract_url   = f"https://solscan.io/token/{token_address}"

    lines = [
        f"{severity_icon} <b>{symbol} — {label}</b>{new_flag}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Rank: {rank_str}",
        # FIX 1 — shortened link + full copyable address below
        f'🏦 Wallet: <a href="{solscan_url}"><code>{short_addr}</code></a>  |  Age: {age_str}',
        f"📋 <code>{addr}</code>",
        f'📜 Contract: <a href="{contract_url}">{token_address[:8]}...{token_address[-4:]}</a>',
        f"📈 Supply: {supply_str}",
        f"💰 Tokens: {tokens_str}",
        f"💵 Price: {price_line}",
        f"⏰ Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
    ]

    if cross_coin:
        cross_parts = [f"{sym}: {pct:.2f}%" for sym, pct in sorted(cross_coin.items(), key=lambda kv: -kv[1])]
        lines.append(f"🔗 Cross-coin: also holds {', '.join(cross_parts)}")

    # FIX 3 — wallet intel
    if wallet_intel:
        intel_lines: list[str] = []
        other   = wallet_intel.get("other_monitored", {})
        majors  = wallet_intel.get("major_tokens", {})
        sol_bal = wallet_intel.get("sol_balance")
        total   = wallet_intel.get("total_usd_est")

        for sym, data in other.items():
            usd    = data.get("usd", 0)
            rank   = data.get("rank")
            rank_s = f" (#{rank} holder)" if rank else ""
            usd_s  = f" ~${usd:,.0f}" if usd >= 1 else ""
            intel_lines.append(f"  • Also holds {sym}{rank_s}{usd_s}")

        major_parts = []
        for sym, data in sorted(majors.items(), key=lambda kv: -kv[1].get("usd", 0)):
            usd = data.get("usd", 0)
            if usd >= 500:
                major_parts.append(f"{sym}: ~${usd:,.0f}")
        if major_parts:
            intel_lines.append(f"  • {', '.join(major_parts[:4])}")

        if sol_bal and sol_bal >= 0.1:
            sol_usd = wallet_intel.get("sol_usd")
            usd_s   = f" (~${sol_usd:,.0f})" if sol_usd and sol_usd >= 1 else ""
            intel_lines.append(f"  • SOL: {sol_bal:.1f} SOL{usd_s}")

        if total and total >= 100:
            intel_lines.append(f"  • Est. total portfolio: ~${total:,.0f}")

        if intel_lines:
            lines.append("🔍 Wallet Intel:")
            lines.extend(intel_lines)

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    if ai_interp:
        lines.append(f"🤖 <i>Analysis: {html.escape(ai_interp)}</i>")

    lines.append(
        f'🔗 <a href="{solscan_url}">Solscan</a> | '
        f'<a href="{dex_url}">DexScreener</a> | '
        f'<a href="{bubblemaps_url}">Bubblemaps</a>'
    )
    return "\n".join(lines)


# ── Hourly digest ─────────────────────────────────────────────────────────────

def send_hourly_digest(symbol: str, token_address: str, flows: list[dict], price_ctx: dict) -> None:
    if not flows:
        return
    accumulators = [f for f in flows if f["delta"] > 0]
    distributors = [f for f in flows if f["delta"] < 0]
    net_flow     = sum(f["delta"] for f in flows)
    largest      = max(flows, key=lambda f: abs(f["delta"]))
    sentiment    = "BULLISH 🟢" if net_flow > 0 else "BEARISH 🔴" if net_flow < 0 else "NEUTRAL ⚪"
    now          = datetime.now(timezone.utc)
    price        = price_ctx.get("price") or 0.0
    change_1h    = price_ctx.get("change_1h")
    change_24h   = price_ctx.get("change_24h")
    price_str    = f"${price:.6f}".rstrip("0").rstrip(".") if price else "N/A"
    price_parts  = [price_str]
    if price and change_1h is not None:
        price_parts.append(f"{change_1h:+.1f}% 1h")
    if price and change_24h is not None:
        price_parts.append(f"{change_24h:+.1f}% 24h")
    price_line   = " | ".join(price_parts)

    msg = (
        f"📊 <b>{symbol} — Flow Digest</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Period ending {now.strftime('%H:%M UTC')}\n"
        f"🟢 Accumulators: {len(accumulators)} (+{sum(f['delta'] for f in accumulators):.3f}% net)\n"
        f"🔴 Distributors: {len(distributors)} ({sum(f['delta'] for f in distributors):.3f}% net)\n"
        f"🏆 Net Flow: {net_flow:+.3f}% ({sentiment})\n"
        f"🐋 Largest: #{largest.get('rank', '?')} holder {largest['delta']:+.3f}%\n"
        f"💵 {symbol} Price: {price_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    ok, err = send_alert(msg)
    if not ok:
        log.error("Flow digest failed: %s", err)
    else:
        log.info("Flow digest sent for %s", symbol)


# ── Daily digest (FIX 6) ──────────────────────────────────────────────────────

def send_daily_digest() -> None:
    """Send 8am AEST (22:00 UTC) daily summary of last 24h from whale_alerts."""
    if _supabase is None:
        log.warning("Cannot send daily digest — Supabase not connected")
        return

    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        result = (
            _supabase.table("whale_alerts")
            .select("token_symbol,change_type,delta_pct,wallet_address,alerted_at,telegram_sent")
            .gte("alerted_at", since)
            .eq("telegram_sent", True)
            .execute()
        )
        rows = result.data or []
    except Exception as exc:
        log.error("Daily digest query failed: %s", exc)
        return

    if not rows:
        log.info("Daily digest: no alerts in last 24h — skipping")
        return

    by_token: dict[str, list[dict]] = {}
    for row in rows:
        sym = row.get("token_symbol") or "?"
        by_token.setdefault(sym, []).append(row)

    now   = datetime.now(timezone.utc)
    lines = [
        f"📊 <b>DAILY DIGEST — {now.strftime('%d %b %Y')}</b>",
        f"📅 8:00 AM AEST summary",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for sym, alerts in by_token.items():
        token_address = TOKENS.get(sym, "")
        price_ctx     = fetch_price_context(token_address) if token_address else {}
        price         = price_ctx.get("price") or 0.0
        change_24h    = price_ctx.get("change_24h")

        flows       = [a for a in alerts if a.get("change_type") == "MOVE"]
        new_entries = [a for a in alerts if a.get("change_type") == "NEW"]
        exits       = [a for a in alerts if a.get("change_type") == "EXIT"]
        net_flow    = sum(a.get("delta_pct") or 0 for a in flows)

        wallet_counts: dict[str, float] = {}
        for a in alerts:
            addr = a.get("wallet_address", "")
            wallet_counts[addr] = wallet_counts.get(addr, 0) + abs(a.get("delta_pct") or 0)
        most_active     = max(wallet_counts, key=lambda k: wallet_counts[k]) if wallet_counts else None
        most_active_cnt = sum(1 for a in alerts if a.get("wallet_address") == most_active)
        most_active_net = wallet_counts.get(most_active, 0) if most_active else 0

        price_str = ""
        if price:
            prc = f"${price:.6f}".rstrip("0").rstrip(".")
            price_str = prc
            if change_24h is not None:
                price_str += f" ({change_24h:+.1f}% 24h)"

        lines.append(f"\n<b>{sym}</b>")
        lines.append(f"• Net flow: {net_flow:+.3f}% supply")
        if most_active:
            short = f"{most_active[:8]}...{most_active[-6:]}"
            lines.append(f"• Most active: <code>{short}</code> ({most_active_cnt} moves, {most_active_net:+.3f}%)")
        lines.append(f"• New entries: {len(new_entries)} | Exits: {len(exits)}")
        if price_str:
            lines.append(f"• Price: {price_str}")

    lines += [
        "\n━━━━━━━━━━━━━━━━━━━━━━",
        f"🔔 Total alerts sent: {len(rows)}",
    ]

    ok, err = send_alert("\n".join(lines))
    if ok:
        log.info("Daily digest sent (%d alerts)", len(rows))
    else:
        log.error("Daily digest failed: %s", err)


def _maybe_send_daily_digest() -> None:
    """Send daily digest once at 22:00 UTC; uses Supabase to prevent duplicate sends."""
    now = datetime.now(timezone.utc)
    if now.hour != 22:
        return

    today_str = now.strftime("%Y-%m-%d")
    if _supabase:
        try:
            r = _supabase.table("bot_config").select("value").eq("key", "last_daily_digest").execute()
            if r.data and r.data[0]["value"].startswith(today_str):
                log.info("Daily digest already sent today — skipping")
                return
        except Exception:
            pass

    send_daily_digest()

    if _supabase:
        try:
            now_iso = now.isoformat()
            r = _supabase.table("bot_config").select("key").eq("key", "last_daily_digest").execute()
            if r.data:
                _supabase.table("bot_config").update(
                    {"value": now_iso, "updated_at": now_iso}
                ).eq("key", "last_daily_digest").execute()
            else:
                _supabase.table("bot_config").insert(
                    {"key": "last_daily_digest", "value": now_iso, "updated_at": now_iso}
                ).execute()
        except Exception as exc:
            log.warning("Could not record daily digest timestamp: %s", exc)


# ── Startup diagnostics ───────────────────────────────────────────────────────

def log_startup_diagnostics() -> None:
    log.info("── Startup diagnostics ──────────────────────────────────────")
    log.info("HELIUS_API_KEY       : %s", "✅ set" if HELIUS_API_KEY     else "❌ MISSING")
    log.info("TELEGRAM_BOT_TOKEN   : %s", "✅ set" if TELEGRAM_BOT_TOKEN else "❌ MISSING")
    log.info("TELEGRAM_CHAT_ID     : %s", "✅ set" if TELEGRAM_CHAT_ID   else "❌ MISSING")
    log.info("SUPABASE_URL         : %s", "✅ set" if SUPABASE_URL       else "❌ MISSING")
    log.info("SUPABASE_SERVICE_KEY : %s", "✅ set" if SUPABASE_KEY       else "❌ MISSING")
    log.info("ANTHROPIC_API_KEY    : %s", "✅ set" if ANTHROPIC_API_KEY  else "❌ MISSING")
    log.info("MOVE_THRESHOLD_PCT   : %.4f%%", MOVE_THRESHOLD_PCT)
    log.info("MIN_HOLDER_CHANGE_TOKENS: %.0f", MIN_HOLDER_CHANGE_TOKENS)
    log.info("Tokens tracked       : %s", list(TOKENS.keys()))
    log.info("────────────────────────────────────────────────────────────")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id":    TELEGRAM_CHAT_ID,
                    "text":       (
                        f"🟢 <b>Monitor online</b> — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                        f"Threshold: {MOVE_THRESHOLD_PCT:.4f}%  |  Tokens: {', '.join(TOKENS)}"
                    ),
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if resp.ok:
                log.info("✅ Telegram connectivity OK")
            else:
                log.error("❌ Telegram test failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.error("❌ Telegram test exception: %s", exc)
    else:
        log.error("❌ Telegram not tested — token or chat ID missing")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_holder_monitor() -> None:
    global _hourly_flows, _hourly_alert_count
    _hourly_flows       = []
    _hourly_alert_count = 0

    log.info(
        "── Holder monitor starting — %s  pct=%.4f%%  tokens=%.0f",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        MOVE_THRESHOLD_PCT, MIN_HOLDER_CHANGE_TOKENS,
    )

    token_emojis = {sym: info.get("emoji", "🔹") for sym, info in _cfg.get("solana_tokens", {}).items()}

    # Pass 1: fetch all token holders + prices
    all_current:   dict[str, list[dict[str, Any]]] = {}
    all_price_ctx: dict[str, dict[str, Any]]        = {}
    all_addresses: dict[str, str]                   = {}

    for symbol, token_address in TOKENS.items():
        log.info("Fetching %s (%s...)", symbol, token_address[:8])
        try:
            current = fetch_holders(token_address)
        except Exception as exc:
            log.error("fetch_holders failed for %s: %s — skipping", symbol, exc)
            continue
        if not current:
            log.warning("Empty holder list for %s — skipping", symbol)
            continue
        log.info("  %d holders fetched", len(current))
        all_current[symbol]   = current
        all_addresses[symbol] = token_address
        try:
            all_price_ctx[symbol] = fetch_price_context(token_address)
        except Exception:
            all_price_ctx[symbol] = {}
        write_snapshot_to_supabase(symbol, token_address, current)

    cross_holdings = build_cross_holdings(all_current)
    persist_wallet_relationships(cross_holdings)

    # Pass 2: diff and alert per token
    all_run_changes: dict[str, list[dict[str, Any]]] = {}

    for symbol, current in all_current.items():
        token_address = all_addresses[symbol]
        price_ctx     = all_price_ctx.get(symbol, {})
        token_flows:  list[dict[str, Any]] = []

        log.info("Checking %s — pct=%.4f%%  tokens=%.0f", symbol, MOVE_THRESHOLD_PCT, MIN_HOLDER_CHANGE_TOKENS)

        snapshot = load_snapshot(symbol)
        if snapshot:
            try:
                changes = compare_holders(snapshot["holders"], current)
            except (KeyError, TypeError) as exc:
                log.error("Snapshot comparison failed for %s: %s", symbol, exc)
                save_snapshot(symbol, current)
                continue

            all_run_changes[symbol] = changes
            log.info("  %d change(s) detected", len(changes))

            for change in changes:
                addr        = change["address"]
                change_type = change["type"]
                delta       = change["delta"]

                if is_rate_limited(addr):
                    log.info("  Rate-limited: %s %s...", symbol, addr[:8])
                    continue

                new_rank    = change.get("new_rank") or change.get("old_rank") or 0
                wallet_info = get_wallet_first_seen(addr, symbol, new_rank)
                sev_name, _ = get_severity(change)
                ai_interp   = get_ai_interpretation(symbol, sev_name, change_type, delta, new_rank)

                wallet_cross = {
                    s: p for s, p in cross_holdings.get(addr, {}).items() if s != symbol
                }

                # FIX 3 — wallet intel for significant moves only
                wallet_intel = None
                if abs(delta) >= WALLET_INTEL_MIN_DELTA_PCT or change_type in ("NEW", "EXIT"):
                    try:
                        wallet_intel = fetch_wallet_intel(addr, symbol)
                    except Exception as exc:
                        log.warning("  Wallet intel skipped for %s: %s", addr[:8], exc)

                msg = format_quant_alert(
                    symbol, token_address, change, price_ctx,
                    wallet_info, ai_interp,
                    cross_coin=wallet_cross or None,
                    wallet_intel=wallet_intel,
                )
                kb = make_inline_keyboard(addr, token_address)

                telegram_sent = False
                try:
                    ok, err = send_alert(msg, reply_markup=kb)
                    telegram_sent = ok
                    if not ok:
                        log.error("  Telegram failed: %s", err)
                except Exception as exc:
                    log.error("  Unexpected Telegram error: %s", exc)

                if telegram_sent:
                    record_alert_sent(addr)
                    token_flows.append({"address": addr, "delta": delta, "rank": new_rank})
                    log.info("  Alert sent — %s #%s %s %+.4f%%", symbol, new_rank, change_type, delta)

                write_alert_to_supabase(symbol, token_address, change, telegram_sent)

            if not changes:
                log.info("  No significant changes")

            send_hourly_digest(symbol, token_address, token_flows, price_ctx)
        else:
            log.info("  No previous snapshot — creating baseline")
            try:
                send_telegram(
                    f"📸 <b>{symbol}</b> — baseline snapshot created.\n"
                    f"Tracking {len(current)} holders.\n"
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            except Exception as exc:
                log.error("Baseline notification failed for %s: %s", symbol, exc)

        save_snapshot(symbol, current)
        log.info("  Snapshot saved → snapshots/%s_holders.json", symbol)

    detect_coordinated_moves(all_run_changes, all_addresses, all_price_ctx)
    send_cross_coin_digest(cross_holdings, all_price_ctx, token_emojis)


def run() -> None:
    _load_bot_config_from_supabase()
    log_startup_diagnostics()
    run_holder_monitor()
    _maybe_send_daily_digest()
    log.info("── Monitor complete")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Solana whale monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run all logic but skip Telegram messages and Supabase writes")
    _args = parser.parse_args()
    if _args.dry_run:
        DRY_RUN = True
        log.info("DRY RUN — Telegram and Supabase writes disabled")
    run()
