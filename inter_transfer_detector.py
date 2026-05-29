"""
Inter-Transfer Detector
=======================
Confirms wallet cluster coordination by scanning for native SOL transfers
between cluster members on-chain.

Usage:
  python inter_transfer_detector.py --cluster ALON_Zs78YrHs [--test]
  python inter_transfer_detector.py --backfill [--symbol ALON]
  python inter_transfer_detector.py --test --cluster ALON_Zs78YrHs
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Env vars (read directly so module works standalone) ───────────────────────
HELIUS_API_KEY      = os.environ.get("HELIUS_API_KEY", "")
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY        = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_RPC          = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

MIN_SOL_LAMPORTS = 100_000   # 0.0001 SOL — filters out dust / fee artefacts


# ── Supabase ──────────────────────────────────────────────────────────────────

def _init_supabase() -> Any:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("Supabase not configured — DB writes disabled")
        return None
    try:
        from supabase import create_client
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        client.table("wallet_clusters").select("cluster_id").limit(1).execute()
        return client
    except Exception as exc:
        log.error("Supabase init failed: %s", exc)
        return None


_supabase = _init_supabase()


# ── Telegram fallback (used when running as CLI, not inside bot process) ──────

def _send_alert(msg: str) -> None:
    try:
        from monitor import send_alert
        send_alert(msg)
        return
    except Exception:
        pass
    target = TELEGRAM_CHANNEL_ID or TELEGRAM_CHAT_ID
    if not target or not TELEGRAM_BOT_TOKEN:
        log.info("Telegram not configured — printing alert:\n%s", msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": target, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    MAX_REQUESTS = 5
    WINDOW_SEC   = 60
    MIN_SLEEP    = 12
    RETRY_SLEEP  = 60
    MAX_RETRIES  = 3

    def __init__(self) -> None:
        self._window: deque[float] = deque()

    def _purge_old(self) -> None:
        now = time.monotonic()
        while self._window and now - self._window[0] >= self.WINDOW_SEC:
            self._window.popleft()

    def acquire(self) -> None:
        self._purge_old()
        if len(self._window) >= self.MAX_REQUESTS:
            oldest = self._window[0]
            wait = self.WINDOW_SEC - (time.monotonic() - oldest) + 0.1
            if wait > 0:
                log.debug("Rate window full — sleeping %.1fs", wait)
                time.sleep(wait)
            self._purge_old()
        time.sleep(self.MIN_SLEEP)
        self._window.append(time.monotonic())

    def rpc_call(
        self,
        payload: dict,
        label: str,
        req_n: int,
    ) -> dict | None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        for attempt in range(self.MAX_RETRIES):
            self.acquire()
            try:
                resp = requests.post(HELIUS_RPC, json=payload, timeout=20)
                if resp.status_code == 429:
                    status = "RETRY" if attempt < self.MAX_RETRIES - 1 else "SKIP"
                    log.info(
                        "[%s] Request %d | %s | status %s (429 — sleeping %ds)",
                        ts, req_n, label, status, self.RETRY_SLEEP,
                    )
                    time.sleep(self.RETRY_SLEEP)
                    continue
                resp.raise_for_status()
                result = resp.json()
                log.info("[%s] Request %d | %s | status OK", ts, req_n, label)
                return result
            except requests.RequestException as exc:
                status = "RETRY" if attempt < self.MAX_RETRIES - 1 else "SKIP"
                log.info("[%s] Request %d | %s | status %s (%s)", ts, req_n, label, status, exc)
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_SLEEP)
        return None


# ── RPC helpers ───────────────────────────────────────────────────────────────

def _get_signatures(
    wallet: str,
    days: int,
    limiter: RateLimiter,
    req_counter: list[int],
    verbose: bool = False,
) -> list[str]:
    cutoff = time.time() - days * 86400
    sigs: list[str] = []
    before: str | None = None
    label = f"{wallet[:8]}…{wallet[-4:]}"

    while True:
        params: dict[str, Any] = {"limit": 1000}
        if before:
            params["before"] = before

        req_counter[0] += 1
        result = limiter.rpc_call(
            {
                "jsonrpc": "2.0",
                "id": req_counter[0],
                "method": "getSignaturesForAddress",
                "params": [wallet, params],
            },
            label,
            req_counter[0],
        )
        if not result:
            break

        page = result.get("result") or []
        if not page:
            break

        for entry in page:
            block_time = entry.get("blockTime") or 0
            if block_time and block_time < cutoff:
                if verbose:
                    log.info("  Reached cutoff (%d days) for %s", days, label)
                return sigs
            sig = entry.get("signature")
            if sig:
                sigs.append(sig)
        before = page[-1].get("signature")
        if len(page) < 1000:
            break

    if verbose:
        log.info("  %s: %d signatures collected", label, len(sigs))
    return sigs


def _get_transaction(
    sig: str,
    limiter: RateLimiter,
    req_counter: list[int],
    verbose: bool = False,
) -> dict | None:
    req_counter[0] += 1
    label = f"tx {sig[:12]}…"
    result = limiter.rpc_call(
        {
            "jsonrpc": "2.0",
            "id": req_counter[0],
            "method": "getTransaction",
            "params": [
                sig,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        },
        label,
        req_counter[0],
    )
    if not result:
        return None
    tx = result.get("result")
    if verbose and tx:
        log.info("  tx %s… fetched OK", sig[:12])
    return tx


# ── SOL transfer detection (pure, no I/O) ────────────────────────────────────

def _detect_sol_transfers(tx: dict, wallet_set: set[str]) -> list[dict]:
    """
    Checks meta.preBalances / postBalances against accountKeys.
    Returns list of {from_wallet, to_wallet, lamports, sol, sig}.
    """
    try:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return []
        pre  = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        msg  = (tx.get("transaction") or {}).get("message") or {}

        raw_keys = msg.get("accountKeys") or []
        keys: list[str] = []
        for k in raw_keys:
            if isinstance(k, dict):
                keys.append(k.get("pubkey", ""))
            else:
                keys.append(str(k))

        sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
        transfers: list[dict] = []

        n = min(len(keys), len(pre), len(post))
        for i in range(n):
            if keys[i] not in wallet_set:
                continue
            delta_i = post[i] - pre[i]
            if delta_i >= 0:
                continue
            lost = -delta_i
            for j in range(n):
                if i == j or keys[j] not in wallet_set:
                    continue
                gained = post[j] - pre[j]
                if gained >= MIN_SOL_LAMPORTS and gained <= lost:
                    transfers.append({
                        "from_wallet": keys[i],
                        "to_wallet":   keys[j],
                        "lamports":    gained,
                        "sol":         gained / 1e9,
                        "sig":         sig,
                    })
        return transfers
    except Exception as exc:
        log.debug("_detect_sol_transfers error: %s", exc)
        return []


# ── Telegram message formatters ───────────────────────────────────────────────

def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def _fmt_confirmed(
    symbol: str,
    cluster_id: str,
    wallet_count: int,
    supply_pct: float,
    transfers: list[dict],
) -> str:
    shown  = transfers[:5]
    extras = len(transfers) - 5

    transfer_lines = "\n".join(
        f"• {_short(t['from_wallet'])} → {_short(t['to_wallet'])} "
        f"| {t['sol']:.4f} SOL "
        f"| 🔍 https://solscan.io/tx/{t['sig']}"
        for t in shown
    )
    if extras > 0:
        transfer_lines += f"\n[+ {extras} more transfers]"

    ts = datetime.now(timezone.utc).strftime("%H:%M")
    return (
        f"🔗 <b>INTER-TRANSFER CONFIRMED — {symbol}</b>\n"
        f"💀 Bundle proof: on-chain fund flow detected\n\n"
        f"📊 Cluster: <code>{cluster_id}</code>\n"
        f"👥 Wallets: {wallet_count} | Supply: {supply_pct:.2f}%\n"
        f"⚠️ Risk: HIGH — confirmed coordinated bundle\n\n"
        f"💸 <b>Transfers found:</b>\n{transfer_lines}\n\n"
        f"📋 Confidence upgraded: 75 → 95\n"
        f"⏰ Detected: {ts} UTC"
    )


def _fmt_no_transfers(
    symbol: str,
    cluster_id: str,
    wallet_count: int,
    supply_pct: float,
) -> str:
    return (
        f"⏳ <b>INTER-TRANSFER SCAN COMPLETE — {symbol}</b>\n"
        f"No on-chain fund flows found between cluster wallets\n"
        f"Cluster remains TEMPORAL_CLUSTER (timing-based only)\n"
        f"📊 <code>{cluster_id}</code> | 👥 {wallet_count} wallets | {supply_pct:.2f}% supply"
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def _fetch_cluster_wallets(cluster_id: str, supabase: Any) -> dict:
    """Returns {wallets, token_address, token_symbol, total_supply_pct}."""
    if not supabase:
        return {}
    try:
        r = (
            supabase.table("wallet_clusters")
            .select("wallet_address,token_address,token_symbol,total_supply_pct")
            .eq("cluster_id", cluster_id)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return {}
        return {
            "wallets":          [row["wallet_address"] for row in rows],
            "token_address":    rows[0].get("token_address", ""),
            "token_symbol":     rows[0].get("token_symbol", ""),
            "total_supply_pct": rows[0].get("total_supply_pct") or 0.0,
        }
    except Exception as exc:
        log.error("_fetch_cluster_wallets failed: %s", exc)
        return {}


def _upsert_relationship(
    wallet_a: str,
    wallet_b: str,
    token_address: str,
    sigs: list[str],
    supabase: Any,
) -> bool:
    if not supabase:
        return False
    try:
        supabase.table("wallet_relationships").upsert(
            {
                "wallet_a":          min(wallet_a, wallet_b),
                "wallet_b":          max(wallet_a, wallet_b),
                "relationship_type": "INTER_TRANSFER",
                "token_address":     token_address,
                "confidence_score":  95,
                "evidence":          ",".join(sigs[:20]),
            },
            on_conflict="wallet_a,wallet_b,relationship_type,token_address",
        ).execute()
        return True
    except Exception as exc:
        log.error("_upsert_relationship failed: %s", exc)
        return False


# ── Core scan ─────────────────────────────────────────────────────────────────

def scan_cluster(
    cluster_id: str,
    symbol: str = "ALON",
    test_mode: bool = False,
    dry_run: bool = False,
    progress_cb: Any = None,
    supabase: Any = None,
) -> dict:
    """
    Scan a cluster for native SOL transfers between member wallets.

    Args:
        cluster_id:  e.g. "ALON_Zs78YrHs"
        symbol:      token symbol for display / Telegram message
        test_mode:   only 2 wallets, 30 days, verbose logging
        dry_run:     skip DB writes and Telegram (implied by test_mode)
        progress_cb: optional callable(msg: str) for Telegram progress updates
        supabase:    pass existing client, otherwise uses module-level _supabase
    """
    sb = supabase or _supabase
    info = _fetch_cluster_wallets(cluster_id, sb)
    if not info:
        msg = f"Cluster {cluster_id} not found in wallet_clusters table."
        log.warning(msg)
        return {"error": msg}

    all_wallets:   list[str] = info["wallets"]
    token_address: str       = info["token_address"]
    supply_pct:    float     = info["total_supply_pct"]
    verbose:       bool      = test_mode

    days          = 30 if test_mode else 180
    wallets       = all_wallets[:2] if test_mode else all_wallets
    wallet_set    = set(wallets)
    limiter       = RateLimiter()
    req_counter   = [0]

    log.info(
        "scan_cluster: %s | %d wallets | %d days | test=%s dry_run=%s",
        cluster_id, len(wallets), days, test_mode, dry_run,
    )

    # Phase 1: collect all unique signatures
    all_sigs: set[str] = set()
    for idx, wallet in enumerate(wallets, 1):
        if progress_cb and idx % 10 == 0:
            progress_cb(f"🔍 Scanning wallet {idx}/{len(wallets)}…")
        label = f"{wallet[:8]}…{wallet[-4:]}"
        log.info("  [%d/%d] collecting sigs for %s", idx, len(wallets), label)
        sigs = _get_signatures(wallet, days, limiter, req_counter, verbose=verbose)
        all_sigs.update(sigs)
        log.info("  %s: %d sigs → total unique so far: %d", label, len(sigs), len(all_sigs))

    log.info("Phase 1 complete: %d unique signatures across %d wallets", len(all_sigs), len(wallets))

    # Phase 2: fetch each unique tx and detect SOL transfers
    transfers: list[dict] = []
    sig_list = list(all_sigs)
    for idx, sig in enumerate(sig_list, 1):
        if verbose:
            log.info("  Fetching tx %d/%d: %s…", idx, len(sig_list), sig[:16])
        tx = _get_transaction(sig, limiter, req_counter, verbose=verbose)
        if not tx:
            continue
        found = _detect_sol_transfers(tx, wallet_set)
        if found:
            log.info("  ✅ SOL transfer found in tx %s…: %s", sig[:12], found)
            transfers.extend(found)
        elif verbose:
            log.info("  tx %s… — no cluster SOL transfer", sig[:12])

    log.info("Phase 2 complete: %d SOL transfers found", len(transfers))

    if test_mode or dry_run:
        print("\n" + "=" * 60)
        print(f"TEST SCAN RESULTS — {cluster_id}")
        print("=" * 60)
        print(f"Wallets scanned: {len(wallets)}")
        print(f"Signatures checked: {len(sig_list)}")
        print(f"SOL transfers found: {len(transfers)}")
        for t in transfers:
            print(
                f"  {_short(t['from_wallet'])} → {_short(t['to_wallet'])} "
                f"| {t['sol']:.4f} SOL | {t['sig'][:16]}…"
            )
        print("=" * 60)
        return {
            "cluster_id":   cluster_id,
            "wallet_count": len(wallets),
            "sig_count":    len(sig_list),
            "transfers":    transfers,
            "dry_run":      True,
        }

    # Phase 3: persist confirmed pairs
    pair_sigs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for t in transfers:
        key = (min(t["from_wallet"], t["to_wallet"]), max(t["from_wallet"], t["to_wallet"]))
        pair_sigs[key].append(t["sig"])

    saved = 0
    for (wa, wb), sigs in pair_sigs.items():
        if _upsert_relationship(wa, wb, token_address, sigs, sb):
            saved += 1
    log.info("Saved %d INTER_TRANSFER relationships to Supabase", saved)

    # Phase 4: fire Telegram summary
    if transfers:
        unique_transfers = list({t["sig"]: t for t in transfers}.values())
        msg = _fmt_confirmed(symbol, cluster_id, len(wallets), supply_pct, unique_transfers)
    else:
        msg = _fmt_no_transfers(symbol, cluster_id, len(wallets), supply_pct)

    _send_alert(msg)

    return {
        "cluster_id":   cluster_id,
        "wallet_count": len(wallets),
        "sig_count":    len(sig_list),
        "transfers":    transfers,
        "pairs_saved":  saved,
    }


# ── Backfill ──────────────────────────────────────────────────────────────────

def backfill(symbol_filter: str | None = None, supabase: Any = None) -> None:
    sb = supabase or _supabase
    if not sb:
        log.error("Supabase not available — cannot backfill")
        return
    try:
        q = sb.table("wallet_clusters").select("cluster_id,token_symbol,token_address")
        if symbol_filter:
            q = q.eq("token_symbol", symbol_filter.upper())
        r = q.execute()
    except Exception as exc:
        log.error("backfill: failed to list clusters: %s", exc)
        return

    seen: dict[str, str] = {}
    for row in r.data or []:
        cid = row["cluster_id"]
        if cid not in seen:
            seen[cid] = row.get("token_symbol", "")

    cluster_ids = list(seen.items())
    log.info("Backfill: %d clusters to scan", len(cluster_ids))

    for idx, (cid, sym) in enumerate(cluster_ids, 1):
        log.info("Scanning cluster %d of %d: %s", idx, len(cluster_ids), cid)
        try:
            scan_cluster(cid, sym or "", supabase=sb)
        except Exception as exc:
            log.error("backfill: scan_cluster(%s) failed: %s", cid, exc)


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scan wallet clusters for on-chain SOL inter-transfers"
    )
    parser.add_argument("--cluster",  help="cluster_id to scan (e.g. ALON_Zs78YrHs)")
    parser.add_argument("--symbol",   default="ALON", help="token symbol (default: ALON)")
    parser.add_argument("--backfill", action="store_true",
                        help="scan all clusters in wallet_clusters table")
    parser.add_argument("--test",     action="store_true",
                        help="test mode: 2 wallets, 30 days, no DB write, no Telegram")
    args = parser.parse_args()

    if args.backfill:
        backfill(symbol_filter=args.symbol if args.symbol != "ALON" else None)
    elif args.cluster:
        scan_cluster(
            args.cluster,
            symbol=args.symbol,
            test_mode=args.test,
            dry_run=args.test,
        )
    else:
        parser.print_help()
