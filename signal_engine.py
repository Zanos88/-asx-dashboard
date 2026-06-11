"""
Signal Engine — swap backfill + wallet tier computation.

Responsibilities:
  - Page through Helius Enhanced TX swaps for a wallet (up to N days)
  - Aggregate per-token positions into win rate, avg hold time, PnL
  - Assign TIER_A / TIER_B / TIER_C based on computed stats

No Telegram. No DB writes except smart_wallets updates (passed in from caller).
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

HELIUS_API_KEY   = os.environ.get("HELIUS_API_KEY", "")
HELIUS_TX_RATE_SEC = 0.13   # ~8 req/sec headroom


# ── Tier logic ────────────────────────────────────────────────────────────────

def compute_tier(win_rate: float, avg_hold_min: float, trades_90d: int) -> str:
    """
    Classify a wallet's trading behaviour into a tier.

    TIER_C  sniper / MEV — hold < 15 min (fastest reject)
    TIER_A  copy-live    — hold >= 4h, 20-200 trades/90d, win rate >= 60%
    TIER_B  paper-only   — hold >= 1h, 200-1200 trades/90d, win rate >= 65%
    TIER_C  everything else (too few trades, low win rate, or edge cases)
    """
    if avg_hold_min < 15:
        return "TIER_C"
    if avg_hold_min >= 240 and 20 <= trades_90d <= 200 and win_rate >= 0.60:
        return "TIER_A"
    if avg_hold_min >= 60 and 200 < trades_90d <= 1200 and win_rate >= 0.65:
        return "TIER_B"
    return "TIER_C"


# ── Helius paginator ──────────────────────────────────────────────────────────

def _fetch_swap_page(
    wallet: str,
    api_key: str,
    before: str | None = None,
) -> list[dict]:
    params: dict[str, Any] = {"api-key": api_key, "limit": 100, "type": "SWAP"}
    if before:
        params["before"] = before
    try:
        resp = requests.get(
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
            params=params,
            timeout=20,
        )
        if resp.status_code == 429:
            log.warning("Helius 429 — sleeping 2s")
            time.sleep(2)
            resp = requests.get(
                f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
                params=params,
                timeout=20,
            )
        if not resp.ok:
            log.debug("Helius swap page failed %s: %s", wallet[:8], resp.status_code)
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.debug("_fetch_swap_page error %s: %s", wallet[:8], exc)
        return []


# ── Backfill ──────────────────────────────────────────────────────────────────

def run_swap_backfill(
    wallet: str,
    helius_api_key: str,
    days: int = 30,
    progress_cb: Callable[[str], None] | None = None,
) -> dict:
    """
    Page through Helius SWAP transactions for wallet over the last `days` days.

    Returns:
        {
            "win_rate":         float,
            "trade_count":      int,
            "trades_90d":       int,
            "avg_hold_time_min": float,
            "total_pnl_sol":    float,
        }
    """
    cutoff_ts = (
        datetime.now(timezone.utc).timestamp() - days * 86400
        if days else 0
    )
    cutoff_90d = datetime.now(timezone.utc).timestamp() - 90 * 86400

    # positions[mint] = {"buys": [(ts, sol, tokens)], "sells": [(ts, sol, tokens)]}
    positions: dict[str, dict[str, list]] = defaultdict(lambda: {"buys": [], "sells": []})
    swaps_90d = 0
    before: str | None = None
    page = 0

    while True:
        time.sleep(HELIUS_TX_RATE_SEC)
        page += 1
        txns = _fetch_swap_page(wallet, helius_api_key, before=before)
        if not txns:
            break

        stop = False
        for tx in txns:
            ts = tx.get("timestamp") or 0
            if cutoff_ts and ts < cutoff_ts:
                stop = True
                break

            swap = (tx.get("events") or {}).get("swap") or {}
            if not swap:
                continue

            sig = tx.get("signature", "")

            # Count for 90d window regardless of position direction
            if ts >= cutoff_90d:
                swaps_90d += 1

            # BUY: wallet receives token outputs
            for out in swap.get("tokenOutputs", []):
                if out.get("userAccount") != wallet:
                    continue
                mint = out.get("mint")
                if not mint:
                    continue
                token_amount = float(out.get("tokenAmount") or 0)
                sol_spent = (swap.get("nativeInput") or {}).get("amount", 0) / 1e9
                if sol_spent > 0 or token_amount > 0:
                    positions[mint]["buys"].append((ts, sol_spent, token_amount))

            # SELL: wallet sends token inputs
            for inp in swap.get("tokenInputs", []):
                if inp.get("userAccount") != wallet:
                    continue
                mint = inp.get("mint")
                if not mint:
                    continue
                token_amount = float(inp.get("tokenAmount") or 0)
                sol_received = (swap.get("nativeOutput") or {}).get("amount", 0) / 1e9
                if sol_received > 0 or token_amount > 0:
                    positions[mint]["sells"].append((ts, sol_received, token_amount))

        if progress_cb and page % 5 == 0:
            progress_cb(f"📊 Backfill page {page} — {len(positions)} tokens seen so far…")

        if stop or len(txns) < 100:
            break

        before = txns[-1].get("signature")
        if not before:
            break

    log.info("Backfill complete: %d pages, %d tokens for %s", page, len(positions), wallet[:8])

    # Aggregate stats over closed positions (token has both buys and sells)
    wins = 0
    trade_count = 0
    hold_times: list[float] = []
    total_pnl = 0.0

    for mint, pos in positions.items():
        if not pos["buys"] or not pos["sells"]:
            continue

        total_buy_sol  = sum(b[1] for b in pos["buys"])
        total_sell_sol = sum(s[1] for s in pos["sells"])

        # Hold time: latest buy → earliest sell
        last_buy_ts   = max(b[0] for b in pos["buys"])
        first_sell_ts = min(s[0] for s in pos["sells"])
        hold_min = (first_sell_ts - last_buy_ts) / 60.0
        if hold_min < 0:
            hold_min = 0.0

        trade_count += 1
        hold_times.append(hold_min)
        total_pnl += total_sell_sol - total_buy_sol
        if total_sell_sol > total_buy_sol:
            wins += 1

    win_rate        = wins / trade_count if trade_count > 0 else 0.0
    avg_hold_min    = sum(hold_times) / len(hold_times) if hold_times else 0.0

    return {
        "win_rate":          round(win_rate, 4),
        "trade_count":       trade_count,
        "trades_90d":        swaps_90d,
        "avg_hold_time_min": round(avg_hold_min, 2),
        "total_pnl_sol":     round(total_pnl, 4),
    }
