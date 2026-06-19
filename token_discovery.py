"""
token_discovery.py — Phase 1C

Polls Pump.fun and Raydium for newly created token pools, inserts rows into
discovered_tokens (ON CONFLICT DO NOTHING dedup), and records the last-polled
timestamp in bot_config so subsequent runs only fetch new tokens.

Runs on a 15-minute GitHub Actions cron (see .github/workflows/token_discovery.yml).
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests
from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("token_discovery")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY", "")

# Pump.fun public graduation/bonding-curve endpoint (no auth required)
PUMP_FUN_NEW_TOKENS_URL = "https://frontend-api.pump.fun/coins"
# Raydium new AMM pool pairs (public)
RAYDIUM_NEW_PAIRS_URL = "https://api.raydium.io/v2/main/pairs"

BOT_CONFIG_KEY = "token_discovery_last_polled"
# Maximum tokens to insert per platform per run (keep runs fast)
MAX_PER_PLATFORM = 50


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        return None
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _get_last_polled(sb) -> datetime | None:
    try:
        r = sb.table("bot_config").select("value").eq("key", BOT_CONFIG_KEY).execute()
        rows = r.data or []
        if rows and rows[0].get("value"):
            return datetime.fromisoformat(rows[0]["value"])
    except Exception as exc:
        log.warning("Failed to read %s from bot_config: %s", BOT_CONFIG_KEY, exc)
    return None


def _set_last_polled(sb, ts: datetime) -> None:
    try:
        sb.table("bot_config").upsert(
            {"key": BOT_CONFIG_KEY, "value": ts.isoformat()},
            on_conflict="key",
        ).execute()
    except Exception as exc:
        log.warning("Failed to write %s to bot_config: %s", BOT_CONFIG_KEY, exc)


def _insert_tokens(sb, tokens: list[dict]) -> int:
    """Insert token rows, ignoring duplicates. Returns inserted count."""
    if not tokens:
        return 0
    try:
        sb.table("discovered_tokens").upsert(tokens, on_conflict="token_address").execute()
        return len(tokens)
    except Exception as exc:
        log.warning("Insert to discovered_tokens failed: %s", exc)
        return 0


def _fetch_pump_fun(since: datetime | None) -> list[dict]:
    """
    Fetch recently created tokens from Pump.fun public API.
    Pump.fun /coins returns tokens sorted by created_timestamp desc.
    We page through until we pass `since` or hit MAX_PER_PLATFORM.
    """
    results = []
    limit = 50
    offset = 0
    since_ts = since.timestamp() if since else 0.0

    while len(results) < MAX_PER_PLATFORM:
        try:
            resp = requests.get(
                PUMP_FUN_NEW_TOKENS_URL,
                params={"limit": limit, "offset": offset, "sort": "created_timestamp", "order": "DESC"},
                timeout=20,
            )
            if resp.status_code == 429:
                time.sleep(5)
                resp = requests.get(
                    PUMP_FUN_NEW_TOKENS_URL,
                    params={"limit": limit, "offset": offset, "sort": "created_timestamp", "order": "DESC"},
                    timeout=20,
                )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Pump.fun fetch failed: %s", exc)
            break

        page = resp.json()
        if not page:
            break

        for item in page:
            created_ts = item.get("created_timestamp", 0) / 1000  # ms → s
            if created_ts <= since_ts:
                return results  # passed the watermark — stop
            mint = item.get("mint")
            if not mint:
                continue
            results.append({
                "token_address":       mint,
                "token_symbol":        item.get("symbol") or item.get("name"),
                "platform":            "pump.fun",
                "pool_created_at":     datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat(),
                "initial_liquidity_usd": float(item.get("usd_market_cap") or 0),
                "is_watched":          False,
            })
            if len(results) >= MAX_PER_PLATFORM:
                return results

        offset += limit
        time.sleep(0.3)

    return results


def _fetch_raydium(since: datetime | None) -> list[dict]:
    """
    Fetch recently created Raydium AMM pools.
    /v2/main/pairs returns all pairs; we filter by createTime > since.
    """
    since_ts = since.timestamp() if since else 0.0
    try:
        resp = requests.get(RAYDIUM_NEW_PAIRS_URL, timeout=30)
        if resp.status_code == 429:
            time.sleep(5)
            resp = requests.get(RAYDIUM_NEW_PAIRS_URL, timeout=30)
        resp.raise_for_status()
        pairs = resp.json()
    except Exception as exc:
        log.warning("Raydium fetch failed: %s", exc)
        return []

    results = []
    # pairs sorted by createTime desc on Raydium
    for pair in pairs:
        created_ts = pair.get("createTime", 0)
        if isinstance(created_ts, str):
            try:
                created_ts = float(created_ts)
            except ValueError:
                continue
        if created_ts <= since_ts:
            break
        # Raydium pairs are token/SOL or token/USDC — use baseMint as the token address
        mint = pair.get("baseMint")
        if not mint or mint in ("So11111111111111111111111111111111111111112",):
            continue  # skip SOL itself
        results.append({
            "token_address":       mint,
            "token_symbol":        pair.get("name", "").split("-")[0].strip() or None,
            "platform":            "raydium",
            "pool_created_at":     datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat(),
            "initial_liquidity_usd": float(pair.get("liquidity") or 0),
            "is_watched":          False,
        })
        if len(results) >= MAX_PER_PLATFORM:
            break

    return results


def main() -> None:
    sb = _get_supabase()
    if not sb:
        return

    last_polled = _get_last_polled(sb)
    if last_polled:
        log.info("Last polled: %s", last_polled.isoformat())
    else:
        log.info("No prior poll timestamp — fetching latest %d per platform", MAX_PER_PLATFORM)

    run_start = datetime.now(timezone.utc)

    pump_tokens = _fetch_pump_fun(last_polled)
    log.info("Pump.fun: %d new tokens fetched", len(pump_tokens))

    raydium_tokens = _fetch_raydium(last_polled)
    log.info("Raydium: %d new tokens fetched", len(raydium_tokens))

    all_tokens = pump_tokens + raydium_tokens
    inserted = _insert_tokens(sb, all_tokens)
    log.info("Inserted %d rows into discovered_tokens", inserted)

    _set_last_polled(sb, run_start)
    log.info("Done. last_polled updated to %s", run_start.isoformat())


if __name__ == "__main__":
    main()
