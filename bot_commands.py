"""
Persistent Telegram command bot for the whale monitor.
Run with: python bot_commands.py
Railway worker: see Procfile
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from monitor import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    MOVE_THRESHOLD_PCT,
    MIN_HOLDER_CHANGE_TOKENS,
    MAJOR_TOKEN_MINTS,
    TOKENS,
    HELIUS_API_KEY,
    PUBLIC_SOLANA_RPC,
    _CONFIG_PATH,
    _load_config,
    _supabase,
    update_bot_config,
    get_live_tracked_tokens,
    build_cross_holdings,
    fetch_wallet_token_amounts,
    fetch_holders,
    fetch_wallet_intel,
    fetch_wallet_winrate,
    fetch_price_context,
    format_quant_alert,
    resolve_owners_batch,
    classify_address,
    load_snapshot,
    get_amount,
    send_alert,
)
from wallet_relationship_engine import (
    get_wallet_clusters_for_token,
    get_cluster_for_wallet,
    get_relationships_for_token,
    run_relationship_detection,
    backfill_from_supabase,
    is_cluster_member,
)
from address_filters import classify_and_filter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# Single source of truth for all registered commands.
# Feeds both the BotFather log-hint and set_my_commands (auto-syncs the pinned menu on deploy).
_COMMAND_LIST: list[tuple[str, str]] = [
    ("ping",           "Check bot is alive"),
    ("status",         "Config & connection status"),
    ("run",            "Trigger a full monitor run immediately"),
    ("alert",          "Global mute toggle: /alert on|off (owner only)"),
    ("mute",           "Mute Telegram alerts for one token: /mute SYMBOL"),
    ("unmute",         "Re-enable alerts for a token: /unmute SYMBOL"),
    ("muted",          "List currently muted tokens"),
    ("snapshot",       "Latest saved snapshot for all tokens"),
    ("holders",        "Live top-10 holders from Helius"),
    ("top",            "Full top-20 with activity status"),
    ("moves",          "Wallet movements in last 24h"),
    ("crosswallets",   "Cross-token holders: /crosswallets [deep] [SYMBOL]"),
    ("multiholders",   "Alias for crosswallets"),
    ("bundle",         "Cluster report for token OR wallet lookup"),
    ("clusters",       "All detected wallet clusters"),
    ("relationships",  "Full wallet relationship graph"),
    ("classify",       "Classify address: wallet, LP pool, or program"),
    ("topwallets",     "Rank wallets by win rate"),
    ("addtoken",       "Start tracking a token"),
    ("removetoken",    "Stop tracking a token"),
    ("threshold",      "Set move alert threshold"),
    ("movethreshold",  "Alias for threshold"),
    ("testalert",      "Fire a synthetic alert to verify pipeline"),
    ("related",        "External token holdings for top wallets"),
    ("scancluster",    "On-chain SOL transfer scan for a token's bundle cluster"),
    ("scantest",       "Test inter-transfer scan (2 wallets, 30 days, no DB write)"),
    ("addwallet",      "Track a smart wallet (validates + runs 30d backfill)"),
    ("tier",           "Show tier and stats for a tracked wallet"),
    ("backfill",       "Re-run 30d swap backfill for a wallet"),
    ("evidence",       "Show on-chain proof for a wallet's relationships"),
    ("rejections",     "Last 20 toxic-flow filter rejections"),
    ("injectevidence", "Full-history evidence scan for all clusters"),
]

BOTFATHER_COMMANDS = "\n".join(f"{cmd} - {desc}" for cmd, desc in _COMMAND_LIST)

def _parse_chat_ids(raw: str) -> set[int]:
    return {int(cid.strip()) for cid in raw.split(",") if cid.strip().lstrip("-").isdigit()}

_AUTHORIZED_CHATS: frozenset[int] = frozenset(
    (_parse_chat_ids(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else set())
    | _parse_chat_ids(os.environ.get("TELEGRAM_EXTRA_CHAT_IDS", ""))
)
_REPO_DIR = os.path.dirname(os.path.abspath(_CONFIG_PATH))
_monitor_proc: subprocess.Popen | None = None
_active_scans:     dict[str, float] = {}   # cluster_id → start epoch
_active_backfills: dict[str, float] = {}   # wallet_address → start epoch
_scan_lock = threading.Lock()


# ── Auth + config helpers ─────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    return update.effective_chat.id in _AUTHORIZED_CHATS


async def _deny(update: Update) -> None:
    await update.message.reply_text("⛔ Unauthorized.")


def _save_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def _persist_config(cfg: dict) -> str:
    """Save config.json locally and upsert all changed keys to Supabase bot_config."""
    _save_config(cfg)
    results = []
    tokens = cfg.get("solana_tokens", {})
    if update_bot_config("tracked_tokens", json.dumps({s: v["address"] for s, v in tokens.items()})):
        results.append("tokens")
    if "move_threshold_pct" in cfg:
        if update_bot_config("move_threshold_pct", str(cfg["move_threshold_pct"])):
            results.append("threshold")
    if results:
        return f"✅ Saved to Supabase ({', '.join(results)}) — takes effect on next cron run."
    return "⚠️ Saved locally only (Supabase write failed — check logs)."


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🟢 Bot online")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    await update.message.reply_text(
        "👋 <b>Whale Monitor Bot</b>\n\nUse /help to see available commands.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    await update.message.reply_text(
        "<b>📋 Bot Commands</b>\n\n"

        "<b>🔍 MONITORING</b>\n"
        "/ping — Check bot is alive\n"
        "/status — Config &amp; connection status\n"
        "/run — Trigger a full monitor run now\n"
        "/alert on|off — Mute/unmute automatic alerts (owner)\n\n"

        "<b>📊 PORTFOLIO</b>\n"
        "/snapshot — Latest saved snapshot (all tokens)\n"
        "/holders &lt;SYMBOL&gt; — Live top-10 holders\n"
        "/top [SYMBOL] — Top 20 holders with dormancy flags\n"
        "/moves [SYMBOL] — Wallet movements in last 24h\n\n"

        "<b>🔬 INTELLIGENCE</b>\n"
        "/bundle [SYMBOL|WALLET] — Token cluster report or wallet lookup\n"
        "/clusters [SYMBOL] — Detected wallet clusters\n"
        "/relationships [SYMBOL] — Full wallet relationship graph\n"
        "/classify &lt;ADDRESS&gt; — Wallet / LP pool / program check\n"
        "/topwallets [TOKEN] — Rank wallets by meme win rate\n"
        "/related — External token holdings for top wallets\n"
        "/checkbundles [SYMBOL] — Round-number &amp; identical-balance detection\n"
        "/scancluster [SYMBOL] — On-chain SOL transfer scan for cluster\n"
        "/scantest [SYMBOL] — Test scan (2 wallets, no DB write)\n\n"

        "<b>⚙️ CONFIG</b>\n"
        "/addtoken &lt;SYMBOL&gt; &lt;ADDRESS&gt; — Start tracking a token\n"
        "/removetoken &lt;SYMBOL&gt; — Stop tracking a token\n"
        "/threshold &lt;PCT&gt; — Set move alert threshold (e.g. 0.01)\n"
        "/movethreshold &lt;PCT&gt; — Alias for /threshold\n\n"

        "<b>🧪 TESTING</b>\n"
        "/testalert [SYMBOL] — Fire a synthetic alert to test the pipeline\n\n"

        "<b>🎯 SMART WALLETS</b>\n"
        "/addwallet &lt;ADDR&gt; — Track wallet (validates + 30d backfill)\n"
        "/tier &lt;ADDR&gt; — Show tier and performance stats\n"
        "/backfill &lt;ADDR&gt; — Re-run swap backfill for a wallet\n"
        "/evidence &lt;ADDR&gt; — On-chain proof for wallet relationships\n"
        "/rejections — Last 20 toxic-flow filter rejections\n"
        "/injectevidence — Full-history evidence scan for all clusters",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    cfg = _load_config()
    tokens = get_live_tracked_tokens()
    helius_ok  = "✅" if os.environ.get("HELIUS_API_KEY") else "⚠️ not set (public RPC)"
    supabase_ok = "✅" if _supabase is not None else "❌ not connected (check SUPABASE_SERVICE_ROLE_KEY)"
    token_lines = "\n".join(
        f"  • <b>{sym}</b>: <code>{addr[:8]}…</code>" for sym, addr in tokens.items()
    ) or "  (none)"
    await update.message.reply_text(
        f"📊 <b>Monitor Status</b>\n\n"
        f"<b>Move threshold:</b> {cfg.get('move_threshold_pct', MOVE_THRESHOLD_PCT):.4f}%\n"
        f"<b>Min token change:</b> {cfg.get('min_holder_change_tokens', MIN_HOLDER_CHANGE_TOKENS):.0f}\n"
        f"<b>Helius RPC:</b> {helius_ok}\n"
        f"<b>Supabase:</b> {supabase_ok}\n\n"
        f"<b>Tracked tokens:</b>\n{token_lines}\n\n"
        f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>",
        parse_mode="HTML",
    )


async def cmd_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    cfg = _load_config()
    tokens = get_live_tracked_tokens()
    if not tokens:
        await update.message.reply_text("No tokens tracked. Use /addtoken.")
        return
    lines = [f"📸 <b>Latest Snapshots</b> — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"]
    for sym in tokens:
        snap = load_snapshot(sym)
        if not snap:
            lines.append(f"<b>{sym}</b>: no snapshot yet\n")
            continue
        holders = snap.get("holders", [])
        total = sum(get_amount(h) for h in holders) or 1.0
        ts = snap.get("timestamp", "")[:16].replace("T", " ")
        lines.append(f"<b>{sym}</b> ({ts} UTC):")
        for i, h in enumerate(holders[:5], 1):
            pct  = get_amount(h) / total * 100
            addr = h.get("address", "?")
            lines.append(f"  #{i} <code>{addr[:8]}…</code> {pct:.2f}%")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_holders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /holders &lt;SYMBOL&gt;", parse_mode="HTML")
        return
    sym = context.args[0].upper()
    tokens = get_live_tracked_tokens()
    if sym not in tokens:
        known = ", ".join(tokens) or "none"
        await update.message.reply_text(f"Unknown token '{sym}'. Tracked: {known}")
        return
    await update.message.reply_text(f"⏳ Fetching live holders for {sym}…")
    try:
        raw = fetch_holders(tokens[sym])
    except Exception as exc:
        await update.message.reply_text(f"❌ RPC error: {html.escape(str(exc))}")
        return
    if not raw:
        await update.message.reply_text(f"No holders returned for {sym}.")
        return

    # Filter LP pools / programs
    rpc_url = (
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        if HELIUS_API_KEY else PUBLIC_SOLANA_RPC
    )
    loop = asyncio.get_running_loop()
    fr   = await loop.run_in_executor(
        None, classify_and_filter, raw, rpc_url, resolve_owners_batch, _supabase
    )
    real    = fr["real_holders"]
    excl    = fr["excluded"]
    lp_pct  = fr["lp_pct"]

    all_holders = real  # already owner-wallet addresses, reranked
    total_real  = sum(get_amount(h) for h in all_holders) or 1.0

    lines = [f"🐋 <b>{sym} Top-10 Holders</b> (live — LP filtered)\n"]

    # Show excluded LP entries first (greyed out)
    raw_total = sum(get_amount(h) for h in raw) or 1.0
    for (ta, owner, reason) in excl[:3]:
        exc_amt = next(
            (get_amount(h) for h in raw if h.get("address") == ta), 0.0
        )
        exc_pct = exc_amt / raw_total * 100
        short   = f"{owner[:8]}…{owner[-6:]}"
        lines.append(f"<i>[{reason}] <code>{short}</code> {exc_pct:.2f}% — excluded</i>")

    if excl:
        lines.append("")

    for i, h in enumerate(all_holders[:10], 1):
        pct  = get_amount(h) / total_real * 100
        addr = h.get("address", "?")
        link = f'<a href="https://solscan.io/account/{addr}">{addr[:8]}…{addr[-6:]}</a>'
        flag = " 🔶" if pct >= 20.0 else ""
        lines.append(f"#{i} {link}  {pct:.2f}%{flag}")

    conc_top5 = sum(get_amount(h) / total_real * 100 for h in all_holders[:5])
    lines.append(
        f"\n<b>Top-5 concentration (excl. LP):</b> {conc_top5:.1f}%"
    )
    if lp_pct > 0:
        lines.append(f"<b>LP / programs hold:</b> ~{lp_pct:.1f}% of supply")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_addtoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /addtoken &lt;SYMBOL&gt; &lt;ADDRESS&gt;", parse_mode="HTML"
        )
        return
    sym  = context.args[0].upper()
    mint = context.args[1].strip()
    if not (32 <= len(mint) <= 50):
        await update.message.reply_text("❌ That doesn't look like a valid Solana mint address.")
        return
    cfg = _load_config()
    cfg.setdefault("solana_tokens", {})[sym] = {"address": mint, "name": sym.title(), "emoji": "🔹"}
    status = _persist_config(cfg)
    await update.message.reply_text(
        f"✅ Now tracking <b>{sym}</b> (<code>{mint[:8]}…</code>)\n{status}",
        parse_mode="HTML",
    )


async def cmd_removetoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /removetoken &lt;SYMBOL&gt;", parse_mode="HTML")
        return
    sym = context.args[0].upper()
    cfg = _load_config()
    if sym not in cfg.get("solana_tokens", {}):
        await update.message.reply_text(f"'{sym}' is not currently tracked.")
        return
    del cfg["solana_tokens"][sym]
    status = _persist_config(cfg)
    await update.message.reply_text(f"✅ Removed <b>{sym}</b>.\n{status}", parse_mode="HTML")


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /threshold &lt;PCT&gt;  e.g. /threshold 0.01", parse_mode="HTML"
        )
        return
    try:
        pct = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")
        return
    if not 0 < pct <= 100:
        await update.message.reply_text("❌ Threshold must be between 0 and 100.")
        return
    cfg = _load_config()
    cfg["move_threshold_pct"] = pct
    status = _persist_config(cfg)
    await update.message.reply_text(
        f"✅ Move threshold set to <b>{pct:.4f}%</b>\n{status}",
        parse_mode="HTML",
    )


async def cmd_related(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return

    await update.message.reply_text(
        "⏳ Fetching live external holdings for top wallets… (may take 20–30s)",
    )

    cfg    = _load_config()
    tokens = get_live_tracked_tokens()
    if not tokens:
        await update.message.reply_text("No tokens tracked. Use /addtoken.")
        return

    loop   = asyncio.get_running_loop()
    lines  = ["🕸 <b>Cross-Token Whale Intelligence</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    ext_names = ", ".join(MAJOR_TOKEN_MINTS.keys())

    for sym, token_addr in tokens.items():
        snap = load_snapshot(sym)
        emoji = cfg.get("solana_tokens", {}).get(sym, {}).get("emoji", "🔹")
        if not snap:
            lines.append(f"\n{emoji} <b>{sym}</b>: no snapshot yet")
            continue

        all_holders = snap.get("holders", [])
        total       = sum(get_amount(h) for h in all_holders) or 1.0
        top10       = all_holders[:10]

        wallet_lines: list[str] = []
        for rank, h in enumerate(top10, 1):
            addr = h.get("address", "")
            pct  = get_amount(h) / total * 100

            # Run synchronous RPC call in a thread so we don't block the event loop
            intel = await loop.run_in_executor(None, fetch_wallet_intel, addr, sym)

            majors  = intel.get("major_tokens", {})
            sol_usd = intel.get("sol_usd")

            # Only show external positions >= $500
            significant = {
                s: d for s, d in majors.items() if d.get("usd", 0) >= 500
            }
            if sol_usd and sol_usd >= 500:
                significant["SOL"] = {"usd": sol_usd}

            if not significant:
                continue

            parts = [
                f"{ext} ~${d['usd']:,.0f}"
                for ext, d in sorted(significant.items(), key=lambda kv: -kv[1].get("usd", 0))
            ]
            short = f"{addr[:8]}…{addr[-6:]}"
            wallet_lines.append(
                f"  #{rank} <code>{short}</code> ({pct:.2f}%)\n"
                f"    • {' | '.join(parts)}"
            )

        lines.append(f"\n{emoji} <b>{sym}</b>:")
        if wallet_lines:
            lines.extend(wallet_lines)
        else:
            lines.append("  No top-10 holder holds ≥$500 in any external token")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>Checks: {ext_names}</i>")
    lines.append(f"<i>Threshold ≥$500 | {datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return

    live = get_live_tracked_tokens()
    sym = (context.args[0].upper() if context.args else None) or next(iter(live), None)
    addr = live.get(sym)
    if not addr:
        await update.message.reply_text(
            f"Unknown token '{sym}'. Tracked: {', '.join(live) or 'none'}"
        )
        return
    tok = {"address": addr}

    snap = load_snapshot(sym)
    if not snap or not snap.get("holders"):
        await update.message.reply_text("No snapshot found — run /run first.")
        return

    holders = snap["holders"]
    h1      = holders[0]
    addr    = h1.get("address", "11111111111111111111111111111111")
    total   = sum(get_amount(h) for h in holders) or 1.0
    old_pct = get_amount(h1) / total * 100

    fake_change = {
        "type": "MOVE", "address": addr,
        "old_pct": old_pct, "new_pct": old_pct + 0.05, "delta": 0.05,
        "old_rank": 1, "new_rank": 1,
        "tokens_delta": 50_000,
        "old_tokens": get_amount(h1), "new_tokens": get_amount(h1) + 50_000,
        "trigger": "pct",
    }

    loop      = asyncio.get_running_loop()
    price_ctx = await loop.run_in_executor(None, fetch_price_context, tok["address"])
    msg = format_quant_alert(
        sym, tok["address"], fake_change, price_ctx,
        wallet_info={"age_days": 45, "is_new_wallet": False},
        ai_interp="[TEST] Synthetic move — verifying alert pipeline end-to-end.",
    )
    msg = f"⚠️ <b>TEST ALERT — pipeline check</b>\n\n{msg}"

    ok, err = send_alert(msg)
    if ok:
        await update.message.reply_text("✅ Test alert sent to channel.")
    else:
        await update.message.reply_text(
            f"❌ Send failed: {html.escape(str(err))}\n"
            "Check TELEGRAM_CHANNEL_ID env var and bot membership."
        )


async def cmd_classify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Classify a Solana address — is it a real wallet, LP pool, or program?"""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /classify &lt;SOLANA_ADDRESS&gt;\n"
            "Example: /classify 5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
            parse_mode="HTML",
        )
        return

    address = context.args[0].strip()
    if len(address) < 32 or len(address) > 44:
        await update.message.reply_text("❌ That doesn't look like a valid Solana address (32–44 chars).")
        return

    rpc_url = (
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        if HELIUS_API_KEY else PUBLIC_SOLANA_RPC
    )

    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, classify_address, address, rpc_url, _supabase
    )

    label      = result["label"]
    detail     = result["detail"]
    solscan    = f'<a href="https://solscan.io/account/{address}">{address[:8]}…{address[-6:]}</a>'
    icon       = {"WALLET": "✅", "KNOWN_PROGRAM": "⛔", "LP_POOL": "🏊", "PROGRAM": "🤖"}.get(label, "❓")

    await update.message.reply_text(
        f"{icon} <b>Address Classification</b>\n\n"
        f"<b>Address:</b> {solscan}\n"
        f"<b>Label:</b> {label}\n"
        f"<b>Detail:</b> {html.escape(detail)}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_clusters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all detected wallet clusters for a token."""
    if not _authorized(update):
        await _deny(update)
        return
    live = get_live_tracked_tokens()
    sym = (context.args[0].upper() if context.args else None) or next(iter(live), None)
    addr = live.get(sym)
    if not addr:
        await update.message.reply_text(
            f"Unknown token '{sym}'. Tracked: {', '.join(live) or 'none'}"
        )
        return
    tok = {"address": addr}

    loop     = asyncio.get_running_loop()
    clusters = await loop.run_in_executor(
        None, get_wallet_clusters_for_token, tok["address"], _supabase
    )

    if not clusters:
        await update.message.reply_text(
            f"No clusters detected for {sym} yet.\n"
            "Run /run to trigger detection, or wait for next cron cycle."
        )
        return

    lines = [
        f"🔍 <b>{sym} — Wallet Clusters</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    risk_icons = {"HIGH_RISK": "🔴", "MEDIUM": "🟡", "SMART_MONEY": "🟢"}

    for i, cl in enumerate(clusters[:8], 1):
        risk    = cl.get("risk_level", "UNKNOWN")
        ctype   = cl.get("cluster_type", "?")
        method  = cl.get("detection_method", "?")
        pct     = cl.get("total_supply_pct") or 0
        n       = cl.get("wallet_count") or len(cl.get("wallets", []))
        icon    = risk_icons.get(risk, "⚪")
        cid     = cl.get("cluster_id", "")[-8:]

        lines.append(f"\n{icon} <b>Cluster #{i}</b> — {ctype} ({method})")
        lines.append(f"   Wallets: {n} | Supply: {pct:.2f}% | Risk: {risk}")

        if cl.get("funder_address"):
            f = cl["funder_address"]
            lines.append(f'   Funder: <a href="https://solscan.io/account/{f}">{f[:8]}…{f[-6:]}</a>')

        for w in (cl.get("wallets") or [])[:3]:
            short = f"{w[:8]}…{w[-6:]}"
            lines.append(f'   📋 <a href="https://solscan.io/account/{w}">{short}</a>')
        more = n - 3
        if more > 0:
            lines.append(f"   … +{more} more")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>{datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


async def cmd_bundle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check if a specific wallet is in any bundle/cluster."""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /bundle &lt;WALLET_ADDRESS&gt;", parse_mode="HTML"
        )
        return

    wallet = context.args[0].strip()
    if len(wallet) < 32 or len(wallet) > 44:
        await update.message.reply_text("❌ Invalid Solana address.")
        return

    loop = asyncio.get_running_loop()
    cl   = await loop.run_in_executor(None, get_cluster_for_wallet, wallet, _supabase)

    short = f"{wallet[:8]}…{wallet[-6:]}"
    link  = f'<a href="https://solscan.io/account/{wallet}">{short}</a>'

    if not cl:
        await update.message.reply_text(
            f"🔍 Wallet: {link}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ No cluster membership detected.\n"
            "This wallet has not been linked to any bundle or coordinated group.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    risk    = cl.get("risk_level", "?")
    ctype   = cl.get("cluster_type", "?")
    method  = cl.get("detection_method", "?")
    pct     = cl.get("total_supply_pct") or 0
    n       = cl.get("wallet_count") or 0
    funder  = cl.get("funder_address")
    cid     = cl.get("cluster_id", "")
    members = cl.get("wallets", [])
    risk_icon = {"HIGH_RISK": "🔴", "MEDIUM": "🟡", "SMART_MONEY": "🟢"}.get(risk, "⚪")

    lines = [
        f"🔍 Wallet: {link}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 {risk_icon} Cluster: {ctype} ({cid[-8:]})",
        f"Method: {method} | Supply: {pct:.2f}% | {n} wallets",
    ]
    if funder:
        lines.append(
            f'🏦 Funder: <a href="https://solscan.io/account/{funder}">{funder[:8]}…{funder[-6:]}</a>'
        )

    peers = [w for w in members if w != wallet][:4]
    if peers:
        lines.append("🤝 Related wallets:")
        for p in peers:
            ps = f"{p[:8]}…{p[-6:]}"
            lines.append(f'  <a href="https://solscan.io/account/{p}">{ps}</a>')

    # Relationship count
    if _supabase:
        try:
            r = _supabase.table("wallet_relationships").select("relationship_type").or_(
                f"wallet_a.eq.{wallet},wallet_b.eq.{wallet}"
            ).execute()
            by_type: dict[str, int] = {}
            for row in (r.data or []):
                t = row["relationship_type"]
                by_type[t] = by_type.get(t, 0) + 1
            if by_type:
                rel_str = " | ".join(f"{t}: {n}" for t, n in sorted(by_type.items()))
                lines.append(f"📊 Relationships: {rel_str}")
        except Exception:
            pass

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if risk == "HIGH_RISK":
        lines.append("⚠️ HIGH RISK — likely insider/team wallet")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


def _get_muted_tokens() -> set[str]:
    """Read the live muted_tokens set from bot_config."""
    try:
        if _supabase:
            r = _supabase.table("bot_config").select("value").eq("key", "muted_tokens").execute()
            if r.data and r.data[0].get("value"):
                return {str(s).strip().upper() for s in json.loads(r.data[0]["value"]) if str(s).strip()}
    except Exception as exc:
        log.warning("read muted_tokens failed: %s", exc)
    return set()


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mute Telegram alerts for ONE token. Data collection is unaffected."""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /mute &lt;SYMBOL&gt;", parse_mode="HTML")
        return
    sym  = context.args[0].upper()
    live = get_live_tracked_tokens()
    if sym not in live:
        await update.message.reply_text(f"Unknown token '{sym}'. Tracked: {', '.join(live) or 'none'}")
        return
    muted = _get_muted_tokens()
    muted.add(sym)
    if update_bot_config("muted_tokens", json.dumps(sorted(muted))):
        await update.message.reply_text(
            f"🔇 Muted {sym}. Telegram alerts suppressed — snapshots, relationships, "
            f"evidence and clusters keep being collected."
        )
    else:
        await update.message.reply_text("Failed to update muted_tokens.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-enable Telegram alerts for a token."""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /unmute &lt;SYMBOL&gt;", parse_mode="HTML")
        return
    sym   = context.args[0].upper()
    muted = _get_muted_tokens()
    if sym not in muted:
        await update.message.reply_text(f"{sym} is not muted. Muted: {', '.join(sorted(muted)) or 'none'}")
        return
    muted.discard(sym)
    if update_bot_config("muted_tokens", json.dumps(sorted(muted))):
        await update.message.reply_text(f"🔔 Unmuted {sym}. Telegram alerts restored.")
    else:
        await update.message.reply_text("Failed to update muted_tokens.")


async def cmd_muted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List currently muted tokens."""
    if not _authorized(update):
        await _deny(update)
        return
    muted = _get_muted_tokens()
    await update.message.reply_text("🔇 Muted tokens: " + (", ".join(sorted(muted)) or "none (all alerting)"))


def _latest_holders_by_token(live: dict[str, str]) -> dict[str, tuple[list[dict], float]]:
    """{sym: (holders[{address,uiAmount}], total_supply)} from the latest snapshot batch."""
    out: dict[str, tuple[list[dict], float]] = {}
    for sym, mint in live.items():
        try:
            r = (_supabase.table("wallet_snapshots")
                 .select("wallet_address,balance,total_supply,captured_at")
                 .eq("token_address", mint).order("captured_at", desc=True)
                 .limit(60).execute())
            rows = r.data or []
        except Exception as exc:
            log.warning("crosswallets snapshot query failed for %s: %s", sym, exc)
            rows = []
        if not rows:
            continue
        latest = (rows[0].get("captured_at") or "")[:19]
        holders, seen, supply = [], set(), 0.0
        for row in rows:
            if (row.get("captured_at") or "")[:19] != latest:
                continue
            w = row["wallet_address"]
            if w in seen:
                continue
            seen.add(w)
            holders.append({"address": w, "uiAmount": float(row.get("balance") or 0)})
            supply = float(row.get("total_supply") or 0) or supply
        out[sym] = (holders, supply)
    return out


def _crosswallets_fast(live: dict[str, str]) -> dict[str, dict[str, float]]:
    """Top-20 intersection — reuses build_cross_holdings (zero duplicated calc)."""
    data = _latest_holders_by_token(live)
    all_current  = {sym: hd[0] for sym, hd in data.items()}
    all_supplies = {sym: hd[1] for sym, hd in data.items()}
    return build_cross_holdings(all_current, all_supplies)


def _crosswallets_deep(live: dict[str, str]) -> dict[str, dict[str, float]]:
    """
    Per-wallet on-chain cross-balance check — catches holders that aren't in any token's
    top-20 (the screenshot case). Candidate set = union of current top holders; each is
    checked for real balances of every tracked mint via fetch_wallet_token_amounts.
    """
    data      = _latest_holders_by_token(live)
    supplies  = {sym: hd[1] for sym, hd in data.items()}
    mint_sym  = {mint: sym for sym, mint in live.items()}
    candidates: set[str] = set()
    for holders, _ in data.values():
        candidates.update(h["address"] for h in holders)
    result: dict[str, dict[str, float]] = {}
    for w in list(candidates)[:80]:          # bounded
        amts = fetch_wallet_token_amounts(w)
        if not amts:
            continue
        held: dict[str, float] = {}
        for mint, sym in mint_sym.items():
            amt = amts.get(mint)
            if amt and amt > 0:
                sup = supplies.get(sym) or 0.0
                held[sym] = round(amt / sup * 100, 4) if sup > 0 else 0.0
        if len(held) >= 2:
            result[w] = held
    return result


async def cmd_crosswallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    On-demand cross-token holders. Usage:
      /crosswallets            — fast (top-20 snapshot intersection)
      /crosswallets deep       — on-chain per-wallet scan (catches sub-top-20 holders)
      /crosswallets [deep] SYM — only wallets also overlapping SYM
    """
    if not _authorized(update):
        await _deny(update)
        return
    args = context.args or []
    deep = any(a.lower() == "deep" for a in args)
    filt = next((a.upper() for a in args if a.lower() != "deep"), None)
    live = get_live_tracked_tokens()
    if len(live) < 2:
        await update.message.reply_text("Need 2+ tracked tokens to find cross-holders.")
        return
    if filt and filt not in live:
        await update.message.reply_text(f"Unknown token '{filt}'. Tracked: {', '.join(live)}")
        return
    if deep:
        await update.message.reply_text("⏳ Deep on-chain scan — this can take ~20s…")
    loop  = asyncio.get_running_loop()
    cross = await loop.run_in_executor(None, _crosswallets_deep if deep else _crosswallets_fast, live)
    multi = {a: h for a, h in cross.items() if len(h) >= 2 and (filt is None or filt in h)}
    mode  = "deep" if deep else "snapshot"
    if not multi:
        scope = f" overlapping {filt}" if filt else ""
        await update.message.reply_text(f"No wallets hold 2+ tracked tokens{scope} ({mode} scan).")
        return
    lines = [
        f"🕸 <b>Cross-coin Whales ({mode}) — {datetime.now(timezone.utc).strftime('%H:%M UTC')}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for addr, h in sorted(multi.items(), key=lambda kv: -sum(kv[1].values()))[:15]:
        lines.append(f"<code>{addr[:8]}...{addr[-6:]}</code>  (combined {sum(h.values()):.2f}%)")
        lines.append(f"📋 <code>{addr}</code>")
        for sym, pct in sorted(h.items(), key=lambda kv: -kv[1]):
            lines.append(f"  • {sym}: {pct:.2f}%")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if not deep:
        lines.append("ℹ️ snapshot scan = top-20 intersection. Use <code>/crosswallets deep</code> for sub-top-20 holders.")
    await update.message.reply_text("\n".join(lines).strip(), parse_mode="HTML")


async def cmd_relationships(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the full relationship graph for a token."""
    if not _authorized(update):
        await _deny(update)
        return
    live = get_live_tracked_tokens()
    sym = (context.args[0].upper() if context.args else None) or next(iter(live), None)
    addr = live.get(sym)
    if not addr:
        await update.message.reply_text(
            f"Unknown token '{sym}'. Tracked: {', '.join(live) or 'none'}"
        )
        return
    tok = {"address": addr}

    loop  = asyncio.get_running_loop()
    rels  = await loop.run_in_executor(
        None, get_relationships_for_token, tok["address"], _supabase
    )

    if not rels:
        await update.message.reply_text(
            f"No relationships detected for {sym} yet.\nRun /run to trigger detection."
        )
        return

    # Group by pair
    pair_map: dict[tuple[str, str], list[str]] = {}
    for r in rels:
        a, b = r["wallet_a"], r["wallet_b"]
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        pair_map.setdefault(key, []).append(r["relationship_type"])

    lines = [
        f"🕸 <b>{sym} — Relationship Map</b>",
        f"<i>{len(pair_map)} wallet pairs | {datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    sorted_pairs = sorted(
        pair_map.items(),
        key=lambda kv: -sum(
            {"JITO_BUNDLE": 4, "INTER_TRANSFER": 3, "COMMON_FUNDER": 2,
             "TEMPORAL_CLUSTER": 1, "CROSS_TOKEN_HOLDER": 0}.get(t, 0)
            for t in kv[1]
        )
    )

    for (a, b), types in sorted_pairs[:20]:
        as_ = f"{a[:6]}…{a[-4:]}"
        bs_ = f"{b[:6]}…{b[-4:]}"
        type_str = " | ".join(sorted(set(types), key=lambda t: -{"JITO_BUNDLE": 4, "INTER_TRANSFER": 3,
            "COMMON_FUNDER": 2, "TEMPORAL_CLUSTER": 1, "CROSS_TOKEN_HOLDER": 0}.get(t, 0)))
        lines.append(f"<code>{as_}</code> ↔ <code>{bs_}</code>  [{type_str}]")

    if len(sorted_pairs) > 20:
        lines.append(f"… +{len(sorted_pairs) - 20} more pairs")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    global _monitor_proc
    if _monitor_proc and _monitor_proc.poll() is None:
        await update.message.reply_text("⏳ A run is already in progress — please wait.")
        return
    import sys
    monitor_path = os.path.join(_REPO_DIR, "monitor.py")
    _stderr_log = open("/tmp/monitor_run.log", "w")
    _monitor_proc = subprocess.Popen(
        [sys.executable, monitor_path],
        stdout=subprocess.DEVNULL,
        stderr=_stderr_log,
    )
    await update.message.reply_text(
        "⏳ Manual monitor run started.\n"
        "Alerts will appear in the group within ~60s."
    )
    await asyncio.sleep(2)
    if _monitor_proc.poll() is not None:
        _stderr_log.flush()
        try:
            err = open("/tmp/monitor_run.log").read(500).strip()
        except OSError:
            err = "(no log)"
        await update.message.reply_text(
            f"❌ Monitor process exited immediately.\n<pre>{html.escape(err)}</pre>",
            parse_mode="HTML",
        )
        return

    # Wait up to 120s for completion and report result
    loop = asyncio.get_running_loop()
    try:
        exit_code = await asyncio.wait_for(
            loop.run_in_executor(None, _monitor_proc.wait),
            timeout=120,
        )
        if exit_code == 0:
            await update.message.reply_text("✅ Monitor run complete — check above for any alerts.")
        else:
            _stderr_log.flush()
            try:
                err = open("/tmp/monitor_run.log").read(500).strip()
            except OSError:
                err = "(no log)"
            await update.message.reply_text(
                f"❌ Monitor exited with code {exit_code}.\n<pre>{html.escape(err)}</pre>",
                parse_mode="HTML",
            )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⏱ Monitor still running after 120s — alerts will appear when ready."
        )


async def cmd_topwallets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return

    arg = context.args[0].upper() if context.args else None
    if arg and arg not in MAJOR_TOKEN_MINTS:
        await update.message.reply_text(
            f"Unknown token '{arg}'. Options: {', '.join(MAJOR_TOKEN_MINTS)}",
        )
        return
    mints = {arg: MAJOR_TOKEN_MINTS[arg]} if arg else dict(MAJOR_TOKEN_MINTS)

    await update.message.reply_text("⏳ Scanning top wallets… (may take 20–40s)")

    loop = asyncio.get_running_loop()

    # Collect unique wallet addresses across selected tokens
    token_holder_map: dict[str, list[str]] = {}
    for sym, mint in mints.items():
        try:
            holders = await loop.run_in_executor(None, fetch_holders, mint)
            token_holder_map[sym] = [
                h.get("address", "") for h in holders[:20] if h.get("address")
            ]
        except Exception as exc:
            log.warning("fetch_holders failed for %s: %s", sym, exc)

    all_token_accounts: list[str] = list({a for addrs in token_holder_map.values() for a in addrs})
    if not all_token_accounts:
        await update.message.reply_text("❌ No holders found.")
        return

    # Resolve token account addresses → owner wallet addresses (one batch RPC call)
    owners_map = await loop.run_in_executor(None, resolve_owners_batch, all_token_accounts)
    all_wallets = list(set(owners_map.values()))
    if not all_wallets:
        await update.message.reply_text("❌ Could not resolve wallet owners.")
        return

    # Win rate per owner wallet
    results: list[tuple[str, dict]] = []
    for addr in all_wallets:
        wr = await loop.run_in_executor(None, fetch_wallet_winrate, addr)
        if wr["wins"] + wr["losses"] > 0:
            results.append((addr, wr))
    results.sort(key=lambda x: -x[1]["win_rate"])

    # Most held tokens across wallets
    token_pop: dict[str, int] = {}
    for _, wr in results:
        for sym in wr["changes"]:
            token_pop[sym] = token_pop.get(sym, 0) + 1
    top_tokens = sorted(token_pop, key=lambda s: -token_pop[s])

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        "🏆 <b>Top Wallets by Win Rate</b>",
        f"Tokens: {' '.join(mints)} | {len(all_wallets)} owners scanned | {ts}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for i, (addr, wr) in enumerate(results[:10], 1):
        link    = f'<a href="https://solscan.io/account/{addr}">{addr}</a>'
        pct     = f"{wr['win_rate']*100:.0f}%"
        wl      = f"({wr['wins']}W/{wr['losses']}L)"
        usd     = f"  ~${wr['usd_total']:,.0f}" if wr.get("usd_total") else ""
        winners = " | ".join(
            f"{s} <b>+{c:.1f}%</b>"
            for s, c in sorted(wr["changes"].items(), key=lambda kv: -kv[1])
            if c > 0
        )
        losers  = " | ".join(
            f"{s} {c:.1f}%"
            for s, c in sorted(wr["changes"].items(), key=lambda kv: kv[1])
            if c < 0
        )
        lines.append(f"\n#{i}  {link}  {pct} {wl}{usd}")
        if winners:
            lines.append(f"    📈 {winners}")
        if losers:
            lines.append(f"    📉 {losers}")

    if top_tokens:
        pop_str = " | ".join(f"{s} ({token_pop[s]})" for s in top_tokens[:5])
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🔥 Most held: {pop_str}")

    msg = "\n".join(lines)
    # Telegram message limit is 4096 chars
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…"
    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_checkbundles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /checkbundles [SYMBOL]
    Run round-number and identical-balance detection on the latest holder snapshot.
    Owner only.
    """
    if not _authorized(update):
        await _deny(update)
        return

    from wallet_relationship_engine import detect_round_number_holders, detect_identical_balance_pairs

    arg = context.args[0].upper() if context.args else None
    tokens_to_check = {arg: TOKENS[arg]} if (arg and arg in TOKENS) else dict(TOKENS)

    if not tokens_to_check:
        await update.message.reply_text("No tracked tokens. Use /addtoken to add one.")
        return

    await update.message.reply_text(f"⏳ Scanning bundles for {', '.join(tokens_to_check)}…")

    loop = asyncio.get_running_loop()
    lines: list[str] = []

    for sym, token_address in tokens_to_check.items():
        try:
            raw = await loop.run_in_executor(None, fetch_holders, token_address)
        except Exception as exc:
            lines.append(f"❌ {sym}: fetch failed — {exc}")
            continue
        if not raw:
            lines.append(f"❌ {sym}: no holders returned")
            continue

        _rpc = (
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
            if HELIUS_API_KEY else PUBLIC_SOLANA_RPC
        )
        from address_filters import classify_and_filter
        filter_result = classify_and_filter(raw, _rpc, resolve_owners_batch, _supabase)
        holders = filter_result["real_holders"]

        round_rels = detect_round_number_holders(holders, token_address, sym)
        ident_rels = detect_identical_balance_pairs(holders, token_address, sym)

        lines.append(f"\n<b>🔍 {sym} Bundle Check</b>")
        lines.append(f"Real holders: {len(holders)} (LP excluded ✅)")

        # Summarise round-number bundles
        import json as _json
        from collections import defaultdict
        round_groups: dict[int, set[str]] = defaultdict(set)
        for rel in round_rels:
            try:
                ev = _json.loads(rel.get("evidence") or "{}")
                m = ev.get("round_millions", 0)
                round_groups[m].update([rel["wallet_a"], rel["wallet_b"]])
            except Exception:
                pass

        if round_groups:
            lines.append("⚠️ <b>Round-number bundles:</b>")
            for amount, wallets in sorted(round_groups.items(), key=lambda x: -len(x[1])):
                lines.append(f"  {len(wallets)} wallets × {amount}M tokens each:")
                for w in list(wallets)[:4]:
                    lines.append(f"    📋 <code>{w}</code>")
                if len(wallets) > 4:
                    lines.append(f"    … +{len(wallets) - 4} more")
        else:
            lines.append("✅ No round-number bundles detected")

        # Summarise identical-balance pairs
        if ident_rels:
            lines.append(f"⚠️ <b>Identical-balance pairs ({len(ident_rels)}):</b>")
            for rel in ident_rels[:5]:
                try:
                    ev = _json.loads(rel.get("evidence") or "{}")
                    diff = ev.get("diff_pct", 0)
                    bal  = ev.get("balance_a", 0)
                    lines.append(
                        f"  <code>{rel['wallet_a'][:8]}…</code> ≈ "
                        f"<code>{rel['wallet_b'][:8]}…</code>  "
                        f"({bal/1e6:.2f}M, {diff:.4f}% diff)"
                    )
                except Exception:
                    pass
            if len(ident_rels) > 5:
                lines.append(f"  … +{len(ident_rels) - 5} more pairs")
        else:
            lines.append("✅ No identical-balance pairs detected")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…"
    await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)


def _time_ago(dt: datetime) -> str:
    diff = datetime.now(timezone.utc) - dt
    d, s = diff.days, int(diff.total_seconds())
    if d >= 1:  return f"{d}d ago"
    if s >= 3600: return f"{s // 3600}h ago"
    return f"{s // 60}m ago"


async def cmd_moves(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all wallet movements in the last 24h for a token."""
    if not _authorized(update):
        await _deny(update)
        return
    cfg    = _load_config()
    tokens = get_live_tracked_tokens()
    symbol = context.args[0].upper() if context.args else None
    syms   = [symbol] if symbol and symbol in tokens else list(tokens.keys())
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    for sym in syms:
        if not _supabase:
            await update.message.reply_text("❌ Supabase not configured.")
            return
        try:
            r = (_supabase.table("whale_alerts")
                 .select("wallet_address,change_type,delta_pct,alerted_at")
                 .eq("token_symbol", sym)
                 .gt("alerted_at", cutoff)
                 .order("alerted_at", desc=True)
                 .execute())
        except Exception as exc:
            await update.message.reply_text(f"❌ DB error: {exc}")
            return
        if not r.data:
            await update.message.reply_text(f"No movements above threshold in 24h for {sym}.")
            continue
        lines = [f"📊 <b>{sym} — Movements (24h)</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
        for row in r.data:
            addr = row["wallet_address"]
            dt   = datetime.fromisoformat(row["alerted_at"].replace("Z", "+00:00"))
            dpct = row.get("delta_pct") or 0
            sign = "🟢" if dpct > 0 else "🔴"
            lines.append(
                f"{sign} {row['change_type']} {dpct:+.3f}% — "
                f"<code>{addr[:6]}…{addr[-4:]}</code> ({_time_ago(dt)})"
            )
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
        )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top 20 holders with last-movement date and dormancy flags."""
    if not _authorized(update):
        await _deny(update)
        return
    cfg    = _load_config()
    tokens = get_live_tracked_tokens()
    symbol = context.args[0].upper() if context.args else None
    syms   = [symbol] if symbol and symbol in tokens else list(tokens.keys())
    for sym in syms:
        snap    = load_snapshot(sym)
        holders = (snap or {}).get("holders", [])
        if not holders:
            await update.message.reply_text(f"No snapshot for {sym}.")
            continue
        total = sum(get_amount(h) for h in holders) or 1.0
        now   = datetime.now(timezone.utc)
        lines = [f"🏆 <b>{sym} — Top 20 Holders</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
        for rank, h in enumerate(holders[:20], 1):
            addr = h.get("address", "")
            wpct = get_amount(h) / total * 100
            flag = ""
            if _supabase and addr:
                try:
                    r = (_supabase.table("whale_alerts")
                         .select("alerted_at")
                         .eq("wallet_address", addr).eq("token_symbol", sym)
                         .order("alerted_at", desc=True).limit(1).execute())
                    if r.data:
                        ldt  = datetime.fromisoformat(r.data[0]["alerted_at"].replace("Z", "+00:00"))
                        ldt  = ldt if ldt.tzinfo else ldt.replace(tzinfo=timezone.utc)
                        days = (now - ldt).days
                        if days < 1:   flag = f"🔥 Active ({_time_ago(ldt)})"
                        elif days > 30: flag = f"⚠️ Dormant ({days}d)"
                        else:           flag = f"Last: {days}d ago"
                    else:
                        flag = "No recorded moves"
                except Exception:
                    pass
            lines.append(f"#{rank:>2}  <code>{addr[:6]}…{addr[-4:]}</code>  {wpct:.2f}%  {flag}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
        )


async def cmd_alert_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mute or unmute all automatic alerts. Owner only. Usage: /alert on|off"""
    if not _authorized(update):
        await _deny(update)
        return
    owner_id = int(os.environ.get("OWNER_USER_ID", "0"))
    if owner_id and update.effective_user and update.effective_user.id != owner_id:
        await update.message.reply_text("❌ Owner only command.")
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /alert on|off")
        return
    muting = context.args[0].lower() == "off"
    update_bot_config("alerts_muted", "true" if muting else "false")
    _monitor_mod.ALERTS_MUTED = muting
    state = "🔇 MUTED" if muting else "🔔 ACTIVE"
    await update.message.reply_text(f"Alerts: {state}")


# ── Inter-transfer scan commands ─────────────────────────────────────────────

def _run_scan_with_progress(
    loop: asyncio.AbstractEventLoop,
    chat_id: int,
    app: Any,
    symbol: str,
    test_mode: bool,
) -> None:
    """Runs in executor thread. Sends Telegram progress updates every 10 wallets."""
    import inter_transfer_detector as itd

    def progress_cb(msg: str) -> None:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML"),
            loop,
        )

    cfg       = _load_config()
    tokens = get_live_tracked_tokens()
    token_addr = tokens.get(symbol.upper(), "")
    sb         = _supabase

    if not sb:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text="❌ Supabase not configured."),
            loop,
        )
        return

    try:
        r = (
            sb.table("wallet_clusters")
            .select("cluster_id")
            .eq("token_address", token_addr)
            .limit(1)
            .execute()
        )
        rows = r.data or []
    except Exception as exc:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=f"❌ DB error: {exc}"),
            loop,
        )
        return

    if not rows:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(
                chat_id=chat_id,
                text=f"No clusters found for {symbol}. Run /run first.",
            ),
            loop,
        )
        return

    cluster_id = rows[0]["cluster_id"]

    with _scan_lock:
        if cluster_id in _active_scans:
            started = _active_scans[cluster_id]
            elapsed = int(time.time() - started)
            m, s = divmod(elapsed, 60)
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ Scan already running for <code>{cluster_id}</code> (started {m}m {s}s ago). Please wait.",
                    parse_mode="HTML",
                ),
                loop,
            )
            return
        _active_scans[cluster_id] = time.time()

    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Starting {'test ' if test_mode else ''}scan for cluster <code>{cluster_id}</code>…",
            parse_mode="HTML",
        ),
        loop,
    )

    try:
        result = itd.scan_cluster(
            cluster_id,
            symbol=symbol.upper(),
            test_mode=test_mode,
            dry_run=test_mode,
            progress_cb=progress_cb,
            supabase=sb,
        )

        n_transfers = len(result.get("transfers") or [])
        n_sigs      = result.get("sig_count", 0)
        n_wallets   = result.get("wallet_count", 0)
        summary = (
            f"✅ Scan complete — {cluster_id}\n"
            f"Wallets: {n_wallets} | Sigs checked: {n_sigs} | Transfers found: {n_transfers}"
        )
        if test_mode:
            summary = f"[TEST MODE — no DB written]\n{summary}"
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=summary),
            loop,
        )
    finally:
        with _scan_lock:
            _active_scans.pop(cluster_id, None)


async def cmd_scancluster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger on-chain SOL inter-transfer scan for a token's cluster. Admin only."""
    if not _authorized(update):
        await _deny(update)
        return
    cfg    = _load_config()
    tokens = get_live_tracked_tokens()
    sym    = (context.args[0].upper() if context.args else None) or next(iter(tokens), None)
    if not sym or sym not in tokens:
        known = ", ".join(tokens) or "none"
        await update.message.reply_text(f"Usage: /scancluster [SYMBOL]\nTracked: {known}")
        return
    await update.message.reply_text(
        f"🔍 Queuing inter-transfer scan for <b>{sym}</b>…\n"
        "This may take several minutes (5 req/min rate limit). "
        "Progress updates will appear here.",
        parse_mode="HTML",
    )
    loop = asyncio.get_running_loop()
    app  = context.application
    chat_id = update.effective_chat.id
    loop.run_in_executor(None, _run_scan_with_progress, loop, chat_id, app, sym, False)


async def cmd_scantest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test inter-transfer scan (2 wallets, 30 days, no DB write). Admin only."""
    if not _authorized(update):
        await _deny(update)
        return
    cfg    = _load_config()
    tokens = get_live_tracked_tokens()
    sym    = (context.args[0].upper() if context.args else None) or next(iter(tokens), None)
    if not sym or sym not in tokens:
        known = ", ".join(tokens) or "none"
        await update.message.reply_text(f"Usage: /scantest [SYMBOL]\nTracked: {known}")
        return
    await update.message.reply_text(
        f"🧪 Running TEST scan for <b>{sym}</b> (2 wallets, 30 days, no DB write)…",
        parse_mode="HTML",
    )
    loop = asyncio.get_running_loop()
    app  = context.application
    chat_id = update.effective_chat.id
    loop.run_in_executor(None, _run_scan_with_progress, loop, chat_id, app, sym, True)


# ── Smart-wallet backfill thread ────────────────────────────────────────────

def _run_wallet_backfill(
    loop: asyncio.AbstractEventLoop,
    chat_id: int,
    app: Any,
    wallet_address: str,
) -> None:
    """Runs in executor thread. Fetches 30d swap history, tiers the wallet, updates DB."""
    import signal_engine

    def _send(msg: str) -> None:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML"),
            loop,
        )

    helius_key = HELIUS_API_KEY
    sb = _supabase
    short = f"{wallet_address[:8]}…{wallet_address[-6:]}"

    with _scan_lock:
        if wallet_address in _active_backfills:
            started = _active_backfills[wallet_address]
            elapsed = int(time.time() - started)
            m, s = divmod(elapsed, 60)
            _send(f"⏳ Backfill already running for <code>{short}</code> ({m}m {s}s ago).")
            return
        _active_backfills[wallet_address] = time.time()

    # Block cluster members — coordinated wallets must not pollute smart_wallets
    if sb:
        cluster_info = is_cluster_member(wallet_address, sb)
        if cluster_info:
            rel_types = ", ".join(cluster_info["relationship_types"])
            partner_count = len(cluster_info["partners"])
            _send(
                f"🚫 <b>Cluster member blocked</b> — <code>{short}</code>\n"
                f"Wallet linked to {partner_count} partner(s) via {rel_types}.\n"
                f"Not eligible for smart_wallets — coordinated activity, not individual alpha."
            )
            try:
                sb.table("smart_wallets").delete().eq("wallet_address", wallet_address).execute()
            except Exception:
                pass
            with _scan_lock:
                _active_backfills.pop(wallet_address, None)
            return

    try:
        if sb:
            try:
                sb.table("smart_wallets").upsert(
                    {"wallet_address": wallet_address, "status": "BACKFILL_RUNNING",
                     "backfill_started_at": datetime.now(timezone.utc).isoformat()},
                    on_conflict="wallet_address",
                ).execute()
            except Exception as exc:
                log.warning("smart_wallets status update failed: %s", exc)

        def progress_cb(msg: str) -> None:
            _send(msg)

        stats = signal_engine.run_swap_backfill(
            wallet_address, helius_key, days=30, progress_cb=progress_cb
        )
        tier = signal_engine.compute_tier(
            stats["win_rate"], stats["avg_hold_time_min"], stats["trades_90d"]
        )
        status = "EXCLUDED" if tier == "TIER_C" else "ACTIVE"
        now_iso = datetime.now(timezone.utc).isoformat()

        if sb:
            try:
                sb.table("smart_wallets").upsert(
                    {
                        "wallet_address":       wallet_address,
                        "tier":                 tier,
                        "status":               status,
                        "win_rate":             stats["win_rate"],
                        "trade_count":          stats["trade_count"],
                        "trades_90d":           stats["trades_90d"],
                        "avg_hold_time_min":    stats["avg_hold_time_min"],
                        "total_pnl_sol":        stats["total_pnl_sol"],
                        "backfill_days":        30,
                        "backfill_completed_at": now_iso,
                        "updated_at":           now_iso,
                    },
                    on_conflict="wallet_address",
                ).execute()
            except Exception as exc:
                log.warning("smart_wallets final upsert failed: %s", exc)

        tier_emoji = {"TIER_A": "🟢", "TIER_B": "🟡", "TIER_C": "🔴"}.get(tier, "⚪")
        avg_h = stats["avg_hold_time_min"] / 60
        pnl_sign = "+" if stats["total_pnl_sol"] >= 0 else ""
        _send(
            f"{tier_emoji} <b>Backfill complete</b> — <code>{short}</code>\n"
            f"Tier: <b>{tier}</b> | Status: {status}\n"
            f"Win rate: {stats['win_rate']*100:.1f}% | "
            f"Trades (90d): {stats['trades_90d']} | "
            f"Avg hold: {avg_h:.1f}h\n"
            f"PnL (30d): {pnl_sign}{stats['total_pnl_sol']:.3f} SOL"
        )
    except Exception as exc:
        log.error("_run_wallet_backfill failed for %s: %s", short, exc, exc_info=True)
        _send(f"❌ Backfill failed for <code>{short}</code>: {exc}")
    finally:
        with _scan_lock:
            _active_backfills.pop(wallet_address, None)


def _run_wallet_evidence_scan(
    loop: asyncio.AbstractEventLoop,
    chat_id: int,
    app: Any,
    wallet_address: str,
) -> None:
    """Runs in executor thread. Full-history inter-transfer scan for a single wallet."""
    import inter_transfer_detector as itd

    def _send(msg: str) -> None:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=msg),
            loop,
        )

    sb = _supabase
    if not sb:
        return

    short = f"{wallet_address[:8]}…{wallet_address[-6:]}"
    try:
        # Find any cluster containing this wallet
        r = (
            sb.table("wallet_clusters")
            .select("cluster_id,token_symbol")
            .filter("wallet_addresses", "cs", f"{{{wallet_address}}}")
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            _send(f"🔍 Evidence scan: {short} not in any known cluster — skipping.")
            return
        cluster_id  = rows[0]["cluster_id"]
        token_sym   = rows[0].get("token_symbol", "")
        itd.scan_cluster(
            cluster_id,
            symbol=token_sym,
            full_history=True,
            supabase=sb,
        )
        # Count evidence rows
        r2 = (
            sb.table("relationship_evidence")
            .select("id", count="exact")
            .or_(f"wallet_a.eq.{wallet_address},wallet_b.eq.{wallet_address}")
            .execute()
        )
        n_evidence = getattr(r2, "count", None) or len(r2.data or [])
        _send(f"🔍 Evidence scan complete for <code>{short}</code> — {n_evidence} records found.")
    except Exception as exc:
        log.warning("_run_wallet_evidence_scan failed: %s", exc)


_inject_lock = threading.Lock()
_inject_running: bool = False


def _run_inject_evidence(
    loop: asyncio.AbstractEventLoop,
    chat_id: int,
    app: Any,
) -> None:
    """
    Runs in executor thread. Scans every cluster with full_history=True,
    writing per-tx SOL-transfer proof to relationship_evidence.
    """
    global _inject_running
    import inter_transfer_detector as itd

    def _send(msg: str) -> None:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML"),
            loop,
        )

    sb = _supabase
    if not sb:
        _send("❌ Supabase not configured.")
        with _inject_lock:
            _inject_running = False
        return

    try:
        r = sb.table("wallet_clusters").select("cluster_id,token_symbol").execute()
        rows = r.data or []
    except Exception as exc:
        _send(f"❌ Failed to fetch clusters: {exc}")
        with _inject_lock:
            _inject_running = False
        return

    clusters: dict[str, str] = {}
    for row in rows:
        cid = row["cluster_id"]
        if cid not in clusters:
            clusters[cid] = row.get("token_symbol", "")

    if not clusters:
        _send("⚠️ No clusters found in wallet_clusters — nothing to inject.")
        with _inject_lock:
            _inject_running = False
        return

    _send(
        f"🔬 <b>Evidence injection started</b> — {len(clusters)} cluster(s)\n"
        f"Full history scan (no date cutoff). This may take several minutes."
    )

    total_transfers = 0
    total_sigs      = 0

    for idx, (cluster_id, symbol) in enumerate(clusters.items(), 1):
        _send(f"[{idx}/{len(clusters)}] Scanning <code>{cluster_id}</code> ({symbol})…")
        try:
            result = itd.scan_cluster(
                cluster_id,
                symbol=symbol or "UNKNOWN",
                full_history=True,
                supabase=sb,
            )
            n_tx   = len(result.get("transfers") or [])
            n_sigs = result.get("sig_count", 0)
            saved  = result.get("pairs_saved", 0)
            total_transfers += n_tx
            total_sigs      += n_sigs
            _send(
                f"  ✅ <code>{cluster_id}</code> — "
                f"{n_sigs} sigs | {n_tx} transfers | {saved} pairs saved"
            )
        except Exception as exc:
            log.error("_run_inject_evidence: cluster %s failed: %s", cluster_id, exc)
            _send(f"  ❌ <code>{cluster_id}</code> — error: {exc}")

    # Final count from DB
    try:
        r2 = sb.table("relationship_evidence").select("id", count="exact").execute()
        db_count = getattr(r2, "count", None) or len(r2.data or [])
    except Exception:
        db_count = "?"

    _send(
        f"🏁 <b>Injection complete</b>\n"
        f"Clusters scanned: {len(clusters)} | "
        f"Sigs checked: {total_sigs} | "
        f"Transfers found: {total_transfers}\n"
        f"relationship_evidence total rows: <b>{db_count}</b>"
    )
    with _inject_lock:
        _inject_running = False


def _validate_solana_address(addr: str) -> bool:
    return bool(addr) and 32 <= len(addr) <= 44 and addr.isalnum()


def _check_tx_history(addr: str) -> bool:
    """Returns True if wallet has on-chain tx history (any 1 sig found)."""
    helius_rpc = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    try:
        resp = requests.post(
            helius_rpc,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [addr, {"limit": 10, "commitment": "confirmed"}],
            },
            timeout=10,
        )
        if not resp.ok:
            return False
        result = resp.json().get("result") or []
        return len(result) > 0
    except Exception:
        return False


# ── Smart-wallet Telegram commands ──────────────────────────────────────────

async def cmd_injectevidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run full-history evidence injection for all clusters. Admin only."""
    global _inject_running
    if not _authorized(update):
        await _deny(update)
        return
    with _inject_lock:
        if _inject_running:
            await update.message.reply_text("⏳ Injection already running — please wait.")
            return
        _inject_running = True
    loop    = asyncio.get_running_loop()
    app_ref = context.application
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        "🔬 Starting full-history evidence injection for all clusters…\n"
        "Progress updates will appear here.",
    )
    loop.run_in_executor(None, _run_inject_evidence, loop, chat_id, app_ref)


async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a smart wallet to track. Validates on-chain, runs 30d swap backfill."""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /addwallet &lt;SOLANA_ADDRESS&gt;", parse_mode="HTML")
        return
    addr = context.args[0].strip()
    if not _validate_solana_address(addr):
        await update.message.reply_text(
            "❌ That doesn't look like a Solana address (must be 32-44 alphanumeric chars)."
        )
        return

    sb = _supabase
    # Check if already tracked
    if sb:
        try:
            r = sb.table("smart_wallets").select("status,tier").eq("wallet_address", addr).limit(1).execute()
            if r.data:
                row = r.data[0]
                await update.message.reply_text(
                    f"ℹ️ Already tracked — status: <b>{row['status']}</b>, "
                    f"tier: <b>{row.get('tier', 'UNTIERED')}</b>",
                    parse_mode="HTML",
                )
                return
        except Exception:
            pass

    await update.message.reply_text("⏳ Checking on-chain history…")
    if not _check_tx_history(addr):
        await update.message.reply_text(
            "❌ No transaction history found for that address. Verify address and try again."
        )
        return

    short = f"{addr[:8]}…{addr[-6:]}"
    if sb:
        try:
            sb.table("smart_wallets").upsert(
                {"wallet_address": addr, "status": "PENDING",
                 "added_at": datetime.now(timezone.utc).isoformat()},
                on_conflict="wallet_address",
            ).execute()
        except Exception as exc:
            log.warning("smart_wallets insert failed: %s", exc)

    await update.message.reply_text(
        f"✅ Added <code>{short}</code>. Running 30-day swap backfill + evidence scan…\n"
        "Results will appear here when done.",
        parse_mode="HTML",
    )
    loop    = asyncio.get_running_loop()
    app_ref = context.application
    chat_id = update.effective_chat.id
    loop.run_in_executor(None, _run_wallet_backfill, loop, chat_id, app_ref, addr)
    loop.run_in_executor(None, _run_wallet_evidence_scan, loop, chat_id, app_ref, addr)


async def cmd_tier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show tier and stats for a tracked wallet. Usage: /tier <address>"""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /tier &lt;SOLANA_ADDRESS&gt;", parse_mode="HTML")
        return
    addr = context.args[0].strip()
    sb = _supabase
    if not sb:
        await update.message.reply_text("❌ Supabase not configured.")
        return
    try:
        r = sb.table("smart_wallets").select("*").eq("wallet_address", addr).limit(1).execute()
    except Exception as exc:
        await update.message.reply_text(f"❌ DB error: {exc}")
        return
    if not r.data:
        await update.message.reply_text(
            f"❌ Wallet not tracked. Use /addwallet <code>{addr[:12]}…</code> first.",
            parse_mode="HTML",
        )
        return
    row = r.data[0]
    tier   = row.get("tier") or "UNTIERED"
    status = row.get("status") or "UNKNOWN"
    tier_emoji = {"TIER_A": "🟢", "TIER_B": "🟡", "TIER_C": "🔴"}.get(tier, "⚪")
    short = f"{addr[:8]}…{addr[-6:]}"
    wr    = row.get("win_rate")
    tc    = row.get("trade_count")
    t90   = row.get("trades_90d")
    hold  = row.get("avg_hold_time_min")
    pnl   = row.get("total_pnl_sol")
    comp  = (row.get("backfill_completed_at") or "")[:10]
    lines = [
        f"{tier_emoji} <b>{tier}</b> — <a href=\"https://solscan.io/account/{addr}\">{short}</a>",
    ]
    if wr is not None:
        avg_h = (hold or 0) / 60
        pnl_s = f"{'+' if (pnl or 0) >= 0 else ''}{pnl:.3f}" if pnl is not None else "N/A"
        lines.append(
            f"Win rate: {wr*100:.1f}% | Trades (90d): {t90 or '?'} | "
            f"Avg hold: {avg_h:.1f}h | PnL: {pnl_s} SOL"
        )
    lines.append(f"Status: {status}" + (f" | Backfill: {comp}" if comp else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run 30d swap backfill for a tracked wallet. Usage: /backfill <address>"""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /backfill &lt;SOLANA_ADDRESS&gt;", parse_mode="HTML")
        return
    addr = context.args[0].strip()
    if not _validate_solana_address(addr):
        await update.message.reply_text("❌ That doesn't look like a valid Solana address.")
        return

    with _scan_lock:
        if addr in _active_backfills:
            started = _active_backfills[addr]
            elapsed = int(time.time() - started)
            m, s = divmod(elapsed, 60)
            await update.message.reply_text(
                f"⏳ Backfill already running for <code>{addr[:8]}…</code> (started {m}m {s}s ago).",
                parse_mode="HTML",
            )
            return

    sb = _supabase
    if sb:
        try:
            sb.table("smart_wallets").upsert(
                {"wallet_address": addr, "status": "PENDING",
                 "backfill_completed_at": None,
                 "updated_at": datetime.now(timezone.utc).isoformat()},
                on_conflict="wallet_address",
            ).execute()
        except Exception:
            pass

    short = f"{addr[:8]}…{addr[-6:]}"
    await update.message.reply_text(
        f"🔄 Re-running 30d backfill for <code>{short}</code>…",
        parse_mode="HTML",
    )
    loop    = asyncio.get_running_loop()
    app_ref = context.application
    chat_id = update.effective_chat.id
    loop.run_in_executor(None, _run_wallet_backfill, loop, chat_id, app_ref, addr)


async def cmd_evidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show on-chain proof for a wallet's relationships. Usage: /evidence <address>"""
    if not _authorized(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /evidence &lt;SOLANA_ADDRESS&gt;", parse_mode="HTML")
        return
    addr = context.args[0].strip()
    sb = _supabase
    if not sb:
        await update.message.reply_text("❌ Supabase not configured.")
        return
    try:
        r = (
            sb.table("relationship_evidence")
            .select("*")
            .or_(f"wallet_a.eq.{addr},wallet_b.eq.{addr}")
            .order("block_time", desc=True)
            .limit(25)
            .execute()
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ DB error: {exc}")
        return

    rows = r.data or []
    short = f"{addr[:8]}…{addr[-6:]}"
    if not rows:
        await update.message.reply_text(
            f"🔍 No evidence found for <code>{short}</code>.\n"
            "Run /scancluster or /addwallet to gather on-chain proof.",
            parse_mode="HTML",
        )
        return

    lines = [f"🔗 <b>Evidence for <code>{short}</code></b> ({len(rows)} records)\n"]
    for ev in rows[:15]:
        rel_type  = ev.get("relationship_type", "UNKNOWN")
        bt        = (ev.get("block_time") or "")[:16].replace("T", " ")
        sig       = ev.get("tx_signature") or ""
        amt       = ev.get("amount_sol")
        raw       = ev.get("raw_json") or {}
        counterpart = ""
        wa, wb = ev.get("wallet_a", ""), ev.get("wallet_b", "")
        other = wb if wa == addr else wa
        if other and other != addr:
            counterpart = f"  ↔ <code>{other[:8]}…{other[-4:]}</code>\n"
        detail = ""
        if rel_type == "SOL_TRANSFER" and amt:
            sender   = raw.get("sender", "")
            receiver = raw.get("receiver", "")
            direction = "→" if sender == addr else "←"
            detail = f"  {amt:.4f} SOL {direction} <code>{other[:8]}…{other[-4:]}</code>\n"
        elif rel_type == "COMMON_FUNDER":
            funder = raw.get("funder", "")
            detail = f"  Shared funder: <code>{funder[:8]}…</code>\n" if funder else ""
        sig_link = (
            f'  tx: <a href="https://solscan.io/tx/{sig}">{sig[:16]}…</a>\n' if sig else ""
        )
        lines.append(
            f"<b>{rel_type}</b> — {bt}\n"
            f"{detail}{counterpart}{sig_link}"
        )

    if len(rows) > 15:
        lines.append(f"… +{len(rows)-15} more records")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


async def cmd_rejections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 20 toxic-flow filter rejections."""
    if not _authorized(update):
        await _deny(update)
        return
    sb = _supabase
    if not sb:
        await update.message.reply_text("❌ Supabase not configured.")
        return
    try:
        r = (
            sb.table("filter_rejections")
            .select("*")
            .order("rejected_at", desc=True)
            .limit(20)
            .execute()
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ DB error: {exc}")
        return

    rows = r.data or []
    if not rows:
        await update.message.reply_text("✅ No filter rejections recorded yet.")
        return

    lines = [f"🚫 <b>Last {len(rows)} filter rejections</b>\n"]
    for row in rows:
        ts     = (row.get("rejected_at") or "")[:16].replace("T", " ")[-5:]  # HH:MM
        code   = row.get("rejection_code") or "?"
        reason = row.get("rejection_reason") or ""
        wallet = row.get("wallet_address") or ""
        sym    = row.get("token_symbol") or "?"
        wshort = f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) >= 10 else wallet
        lines.append(f"[{ts}] <b>{code}</b> | <code>{wshort}</code> | {sym} — {reason}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Global Telegram error handler ────────────────────────────────────────────

async def _telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catch-all error handler registered on the Application.
    Conflict errors (two instances polling simultaneously during Railway blue-green deploy)
    are logged at WARNING and suppressed — the outer retry loop in main.py handles recovery.
    All other errors are logged at ERROR.
    """
    from telegram.error import Conflict, NetworkError, TimedOut
    exc = context.error
    if isinstance(exc, Conflict):
        log.warning("Telegram Conflict: %s (old instance still running — will resolve shortly)", exc)
        return  # suppress — outer loop in main.py will restart if needed
    if isinstance(exc, (NetworkError, TimedOut)):
        log.warning("Telegram network error (transient): %s", exc)
        return
    log.error("Unhandled Telegram error", exc_info=exc)


# ── Entry point ───────────────────────────────────────────────────────────────

async def _set_commands(app: Application) -> None:
    """post_init hook: registers all commands with Telegram so the pinned menu auto-updates."""
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([BotCommand(cmd, desc) for cmd, desc in _COMMAND_LIST])
        log.info("Telegram command menu synced (%d commands)", len(_COMMAND_LIST))
    except Exception as exc:
        log.warning("set_my_commands failed (non-fatal): %s", exc)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_set_commands).build()
    log.info("BotFather commands:\n%s", BOTFATHER_COMMANDS)
    app.add_handler(CommandHandler("ping",          cmd_ping))
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("snapshot",      cmd_snapshot))
    app.add_handler(CommandHandler("holders",       cmd_holders))
    app.add_handler(CommandHandler("addtoken",      cmd_addtoken))
    app.add_handler(CommandHandler("removetoken",   cmd_removetoken))
    app.add_handler(CommandHandler("threshold",     cmd_threshold))
    app.add_handler(CommandHandler("movethreshold", cmd_threshold))
    app.add_handler(CommandHandler("run",           cmd_run))
    app.add_handler(CommandHandler("testalert",     cmd_testalert))
    app.add_handler(CommandHandler("topwallets",    cmd_topwallets))
    app.add_handler(CommandHandler("clusters",      cmd_clusters))
    app.add_handler(CommandHandler("bundle",        cmd_bundle))
    app.add_handler(CommandHandler("relationships", cmd_relationships))
    app.add_handler(CommandHandler("classify",      cmd_classify))
    app.add_handler(CommandHandler("related",       cmd_related))
    app.add_handler(CommandHandler("checkbundles",  cmd_checkbundles))
    app.add_handler(CommandHandler("moves",         cmd_moves))
    app.add_handler(CommandHandler("top",           cmd_top))
    app.add_handler(CommandHandler("alert",         cmd_alert_toggle))
    app.add_handler(CommandHandler("crosswallets",  cmd_crosswallets))
    app.add_handler(CommandHandler("multiholders",  cmd_crosswallets))
    app.add_handler(CommandHandler("mute",          cmd_mute))
    app.add_handler(CommandHandler("unmute",        cmd_unmute))
    app.add_handler(CommandHandler("muted",         cmd_muted))
    app.add_handler(CommandHandler("scancluster",   cmd_scancluster))
    app.add_handler(CommandHandler("scantest",      cmd_scantest))
    app.add_handler(CommandHandler("addwallet",     cmd_addwallet))
    app.add_handler(CommandHandler("tier",          cmd_tier))
    app.add_handler(CommandHandler("backfill",      cmd_backfill))
    app.add_handler(CommandHandler("evidence",      cmd_evidence))
    app.add_handler(CommandHandler("rejections",     cmd_rejections))
    app.add_handler(CommandHandler("injectevidence", cmd_injectevidence))
    app.add_error_handler(_telegram_error_handler)
    log.info("🤖 Bot polling started — authorized chats: %s", sorted(_AUTHORIZED_CHATS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=())


if __name__ == "__main__":
    main()
