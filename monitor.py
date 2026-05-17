"""
Holder Concentration Monitor
=============================
Run standalone or via GitHub Actions cron job (see .github/workflows/monitor.yml).

What it does each run:
  1. Reads live config overrides from Supabase bot_config (set via Telegram commands).
  2. Fetches top-20 holders for every tracked token via Helius RPC.
  3. Fetches live price context from DexScreener.
  4. Builds a cross-coin holdings map (wallets that hold multiple tracked tokens).
  5. Diffs against the previous snapshot (local JSON kept in git).
  6. For each significant change:
       - Determines severity tier (CRITICAL / SIGNIFICANT / NOTABLE).
       - Looks up wallet age from Supabase wallet_metadata.
       - Generates a one-line Claude AI interpretation.
       - Sends a quant-style Telegram alert with cross-coin context + inline buttons.
       - Writes to Supabase whale_alerts and wallet_flow_changes.
  7. Sends an end-of-run flow digest per token.
  8. Detects coordinated moves (2+ wallets same direction, same run).
  9. Sends a cross-coin whale digest when wallets appear in multiple token lists.
  10. Saves updated local JSON snapshots so GitHub Actions can commit them back.

Environment variables:
    HELIUS_API_KEY            Helius RPC API key
    TELEGRAM_BOT_TOKEN        Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID          Target chat or channel ID
    SUPABASE_URL              Supabase project URL (https://xxx.supabase.co)
    SUPABASE_SERVICE_KEY      Supabase service-role key (bypasses RLS)
    ANTHROPIC_API_KEY         Anthropic API key for AI interpretation
    MOVE_THRESHOLD_PCT        % supply change to trigger an alert (overridden by bot_config)
    MIN_HOLDER_CHANGE_TOKENS  raw token amount change threshold (overridden by bot_config)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
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
HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_SERVICE_KEY", "")
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

# Token registry sourced from config.json (overridable via Telegram /addcoin)
TOKENS: dict[str, str] = {
    sym: info["address"]
    for sym, info in _cfg.get("solana_tokens", {}).items()
} or {"ALON": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"}

# Alert rate limiting
RATE_LIMIT_SECS    = 300
MAX_ALERTS_PER_RUN = 20

# ── Module-level run state ────────────────────────────────────────────────────
_ai_cache: dict[str, tuple[str, float]] = {}
_alert_timestamps: dict[str, float] = {}
_hourly_alert_count: int = 0
_hourly_flows: list[dict[str, Any]] = []


# ── Supabase client ───────────────────────────────────────────────────────────

def init_supabase() -> Client | None:
    """Initialise and test the Supabase client."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("Supabase not configured — set SUPABASE_URL and SUPABASE_SERVICE_KEY")
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

def write_snapshot_to_supabase(
    symbol: str,
    token_address: str,
    holders: list[dict[str, Any]],
) -> None:
    """Insert one row per holder into wallet_snapshots."""
    if _supabase is None:
        return
    total        = sum(get_amount(h) for h in holders) or 1.0
    top10_pct    = sum(get_amount(h) / total * 100 for h in holders[:10])
    holder_count = len(holders)
    captured_at  = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "token_address":  token_address,
            "token_symbol":   symbol,
            "symbol":         symbol,
            "wallet_address": h["address"],
            "rank":           rank,
            "balance":        get_amount(h),
            "pct_supply":     round(get_amount(h) / total * 100, 6),
            "captured_at":    captured_at,
            "holder_count":   holder_count,
            "top10_pct":      round(top10_pct, 4),
        }
        for rank, h in enumerate(holders, 1)
    ]
    try:
        _supabase.table("wallet_snapshots").insert(rows).execute()
        log.info("  ✅ %d rows → wallet_snapshots (%s)", len(rows), symbol)
    except Exception as exc:
        log.error("  ❌ wallet_snapshots insert failed for %s: %s", symbol, exc)


def write_alert_to_supabase(
    symbol: str,
    token_address: str,
    change: dict[str, Any],
    telegram_sent: bool,
) -> None:
    """Insert a holder change event into whale_alerts and wallet_flow_changes."""
    if _supabase is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    token_delta = change.get("tokens_delta") or change.get("token_delta") or 0
    delta       = change.get("delta", 0)
    flow_type   = (
        "entry"    if change["type"] == "NEW"
        else "exit"     if change["type"] == "EXIT"
        else "buy"      if delta > 0
        else "sell"
    )
    try:
        _supabase.table("whale_alerts").insert({
            "token_address":  token_address,
            "token_symbol":   symbol,
            "symbol":         symbol,
            "wallet_address": change["address"],
            "change_type":    change["type"],
            "old_pct":        change.get("old_pct"),
            "new_pct":        change.get("new_pct"),
            "delta_pct":      round(delta, 6),
            "token_delta":    token_delta,
            "trigger":        change.get("trigger", "pct"),
            "alerted_at":     now,
            "telegram_sent":  telegram_sent,
        }).execute()
    except Exception as exc:
        log.error("  ❌ whale_alerts insert failed: %s", exc)

    try:
        _supabase.table("wallet_flow_changes").insert({
            "token_address":  token_address,
            "token_symbol":   symbol,
            "symbol":         symbol,
            "wallet_address": change["address"],
            "prev_balance":   change.get("old_tokens"),
            "new_balance":    change.get("new_tokens"),
            "change_amount":  token_delta if delta >= 0 else -token_delta,
            "change_pct":     round(delta, 6),
            "flow_type":      flow_type,
            "detected_at":    now,
        }).execute()
    except Exception as exc:
        log.error("  ❌ wallet_flow_changes insert failed: %s", exc)


def get_wallet_first_seen(address: str, symbol: str, rank: int) -> dict[str, Any]:
    """Return first_seen age for a wallet; inserts a new row in wallet_metadata if unseen."""
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
                "address":         address,
                "symbol":          symbol,
                "first_seen":      now.isoformat(),
                "first_seen_rank": rank,
            }).execute()
            return {"age_days": 0, "is_new_wallet": True}
    except Exception as exc:
        log.warning("wallet_metadata lookup failed for %s: %s", address[:8], exc)
        return default


def persist_wallet_relationships(cross_holdings: dict[str, dict[str, float]]) -> None:
    """Log cross-coin wallet overlaps to wallet_relationships table."""
    if _supabase is None:
        return
    multi = {addr: holdings for addr, holdings in cross_holdings.items() if len(holdings) >= 2}
    if not multi:
        return
    rows = []
    symbols = list(TOKENS.keys())
    for addr, holdings in multi.items():
        sym_list = sorted(holdings.keys())
        for i in range(len(sym_list)):
            for j in range(i + 1, len(sym_list)):
                rows.append({
                    "wallet_address": addr,
                    "coin_a":         sym_list[i],
                    "coin_a_pct":     round(holdings[sym_list[i]], 4),
                    "coin_b":         sym_list[j],
                    "coin_b_pct":     round(holdings[sym_list[j]], 4),
                })
    if rows:
        try:
            _supabase.table("wallet_relationships").insert(rows).execute()
            log.info("✅ %d wallet relationship row(s) written", len(rows))
        except Exception as exc:
            log.error("❌ wallet_relationships insert failed: %s", exc)


# ── Telegram ──────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=False,
)
def send_telegram(msg: str, reply_markup: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Send an HTML-formatted message to Telegram, optionally with inline keyboard."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — missing token or chat ID")
        return False, "not_configured"

    payload: dict[str, Any] = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     msg,
        "parse_mode":               "HTML",
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
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        return {
            "price":     float(best.get("priceUsd") or 0),
            "change_1h": float((best.get("priceChange") or {}).get("h1") or 0),
        }
    except Exception as exc:
        log.warning("DexScreener fetch failed: %s", exc)
        return {}


# ── AI interpretation ─────────────────────────────────────────────────────────

def get_ai_interpretation(
    symbol: str, severity: str, change_type: str, delta: float, rank: int,
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
    resp = requests.post(
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
        json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token_address]},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        log.error("Helius RPC error for %s: %s", token_address[:8], data["error"])
        return []
    return data.get("result", {}).get("value", [])


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


def compare_holders(
    old_holders: list[dict[str, Any]],
    new_holders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Diff two holder lists; returns changes with rank, token amounts, and trigger reason."""
    old_map   = {h["address"]: h for h in old_holders}
    new_map   = {h["address"]: h for h in new_holders}
    old_total = sum(get_amount(h) for h in old_holders) or 1.0
    new_total = sum(get_amount(h) for h in new_holders) or 1.0
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

def build_cross_holdings(
    all_current: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    """Build wallet → {symbol: pct} map from all fetched token holder lists."""
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
    """Send a digest of wallets that hold multiple tracked tokens."""
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
        short = f"{addr[:8]}...{addr[-6:]}"
        total_pct = sum(holdings.values())
        lines.append(f"<code>{short}</code>  (combined {total_pct:.2f}%)")
        for sym, pct in sorted(holdings.items(), key=lambda kv: -kv[1]):
            emoji = token_emojis.get(sym, "🔹")
            lines.append(f"  {emoji} {sym}: {pct:.2f}%")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    ok, err = send_telegram("\n".join(lines).strip())
    if ok:
        log.info("Cross-coin digest sent (%d wallets)", len(multi))
    else:
        log.error("Cross-coin digest failed: %s", err)


def detect_coordinated_moves(
    all_run_changes: dict[str, list[dict[str, Any]]],
    all_addresses: dict[str, str],
) -> None:
    """Detect and alert on 2+ wallets moving the same direction within a single run."""
    for symbol, changes in all_run_changes.items():
        buyers  = [c for c in changes if c["type"] == "MOVE" and c["delta"] > 0]
        sellers = [c for c in changes if c["type"] == "MOVE" and c["delta"] < 0]

        for direction, group in (("BUYING 🟢", buyers), ("SELLING 🔴", sellers)):
            if len(group) < 2:
                continue
            total_delta = sum(abs(c["delta"]) for c in group)
            token_address = all_addresses.get(symbol, "")
            lines = [
                f"⚡ <b>COORDINATED MOVE — {symbol}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"{'🟢' if 'BUYING' in direction else '🔴'} {len(group)} wallets {direction} simultaneously",
            ]
            for c in sorted(group, key=lambda x: -abs(x["delta"]))[:5]:
                rank = c.get("new_rank") or c.get("old_rank") or "?"
                addr = f"{c['address'][:8]}...{c['address'][-6:]}"
                lines.append(f"  #{rank} <code>{addr}</code>  {c['delta']:+.3f}%")
            lines += [
                f"🏆 Net: {total_delta:.3f}% supply moved",
                f"⏰ {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            if token_address:
                lines.append(
                    f'🔗 <a href="https://dexscreener.com/solana/{token_address}">DexScreener</a>'
                )
            ok, err = send_telegram("\n".join(lines))
            if ok:
                log.info("Coordinated move alert sent for %s (%d wallets)", symbol, len(group))
            else:
                log.error("Coordinated move alert failed: %s", err)


# ── Alert formatting ──────────────────────────────────────────────────────────

def format_quant_alert(
    symbol: str,
    token_address: str,
    change: dict[str, Any],
    price_ctx: dict[str, Any],
    wallet_info: dict[str, Any],
    ai_interp: str,
    cross_coin: dict[str, float] | None = None,
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
        rank_str = f"#{new_rank} Holder (↑ from #{old_rank})" if new_rank < old_rank \
                   else f"#{new_rank} Holder (↓ from #{old_rank})" if new_rank > old_rank \
                   else f"#{new_rank} Holder"
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
        sign = "+" if delta > 0 else ""
        supply_str = f"{change['old_pct']:.3f}% → {change['new_pct']:.3f}% ({sign}{delta:.3f}%)"
        sign = "+" if delta > 0 else ""

    # Token / USD
    price   = price_ctx.get("price") or 0.0
    usd_val = tokens_delta * price
    usd_str = f"~${usd_val:,.0f} USD" if usd_val >= 1 else f"~${usd_val:.4f} USD"
    tokens_str = f"{sign}{tokens_delta:,.0f} ({usd_str})"

    # Price
    if price:
        price_str  = f"${price:.6f}".rstrip("0").rstrip(".")
        change_1h  = price_ctx.get("change_1h")
        price_line = f"{price_str} (1h: {change_1h:+.1f}%)" if change_1h is not None else price_str
    else:
        price_line = "N/A"

    # Wallet age
    age_days      = wallet_info.get("age_days")
    is_new_wallet = wallet_info.get("is_new_wallet", False)
    age_str       = "🚨 NEW WALLET (< 24h)" if is_new_wallet else \
                    f"{age_days} day{'s' if age_days != 1 else ''}" if age_days is not None else "Unknown"
    new_flag      = " 🚨" if is_new_wallet and change_type == "NEW" else ""

    # Links
    solscan_url    = f"https://solscan.io/account/{addr}"
    dex_url        = f"https://dexscreener.com/solana/{token_address}"
    bubblemaps_url = f"https://app.bubblemaps.io/sol/token/{token_address}"
    contract_url   = f"https://solscan.io/token/{token_address}"

    lines = [
        f"{severity_icon} <b>{symbol} — {label}</b>{new_flag}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Rank: {rank_str}",
        f'🏦 Wallet: <a href="{solscan_url}"><code>{short_addr}</code></a>  |  Age: {age_str}',
        f'📜 Contract: <a href="{contract_url}">{token_address[:8]}...{token_address[-4:]}</a>',
        f"📈 Supply: {supply_str}",
        f"💰 Tokens: {tokens_str}",
        f"💵 Price: {price_line}",
        f"⏰ Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
    ]

    # Cross-coin context
    if cross_coin:
        cross_parts = [f"{sym}: {pct:.2f}%" for sym, pct in sorted(cross_coin.items(), key=lambda kv: -kv[1])]
        lines.append(f"🔗 Cross-coin: also holds {', '.join(cross_parts)}")

    lines += [
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
    largest      = max(flows, key=lambda f: abs(f["delta"]))
    sentiment    = "BULLISH 🟢" if net_flow > 0 else "BEARISH 🔴" if net_flow < 0 else "NEUTRAL ⚪"
    now          = datetime.now(timezone.utc)
    price        = price_ctx.get("price") or 0.0
    change_1h    = price_ctx.get("change_1h")
    price_str    = f"${price:.6f}".rstrip("0").rstrip(".") if price else "N/A"
    price_line   = f"{price_str} ({change_1h:+.1f}% 1h)" if (price and change_1h is not None) else price_str

    msg = (
        f"📊 <b>{symbol} — Flow Digest</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Period ending {now.strftime('%H:%M UTC')}\n"
        f"🟢 Accumulators: {len(accumulators)} (+{sum(f['delta'] for f in accumulators):.3f}% net)\n"
        f"🔴 Distributors: {len(distributors)} ({sum(f['delta'] for f in distributors):.3f}% net)\n"
        f"🏆 Net Flow: {net_flow:+.3f}% ({sentiment})\n"
        f"🐋 Largest: #{largest.get('rank','?')} holder {largest['delta']:+.3f}%\n"
        f"💵 {symbol} Price: {price_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    ok, err = send_telegram(msg)
    if not ok:
        log.error("Flow digest failed: %s", err)
    else:
        log.info("Flow digest sent for %s", symbol)


# ── Startup diagnostics ───────────────────────────────────────────────────────

def log_startup_diagnostics() -> None:
    log.info("── Startup diagnostics ──────────────────────────────────────")
    log.info("HELIUS_API_KEY       : %s", "✅ set" if HELIUS_API_KEY      else "❌ MISSING")
    log.info("TELEGRAM_BOT_TOKEN   : %s", "✅ set" if TELEGRAM_BOT_TOKEN  else "❌ MISSING")
    log.info("TELEGRAM_CHAT_ID     : %s", "✅ set" if TELEGRAM_CHAT_ID    else "❌ MISSING")
    log.info("SUPABASE_URL         : %s", "✅ set" if SUPABASE_URL        else "❌ MISSING")
    log.info("SUPABASE_SERVICE_KEY : %s", "✅ set" if SUPABASE_KEY        else "❌ MISSING")
    log.info("ANTHROPIC_API_KEY    : %s", "✅ set" if ANTHROPIC_API_KEY   else "❌ MISSING")
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
    """Two-pass holder monitor: fetch all → build cross-holdings → diff and alert."""
    global _hourly_flows, _hourly_alert_count
    _hourly_flows        = []
    _hourly_alert_count  = 0

    log.info(
        "── Holder monitor starting — %s  pct=%.4f%%  tokens=%.0f",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        MOVE_THRESHOLD_PCT, MIN_HOLDER_CHANGE_TOKENS,
    )

    # Pull token emoji map for digests
    token_emojis = {sym: info.get("emoji", "🔹") for sym, info in _cfg.get("solana_tokens", {}).items()}

    # ── Pass 1: fetch all token holder lists + prices ─────────────────────────
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

    # ── Build cross-holdings map ───────────────────────────────────────────────
    cross_holdings = build_cross_holdings(all_current)
    persist_wallet_relationships(cross_holdings)

    # ── Pass 2: diff and alert per token ──────────────────────────────────────
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

                # Cross-coin context for this wallet (other tokens only)
                wallet_cross = {
                    s: p for s, p in cross_holdings.get(addr, {}).items()
                    if s != symbol
                }

                msg = format_quant_alert(
                    symbol, token_address, change, price_ctx,
                    wallet_info, ai_interp, cross_coin=wallet_cross or None,
                )
                kb  = make_inline_keyboard(addr, token_address)

                telegram_sent = False
                try:
                    ok, err = send_telegram(msg, reply_markup=kb)
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

    # ── Post-run digests ──────────────────────────────────────────────────────
    detect_coordinated_moves(all_run_changes, all_addresses)
    send_cross_coin_digest(cross_holdings, all_price_ctx, token_emojis)


def run() -> None:
    """Entry point — run the holder monitor. Never raises; all errors are logged."""
    _load_bot_config_from_supabase()
    log_startup_diagnostics()
    run_holder_monitor()
    log.info("── Monitor complete")


if __name__ == "__main__":
    run()
