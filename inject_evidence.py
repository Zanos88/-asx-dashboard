"""
One-shot initial evidence injection.

Run once on Railway (or any env with SUPABASE_* + HELIUS_API_KEY):
    python inject_evidence.py

Scans every cluster in wallet_clusters with full_history=True, writing
per-tx SOL-transfer proof to relationship_evidence.
Also writes bundle/funder evidence via wallet_relationship_engine's
upsert_relationships (which calls _write_evidence_rows automatically).
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("inject_evidence")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    log.error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — aborting")
    sys.exit(1)
if not HELIUS_API_KEY:
    log.warning("HELIUS_API_KEY not set — SOL-transfer evidence scan will be limited")

from supabase import create_client
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

import inter_transfer_detector as itd

# Fetch all unique cluster IDs
r = sb.table("wallet_clusters").select("cluster_id,token_symbol").execute()
rows = r.data or []
clusters: dict[str, str] = {}
for row in rows:
    cid = row["cluster_id"]
    if cid not in clusters:
        clusters[cid] = row.get("token_symbol", "")

log.info("Found %d cluster(s) to scan: %s", len(clusters), list(clusters.keys()))

total_evidence_before = sb.table("relationship_evidence").select("id", count="exact").execute()
before_count = getattr(total_evidence_before, "count", None) or len(total_evidence_before.data or [])
log.info("relationship_evidence rows before scan: %s", before_count)

for cluster_id, symbol in clusters.items():
    log.info("Scanning cluster %s (%s) with full_history=True…", cluster_id, symbol)
    try:
        result = itd.scan_cluster(
            cluster_id,
            symbol=symbol or "UNKNOWN",
            full_history=True,
            supabase=sb,
        )
        n_transfers = len(result.get("transfers") or [])
        n_sigs      = result.get("sig_count", 0)
        log.info(
            "  %s: %d sigs checked, %d SOL transfers found, %d pairs saved",
            cluster_id, n_sigs, n_transfers, result.get("pairs_saved", 0),
        )
    except Exception as exc:
        log.error("  %s: scan failed — %s", cluster_id, exc, exc_info=True)

total_evidence_after = sb.table("relationship_evidence").select("id", count="exact").execute()
after_count = getattr(total_evidence_after, "count", None) or len(total_evidence_after.data or [])
log.info(
    "Injection complete. relationship_evidence rows: %s → %s (+%s)",
    before_count, after_count, (after_count or 0) - (before_count or 0),
)
