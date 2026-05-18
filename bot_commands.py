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
from datetime import datetime, timezone

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
from address_filters import classify_and_filter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

def _parse_chat_ids(raw: str) -> set[int]:
    return {int(cid.strip()) for cid in raw.split(",") if cid.strip().lstrip("-").isdigit()}

_AUTHORIZED_CHATS: frozenset[int] = frozenset(
    (_parse_chat_ids(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else set())
    | _parse_chat_ids(os.environ.get("TELEGRAM_EXTRA_CHAT_IDS", ""))
)
_REPO_DIR = os.path.dirname(os.path.abspath(_CONFIG_PATH))
_monitor_proc: subprocess.Popen | None = None


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
        "📋 <b>Commands</b>\n\n"
        "/ping — Check bot is alive\n"
        "/status — Config &amp; connection status\n"
        "/snapshot — Latest saved snapshot for all tokens\n"
        "/holders &lt;SYMBOL&gt; — Fetch live top-10 holders\n"
        "/related — External token holdings for top wallets\n"
        "/run — Trigger a full monitor run immediately\n"
        "/testalert [SYMBOL] — Fire a synthetic alert to verify the pipeline\n"
        "/topwallets [TOKEN] — Rank top wallets by meme win rate\n"
        "/classify &lt;ADDRESS&gt; — Check if an address is a wallet, LP pool, or program\n"
        "/addtoken &lt;SYMBOL&gt; &lt;ADDRESS&gt; — Start tracking a token\n"
        "/removetoken &lt;SYMBOL&gt; — Stop tracking a token\n"
        "/threshold &lt;PCT&gt; — Set move alert threshold (e.g. 0.01)\n"
        "/movethreshold &lt;PCT&gt; — Alias for /threshold\n",
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    cfg = _load_config()
    tokens = {sym: info["address"] for sym, info in cfg.get("solana_tokens", {}).items()}
    helius_ok  = "✅" if os.environ.get("HELIUS_API_KEY") else "⚠️ not set (public RPC)"
    supabase_ok = "✅" if os.environ.get("SUPABASE_URL") else "❌ not configured"
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
    tokens = {sym: info["address"] for sym, info in cfg.get("solana_tokens", {}).items()}
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
    cfg = _load_config()
    tokens = {s: info["address"] for s, info in cfg.get("solana_tokens", {}).items()}
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
    tokens = {sym: info["address"] for sym, info in cfg.get("solana_tokens", {}).items()}
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

    sym = (context.args[0].upper() if context.args else None) or next(iter(TOKENS), None)
    cfg = _load_config()
    tok = cfg.get("solana_tokens", {}).get(sym)
    if not tok:
        await update.message.reply_text(
            f"Unknown token '{sym}'. Tracked: {', '.join(TOKENS) or 'none'}"
        )
        return

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


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await _deny(update)
        return
    global _monitor_proc
    if _monitor_proc and _monitor_proc.poll() is None:
        await update.message.reply_text("⏳ A run is already in progress — please wait.")
        return
    monitor_path = os.path.join(_REPO_DIR, "monitor.py")
    _monitor_proc = subprocess.Popen(
        ["python", monitor_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await update.message.reply_text(
        "⏳ Manual monitor run started.\n"
        "Alerts will appear in the group within ~60s."
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
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
    app.add_handler(CommandHandler("classify",      cmd_classify))
    app.add_handler(CommandHandler("related",       cmd_related))
    log.info("🤖 Bot polling started — authorized chats: %s", sorted(_AUTHORIZED_CHATS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=())


if __name__ == "__main__":
    main()
