"""
Trader — Toxic Flow Filter (pre-execution safety chain).

Entry point:
    ToxicFlowFilter(supabase, helius_rpc, dexscreener_fn).check_all(
        wallet, token_address, token_symbol, context_json={}
    ) -> FilterResult(passed, code, reason)

Fail-fast: first failing check returns immediately.
Checks A-G run in order; each is independent but shares the same RPC client.

DRY_RUN_TRADER=True by default — skips filter_rejections writes.
Override via bot_config key "dry_run_trader" = "false".
"""
from __future__ import annotations

import logging
import os
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

HELIUS_RPC = (
    f"https://mainnet.helius-rpc.com/?api-key={os.environ.get('HELIUS_API_KEY', '')}"
)
DRY_RUN_TRADER: bool = True   # overridden by bot_config "dry_run_trader" = "false"

FilterResult = namedtuple("FilterResult", ["passed", "code", "reason"])
_PASS = FilterResult(passed=True, code=None, reason=None)


# ── Solana RPC helper ─────────────────────────────────────────────────────────

def _rpc(method: str, params: list, rpc_url: str, timeout: int = 10) -> Any:
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


# ── ToxicFlowFilter ───────────────────────────────────────────────────────────

class ToxicFlowFilter:
    """
    Seven ordered safety checks before any copy/alert action on a wallet+token pair.
    """

    BURN_ADDRESSES = {
        "1nc1nerator11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
    }
    RAYDIUM_LOCKER = "LocktDzaV1W2Bm9DeZeiyz4J9zs4fRqNiYqQyracRXw"

    def __init__(
        self,
        supabase: Any,
        helius_rpc: str | None = None,
        fetch_dexscreener: Callable[[str], dict] | None = None,
        bot_config: dict | None = None,
    ) -> None:
        self._sb              = supabase
        self._rpc_url         = helius_rpc or HELIUS_RPC
        self._dex             = fetch_dexscreener   # optional fn(token_address) -> dex dict
        self._cfg             = bot_config or {}
        self._dry_run         = self._cfg.get("dry_run_trader", "true").lower() != "false"

    # ── Public entry ──────────────────────────────────────────────────────────

    def check_all(
        self,
        wallet: str,
        token_address: str,
        token_symbol: str = "",
        context_json: dict | None = None,
    ) -> FilterResult:
        checks = [
            self._check_authorities,
            self._check_lp_lock,
            self._check_age_and_holders,
            self._check_not_top5,
            self._check_deployer_link,
            self._check_self_frontrun,
            self._check_cluster_risk,
        ]
        for check in checks:
            try:
                result = check(wallet, token_address)
                if not result.passed:
                    self._log_rejection(
                        wallet, token_address, token_symbol,
                        result.code, result.reason, context_json or {},
                    )
                    return result
            except Exception as exc:
                log.warning("ToxicFlowFilter check %s raised: %s", check.__name__, exc)
                # On error, fail-safe: pass (don't block on broken checks)
        return _PASS

    # ── Check A: mint / freeze authorities ───────────────────────────────────

    def _check_authorities(self, wallet: str, token_address: str) -> FilterResult:
        result = _rpc(
            "getParsedAccountInfo",
            [token_address, {"encoding": "jsonParsed"}],
            self._rpc_url,
        )
        if result is None:
            return _PASS  # can't fetch → don't block
        try:
            info = result["value"]["data"]["parsed"]["info"]
            if info.get("mintAuthority") is not None:
                return FilterResult(False, "A", "MINT_AUTHORITY_NOT_REVOKED")
            if info.get("freezeAuthority") is not None:
                return FilterResult(False, "A", "FREEZE_AUTHORITY_NOT_REVOKED")
        except (KeyError, TypeError):
            pass
        return _PASS

    # ── Check B: LP lock ─────────────────────────────────────────────────────

    def _check_lp_lock(self, wallet: str, token_address: str) -> FilterResult:
        """
        Best-effort LP lock check using getTokenLargestAccounts on the LP mint.
        If LP mint can't be determined, passes (don't block on incomplete data).
        """
        lp_mint = self._find_lp_mint(token_address)
        if not lp_mint:
            log.debug("LP mint not found for %s — passing check B", token_address[:8])
            return _PASS

        result = _rpc(
            "getTokenLargestAccounts",
            [lp_mint],
            self._rpc_url,
        )
        if result is None:
            return _PASS

        accounts = result.get("value") or []
        if not accounts:
            return _PASS

        total_supply = sum(float(a.get("uiAmount") or 0) for a in accounts)
        if total_supply == 0:
            return _PASS

        top_holder = accounts[0]
        top_address = top_holder.get("address", "")
        top_amount  = float(top_holder.get("uiAmount") or 0)
        top_pct     = top_amount / total_supply if total_supply > 0 else 0

        # Tier 1: burned (>95% held by burn address)
        if top_address in self.BURN_ADDRESSES and top_pct >= 0.95:
            return _PASS

        # Tier 2: held by known lock program
        owner = self._resolve_owner(top_address)
        if owner == self.RAYDIUM_LOCKER:
            return _PASS

        # Tier 3: LP present but unlocked
        return FilterResult(False, "B", "LP_NOT_LOCKED")

    def _find_lp_mint(self, token_address: str) -> str | None:
        if not self._dex:
            return None
        try:
            dex = self._dex(token_address)
            pairs = dex.get("pairs") or []
            if pairs:
                return pairs[0].get("lpAddress") or pairs[0].get("pairAddress")
        except Exception:
            pass
        return None

    def _resolve_owner(self, account: str) -> str | None:
        result = _rpc(
            "getParsedAccountInfo",
            [account, {"encoding": "jsonParsed"}],
            self._rpc_url,
        )
        if result is None:
            return None
        try:
            return result["value"]["data"]["parsed"]["info"]["owner"]
        except (KeyError, TypeError):
            return None

    # ── Check C: token age + holder count ────────────────────────────────────

    def _check_age_and_holders(self, wallet: str, token_address: str) -> FilterResult:
        min_holders      = int(self._cfg.get("min_holders", 10))
        min_age_hours    = float(self._cfg.get("token_min_age_hours", 2.0))

        # Holder count via getTokenLargestAccounts (proxy — returns up to 100)
        result = _rpc("getTokenLargestAccounts", [token_address], self._rpc_url)
        if result is not None:
            holder_count = len(result.get("value") or [])
            if holder_count < min_holders:
                return FilterResult(
                    False, "C", f"INSUFFICIENT_HOLDERS:{holder_count}<{min_holders}"
                )

        # Age via DexScreener
        if self._dex:
            try:
                dex = self._dex(token_address)
                pairs = (dex.get("pairs") or [])
                if pairs:
                    created_at_ms = pairs[0].get("pairCreatedAt") or 0
                    if created_at_ms:
                        age_hours = (time.time() - created_at_ms / 1000) / 3600
                        if age_hours < min_age_hours:
                            return FilterResult(
                                False, "C",
                                f"TOKEN_TOO_NEW:{age_hours:.1f}h<{min_age_hours}h",
                            )
            except Exception:
                pass

        return _PASS

    # ── Check D: wallet not in top-5 holders ─────────────────────────────────

    def _check_not_top5(self, wallet: str, token_address: str) -> FilterResult:
        result = _rpc("getTokenLargestAccounts", [token_address], self._rpc_url)
        if result is None:
            return _PASS

        top5_accounts = [a.get("address", "") for a in (result.get("value") or [])[:5]]
        if wallet in top5_accounts:
            return FilterResult(False, "D", "WALLET_IS_TOP5_HOLDER")

        return _PASS

    # ── Check E: deployer link ────────────────────────────────────────────────

    def _check_deployer_link(self, wallet: str, token_address: str) -> FilterResult:
        # Find creation tx for token (oldest signature)
        sigs_result = _rpc(
            "getSignaturesForAddress",
            [token_address, {"limit": 1, "commitment": "confirmed"}],
            self._rpc_url,
        )
        if not sigs_result:
            return _PASS

        creation_sig = sigs_result[-1].get("signature") if sigs_result else None
        if not creation_sig:
            return _PASS

        # Get tx and extract first signer (fee payer = deployer)
        tx_result = _rpc(
            "getTransaction",
            [creation_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            self._rpc_url,
            timeout=15,
        )
        if not tx_result:
            return _PASS

        try:
            account_keys = tx_result["transaction"]["message"]["accountKeys"]
            deployer = account_keys[0].get("pubkey") if account_keys else None
        except (KeyError, TypeError, IndexError):
            return _PASS

        if not deployer:
            return _PASS

        if deployer == wallet:
            return FilterResult(False, "E", "WALLET_IS_TOKEN_DEPLOYER")

        # Check wallet_relationships for any link between wallet and deployer
        if self._sb:
            try:
                wa, wb = min(wallet, deployer), max(wallet, deployer)
                r = (
                    self._sb.table("wallet_relationships")
                    .select("relationship_type")
                    .eq("wallet_a", wa)
                    .eq("wallet_b", wb)
                    .limit(1)
                    .execute()
                )
                if r.data:
                    rel_type = r.data[0].get("relationship_type", "UNKNOWN")
                    return FilterResult(False, "E", f"LINKED_TO_DEPLOYER:{rel_type}")
            except Exception as exc:
                log.debug("Check E deployer relationship query failed: %s", exc)

        return _PASS

    # ── Check F: self-frontrun ────────────────────────────────────────────────

    def _check_self_frontrun(self, wallet: str, token_address: str) -> FilterResult:
        if not self._sb:
            return _PASS
        window_hours = float(self._cfg.get("self_frontrun_window_hours", 24.0))
        window_start = (
            datetime.now(timezone.utc) - timedelta(hours=window_hours)
        ).isoformat()
        try:
            r = (
                self._sb.table("wallet_flow_changes")
                .select("detected_at")
                .eq("wallet_address", wallet)
                .eq("token_address", token_address)
                .eq("flow_type", "sell")
                .gte("detected_at", window_start)
                .limit(1)
                .execute()
            )
            if r.data:
                return FilterResult(False, "F", "SELF_FRONTRUN_DETECTED")
        except Exception as exc:
            log.debug("Check F self-frontrun query failed: %s", exc)
        return _PASS

    # ── Check G: cluster risk ─────────────────────────────────────────────────

    def _check_cluster_risk(self, wallet: str, token_address: str) -> FilterResult:
        if not self._sb:
            return _PASS
        try:
            r = (
                self._sb.table("wallet_clusters")
                .select("risk_level,cluster_id")
                .eq("wallet_address", wallet)
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
            )
            if r.data:
                risk = r.data[0].get("risk_level", "")
                if risk == "HIGH_RISK":
                    cid = r.data[0].get("cluster_id", "")
                    return FilterResult(False, "G", f"HIGH_CLUSTER_RISK:{cid}")
        except Exception as exc:
            log.debug("Check G cluster risk query failed: %s", exc)
        return _PASS

    # ── Rejection logger ──────────────────────────────────────────────────────

    def _log_rejection(
        self,
        wallet: str,
        token_address: str,
        token_symbol: str,
        code: str,
        reason: str,
        context_json: dict,
    ) -> None:
        log.info(
            "ToxicFlowFilter REJECT [%s] wallet=%s token=%s reason=%s",
            code, wallet[:8], token_address[:8], reason,
        )
        if self._dry_run or not self._sb:
            return
        try:
            self._sb.table("filter_rejections").insert({
                "wallet_address":  wallet,
                "token_address":   token_address,
                "token_symbol":    token_symbol,
                "rejection_code":  code,
                "rejection_reason": reason,
                "context_json":    context_json,
                "rejected_at":     datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as exc:
            log.warning("filter_rejections insert failed: %s", exc)
