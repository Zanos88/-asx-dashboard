"""
supply_utils.py — shared token-supply + percent-of-supply helpers.

Single source of truth for the %-of-supply calculation so the bug class
"divide by sum-of-holders instead of true circulating supply" cannot reappear
in a third location. Imported by both monitor.py and api/index.py.

Deliberately dependency-light (only `requests` + stdlib) so the Vercel
serverless function can import it without pulling in telegram/supabase deps.

Failure contract: fetch_token_supply returns None on failure — NEVER 0.0 and
NEVER a fallback denominator. Callers must skip + log rather than emit a
%-of-supply figure built on a degraded denominator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

DEFAULT_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _rpc(method: str, params: list, rpc_url: str, timeout: int = 10):
    """Minimal Solana JSON-RPC call. Returns the `result` field, or None on any failure."""
    try:
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        if not resp.ok:
            return None
        return resp.json().get("result")
    except Exception as exc:
        log.debug("RPC %s failed: %s", method, exc)
        return None


def _endpoints(helius_key: str, public_rpc: str) -> list[tuple[str, str]]:
    eps: list[tuple[str, str]] = []
    if helius_key:
        eps.append(("Helius", f"https://mainnet.helius-rpc.com/?api-key={helius_key}"))
    if public_rpc:
        eps.append(("Solana-public", public_rpc))
    return eps


def fetch_token_supply(
    token_address: str,
    helius_key: str = "",
    public_rpc: str = DEFAULT_PUBLIC_RPC,
    timeout: int = 10,
) -> float | None:
    """
    Return true circulating supply via getTokenSupply RPC (Helius → public fallback).

    Returns a positive float on success, or None on failure. Never returns 0.0 or
    a placeholder — a None result means the caller MUST skip the alert and log,
    not compute a percentage with a degraded denominator.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [token_address],
    }
    endpoints: list[tuple[str, str]] = []
    if helius_key:
        endpoints.append(("Helius", f"https://mainnet.helius-rpc.com/?api-key={helius_key}"))
    if public_rpc:
        endpoints.append(("Solana-public", public_rpc))

    for name, url in endpoints:
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                log.warning("  [supply] %s error for %s: %s", name, token_address[:8], data["error"])
                continue
            value = (data.get("result") or {}).get("value") or {}
            # uiAmount can be null for very large supplies; fall back to uiAmountString
            ui_amount = value.get("uiAmount")
            if ui_amount is not None:
                supply = float(ui_amount)
                if supply > 0:
                    log.info("  [supply] %s → %.0f tokens (uiAmount, %s)", token_address[:8], supply, name)
                    return supply
            ui_str = value.get("uiAmountString") or ""
            if ui_str:
                supply = float(ui_str)
                if supply > 0:
                    log.info("  [supply] %s → %.0f tokens (uiAmountString, %s)", token_address[:8], supply, name)
                    return supply
            log.warning("  [supply] %s returned zero/null for %s", name, token_address[:8])
        except Exception as exc:
            log.error("  [supply] %s failed for %s: %s", name, token_address[:8], exc)

    log.error(
        "⚠️ getTokenSupply failed for %s on all endpoints — caller MUST skip "
        "(no %%-of-supply figure may be emitted without a real denominator)",
        token_address[:8],
    )
    return None


def pct_of_supply(amount: float, total_supply: float | None) -> float | None:
    """
    Percent of circulating supply = amount / total_supply * 100.

    Returns None if total_supply is missing/invalid — callers must treat None as
    "cannot compute, skip" rather than substituting a fallback denominator.
    """
    if not total_supply or total_supply <= 0:
        return None
    return amount / total_supply * 100.0


def fetch_last_activity(
    address: str,
    helius_key: str = "",
    public_rpc: str = DEFAULT_PUBLIC_RPC,
    timeout: int = 10,
) -> datetime | None:
    """
    Most recent on-chain activity timestamp for a wallet via getSignaturesForAddress.

    Returns a tz-aware UTC datetime of the latest transaction, or None on failure / no
    history. None means "could not determine" — callers MUST skip + log, never substitute
    a sentinel (e.g. 999 days). This measures REAL on-chain activity, not "last alerted".
    """
    for name, url in _endpoints(helius_key, public_rpc):
        result = _rpc(
            "getSignaturesForAddress",
            [address, {"limit": 1}],
            url,
            timeout=timeout,
        )
        if result is None:
            continue  # endpoint failed — try next
        if not result:
            # Endpoint responded but the wallet has no signatures — genuinely unknown.
            log.warning("  [activity] %s: no signatures returned via %s", address[:8], name)
            return None
        block_time = result[0].get("blockTime")
        if block_time:
            return datetime.fromtimestamp(block_time, tz=timezone.utc)
        log.warning("  [activity] %s: signature had no blockTime via %s", address[:8], name)
        return None
    log.error("  [activity] %s: all endpoints failed — caller MUST skip", address[:8])
    return None


def fetch_wallet_token_balance(
    owner: str,
    mint: str,
    helius_key: str = "",
    public_rpc: str = DEFAULT_PUBLIC_RPC,
    timeout: int = 10,
) -> float | None:
    """
    Current on-chain balance (uiAmount) a wallet holds of a given SPL mint, summed across
    its token accounts. Returns 0.0 if the wallet holds none, or None on lookup failure.

    None means "could not verify" — callers MUST skip + log rather than assume a sale.
    Used to confirm a real EXIT (balance ~0) vs a wallet that merely dropped out of the
    tracked top-N while still holding tokens.
    """
    for name, url in _endpoints(helius_key, public_rpc):
        result = _rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
            url,
            timeout=timeout,
        )
        if result is None:
            continue  # endpoint failed — try next
        accounts = (result or {}).get("value") or []
        total = 0.0
        for acc in accounts:
            try:
                info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
                total += float(info.get("uiAmount") or 0)
            except (KeyError, TypeError, ValueError):
                continue
        return total
    log.error("  [balance] %s/%s: all endpoints failed — caller MUST skip", owner[:8], mint[:8])
    return None
