"""
Holder Concentration Monitor + X Sentiment Dispatcher
======================================================
Run standalone or via GitHub Actions cron job (see .github/workflows/monitor.yml).

What it does each run:
  1. Fetches top-20 holders for every tracked token via Helius RPC.
  2. Diffs against the previous snapshot; sends a quant-style Telegram alert for
     each significant change with severity tier, price context, rank movement,
     USD value, wallet age, and a one-line Claude AI interpretation.
  3. Sends an hourly net-flow digest summarising all moves from the run.
  4. Fetches X / Grok sentiment for each token and sends a digest to Telegram.
  5. Saves updated snapshots to disk so GitHub Actions can commit them back.
  6. Persists alert history and wallet metadata to Supabase.

Environment variables:
    HELIUS_API_KEY             Helius RPC API key
    TELEGRAM_BOT_TOKEN         Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID           Target chat or channel ID
    XAI_API_KEY                xAI / Grok API key for X sentiment analysis
    ANTHROPIC_API_KEY          Anthropic API key for AI interpretation
    SUPABASE_URL               Supabase project URL
    SUPABASE_SERVICE_ROLE_KEY  Supabase service-role key (bypasses RLS)
    MOVE_THRESHOLD_PCT         % supply change to trigger an alert (default 0.1)
    SKIP_SENTIMENT             Set to "1" to skip sentiment (e.g. on frequent runs)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
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
HELIUS_API_KEY      = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
XAI_API_KEY         = os.environ.get("XAI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY        = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SNAPSHOT_DIR        = os.path.join(os.path.dirname(__file__), "snapshots")
MOVE_THRESHOLD_PCT  = float(os.environ.get("MOVE_THRESHOLD_PCT", "0.1"))
SKIP_SENTIMENT      = os.environ.get("SKIP_SENTIMENT", "0") == "1"

RATE_LIMIT_SECS    = 300   # 5 min per wallet
MAX_ALERTS_PER_RUN = 20    # hard cap per run

# Token registry: symbol → mint address
TOKENS: dict[str, str] = {
    "ALON": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS",
}

# ── Module-level run state ────────────────────────────────────────────────────
_ai_cache: dict[str, tuple[str, float]] = {}   # cache_key → (text, unix_ts)
_alert_timestamps: dict[str, float] = {}        # address → last alert unix ts
_hourly_alert_count: int = 0
_hourly_flows: list[dict[str, Any]] = []        # accumulated for digest


# ── Supabase ──────────────────────────────────────────────────────────────────

def get_supabase_client() -> Any | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("Supabase not configured — skipping DB operations")
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as exc:
        log.error("Supabase client init failed: %s", exc)
        return None


def get_wallet_first_seen(
    sb: Any,
    address: str,
    symbol: str,
    rank: int,
) -> dict[str, Any]:
    """Return first_seen metadata for a wallet, inserting a new row if unseen."""
    default = {"age_days": None, "is_new_wallet": False}
    if sb is None:
        return default
    try:
        result = sb.table("wallet_metadata").select("first_seen").eq("address", address).execute()
        now = datetime.now(timezone.utc)
        if result.data:
            first_seen_str = result.data[0]["first_seen"]
            # Handle both Z-suffix and +00:00 offset
            first_seen = datetime.fromisoformat(first_seen_str.replace("Z", "+00:00"))
            age_days = (now - first_seen).days
            return {"age_days": age_days, "is_new_wallet": age_days < 1}
        else:
            sb.table("wallet_metadata").insert({
                "address":         address,
                "symbol":          symbol,
                "first_seen":      now.isoformat(),
                "first_seen_rank": rank,
            }).execute()
            return {"age_days": 0, "is_new_wallet": True}
    except Exception as exc:
        log.error("wallet_metadata lookup failed for %s: %s", address[:8], exc)
        return default


def log_alert_to_supabase(
    sb: Any,
    symbol: str,
    token_address: str,
    change: dict[str, Any],
    price_ctx: dict[str, Any],
    telegram_sent: bool,
) -> None:
    if sb is None:
        return
    addr        = change["address"]
    change_type = change["type"]
    delta_pct   = change["delta"]
    old_pct     = change.get("old_pct")
    new_pct     = change.get("new_pct")
    tokens_delta = change.get("tokens_delta", 0.0)
    price       = price_ctx.get("price", 0.0) or 0.0
    change_usd  = tokens_delta * price
    flow_type   = (
        "entry"        if change_type == "NEW"
        else "exit"    if change_type == "EXIT"
        else "accumulation" if delta_pct > 0
        else "distribution"
    )

    try:
        sb.table("whale_alerts").insert({
            "token_address": token_address,
            "token_symbol":  symbol,
            "wallet_address": addr,
            "change_type":   change_type,
            "old_pct":       old_pct,
            "new_pct":       new_pct,
            "delta_pct":     delta_pct,
            "token_delta":   tokens_delta,
            "trigger":       "holder_monitor",
            "telegram_sent": telegram_sent,
            "symbol":        symbol,
        }).execute()
    except Exception as exc:
        log.error("whale_alerts insert failed: %s", exc)

    try:
        old_bal = change.get("old_tokens", 0.0) or 0.0
        new_bal = change.get("new_tokens", 0.0) or 0.0
        sb.table("wallet_flow_changes").insert({
            "token_address": token_address,
            "token_symbol":  symbol,
            "wallet_address": addr,
            "prev_balance":  old_bal,
            "new_balance":   new_bal,
            "change_amount": tokens_delta if delta_pct > 0 else -tokens_delta,
            "change_pct":    delta_pct,
            "change_usd":    change_usd if delta_pct > 0 else -change_usd,
            "flow_type":     flow_type,
            "symbol":        symbol,
        }).execute()
    except Exception as exc:
        log.error("wallet_flow_changes insert failed: %s", exc)


# ── Telegram ──────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=False,
)
def send_telegram(msg: str, reply_markup: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Send an HTML-formatted message to Telegram, optionally with inline keyboard buttons."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping")
        return False, "not_configured"

    payload: dict[str, Any] = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=10,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.error("Telegram HTTP error %s: %s", resp.status_code, resp.text[:200])
        return False, str(exc)

    return True, ""


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
    last_ts = _alert_timestamps.get(address, 0.0)
    return (time.time() - last_ts) < RATE_LIMIT_SECS


def record_alert_sent(address: str) -> None:
    global _hourly_alert_count
    _alert_timestamps[address] = time.time()
    _hourly_alert_count += 1


# ── Severity ──────────────────────────────────────────────────────────────────

def get_severity(change: dict[str, Any]) -> tuple[str, str]:
    """Return (severity_name, emoji) based on move size and change type."""
    abs_delta = abs(change["delta"])
    if change["type"] in ("NEW", "EXIT") or abs_delta >= 1.0:
        return "CRITICAL", "🔴"
    elif abs_delta >= 0.5:
        return "SIGNIFICANT", "🟡"
    return "NOTABLE", "🟢"


# ── DexScreener price ─────────────────────────────────────────────────────────

def fetch_price_context(token_address: str) -> dict[str, Any]:
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
            timeout=10,
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return {}
        best = max(
            pairs,
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
        )
        return {
            "price":     float(best.get("priceUsd") or 0),
            "change_1h": float((best.get("priceChange") or {}).get("h1") or 0),
        }
    except Exception as exc:
        log.warning("DexScreener fetch failed: %s", exc)
        return {}


# ── AI interpretation ─────────────────────────────────────────────────────────

def get_ai_interpretation(
    symbol: str,
    severity: str,
    change_type: str,
    delta: float,
    rank: int,
) -> str:
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
            model="claude-sonnet-4-5",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        interp = msg.content[0].text.strip()
        _ai_cache[cache_key] = (interp, time.time())
        return interp
    except Exception as exc:
        log.warning("AI interpretation failed: %s", exc)
        return ""


# ── Helius RPC ────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def fetch_holders(token_address: str) -> list[dict[str, Any]]:
    """Fetch the top-20 token holders via Helius getTokenLargestAccounts."""
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
    """Query xAI / Grok for real-time X sentiment on a token."""
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
            json.dump(
                {"timestamp": datetime.utcnow().isoformat(), "holders": holders},
                fh,
                indent=2,
            )
    except OSError as exc:
        log.error("Could not save snapshot for %s: %s", symbol, exc)


# ── Comparison ────────────────────────────────────────────────────────────────

def get_amount(holder: dict[str, Any]) -> float:
    ui = holder.get("uiAmount")
    return float(ui) if ui is not None else float(holder.get("amount", 0))


def compare_holders(
    old_holders: list[dict[str, Any]],
    new_holders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Diff two holder lists and return significant changes.

    Each change dict includes: type, address, old_pct, new_pct, delta,
    old_rank, new_rank, tokens_delta, old_tokens, new_tokens.
    """
    old_map   = {h["address"]: h for h in old_holders}
    new_map   = {h["address"]: h for h in new_holders}
    old_total = sum(get_amount(h) for h in old_holders) or 1.0
    new_total = sum(get_amount(h) for h in new_holders) or 1.0

    # 1-indexed ranks
    old_rank_map = {h["address"]: i + 1 for i, h in enumerate(old_holders)}
    new_rank_map = {h["address"]: i + 1 for i, h in enumerate(new_holders)}

    changes: list[dict[str, Any]] = []

    for addr, h in new_map.items():
        if addr not in old_map:
            tokens = get_amount(h)
            pct    = tokens / new_total * 100
            changes.append({
                "type":         "NEW",
                "address":      addr,
                "old_pct":      None,
                "new_pct":      pct,
                "delta":        pct,
                "old_rank":     None,
                "new_rank":     new_rank_map.get(addr),
                "tokens_delta": tokens,
                "old_tokens":   0.0,
                "new_tokens":   tokens,
            })

    for addr, h in old_map.items():
        if addr not in new_map:
            tokens = get_amount(h)
            pct    = tokens / old_total * 100
            changes.append({
                "type":         "EXIT",
                "address":      addr,
                "old_pct":      pct,
                "new_pct":      None,
                "delta":        -pct,
                "old_rank":     old_rank_map.get(addr),
                "new_rank":     None,
                "tokens_delta": tokens,
                "old_tokens":   tokens,
                "new_tokens":   0.0,
            })

    for addr in set(old_map) & set(new_map):
        old_tokens = get_amount(old_map[addr])
        new_tokens = get_amount(new_map[addr])
        old_pct    = old_tokens / old_total * 100
        new_pct    = new_tokens / new_total * 100
        delta      = new_pct - old_pct
        if abs(delta) >= MOVE_THRESHOLD_PCT:
            changes.append({
                "type":         "MOVE",
                "address":      addr,
                "old_pct":      old_pct,
                "new_pct":      new_pct,
                "delta":        delta,
                "old_rank":     old_rank_map.get(addr),
                "new_rank":     new_rank_map.get(addr),
                "tokens_delta": abs(new_tokens - old_tokens),
                "old_tokens":   old_tokens,
                "new_tokens":   new_tokens,
            })

    return changes


# ── Alert formatting ──────────────────────────────────────────────────────────

def format_quant_alert(
    symbol: str,
    token_address: str,
    change: dict[str, Any],
    price_ctx: dict[str, Any],
    wallet_info: dict[str, Any],
    ai_interp: str,
) -> str:
    addr        = change["address"]
    short_addr  = f"{addr[:6]}...{addr[-4:]}"
    new_rank    = change.get("new_rank")
    old_rank    = change.get("old_rank")
    change_type = change["type"]
    delta       = change["delta"]
    tokens_delta = change.get("tokens_delta", 0.0)
    severity_name, severity_icon = get_severity(change)

    # ── Alert label ───────────────────────────────────────────────────────────
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
            "CRITICAL MOVE"    if severity_name == "CRITICAL"
            else "SIGNIFICANT MOVE" if severity_name == "SIGNIFICANT"
            else "NOTABLE MOVE"
        )

    # ── Rank line ─────────────────────────────────────────────────────────────
    if new_rank and old_rank:
        if new_rank < old_rank:
            rank_str = f"#{new_rank} Holder (↑ from #{old_rank})"
        elif new_rank > old_rank:
            rank_str = f"#{new_rank} Holder (↓ from #{old_rank})"
        else:
            rank_str = f"#{new_rank} Holder"
    elif new_rank:
        rank_str = f"#{new_rank} Holder (NEW)"
    elif old_rank:
        rank_str = f"Former #{old_rank} Holder (EXITED)"
    else:
        rank_str = "Unknown Rank"

    # ── Supply line ───────────────────────────────────────────────────────────
    if change_type == "NEW":
        supply_str = f"0% → {change['new_pct']:.2f}% (+{change['new_pct']:.2f}%)"
        sign = "+"
    elif change_type == "EXIT":
        supply_str = f"{change['old_pct']:.2f}% → 0% (-{change['old_pct']:.2f}%)"
        sign = "-"
    else:
        sign = "+" if delta > 0 else ""
        supply_str = f"{change['old_pct']:.2f}% → {change['new_pct']:.2f}% ({sign}{delta:.2f}%)"
        sign = "+" if delta > 0 else ""

    # ── Token / USD line ──────────────────────────────────────────────────────
    price = price_ctx.get("price") or 0.0
    usd_val = tokens_delta * price
    usd_str = f"~${usd_val:,.0f} USD" if usd_val >= 1 else f"~${usd_val:.4f} USD"
    tokens_str = f"{sign}{tokens_delta:,.0f} ({usd_str})"

    # ── Price line ────────────────────────────────────────────────────────────
    if price:
        price_str = f"${price:.6f}".rstrip("0").rstrip(".")
        change_1h = price_ctx.get("change_1h")
        if change_1h is not None:
            price_line = f"{price_str} (1h: {change_1h:+.1f}%)"
        else:
            price_line = price_str
    else:
        price_line = "N/A"

    # ── Wallet age ────────────────────────────────────────────────────────────
    age_days = wallet_info.get("age_days")
    is_new_wallet = wallet_info.get("is_new_wallet", False)
    if age_days is None:
        age_str = "Unknown"
    elif is_new_wallet:
        age_str = "🚨 NEW WALLET (< 24h)"
    else:
        age_str = f"{age_days} day{'s' if age_days != 1 else ''}"

    new_wallet_flag = " 🚨" if is_new_wallet and change_type == "NEW" else ""

    # ── Links ─────────────────────────────────────────────────────────────────
    solscan_url    = f"https://solscan.io/account/{addr}"
    dex_url        = f"https://dexscreener.com/solana/{token_address}"
    bubblemaps_url = f"https://app.bubblemaps.io/sol/token/{token_address}"

    lines = [
        f"{severity_icon} <b>{symbol} — {label}</b>{new_wallet_flag}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Rank: {rank_str}",
        f"🏦 Wallet: <code>{short_addr}</code>  |  Age: {age_str}",
        f"📈 Supply: {supply_str}",
        f"💰 Tokens: {tokens_str}",
        f"💵 Price: {price_line}",
        f"⏰ Time: {datetime.utcnow().strftime('%H:%M UTC')}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if ai_interp:
        lines.append(f"🤖 <i>Analysis: {ai_interp}</i>")

    lines.append(
        f'🔗 <a href="{solscan_url}">Solscan</a> | '
        f'<a href="{dex_url}">DexScreener</a> | '
        f'<a href="{bubblemaps_url}">Bubblemaps</a>'
    )

    return "\n".join(lines)


# ── Hourly digest ─────────────────────────────────────────────────────────────

def send_hourly_digest(
    symbol: str,
    token_address: str,
    flows: list[dict[str, Any]],
    price_ctx: dict[str, Any],
) -> None:
    if not flows:
        return

    accumulators = [f for f in flows if f["delta"] > 0]
    distributors = [f for f in flows if f["delta"] < 0]
    net_flow     = sum(f["delta"] for f in flows)
    acc_sum      = sum(f["delta"] for f in accumulators)
    dist_sum     = sum(f["delta"] for f in distributors)
    largest      = max(flows, key=lambda f: abs(f["delta"]))
    sentiment    = "BULLISH 🟢" if net_flow > 0 else "BEARISH 🔴" if net_flow < 0 else "NEUTRAL ⚪"

    now          = datetime.utcnow()
    period_start = now.strftime("%H:00")
    period_end   = now.strftime("%H:%M")

    price     = price_ctx.get("price") or 0.0
    change_1h = price_ctx.get("change_1h")
    price_str = f"${price:.6f}".rstrip("0").rstrip(".") if price else "N/A"
    price_line = f"{price_str} ({change_1h:+.1f}% 1h)" if (price and change_1h is not None) else price_str

    largest_rank = largest.get("rank", "?")
    largest_delta = largest["delta"]

    msg = (
        f"📊 <b>{symbol} — Hourly Flow Digest</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Period: {period_start} - {period_end} UTC\n"
        f"🟢 Accumulators: {len(accumulators)} wallet{'s' if len(accumulators) != 1 else ''}"
        f" (+{acc_sum:.2f}% net)\n"
        f"🔴 Distributors: {len(distributors)} wallet{'s' if len(distributors) != 1 else ''}"
        f" ({dist_sum:.2f}% net)\n"
        f"🏆 Net Flow: {net_flow:+.2f}% ({sentiment})\n"
        f"🐋 Largest Move: #{largest_rank} holder {largest_delta:+.2f}%\n"
        f"💵 {symbol} Price: {price_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    ok, err = send_telegram(msg)
    if not ok:
        log.error("Failed to send hourly digest: %s", err)
    else:
        log.info("Hourly digest sent for %s", symbol)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_holder_monitor() -> None:
    """Check holder concentration for every token and send quant-style alerts."""
    global _hourly_flows

    log.info("── Holder monitor starting — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    sb = get_supabase_client()

    for symbol, token_address in TOKENS.items():
        log.info("Checking %s (%s...)", symbol, token_address[:8])
        _hourly_flows = []
        price_ctx: dict[str, Any] = {}

        try:
            current = fetch_holders(token_address)
        except Exception as exc:
            log.error("fetch_holders failed for %s after retries: %s — skipping", symbol, exc)
            continue

        if not current:
            log.warning("Empty holder list for %s — skipping", symbol)
            continue

        log.info("  Fetched %d holders", len(current))

        try:
            price_ctx = fetch_price_context(token_address)
            log.info("  Price context: %s", price_ctx)
        except Exception as exc:
            log.warning("  Price fetch failed: %s", exc)

        snapshot = load_snapshot(symbol)

        if snapshot:
            try:
                changes = compare_holders(snapshot["holders"], current)
            except (KeyError, TypeError) as exc:
                log.error("Snapshot comparison failed for %s: %s", symbol, exc)
                save_snapshot(symbol, current)
                continue

            log.info("  %d change(s) detected", len(changes))

            for change in changes:
                addr        = change["address"]
                change_type = change["type"]
                delta       = change["delta"]

                # Skip sub-threshold moves (already filtered but belt-and-suspenders)
                if change_type == "MOVE" and abs(delta) < MOVE_THRESHOLD_PCT:
                    continue

                if is_rate_limited(addr):
                    log.info("  Rate-limited alert for %s (wallet %s...)", symbol, addr[:8])
                    continue

                new_rank = change.get("new_rank") or change.get("old_rank") or 0

                wallet_info = get_wallet_first_seen(sb, addr, symbol, new_rank)

                severity_name, _ = get_severity(change)
                ai_interp = get_ai_interpretation(
                    symbol, severity_name, change_type, delta, new_rank
                )

                msg     = format_quant_alert(symbol, token_address, change, price_ctx, wallet_info, ai_interp)
                kb      = make_inline_keyboard(addr, token_address)
                ok, err = send_telegram(msg, reply_markup=kb)

                if ok:
                    record_alert_sent(addr)
                    _hourly_flows.append({
                        "address": addr,
                        "delta":   delta,
                        "rank":    new_rank,
                    })
                    log.info("  Alert sent — %s #%s %s %+.2f%%", symbol, new_rank, change_type, delta)
                else:
                    log.error("  Alert delivery failed for %s: %s", addr[:8], err)

                log_alert_to_supabase(sb, symbol, token_address, change, price_ctx, ok)

            if not changes:
                log.info("  No significant changes")

            # Send hourly digest after processing all changes
            send_hourly_digest(symbol, token_address, _hourly_flows, price_ctx)

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
    """Fetch and broadcast X / Grok sentiment for every token in the registry."""
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
    """Entry point — run holder monitor then sentiment digest."""
    run_holder_monitor()
    run_sentiment_digest()
    log.info("── Monitor complete")


if __name__ == "__main__":
    run()
