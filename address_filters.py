"""
LP pool and program address filtering for Solana holder analysis.

getTokenLargestAccounts returns SPL token account addresses, not owner wallet addresses.
Some of those accounts are owned by LP pools, DEX programs, or system programs — not real
whale wallets. This module resolves, classifies, and filters them out before any alert or
scoring logic runs.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

# ── Known LP / program addresses to exclude ────────────────────────────────────
# These are PROGRAM addresses that own token accounts — they are NOT real wallets.
# We check both token-account owners AND the raw account addresses themselves.
KNOWN_EXCLUDED_ADDRESSES: frozenset[str] = frozenset({
    # Raydium
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",   # Raydium AMM authority
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",   # Raydium CLMM
    "HWy1jotHpo6UqeQxx49dpYYdQB8wj9Qk9MdxwjLvDHB8",   # Raydium fee collector
    # Orca
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",    # Orca Whirlpool
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",   # Orca v1
    # Meteora
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EkAW7vAm",   # Meteora DLMM
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",    # Meteora LB pair
    # Pump.fun
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",    # Pump.fun
    # Jupiter
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",    # Jupiter v6
    # System / SPL
    "11111111111111111111111111111111",                  # System program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",    # SPL Token program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bz",    # Associated Token program
    "So11111111111111111111111111111111111111112",       # WSOL mint
    # Token-2022
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",    # Token-2022 program
    # Serum / OpenBook
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",    # Serum DEX v3
    "opnb2LAfJYbRMAHHvqjCwQxanZn7n13jnclTjghUCUL",    # OpenBook v2
})

# Single-char or obviously invalid addresses
_MIN_ADDR_LEN = 32


def is_lp_or_system(owner: str) -> bool:
    """Return True if the address is a known LP pool, DEX program, or system address."""
    return owner in KNOWN_EXCLUDED_ADDRESSES or len(owner) < _MIN_ADDR_LEN


# ── Executable-account cache (24 h) ───────────────────────────────────────────
_exec_cache: dict[str, tuple[bool, float]] = {}
_EXEC_CACHE_TTL = 86_400  # 24 hours


def check_executable_batch(addresses: list[str], rpc_url: str) -> dict[str, bool]:
    """
    Return {address: is_executable} for each address.
    Executable = program, not a real user wallet.
    Results cached for 24 h to avoid repeated RPC calls.
    """
    now = time.time()
    result: dict[str, bool] = {}
    to_fetch: list[str] = []

    for addr in addresses:
        if addr in _exec_cache and now - _exec_cache[addr][1] < _EXEC_CACHE_TTL:
            result[addr] = _exec_cache[addr][0]
        else:
            to_fetch.append(addr)

    if not to_fetch:
        return result

    batch = [
        {"jsonrpc": "2.0", "id": i, "method": "getAccountInfo",
         "params": [addr, {"encoding": "base64"}]}
        for i, addr in enumerate(to_fetch)
    ]
    try:
        resp = requests.post(rpc_url, json=batch, timeout=20)
        resp.raise_for_status()
        for item in resp.json():
            idx = item.get("id")
            if idx is None or idx >= len(to_fetch):
                continue
            addr = to_fetch[idx]
            value = (item.get("result") or {}).get("value") or {}
            is_exec = bool(value.get("executable", False))
            _exec_cache[addr] = (is_exec, now)
            result[addr] = is_exec
    except Exception as exc:
        log.warning("check_executable_batch failed: %s", exc)
        for addr in to_fetch:
            result[addr] = False  # assume not executable on failure

    return result


# ── Core filter function ───────────────────────────────────────────────────────

def classify_and_filter(
    raw_holders: list[dict[str, Any]],
    rpc_url: str,
    resolve_owners_fn: Callable[[list[str]], dict[str, str]],
    supabase_client: Any = None,
) -> dict[str, Any]:
    """
    Take raw getTokenLargestAccounts output, resolve token accounts → owner wallets,
    filter LP/program addresses, and return clean ranked wallet holders.

    Args:
        raw_holders:       List of dicts from getTokenLargestAccounts (have 'address' = token account)
        rpc_url:           RPC endpoint for executable checks
        resolve_owners_fn: Callable that maps token account addrs → owner wallet addrs
        supabase_client:   Optional Supabase client for persisting classifications

    Returns dict with:
        real_holders:     List of dicts with 'address' = owner wallet, 'uiAmountString', etc.,
                          reranked 1..N, LP/program addresses removed
        excluded:         List of (token_account, owner, reason) tuples
        lp_pct:           Estimated % of supply held by excluded addresses
        real_holder_pct:  Estimated % of supply held by real wallets
    """
    if not raw_holders:
        return {"real_holders": [], "excluded": [], "lp_pct": 0.0, "real_holder_pct": 0.0}

    # Step 1: resolve token accounts → owner wallets
    token_account_addrs = [h["address"] for h in raw_holders]
    owner_map = resolve_owners_fn(token_account_addrs)

    log.info("  LP filter: resolved %d/%d token accounts to owners",
             len(owner_map), len(raw_holders))

    # Step 2: identify addresses needing executable check (>5% holders not in known list)
    total_supply_est = sum(
        float(h.get("uiAmountString") or h.get("amount") or 0)
        for h in raw_holders
    ) or 1.0

    check_candidates: list[str] = []
    for h in raw_holders:
        ta = h["address"]
        owner = owner_map.get(ta, ta)  # fall back to token account itself
        if not is_lp_or_system(owner):
            check_candidates.append(owner)

    exec_flags: dict[str, bool] = {}
    if check_candidates:
        exec_flags = check_executable_batch(list(set(check_candidates)), rpc_url)

    # Step 3: classify each holder
    real_holders: list[dict[str, Any]] = []
    excluded: list[tuple[str, str, str]] = []
    lp_amount = 0.0
    real_amount = 0.0

    for h in raw_holders:
        ta    = h["address"]
        owner = owner_map.get(ta, ta)
        amt   = float(h.get("uiAmountString") or h.get("amount") or 0)

        if is_lp_or_system(owner):
            reason = "KNOWN_PROGRAM" if owner in KNOWN_EXCLUDED_ADDRESSES else "SYSTEM"
            excluded.append((ta, owner, reason))
            lp_amount += amt
            log.debug("  LP filter: excluded %s (owner=%s, reason=%s)", ta[:8], owner[:8], reason)
            continue

        if exec_flags.get(owner, False):
            excluded.append((ta, owner, "EXECUTABLE"))
            lp_amount += amt
            log.debug("  LP filter: excluded executable program %s", owner[:8])
            continue

        # Unresolved token account (no owner found) — keep but flag
        if ta not in owner_map:
            log.debug("  LP filter: owner not resolved for %s — keeping", ta[:8])

        # Real wallet — keep with owner address
        holder_entry = dict(h)
        holder_entry["token_account"] = ta
        holder_entry["address"] = owner   # replace token account with owner wallet
        real_holders.append(holder_entry)
        real_amount += amt

    lp_pct   = lp_amount   / total_supply_est * 100
    real_pct  = real_amount / total_supply_est * 100

    log.info(
        "  LP filter: %d real holders, %d excluded | LP est %.2f%% supply",
        len(real_holders), len(excluded), lp_pct,
    )

    if supabase_client and (real_holders or excluded):
        _persist_classifications(real_holders, excluded, supabase_client)

    return {
        "real_holders":    real_holders,
        "excluded":        excluded,
        "lp_pct":          lp_pct,
        "real_holder_pct": real_pct,
    }


# ── Single-address classification (for /classify command) ─────────────────────

def classify_address(address: str, rpc_url: str, supabase_client: Any = None) -> dict[str, Any]:
    """
    Classify a single address for the /classify Telegram command.

    Returns dict with:
        address, is_known_program, is_executable, label, detail
    """
    result: dict[str, Any] = {
        "address":          address,
        "is_known_program": False,
        "is_executable":    False,
        "label":            "WALLET",
        "detail":           "",
    }

    if is_lp_or_system(address):
        result["is_known_program"] = True
        result["label"]   = "KNOWN_PROGRAM"
        result["detail"]  = "Listed in KNOWN_EXCLUDED_ADDRESSES — LP pool or system program"
        return result

    exec_flags = check_executable_batch([address], rpc_url)
    if exec_flags.get(address, False):
        result["is_executable"] = True
        result["label"]  = "PROGRAM"
        result["detail"] = "On-chain account has executable=true — program, not a wallet"
        return result

    # Check Supabase cache
    if supabase_client:
        try:
            row = supabase_client.table("address_classifications") \
                .select("label,is_program,is_lp") \
                .eq("address", address) \
                .maybe_single() \
                .execute()
            if row.data:
                result["label"]            = row.data.get("label", "WALLET")
                result["is_known_program"] = bool(row.data.get("is_program"))
                result["detail"]           = f"Cached classification: {result['label']}"
                return result
        except Exception as exc:
            log.debug("classify_address Supabase lookup failed: %s", exc)

    result["detail"] = "No program flags — appears to be a real user wallet"
    return result


# ── Supabase persistence ───────────────────────────────────────────────────────

def _persist_classifications(
    real_holders: list[dict[str, Any]],
    excluded: list[tuple[str, str, str]],
    supabase_client: Any,
) -> None:
    """Upsert classification results to address_classifications table."""
    if supabase_client is None:
        return

    rows: list[dict[str, Any]] = []

    for h in real_holders:
        rows.append({
            "address":   h["address"],
            "is_program": False,
            "is_lp":      False,
            "label":      "WALLET",
        })

    for (ta, owner, reason) in excluded:
        is_lp = reason in ("KNOWN_PROGRAM", "EXECUTABLE")
        rows.append({
            "address":   owner,
            "is_program": True,
            "is_lp":      is_lp,
            "label":      "LP_POOL" if is_lp else "PROGRAM",
        })

    if not rows:
        return

    try:
        supabase_client.table("address_classifications").upsert(
            rows, on_conflict="address"
        ).execute()
        log.debug("  LP filter: persisted %d classifications to Supabase", len(rows))
    except Exception as exc:
        log.warning("  LP filter: Supabase persist failed: %s", exc)
