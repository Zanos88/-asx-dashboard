"""
rugpull_scorer.py — transparent rug-risk score for a token.

DEPENDENCY DECISION (recorded): the "deployer not insider-linked" component (25 pts)
depends on C1 (insider/deployer-link detection), which is NOT built yet. Per the
no-silent-default standard enforced across this pipeline, that component is returned as
explicitly PENDING (points=None) rather than defaulted to a pass. The score therefore
caps at 75/100 until C1 ships, and every component reports its own status so a consumer
can never mistake a partial score for a full one.

Components (each max 25):
  mint_freeze_revoked   real   — mint & freeze authority both revoked (getParsedAccountInfo)
  lp_locked             real   — LP burned / held by a known locker (best-effort)
  top10_ex_lp_conc      real   — top-10 non-LP holders control < 50% of supply (wallet_snapshots)
  deployer_insider      PENDING (C1 not built) — points=None, never a silent pass

Reuses supply_utils for RPC + percent helpers — no duplicated calc with monitor/trader.
"""

from __future__ import annotations

import logging
from typing import Any

from supply_utils import _rpc, DEFAULT_PUBLIC_RPC

log = logging.getLogger(__name__)

# Known burn / locker addresses (same set trader.py uses for LP-lock).
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",
}
RAYDIUM_LOCKER = "LocktDzaV1W2Bm9DeZeiyz4J9zs4fRqNiYqQyracRXw"


def _component(name: str, points: int | None, max_pts: int, status: str, detail: str = "") -> dict:
    return {"component": name, "points": points, "max": max_pts, "status": status, "detail": detail}


def _score_authorities(token_address: str, rpc_url: str) -> dict:
    """25 pts if both mint & freeze authority are revoked. None on lookup failure."""
    result = _rpc("getParsedAccountInfo", [token_address, {"encoding": "jsonParsed"}], rpc_url)
    if result is None:
        return _component("mint_freeze_revoked", None, 25, "error", "authority lookup failed")
    try:
        info = result["value"]["data"]["parsed"]["info"]
    except (KeyError, TypeError):
        return _component("mint_freeze_revoked", None, 25, "error", "unparseable mint account")
    mint_revoked   = info.get("mintAuthority") is None
    freeze_revoked = info.get("freezeAuthority") is None
    pts = 25 if (mint_revoked and freeze_revoked) else 0
    detail = f"mint_revoked={mint_revoked} freeze_revoked={freeze_revoked}"
    return _component("mint_freeze_revoked", pts, 25, "ok", detail)


def _find_lp_largest(token_address: str, rpc_url: str) -> list[dict] | None:
    """Best-effort: largest accounts of the token mint itself (LP pool usually #1)."""
    result = _rpc("getTokenLargestAccounts", [token_address], rpc_url)
    if result is None:
        return None
    return result.get("value") or []


def _score_lp_locked(token_address: str, rpc_url: str) -> dict:
    """
    25 pts if the largest holder (LP pool) is burned or held by a known locker.
    Indeterminate (None) if largest accounts can't be fetched — never a silent pass.
    """
    accounts = _find_lp_largest(token_address, rpc_url)
    if accounts is None:
        return _component("lp_locked", None, 25, "error", "largest-accounts lookup failed")
    if not accounts:
        return _component("lp_locked", None, 25, "indeterminate", "no holder accounts returned")
    total = sum(float(a.get("uiAmount") or 0) for a in accounts) or 0.0
    if total <= 0:
        return _component("lp_locked", None, 25, "indeterminate", "zero total in largest accounts")
    top = accounts[0]
    top_addr = top.get("address", "")
    top_pct = (float(top.get("uiAmount") or 0) / total) if total else 0.0
    if top_addr in BURN_ADDRESSES and top_pct >= 0.95:
        return _component("lp_locked", 25, 25, "ok", "top holder burned >=95%")
    # Resolve owner to check for a known locker program.
    owner = None
    oinfo = _rpc("getAccountInfo", [top_addr, {"encoding": "jsonParsed"}], rpc_url)
    try:
        owner = oinfo["value"]["data"]["parsed"]["info"]["owner"] if oinfo else None
    except (KeyError, TypeError):
        owner = None
    if owner == RAYDIUM_LOCKER:
        return _component("lp_locked", 25, 25, "ok", "LP held by Raydium locker")
    return _component("lp_locked", 0, 25, "ok", f"LP not locked (top={top_addr[:8]}, {top_pct:.0%})")


def _score_concentration(token_address: str, supabase: Any) -> dict:
    """
    25 pts if top-10 (ex-LP) holders control < 50% of supply, scaled down as concentration
    rises. Uses the already-correct stored wallet_snapshots.pct_supply (no recompute).
    """
    if supabase is None:
        return _component("top10_ex_lp_conc", None, 25, "error", "no supabase")
    try:
        r = (supabase.table("wallet_snapshots")
             .select("wallet_address,pct_supply,captured_at")
             .eq("token_address", token_address)
             .order("captured_at", desc=True)
             .limit(200).execute())
        rows = r.data or []
    except Exception as exc:
        return _component("top10_ex_lp_conc", None, 25, "error", f"query failed: {exc}")
    if not rows:
        return _component("top10_ex_lp_conc", None, 25, "indeterminate", "no snapshots for token")
    # Latest snapshot batch = most recent captured_at; dedupe to latest per wallet.
    latest_ts = rows[0]["captured_at"][:19]
    seen: dict[str, float] = {}
    for row in rows:
        if (row.get("captured_at") or "")[:19] != latest_ts:
            continue
        w = row["wallet_address"]
        if w not in seen:
            seen[w] = float(row.get("pct_supply") or 0)
    top10 = sum(sorted(seen.values(), reverse=True)[:10])
    # <50% -> 25 pts; linearly down to 0 at 100%.
    if top10 < 50:
        pts = 25
    elif top10 >= 100:
        pts = 0
    else:
        pts = round(25 * (100 - top10) / 50)
    return _component("top10_ex_lp_conc", pts, 25, "ok", f"top10_ex_lp={top10:.2f}%")


def compute_rugpull_score(
    token_address: str,
    helius_key: str = "",
    supabase: Any = None,
    public_rpc: str = DEFAULT_PUBLIC_RPC,
) -> dict:
    """
    Return a transparent rug-risk score. score is the sum of REAL component points;
    max_possible_score is 75 (the deployer_insider component is PENDING until C1 ships and
    is never defaulted to a pass). Each component carries its own status so a partial score
    is never mistaken for a complete one.
    """
    rpc_url = (
        f"https://mainnet.helius-rpc.com/?api-key={helius_key}" if helius_key else public_rpc
    )
    components = [
        _score_authorities(token_address, rpc_url),
        _score_lp_locked(token_address, rpc_url),
        _score_concentration(token_address, supabase),
        _component("deployer_insider", None, 25, "pending_C1",
                   "insider/deployer-link detection (C1) not built — not defaulted to pass"),
    ]
    scored = [c for c in components if c["points"] is not None]
    score = sum(c["points"] for c in scored)
    max_scored = sum(c["max"] for c in scored)
    return {
        "token_address":      token_address,
        "score":              score,                 # 0..max_scored, from REAL components only
        "max_scored":         max_scored,            # sum of components that actually scored
        "max_possible_score": 75,                    # ceiling until C1 (deployer) ships
        "components":         components,
        "pending_components": [c["component"] for c in components if c["status"].startswith("pending")],
        "note": "Partial score — deployer_insider pending C1; never default-pass.",
    }
