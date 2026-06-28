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

import requests

log = logging.getLogger(__name__)

DEFAULT_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


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
