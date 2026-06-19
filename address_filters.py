"""
LP pool and program address filtering for Solana holder analysis.

getTokenLargestAccounts returns SPL token account addresses, not owner wallet addresses.
Some of those accounts are owned by LP pools, DEX programs, or system programs — not real
whale wallets. This module resolves, classifies, and filters them out before any alert or
scoring logic runs.

Detection order (fastest/cheapest first):
  1. KNOWN_LP_ADDRESSES     — direct token account match, zero API calls
  2. Supabase DB cache       — previously classified addresses, zero API calls
  3. resolve_owners_fn       — SPL token account owner → KNOWN_EXCLUDED_ADDRESSES
  4. getAccountInfo.owner    — token account's program owner in KNOWN_LP_OWNER_PROGRAMS
                               catches Raydium Authority V4 and all AMM pool accounts
                               whose SPL owner resolution returns nothing
  5. executable flag          — resolved owner is an on-chain program (final fallback)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

# ── Programs that OWN LP pool token accounts ───────────────────────────────────
# When getAccountInfo(token_account).value.owner == one of these → it's an LP pool,
# not a real token account held by a whale wallet.
# Standard SPL token accounts have owner = TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA;
# AMM pool accounts have owner = the AMM program below.
KNOWN_LP_OWNER_PROGRAMS: frozenset[str] = frozenset({
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EkAW7vAm",  # Meteora DLMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # Pump.fun
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora Dynamic AMM
})

# ── Direct known LP pool token account addresses ───────────────────────────────
# These appear in getTokenLargestAccounts directly as the "holder" address.
# Excluded before any API call — no owner resolution needed.
KNOWN_LP_ADDRESSES: frozenset[str] = frozenset({
    "Gi1VCbPL6Sdcytjp6f1uG1PvCHq25FuNgnVqySHBnKNk",  # Raydium LP pool (ALON)
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM authority
    "DhVpojXMTbZMuTaCgiiaFU7U8GvEEhnYo4G9BUdiEYGh",  # known LP
    "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfFnyJyUghFMJ",  # known LP
})

# ── Known owner/program addresses (appear as resolved SPL token account owner) ─
# When resolve_owners_fn(token_account) returns one of these → not a real wallet.
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

_MIN_ADDR_LEN = 32
_RPC_BATCH_SIZE = 5  # Helius rejects large batch getAccountInfo; 5 avoids 413


def is_lp_or_system(owner: str) -> bool:
    """Return True if the address is a known LP pool, DEX program, or system address."""
    return owner in KNOWN_EXCLUDED_ADDRESSES or len(owner) < _MIN_ADDR_LEN


def is_lp_or_system_address(address: str) -> bool:
    """
    Unified LP/program check across all three exclusion sets.
    Checks KNOWN_LP_ADDRESSES, KNOWN_LP_OWNER_PROGRAMS, and KNOWN_EXCLUDED_ADDRESSES.
    Use this for single-address validation and tests.
    """
    return (
        address in KNOWN_LP_ADDRESSES
        or address in KNOWN_LP_OWNER_PROGRAMS
        or is_lp_or_system(address)
    )


# ── Account info cache (24 h) ─────────────────────────────────────────────────
# Stores {executable: bool, owner: str} per address — avoids repeated RPC calls.
_account_cache: dict[str, tuple[dict[str, Any], float]] = {}
_ACCOUNT_CACHE_TTL = 86_400  # 24 hours


def _fetch_account_info_batch(addresses: list[str], rpc_url: str) -> dict[str, dict[str, Any]]:
    """
    Batch getAccountInfo (base64 encoding) for a list of addresses.
    Returns {addr: {"executable": bool, "owner": str}}.
    Results cached 24 h.
    """
    now = time.time()
    result: dict[str, dict[str, Any]] = {}
    to_fetch: list[str] = []

    for addr in addresses:
        if addr in _account_cache and now - _account_cache[addr][1] < _ACCOUNT_CACHE_TTL:
            result[addr] = _account_cache[addr][0]
        else:
            to_fetch.append(addr)

    if not to_fetch:
        return result

    log.info("🔬DBG _fetch_account_info_batch: %d addrs (%d cached, %d to fetch), chunk size %d",
             len(addresses), len(addresses) - len(to_fetch), len(to_fetch), _RPC_BATCH_SIZE)
    for chunk_start in range(0, len(to_fetch), _RPC_BATCH_SIZE):
        chunk = to_fetch[chunk_start:chunk_start + _RPC_BATCH_SIZE]
        batch = [
            {"jsonrpc": "2.0", "id": i, "method": "getAccountInfo",
             "params": [addr, {"encoding": "base64"}]}
            for i, addr in enumerate(chunk)
        ]
        chunk_idx = chunk_start // _RPC_BATCH_SIZE
        try:
            resp = requests.post(rpc_url, json=batch, timeout=20)
            log.info("🔬DBG getAccountInfo chunk[%d] size=%d HTTP %s body[:300]=%r",
                     chunk_idx, len(chunk), resp.status_code, resp.text[:300])
            resp.raise_for_status()
            for item in resp.json():
                idx = item.get("id")
                if idx is None or idx >= len(chunk):
                    continue
                addr = chunk[idx]
                value = (item.get("result") or {}).get("value") or {}
                info: dict[str, Any] = {
                    "executable": bool(value.get("executable", False)),
                    "owner":      value.get("owner", ""),
                }
                _account_cache[addr] = (info, now)
                result[addr] = info
        except Exception as exc:
            log.warning("🔬DBG _fetch_account_info_batch chunk[%d] failed (size=%d): %s: %s",
                        chunk_idx, len(chunk), type(exc).__name__, exc)
            for addr in chunk:
                result[addr] = {"executable": False, "owner": ""}

    return result


def check_executable_batch(addresses: list[str], rpc_url: str) -> dict[str, bool]:
    """Return {address: is_executable}. Kept for backward compatibility."""
    info = _fetch_account_info_batch(addresses, rpc_url)
    return {addr: data["executable"] for addr, data in info.items()}


# ── Supabase LP cache lookup ───────────────────────────────────────────────────

def _check_supabase_lp_cache(
    addresses: list[str], supabase_client: Any
) -> dict[str, bool]:
    """
    Batch-query address_classifications for is_lp status.
    Returns {address: True} for every address the DB knows is an LP pool.
    """
    if not supabase_client or not addresses:
        return {}
    try:
        r = supabase_client.table("address_classifications") \
            .select("address,is_lp") \
            .in_("address", addresses) \
            .execute()
        return {
            row["address"]: True
            for row in (r.data or [])
            if row.get("is_lp")
        }
    except Exception as exc:
        log.debug("_check_supabase_lp_cache failed: %s", exc)
        return {}


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
        rpc_url:           Helius/RPC endpoint
        resolve_owners_fn: Maps token account addrs → SPL token account owner (beneficiary wallet)
        supabase_client:   Optional Supabase client for DB cache lookups and persistence

    Returns dict with:
        real_holders:     List of dicts with 'address' = owner wallet, reranked 1..N
        excluded:         List of (token_account, owner, reason) tuples
        lp_pct:           Estimated % of supply held by excluded addresses
        real_holder_pct:  Estimated % of supply held by real wallets
    """
    if not raw_holders:
        return {"real_holders": [], "excluded": [], "lp_pct": 0.0, "real_holder_pct": 0.0}

    token_account_addrs = [h["address"] for h in raw_holders]

    total_supply_est = sum(
        float(h.get("uiAmountString") or h.get("amount") or 0)
        for h in raw_holders
    ) or 1.0

    # ── Step 1: Supabase cache (before any Helius calls) ──────────────────────
    db_lp_cache = _check_supabase_lp_cache(token_account_addrs, supabase_client)
    if db_lp_cache:
        log.info("  LP filter: %d addresses found in Supabase LP cache", len(db_lp_cache))

    # ── Step 2: resolve token accounts → SPL owner wallets ────────────────────
    owner_map = resolve_owners_fn(token_account_addrs)
    log.info("  LP filter: resolved %d/%d token accounts to owners",
             len(owner_map), len(raw_holders))
    _unresolved = [ta for ta in token_account_addrs if ta not in owner_map]
    log.info("🔬DBG owner resolution: %d resolved, %d UNRESOLVED %s",
             len(owner_map), len(_unresolved), [a[:8] for a in _unresolved[:25]])

    # Also cache Supabase lookup for resolved owners (different addresses)
    resolved_owners = list({v for v in owner_map.values() if v})
    if resolved_owners:
        owner_db_cache = _check_supabase_lp_cache(resolved_owners, supabase_client)
        db_lp_cache.update(owner_db_cache)

    # ── Step 3: batch getAccountInfo on token accounts for LP owner check ─────
    # Covers addresses that aren't standard SPL token accounts — their jsonParsed
    # owner resolution returns nothing, so they'd fall back to the token account
    # address itself. getAccountInfo.value.owner reveals the controlling program.
    needs_owner_check: list[str] = []
    for h in raw_holders:
        ta    = h["address"]
        owner = owner_map.get(ta, ta)
        if (ta in KNOWN_LP_ADDRESSES
                or db_lp_cache.get(ta)
                or db_lp_cache.get(owner)
                or is_lp_or_system(owner)):
            continue
        needs_owner_check.append(ta)

    ta_account_info: dict[str, dict[str, Any]] = {}
    if needs_owner_check:
        ta_account_info = _fetch_account_info_batch(list(set(needs_owner_check)), rpc_url)

    # ── Step 4: executable check on resolved owners (final fallback) ──────────
    needs_exec_check: list[str] = []
    for h in raw_holders:
        ta    = h["address"]
        owner = owner_map.get(ta, ta)
        if (ta in KNOWN_LP_ADDRESSES
                or db_lp_cache.get(ta)
                or db_lp_cache.get(owner)
                or is_lp_or_system(owner)):
            continue
        ta_info = ta_account_info.get(ta, {})
        if ta_info.get("owner") in KNOWN_LP_OWNER_PROGRAMS:
            continue
        needs_exec_check.append(owner)

    exec_flags: dict[str, bool] = {}
    if needs_exec_check:
        exec_flags = check_executable_batch(list(set(needs_exec_check)), rpc_url)

    # ── Step 5: classify each holder ──────────────────────────────────────────
    real_holders: list[dict[str, Any]] = []
    excluded: list[tuple[str, str, str]] = []
    lp_amount  = 0.0
    real_amount = 0.0

    for h in raw_holders:
        ta    = h["address"]
        owner = owner_map.get(ta, ta)
        amt   = float(h.get("uiAmountString") or h.get("amount") or 0)

        # Check 1: direct token account address in known LP set
        if ta in KNOWN_LP_ADDRESSES:
            excluded.append((ta, owner, "KNOWN_LP_ADDRESS"))
            lp_amount += amt
            log.info("🚫 Excluded LP pool: %s (reason: KNOWN_LP_ADDRESS)", ta[:16])
            continue

        # Check 2: Supabase DB cache (token account or resolved owner)
        if db_lp_cache.get(ta) or db_lp_cache.get(owner):
            excluded.append((ta, owner, "DB_CACHE"))
            lp_amount += amt
            log.info("🚫 Excluded LP pool: %s (reason: DB_CACHE)", ta[:16])
            continue

        # Check 3: resolved SPL owner in known excluded programs
        if is_lp_or_system(owner):
            reason = "KNOWN_PROGRAM" if owner in KNOWN_EXCLUDED_ADDRESSES else "SYSTEM"
            excluded.append((ta, owner, reason))
            lp_amount += amt
            log.info("🚫 Excluded LP pool: %s (owner=%s, reason=%s)", ta[:16], owner[:16], reason)
            continue

        # Check 4: token account program owner in KNOWN_LP_OWNER_PROGRAMS
        # Catches Raydium Authority V4 and any AMM pool account where jsonParsed
        # owner resolution fails — the raw account owner reveals the AMM program.
        ta_info = ta_account_info.get(ta, {})
        prog_owner = ta_info.get("owner", "")
        if prog_owner in KNOWN_LP_OWNER_PROGRAMS:
            excluded.append((ta, prog_owner, "LP_POOL_OWNER"))
            lp_amount += amt
            log.info("🚫 Excluded LP pool: %s (prog owner=%s, reason: LP_POOL_OWNER)", ta[:16], prog_owner[:16])
            continue

        # Check 5: resolved owner is an executable on-chain program
        if exec_flags.get(owner, False):
            excluded.append((ta, owner, "EXECUTABLE"))
            lp_amount += amt
            log.info("🚫 Excluded program account: %s (reason: EXECUTABLE)", owner[:16])
            continue

        # Unresolved token account — owner resolution failed; treat as LP/program to be safe
        if ta not in owner_map:
            log.warning("  LP filter: owner not resolved for %s — excluding (unresolved)", ta[:8])
            excluded.append((ta, ta, "UNRESOLVED_OWNER"))
            lp_amount += amt
            continue

        # Real wallet — replace token account address with owner wallet address
        holder_entry = dict(h)
        holder_entry["token_account"] = ta
        holder_entry["address"] = owner
        real_holders.append(holder_entry)
        real_amount += amt

    lp_pct   = lp_amount   / total_supply_est * 100
    real_pct  = real_amount / total_supply_est * 100

    log.info(
        "  LP filter: %d real holders, %d excluded | LP est %.2f%% supply",
        len(real_holders), len(excluded), lp_pct,
    )
    _reason_counts: dict[str, int] = {}
    for (_ta, _ow, _reason) in excluded:
        _reason_counts[_reason] = _reason_counts.get(_reason, 0) + 1
    log.info("🔬DBG classify_and_filter: %d real, %d excluded by reason=%s",
             len(real_holders), len(excluded), _reason_counts)

    if supabase_client and (real_holders or excluded):
        _persist_classifications(real_holders, excluded, supabase_client)

    return {
        "real_holders":    real_holders,
        "excluded":        excluded,
        "lp_pct":          lp_pct,
        "real_holder_pct": real_pct,
    }


# ── Single-address classification (for /classify Telegram command) ─────────────

def classify_address(address: str, rpc_url: str, supabase_client: Any = None) -> dict[str, Any]:
    """
    Classify a single address for the /classify Telegram command.

    Checks in order: KNOWN_LP_ADDRESSES → Supabase cache → KNOWN_EXCLUDED_ADDRESSES
    → getAccountInfo program owner → executable flag.

    Returns dict with: address, is_known_program, is_executable, label, detail
    """
    result: dict[str, Any] = {
        "address":          address,
        "is_known_program": False,
        "is_executable":    False,
        "label":            "WALLET",
        "detail":           "",
    }

    # 1. Direct LP address list
    if address in KNOWN_LP_ADDRESSES:
        result["is_known_program"] = True
        result["label"]  = "LP_POOL"
        result["detail"] = "Listed in KNOWN_LP_ADDRESSES — direct LP pool token account"
        return result

    # 2. Known excluded programs/owners
    if is_lp_or_system(address):
        result["is_known_program"] = True
        result["label"]   = "KNOWN_PROGRAM"
        result["detail"]  = "Listed in KNOWN_EXCLUDED_ADDRESSES — LP pool or system program"
        return result

    # 3. Supabase DB cache (check before any Helius calls)
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

    # 4. getAccountInfo — check program owner AND executable flag
    account_info = _fetch_account_info_batch([address], rpc_url)
    info = account_info.get(address, {})

    prog_owner = info.get("owner", "")
    if prog_owner in KNOWN_LP_OWNER_PROGRAMS:
        result["is_known_program"] = True
        result["label"]  = "LP_POOL"
        result["detail"] = f"Account owned by LP program {prog_owner[:8]}… — not a real wallet"
        return result

    if info.get("executable", False):
        result["is_executable"] = True
        result["label"]  = "PROGRAM"
        result["detail"] = "On-chain account has executable=true — program, not a wallet"
        return result

    result["detail"] = "No LP or program flags — appears to be a real user wallet"
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
            "address":    h["address"],
            "is_program": False,
            "is_lp":      False,
            "label":      "WALLET",
        })

    for (ta, owner, reason) in excluded:
        is_lp = reason in ("KNOWN_PROGRAM", "EXECUTABLE", "KNOWN_LP_ADDRESS",
                           "DB_CACHE", "LP_POOL_OWNER")
        # Persist the token account address (ta) since that's what appears in getTokenLargestAccounts
        rows.append({
            "address":    ta,
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
