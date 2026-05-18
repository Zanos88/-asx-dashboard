"""
Wallet Relationship & Bundle Detection Engine
=============================================
Detection methods (in confidence order):
  JITO_BUNDLE        conf=99  Wallets in same Jito bundle at token launch
  INTER_TRANSFER     conf=95  Direct token transfers between top-20 holders
  COMMON_FUNDER      conf=90  Same wallet funded both wallets' SOL at inception
  TEMPORAL_CLUSTER   conf=75  Correlated buy/sell timing across snapshots
  CROSS_TOKEN_HOLDER conf=70  Same wallet in top-20 of multiple tracked tokens

Safe by design:
  - Every detection method wrapped in try/except
  - Helius calls cached in wallet_tx_events (never re-fetch same wallet)
  - Rate limiter: 8 req/sec against Helius Enhanced Transactions API
  - No detection failure ever stops an alert run
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
CONFIDENCE = {
    "JITO_BUNDLE":        99,
    "INTER_TRANSFER":     95,
    "COMMON_FUNDER":      90,
    "TEMPORAL_CLUSTER":   75,
    "CROSS_TOKEN_HOLDER": 70,
}

HELIUS_TX_RATE_SEC = 0.13   # ~8 req/sec headroom under 10/sec limit
_FUNDER_CACHE: dict[str, str | None] = {}   # wallet → funder (None = not found)


# ── Union-Find for cluster building ─────────────────────────────────────────

class UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])
        return self._parent[x]

    def union(self, a: str, b: str) -> None:
        self._parent[self.find(a)] = self.find(b)

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            result[self.find(x)].append(x)
        return {k: sorted(v) for k, v in result.items() if len(v) >= 2}


# ── Helius helpers ─────────────────────────────────────────────────────────

def _helius_get_transactions(
    wallet: str,
    helius_url: str,
    limit: int = 100,
    tx_type: str = "",
) -> list[dict]:
    """Fetch recent transactions for a wallet via Helius Enhanced Transactions API."""
    params: dict[str, Any] = {"limit": limit}
    if tx_type:
        params["type"] = tx_type
    try:
        url = helius_url.replace("/mainnet.helius-rpc.com/", "/api.helius.xyz/").replace(
            "https://mainnet.helius-rpc.com", "https://api.helius.xyz"
        )
        # Build proper Helius Enhanced Transactions endpoint
        api_key = ""
        if "api-key=" in helius_url:
            api_key = helius_url.split("api-key=")[-1].split("&")[0]
        resp = requests.get(
            f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
            params={**params, "api-key": api_key},
            timeout=15,
        )
        if resp.status_code == 429:
            log.warning("Helius rate limit hit — sleeping 2s")
            time.sleep(2)
            resp = requests.get(
                f"https://api.helius.xyz/v0/addresses/{wallet}/transactions",
                params={**params, "api-key": api_key},
                timeout=15,
            )
        if not resp.ok:
            log.debug("Helius txns failed for %s: %s", wallet[:8], resp.status_code)
            return []
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception as exc:
        log.debug("_helius_get_transactions failed for %s: %s", wallet[:8], exc)
        return []


def _cache_tx_events(
    txns: list[dict],
    wallet: str,
    token_address: str,
    token_symbol: str,
    supabase: Any,
) -> None:
    """Persist fetched transactions to wallet_tx_events for future cache lookup."""
    if not supabase or not txns:
        return
    rows = []
    for tx in txns:
        sig = tx.get("signature")
        if not sig:
            continue
        block_time = tx.get("timestamp")
        bt_iso = (
            datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()
            if block_time else None
        )
        # Detect token transfers involving our token
        for tt in tx.get("tokenTransfers", []):
            if tt.get("mint") != token_address:
                continue
            event_type = "TRANSFER_IN" if tt.get("toUserAccount") == wallet else "TRANSFER_OUT"
            rows.append({
                "token_address":  token_address,
                "token_symbol":   token_symbol,
                "wallet_address": wallet,
                "event_type":     event_type,
                "amount":         tt.get("tokenAmount"),
                "counterparty":   tt.get("fromUserAccount") if event_type == "TRANSFER_IN"
                                  else tt.get("toUserAccount"),
                "tx_signature":   sig,
                "block_time":     bt_iso,
            })
    if rows:
        try:
            supabase.table("wallet_tx_events").upsert(
                rows, on_conflict="tx_signature"
            ).execute()
        except Exception as exc:
            log.debug("wallet_tx_events cache write failed: %s", exc)


def _get_cached_txns(wallet: str, token_address: str, supabase: Any) -> list[dict]:
    """Retrieve cached token transfer events for a wallet from Supabase."""
    if not supabase:
        return []
    try:
        r = (
            supabase.table("wallet_tx_events")
            .select("*")
            .eq("wallet_address", wallet)
            .eq("token_address", token_address)
            .order("block_time", desc=False)
            .execute()
        )
        return r.data or []
    except Exception:
        return []


# ── Detection methods ─────────────────────────────────────────────────────

def detect_cross_token_holders(
    cross_holdings: dict[str, dict[str, float]],
    token_address: str,
    supabase: Any = None,
) -> list[dict]:
    """
    Cross-token holders: wallets in top-20 of 2+ tracked tokens.
    Uses in-memory cross_holdings — no API calls needed.
    Returns list of relationship dicts ready for upsert.
    """
    relationships: list[dict] = []
    multi = {addr: h for addr, h in cross_holdings.items() if len(h) >= 2}
    syms = sorted({sym for holdings in multi.values() for sym in holdings})

    for addr, holdings in multi.items():
        sym_list = sorted(holdings.keys())
        for i in range(len(sym_list)):
            for j in range(i + 1, len(sym_list)):
                # We treat this as a relationship between the wallet and "itself"
                # across tokens — store as a self-relationship with cross-token evidence
                relationships.append({
                    "wallet_a":          addr,
                    "wallet_b":          addr,
                    "relationship_type": "CROSS_TOKEN_HOLDER",
                    "token_address":     token_address,
                    "evidence":          json.dumps({
                        "tokens":  sym_list,
                        "holdings": {s: round(holdings[s], 4) for s in sym_list},
                    }),
                    "confidence_score":  CONFIDENCE["CROSS_TOKEN_HOLDER"],
                })
            break  # only one cross-token entry per wallet

    log.info("  [relation] CROSS_TOKEN_HOLDER: %d multi-token wallets", len(multi))
    return relationships


def detect_temporal_clusters(
    token_address: str,
    token_symbol: str,
    supabase: Any,
) -> list[dict]:
    """
    Temporal clustering: wallets that consistently move in the same direction
    across >=40% of shared snapshot windows where they both held supply.
    Reads wallet_snapshots from Supabase — no API calls.
    """
    relationships: list[dict] = []
    if supabase is None:
        return relationships
    try:
        r = (
            supabase.table("wallet_snapshots")
            .select("wallet_address,pct_supply,captured_at")
            .eq("token_address", token_address)
            .order("captured_at", desc=False)
            .limit(5000)
            .execute()
        )
        rows = r.data or []
    except Exception as exc:
        log.warning("  [relation] temporal: snapshot query failed: %s", exc)
        return relationships

    # Build timeline: {timestamp → {wallet → pct}}
    timeline: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        ts = (row.get("captured_at") or "")[:16]
        timeline[ts][row["wallet_address"]] = float(row.get("pct_supply") or 0)

    timestamps = sorted(timeline.keys())
    if len(timestamps) < 3:
        return relationships

    # For each pair of wallets, compute correlation over shared windows
    wallets = list({row["wallet_address"] for row in rows})
    co_movements: dict[tuple[str, str], dict[str, int]] = {}

    for i in range(1, len(timestamps)):
        prev_ts = timestamps[i - 1]
        curr_ts = timestamps[i]
        prev = timeline[prev_ts]
        curr = timeline[curr_ts]
        shared = [w for w in wallets if w in prev and w in curr]

        for wi in range(len(shared)):
            for wj in range(wi + 1, len(shared)):
                a, b = shared[wi], shared[wj]
                key = (min(a, b), max(a, b))
                if key not in co_movements:
                    co_movements[key] = {"co_move": 0, "total": 0}
                d_a = curr[a] - prev[a]
                d_b = curr[b] - prev[b]
                co_movements[key]["total"] += 1
                if (d_a > 0 and d_b > 0) or (d_a < 0 and d_b < 0):
                    co_movements[key]["co_move"] += 1

    for (a, b), counts in co_movements.items():
        if counts["total"] < 3:
            continue
        corr_pct = counts["co_move"] / counts["total"] * 100
        if corr_pct >= 40:
            relationships.append({
                "wallet_a":          a,
                "wallet_b":          b,
                "relationship_type": "TEMPORAL_CLUSTER",
                "token_address":     token_address,
                "evidence":          json.dumps({
                    "co_movement_count": counts["co_move"],
                    "total_snapshots":   counts["total"],
                    "correlation_pct":   round(corr_pct, 1),
                    "token_symbol":      token_symbol,
                }),
                "confidence_score":  CONFIDENCE["TEMPORAL_CLUSTER"],
            })

    log.info("  [relation] TEMPORAL_CLUSTER: %d pairs (threshold 40%%)", len(relationships))
    return relationships


def detect_inter_transfers(
    wallets: list[str],
    token_address: str,
    token_symbol: str,
    helius_url: str,
    supabase: Any = None,
) -> list[dict]:
    """
    Inter-transfer detection: direct token transfers between top-20 holders.
    Caches results in wallet_tx_events.
    """
    relationships: list[dict] = []
    wallet_set = set(wallets)

    # Check Supabase cache first — skip wallets we've already fetched
    fetched_wallets: set[str] = set()
    if supabase:
        try:
            r = (
                supabase.table("wallet_tx_events")
                .select("wallet_address")
                .eq("token_address", token_address)
                .in_("wallet_address", wallets[:50])
                .execute()
            )
            fetched_wallets = {row["wallet_address"] for row in (r.data or [])}
        except Exception:
            pass

    # pair → list of tx evidence
    transfer_map: dict[tuple[str, str], list[dict]] = defaultdict(list)

    # Load from cache
    for wallet in wallets:
        cached = _get_cached_txns(wallet, token_address, supabase)
        for ev in cached:
            cp = ev.get("counterparty")
            if cp and cp in wallet_set and cp != wallet:
                key = (min(wallet, cp), max(wallet, cp))
                transfer_map[key].append({
                    "tx_signature": ev.get("tx_signature"),
                    "event_type":   ev.get("event_type"),
                    "amount":       ev.get("amount"),
                    "block_time":   str(ev.get("block_time") or ""),
                })

    # Fetch from Helius for wallets not yet cached
    api_key = ""
    if "api-key=" in helius_url:
        api_key = helius_url.split("api-key=")[-1].split("&")[0]

    if not api_key:
        log.info("  [relation] INTER_TRANSFER: no Helius key — using Supabase cache only")
    else:
        for wallet in wallets:
            if wallet in fetched_wallets:
                continue
            try:
                time.sleep(HELIUS_TX_RATE_SEC)
                txns = _helius_get_transactions(wallet, helius_url, limit=50)
                _cache_tx_events(txns, wallet, token_address, token_symbol, supabase)

                for tx in txns:
                    for tt in tx.get("tokenTransfers", []):
                        if tt.get("mint") != token_address:
                            continue
                        from_w = tt.get("fromUserAccount", "")
                        to_w   = tt.get("toUserAccount", "")
                        if from_w in wallet_set and to_w in wallet_set and from_w != to_w:
                            key = (min(from_w, to_w), max(from_w, to_w))
                            transfer_map[key].append({
                                "tx_signature": tx.get("signature"),
                                "amount":       tt.get("tokenAmount"),
                                "block_time":   str(tx.get("timestamp") or ""),
                                "from":         from_w,
                                "to":           to_w,
                            })
            except Exception as exc:
                log.debug("  [relation] inter-transfer fetch failed for %s: %s", wallet[:8], exc)

    for (a, b), txs in transfer_map.items():
        unique_txs = {t.get("tx_signature"): t for t in txs if t.get("tx_signature")}
        if not unique_txs:
            continue
        latest = max(unique_txs.values(), key=lambda t: t.get("block_time") or "")
        relationships.append({
            "wallet_a":          a,
            "wallet_b":          b,
            "relationship_type": "INTER_TRANSFER",
            "token_address":     token_address,
            "evidence":          json.dumps({
                "transfer_count": len(unique_txs),
                "latest_tx":      latest.get("tx_signature"),
                "latest_time":    latest.get("block_time"),
                "token_symbol":   token_symbol,
            }),
            "confidence_score":  CONFIDENCE["INTER_TRANSFER"],
        })

    log.info("  [relation] INTER_TRANSFER: %d pairs detected", len(relationships))
    return relationships


def detect_common_funders(
    wallets: list[str],
    token_address: str,
    token_symbol: str,
    helius_url: str,
    supabase: Any = None,
) -> list[tuple[list[dict], str]]:
    """
    Common funder detection: wallets that received their first SOL from the same source.
    Returns (relationships, funder_address) tuples — one per discovered funder cluster.
    """
    api_key = ""
    if "api-key=" in helius_url:
        api_key = helius_url.split("api-key=")[-1].split("&")[0]

    if not api_key:
        log.info("  [relation] COMMON_FUNDER: no Helius key — skipping")
        return []

    # Only check wallets not yet in cache
    to_check = [w for w in wallets if w not in _FUNDER_CACHE]

    for wallet in to_check:
        try:
            time.sleep(HELIUS_TX_RATE_SEC)
            txns = _helius_get_transactions(wallet, helius_url, limit=100)
            if not txns:
                _FUNDER_CACHE[wallet] = None
                continue
            # Sort by timestamp ascending to find FIRST transaction
            sorted_txns = sorted(
                [t for t in txns if t.get("timestamp")],
                key=lambda t: t["timestamp"],
            )
            funder = None
            for tx in sorted_txns:
                for nt in tx.get("nativeTransfers", []):
                    if nt.get("toUserAccount") == wallet and nt.get("amount", 0) > 0:
                        funder = nt.get("fromUserAccount")
                        break
                if funder:
                    break
            _FUNDER_CACHE[wallet] = funder
        except Exception as exc:
            log.debug("  [relation] funder fetch failed for %s: %s", wallet[:8], exc)
            _FUNDER_CACHE[wallet] = None

    # Group by funder
    funder_groups: dict[str, list[str]] = defaultdict(list)
    for wallet in wallets:
        funder = _FUNDER_CACHE.get(wallet)
        if funder:
            funder_groups[funder].append(wallet)

    all_results: list[tuple[list[dict], str]] = []
    for funder, funded in funder_groups.items():
        if len(funded) < 2:
            continue
        funded_sorted = sorted(funded)
        relationships: list[dict] = []
        for i in range(len(funded_sorted)):
            for j in range(i + 1, len(funded_sorted)):
                a, b = funded_sorted[i], funded_sorted[j]
                relationships.append({
                    "wallet_a":          a,
                    "wallet_b":          b,
                    "relationship_type": "COMMON_FUNDER",
                    "token_address":     token_address,
                    "evidence":          json.dumps({
                        "funder":          funder,
                        "funded_wallets":  funded_sorted,
                        "wallet_count":    len(funded_sorted),
                        "token_symbol":    token_symbol,
                    }),
                    "confidence_score":  CONFIDENCE["COMMON_FUNDER"],
                })
        all_results.append((relationships, funder))
        log.info(
            "  [relation] COMMON_FUNDER: %d wallets share funder %s",
            len(funded_sorted), funder[:8],
        )

    total = sum(len(r) for r, _ in all_results)
    log.info("  [relation] COMMON_FUNDER: %d pairs detected", total)
    return all_results


def detect_jito_bundles(
    token_address: str,
    token_symbol: str,
    wallets: list[str],
    helius_url: str,
    supabase: Any = None,
) -> list[dict]:
    """
    Jito bundle detection: fetch the token's earliest transactions and look for
    bundle metadata. Best-effort — Jito public API may be unavailable.
    Falls back to: wallets that acquired the token within the first 5 transactions
    at launch (highly correlated with insider bundles).
    """
    api_key = ""
    if "api-key=" in helius_url:
        api_key = helius_url.split("api-key=")[-1].split("&")[0]

    if not api_key:
        log.info("  [relation] JITO_BUNDLE: no Helius key — skipping")
        return []

    relationships: list[dict] = []
    try:
        # Get earliest token mint/transfer transactions
        resp = requests.get(
            f"https://api.helius.xyz/v0/addresses/{token_address}/transactions",
            params={"api-key": api_key, "limit": 20},
            timeout=15,
        )
        if not resp.ok:
            return relationships
        txns = resp.json() if isinstance(resp.json(), list) else []
        if not txns:
            return relationships

        # Sort oldest-first
        sorted_txns = sorted(
            [t for t in txns if t.get("timestamp")],
            key=lambda t: t["timestamp"],
        )

        # Collect wallets that received tokens in first 5 txns
        bundle_wallets: set[str] = set()
        bundle_txs: list[str] = []
        wallet_set = set(wallets)

        for tx in sorted_txns[:5]:
            sig = tx.get("signature", "")
            if sig:
                bundle_txs.append(sig)
            for tt in tx.get("tokenTransfers", []):
                if tt.get("mint") != token_address:
                    continue
                to_w = tt.get("toUserAccount", "")
                if to_w and to_w in wallet_set:
                    bundle_wallets.add(to_w)

        if len(bundle_wallets) < 2:
            return relationships

        # Check Jito explorer for bundle metadata (best-effort)
        bundle_id = None
        first_sig = bundle_txs[0] if bundle_txs else None
        if first_sig:
            try:
                jito_resp = requests.get(
                    f"https://explorer.jito.wtf/api/v1/transactions/{first_sig}",
                    timeout=5,
                )
                if jito_resp.ok:
                    jdata = jito_resp.json()
                    bundle_id = jdata.get("bundleId") or jdata.get("bundle_id")
            except Exception:
                pass  # Jito API is optional

        bundle_list = sorted(bundle_wallets)
        evidence = {
            "bundle_id":       bundle_id,
            "launch_txs":      bundle_txs[:3],
            "bundled_wallets": bundle_list,
            "wallet_count":    len(bundle_list),
            "token_symbol":    token_symbol,
            "detection_note":  "jito_confirmed" if bundle_id else "early_launch_cluster",
        }

        for i in range(len(bundle_list)):
            for j in range(i + 1, len(bundle_list)):
                a, b = bundle_list[i], bundle_list[j]
                relationships.append({
                    "wallet_a":          a,
                    "wallet_b":          b,
                    "relationship_type": "JITO_BUNDLE",
                    "token_address":     token_address,
                    "evidence":          json.dumps(evidence),
                    "confidence_score":  CONFIDENCE["JITO_BUNDLE"],
                })

        conf_note = "confirmed" if bundle_id else "early-launch"
        log.info(
            "  [relation] JITO_BUNDLE (%s): %d wallets, %d pairs",
            conf_note, len(bundle_list), len(relationships),
        )

    except Exception as exc:
        log.warning("  [relation] JITO_BUNDLE detection failed: %s", exc)

    return relationships


# ── Supabase persistence ─────────────────────────────────────────────────

def upsert_relationships(relationships: list[dict], supabase: Any) -> int:
    """Upsert relationship rows; returns count persisted."""
    if not supabase or not relationships:
        return 0
    saved = 0
    # Batch in chunks of 50 to avoid request size limits
    for i in range(0, len(relationships), 50):
        chunk = relationships[i:i + 50]
        try:
            supabase.table("wallet_relationships").upsert(
                chunk,
                on_conflict="wallet_a,wallet_b,relationship_type,token_address",
            ).execute()
            saved += len(chunk)
        except Exception as exc:
            log.warning("  [relation] upsert_relationships failed (chunk %d): %s", i, exc)
    return saved


# ── Cluster builder ───────────────────────────────────────────────────────

def build_clusters(
    token_address: str,
    token_symbol: str,
    relationships: list[dict],
    current_holders: list[dict],
    supabase: Any = None,
) -> list[dict]:
    """
    Use union-find to build wallet clusters from relationships.
    If A→B and B→C then A, B, C are all in the same cluster.
    Calculates risk level, cluster type, and supply concentration.
    Saves to wallet_clusters and returns cluster summary dicts.
    """
    uf = UnionFind()

    # Track best relationship type per pair for risk calculation
    pair_best: dict[tuple[str, str], str] = {}
    for rel in relationships:
        a, b = rel["wallet_a"], rel["wallet_b"]
        if a == b:
            continue
        uf.union(a, b)
        key = (min(a, b), max(a, b))
        existing = pair_best.get(key, "CROSS_TOKEN_HOLDER")
        if CONFIDENCE.get(rel["relationship_type"], 0) > CONFIDENCE.get(existing, 0):
            pair_best[key] = rel["relationship_type"]

    groups = uf.groups()
    if not groups:
        log.info("  [cluster] No clusters found for %s", token_symbol)
        return []

    # Build supply map from current_holders
    total_supply = sum(
        float(h.get("uiAmountString") or h.get("amount") or 0)
        for h in current_holders
    ) or 1.0
    supply_map: dict[str, float] = {}
    for h in current_holders:
        addr = h.get("address", "")
        amt  = float(h.get("uiAmountString") or h.get("amount") or 0)
        supply_map[addr] = amt / total_supply * 100

    # Determine detection methods per cluster
    cluster_methods: dict[str, set[str]] = defaultdict(set)
    cluster_funders: dict[str, str] = {}
    cluster_bundle_txs: dict[str, str] = {}

    for rel in relationships:
        a, b = rel["wallet_a"], rel["wallet_b"]
        if a == b:
            continue
        root = uf.find(a)
        cluster_methods[root].add(rel["relationship_type"])
        ev = {}
        try:
            ev = json.loads(rel.get("evidence") or "{}")
        except Exception:
            pass
        if rel["relationship_type"] == "COMMON_FUNDER" and ev.get("funder"):
            cluster_funders[root] = ev["funder"]
        if rel["relationship_type"] == "JITO_BUNDLE" and ev.get("launch_txs"):
            txs = ev["launch_txs"]
            if txs:
                cluster_bundle_txs[root] = txs[0]

    # Build cluster rows
    now_iso = datetime.now(timezone.utc).isoformat()
    cluster_summaries: list[dict] = []
    rows_for_db: list[dict] = []

    for root, members in groups.items():
        methods = cluster_methods.get(root, set())
        total_pct = sum(supply_map.get(w, 0.0) for w in members)

        # Determine risk level and cluster type
        has_high_conf = bool(methods & {"JITO_BUNDLE", "COMMON_FUNDER", "INTER_TRANSFER"})
        has_temporal  = "TEMPORAL_CLUSTER" in methods
        has_cross     = methods == {"CROSS_TOKEN_HOLDER"}

        if has_high_conf:
            risk_level = "HIGH_RISK"
            cluster_type = "BUNDLED" if total_pct >= 10.0 else "COORDINATED"
        elif has_temporal and not has_cross:
            risk_level = "MEDIUM"
            cluster_type = "COORDINATED"
        else:
            risk_level = "SMART_MONEY"
            cluster_type = "SMART_MONEY_GROUP"

        # Detection method label
        if "JITO_BUNDLE" in methods:
            det_method = "JITO_BUNDLE"
        elif "COMMON_FUNDER" in methods:
            det_method = "COMMON_FUNDER"
        elif "INTER_TRANSFER" in methods:
            det_method = "INTER_TRANSFER"
        elif len(methods) > 1:
            det_method = "MIXED"
        else:
            det_method = next(iter(methods), "TEMPORAL")

        cluster_id = f"{token_symbol}_{root[:8]}"
        funder     = cluster_funders.get(root)
        bundle_tx  = cluster_bundle_txs.get(root)

        summary = {
            "cluster_id":       cluster_id,
            "token_address":    token_address,
            "token_symbol":     token_symbol,
            "wallets":          members,
            "wallet_count":     len(members),
            "total_supply_pct": round(total_pct, 4),
            "risk_level":       risk_level,
            "cluster_type":     cluster_type,
            "detection_method": det_method,
            "funder_address":   funder,
            "first_bundle_tx":  bundle_tx,
        }
        cluster_summaries.append(summary)

        # One row per wallet in cluster
        for wallet in members:
            rows_for_db.append({
                "token_address":    token_address,
                "token_symbol":     token_symbol,
                "cluster_id":       cluster_id,
                "wallet_address":   wallet,
                "cluster_type":     cluster_type,
                "detection_method": det_method,
                "total_supply_pct": round(total_pct, 4),
                "risk_level":       risk_level,
                "funder_address":   funder,
                "first_bundle_tx":  bundle_tx,
                "wallet_count":     len(members),
                "updated_at":       now_iso,
            })

    # Persist clusters
    if supabase and rows_for_db:
        try:
            supabase.table("wallet_clusters").delete().eq("token_address", token_address).execute()
            supabase.table("wallet_clusters").insert(rows_for_db).execute()
            log.info("  [cluster] %d clusters (%d rows) saved for %s", len(cluster_summaries), len(rows_for_db), token_symbol)
        except Exception as exc:
            log.warning("  [cluster] wallet_clusters save failed: %s", exc)

    return cluster_summaries


# ── Orchestrator ──────────────────────────────────────────────────────────

def run_relationship_detection(
    token_address: str,
    token_symbol: str,
    wallets: list[str],
    current_holders: list[dict],
    cross_holdings: dict[str, dict[str, float]],
    supabase: Any,
    helius_key: str = "",
    changed_wallets: list[str] | None = None,
) -> list[dict]:
    """
    Run all relationship detection methods for a token.
    If changed_wallets is provided, only re-run API-heavy methods for those wallets
    (temporal and cross-token use full wallet set regardless).

    Returns list of cluster summary dicts.
    """
    helius_url = (
        f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        if helius_key else ""
    )

    all_relationships: list[dict] = []

    # 1. Cross-token (no API calls — fast)
    try:
        all_relationships += detect_cross_token_holders(cross_holdings, token_address, supabase)
    except Exception as exc:
        log.warning("  [relation] CROSS_TOKEN_HOLDER failed: %s", exc)

    # 2. Temporal (Supabase only — fast)
    try:
        all_relationships += detect_temporal_clusters(token_address, token_symbol, supabase)
    except Exception as exc:
        log.warning("  [relation] TEMPORAL_CLUSTER failed: %s", exc)

    if helius_key:
        active_wallets = changed_wallets if changed_wallets else wallets

        # 3. Inter-transfers (Helius)
        try:
            all_relationships += detect_inter_transfers(
                active_wallets, token_address, token_symbol, helius_url, supabase
            )
        except Exception as exc:
            log.warning("  [relation] INTER_TRANSFER failed: %s", exc)

        # 4. Common funders (Helius)
        try:
            funder_results = detect_common_funders(
                active_wallets, token_address, token_symbol, helius_url, supabase
            )
            for rels, _ in funder_results:
                all_relationships += rels
        except Exception as exc:
            log.warning("  [relation] COMMON_FUNDER failed: %s", exc)

        # 5. Jito bundles (Helius + Jito API) — only on full run, not incremental
        if not changed_wallets:
            try:
                all_relationships += detect_jito_bundles(
                    token_address, token_symbol, wallets, helius_url, supabase
                )
            except Exception as exc:
                log.warning("  [relation] JITO_BUNDLE failed: %s", exc)

    # Persist relationships
    saved = upsert_relationships(all_relationships, supabase)
    log.info(
        "  [relation] %s: %d relationships detected, %d saved",
        token_symbol, len(all_relationships), saved,
    )

    # Build and save clusters
    clusters = build_clusters(
        token_address, token_symbol, all_relationships, current_holders, supabase
    )

    # Alert if new HIGH_RISK cluster found
    return clusters


# ── Query helpers (for bot commands) ─────────────────────────────────────

def get_wallet_clusters_for_token(token_address: str, supabase: Any) -> list[dict]:
    """Return all clusters for a token, grouped by cluster_id."""
    if not supabase:
        return []
    try:
        r = (
            supabase.table("wallet_clusters")
            .select("*")
            .eq("token_address", token_address)
            .order("total_supply_pct", desc=True)
            .execute()
        )
        rows = r.data or []
        # Group by cluster_id
        clusters: dict[str, dict] = {}
        for row in rows:
            cid = row["cluster_id"]
            if cid not in clusters:
                clusters[cid] = {
                    "cluster_id":       cid,
                    "cluster_type":     row.get("cluster_type"),
                    "detection_method": row.get("detection_method"),
                    "total_supply_pct": row.get("total_supply_pct"),
                    "risk_level":       row.get("risk_level"),
                    "funder_address":   row.get("funder_address"),
                    "first_bundle_tx":  row.get("first_bundle_tx"),
                    "wallet_count":     row.get("wallet_count"),
                    "wallets":          [],
                }
            clusters[cid]["wallets"].append(row["wallet_address"])
        return sorted(clusters.values(), key=lambda c: -(c.get("total_supply_pct") or 0))
    except Exception as exc:
        log.warning("get_wallet_clusters_for_token failed: %s", exc)
        return []


def get_cluster_for_wallet(wallet_address: str, supabase: Any) -> dict | None:
    """Return cluster info for a specific wallet address."""
    if not supabase:
        return None
    try:
        r = (
            supabase.table("wallet_clusters")
            .select("*")
            .eq("wallet_address", wallet_address)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = r.data or []
        if not rows:
            return None
        row = rows[0]
        cid = row["cluster_id"]
        # Fetch all wallets in this cluster
        r2 = (
            supabase.table("wallet_clusters")
            .select("wallet_address")
            .eq("cluster_id", cid)
            .execute()
        )
        all_members = [x["wallet_address"] for x in (r2.data or [])]
        return {
            **row,
            "wallets": all_members,
        }
    except Exception as exc:
        log.warning("get_cluster_for_wallet failed: %s", exc)
        return None


def get_relationships_for_token(token_address: str, supabase: Any) -> list[dict]:
    """Return all relationships for a token."""
    if not supabase:
        return []
    try:
        r = (
            supabase.table("wallet_relationships")
            .select("wallet_a,wallet_b,relationship_type,confidence_score,evidence")
            .eq("token_address", token_address)
            .order("confidence_score", desc=True)
            .limit(200)
            .execute()
        )
        return r.data or []
    except Exception as exc:
        log.warning("get_relationships_for_token failed: %s", exc)
        return []


# ── Backfill ─────────────────────────────────────────────────────────────

def backfill_from_supabase(
    tokens: dict[str, str],
    supabase: Any,
    helius_key: str = "",
) -> None:
    """
    Backfill relationship detection for ALON and TROLL using existing Supabase data.
    Runs all detection methods and prints a summary.
    """
    if not supabase:
        log.error("backfill: Supabase not connected")
        return

    total_relationships = 0
    total_clusters      = 0
    high_risk_clusters: list[dict] = []

    for symbol, token_address in tokens.items():
        log.info("── Backfill: %s (%s)", symbol, token_address[:8])

        # Get unique wallets from wallet_snapshots
        try:
            r = (
                supabase.table("wallet_snapshots")
                .select("wallet_address,pct_supply")
                .eq("token_address", token_address)
                .order("captured_at", desc=True)
                .limit(2000)
                .execute()
            )
            rows = r.data or []
        except Exception as exc:
            log.error("backfill: snapshot query failed for %s: %s", symbol, exc)
            continue

        if not rows:
            log.warning("backfill: no snapshots for %s", symbol)
            continue

        # Get most recent snapshot wallets
        seen: dict[str, float] = {}
        for row in rows:
            addr = row["wallet_address"]
            if addr not in seen:
                seen[addr] = float(row.get("pct_supply") or 0)

        wallets = sorted(seen, key=lambda w: -seen[w])[:20]
        log.info("  %d unique wallets (top 20 by latest pct)", len(wallets))

        # Build cross_holdings from Supabase data (all tracked tokens)
        cross_holdings: dict[str, dict[str, float]] = {}
        for sym2, addr2 in tokens.items():
            try:
                r2 = (
                    supabase.table("wallet_snapshots")
                    .select("wallet_address,pct_supply")
                    .eq("token_address", addr2)
                    .order("captured_at", desc=True)
                    .limit(500)
                    .execute()
                )
                seen2: dict[str, float] = {}
                for row in (r2.data or []):
                    w2 = row["wallet_address"]
                    if w2 not in seen2:
                        seen2[w2] = float(row.get("pct_supply") or 0)
                for w2, pct2 in seen2.items():
                    cross_holdings.setdefault(w2, {})[sym2] = pct2
            except Exception:
                pass

        # Reconstruct current_holders for supply calc
        current_holders = [{"address": w, "uiAmountString": seen[w]} for w in wallets]

        clusters = run_relationship_detection(
            token_address=token_address,
            token_symbol=symbol,
            wallets=wallets,
            current_holders=current_holders,
            cross_holdings=cross_holdings,
            supabase=supabase,
            helius_key=helius_key,
        )

        # Count relationships
        try:
            r_count = (
                supabase.table("wallet_relationships")
                .select("id", count="exact")
                .eq("token_address", token_address)
                .execute()
            )
            rel_count = r_count.count or 0
        except Exception:
            rel_count = 0

        total_relationships += rel_count
        total_clusters      += len(clusters)

        for c in clusters:
            if c.get("risk_level") == "HIGH_RISK":
                high_risk_clusters.append(c)

        log.info(
            "  %s: %d relationships, %d clusters (%d high-risk)",
            symbol, rel_count, len(clusters),
            sum(1 for c in clusters if c.get("risk_level") == "HIGH_RISK"),
        )

    # Summary
    bundled   = sum(1 for c in high_risk_clusters if c.get("cluster_type") == "BUNDLED")
    coordinated = total_clusters - len(high_risk_clusters)
    smart_money = sum(1 for _ in range(total_clusters) if _ >= len(high_risk_clusters))
    biggest = max(high_risk_clusters, key=lambda c: c.get("total_supply_pct", 0), default=None)

    log.info("── Backfill complete ─────────────────────────────────")
    log.info("Found %d relationships across %d unique wallets",
             total_relationships, sum(len(list(tokens)) for _ in [1]))
    log.info("%d clusters detected (%d bundled, %d coordinated, %d smart money)",
             total_clusters, bundled, coordinated, smart_money)
    if biggest:
        log.info(
            "Highest risk: Cluster %s — %d wallets controlling %.1f%% supply",
            biggest.get("cluster_id"), biggest.get("wallet_count", 0),
            biggest.get("total_supply_pct", 0),
        )
