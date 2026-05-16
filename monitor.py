"""
Holder Concentration Monitor
=============================
Run standalone or via GitHub Actions cron job (see .github/workflows/monitor.yml).

What it does each run:
  1. Tests the Supabase connection and logs the result.
  2. Fetches top-20 holders for every tracked token via Helius RPC.
  3. Diffs against the previous snapshot (local JSON kept in git).
  4. Writes the current snapshot to Supabase ``wallet_snapshots`` table.
  5. Writes each detected change to Supabase ``whale_alerts`` table.
  6. Sends a Telegram alert for any significant change (NEW / EXIT / MOVE).
  7. Saves updated local JSON snapshots so GitHub Actions can commit them back.

Environment variables:
    HELIUS_API_KEY        Helius RPC API key
    TELEGRAM_BOT_TOKEN    Telegram bot token from @BotFather
    TELEGRAM_CHAT_ID      Target chat or channel ID
    SUPABASE_URL          Supabase project URL (https://xxx.supabase.co)
    SUPABASE_KEY          Supabase service-role or anon key
    MOVE_THRESHOLD_PCT        % supply change to trigger an alert (default from config.json)
    MIN_HOLDER_CHANGE_TOKENS  raw token amount change to trigger an alert (default from config.json)
    SKIP_SENTIMENT            Legacy flag — ignored (sentiment not active)

Supabase table schemas expected:

    wallet_snapshots
        id            bigint (auto)
        symbol        text
        captured_at   timestamptz
        holders       jsonb
        holder_count  int
        top10_pct     float8

    whale_alerts
        id            bigint (auto)
        symbol        text
        change_type   text          -- NEW | EXIT | MOVE
        wallet_address text
        old_pct       float8        -- nullable
        new_pct       float8        -- nullable
        delta_pct     float8
        alerted_at    timestamptz
        telegram_sent bool
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests
from supabase import Client, create_client
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY       = os.environ.get("SUPABASE_KEY", "")
SNAPSHOT_DIR       = os.path.join(os.path.dirname(__file__), "snapshots")

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_config() -> dict[str, Any]:
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load config.json (%s) — using defaults", exc)
        return {}


_cfg = _load_config()

MOVE_THRESHOLD_PCT = float(
    os.environ.get("MOVE_THRESHOLD_PCT", str(_cfg.get("move_threshold_pct", 1.0)))
)
MIN_HOLDER_CHANGE_TOKENS = float(
    os.environ.get("MIN_HOLDER_CHANGE_TOKENS", str(_cfg.get("min_holder_change_tokens", 1000)))
)

# Token registry: symbol → mint address (sourced from config.json)
TOKENS: dict[str, str] = {
    sym: info["address"]
    for sym, info in _cfg.get("solana_tokens", {}).items()
} or {"ALON": "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"}


# ── Supabase client ───────────────────────────────────────────────────────────

def init_supabase() -> Client | None:
    """
    Initialise and test the Supabase client.

    Logs a clear success or failure message so the GitHub Actions log
    shows immediately whether the connection is working.

    Returns:
        Authenticated ``supabase.Client`` on success, or ``None`` if
        ``SUPABASE_URL`` / ``SUPABASE_KEY`` are not set or the connection fails.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.error(
            "Supabase not configured — SUPABASE_URL and SUPABASE_KEY must be set. "
            "Snapshots will be written to local JSON only."
        )
        return None

    try:
        client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        # Lightweight connectivity test: read one row from wallet_snapshots.
        client.table("wallet_snapshots").select("id").limit(1).execute()
        log.info("✅ Supabase connection OK — %s", SUPABASE_URL)
        return client
    except Exception as exc:
        log.error("❌ Supabase connection FAILED: %s", exc)
        return None


_supabase: Client | None = init_supabase()


# ── Supabase writers ──────────────────────────────────────────────────────────

def write_snapshot_to_supabase(
    symbol: str,
    holders: list[dict[str, Any]],
) -> None:
    """
    Insert the current holder snapshot into the ``wallet_snapshots`` table.

    Args:
        symbol:  Token symbol (e.g. "ALON").
        holders: Holder list from ``fetch_holders()``.
    """
    if _supabase is None:
        return

    total = sum(get_amount(h) for h in holders) or 1.0
    top10_pct = sum(
        get_amount(h) / total * 100
        for h in holders[:10]
    )

    row = {
        "symbol":       symbol,
        "captured_at":  datetime.now(timezone.utc).isoformat(),
        "holders":      holders,
        "holder_count": len(holders),
        "top10_pct":    round(top10_pct, 4),
    }

    try:
        _supabase.table("wallet_snapshots").insert(row).execute()
        log.info("  ✅ Snapshot written to Supabase wallet_snapshots (%s)", symbol)
    except Exception as exc:
        log.error("  ❌ Failed to write snapshot to Supabase for %s: %s", symbol, exc)


def write_alert_to_supabase(
    symbol: str,
    change: dict[str, Any],
    telegram_sent: bool,
) -> None:
    """
    Insert a single holder change event into the ``whale_alerts`` table.

    Args:
        symbol:        Token symbol.
        change:        Single change dict from ``compare_holders()``.
        telegram_sent: Whether the Telegram alert was delivered successfully.
    """
    if _supabase is None:
        return

    row = {
        "symbol":         symbol,
        "change_type":    change["type"],
        "wallet_address": change["address"],
        "old_pct":        change.get("old_pct"),
        "new_pct":        change.get("new_pct"),
        "delta_pct":      round(change["delta"], 6),
        "token_delta":    change.get("token_delta"),
        "trigger":        change.get("trigger", "pct"),
        "alerted_at":     datetime.now(timezone.utc).isoformat(),
        "telegram_sent":  telegram_sent,
    }

    try:
        _supabase.table("whale_alerts").insert(row).execute()
        log.info(
            "  ✅ Alert written to Supabase whale_alerts (%s %s %s)",
            symbol, change["type"], change["address"][:8],
        )
    except Exception as exc:
        log.error(
            "  ❌ Failed to write alert to Supabase for %s %s: %s",
            symbol, change["address"][:8], exc,
        )


# ── Telegram ──────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=False,
)
def send_telegram(msg: str) -> tuple[bool, str]:
    """
    Send an HTML-formatted message to Telegram via the Bot API.

    Retries up to 3 times with exponential backoff on network errors.
    Returns (False, reason) rather than raising on persistent failure.

    Args:
        msg: HTML-formatted message body (Telegram HTML subset).

    Returns:
        Tuple of (success: bool, error_description: str).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False, "not_configured"

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
        timeout=10,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        log.error("Telegram HTTP error %s: %s", resp.status_code, resp.text[:200])
        return False, str(exc)

    return True, ""


# ── Helius RPC ────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError)),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def fetch_holders(token_address: str) -> list[dict[str, Any]]:
    """
    Fetch the top-20 token holders via the Helius ``getTokenLargestAccounts`` RPC method.

    Args:
        token_address: Solana token mint address.

    Returns:
        List of holder dicts with ``address``, ``amount``, ``uiAmount`` keys.

    Raises:
        requests.Timeout:         If the request times out after all retries.
        requests.ConnectionError: If the host is unreachable after all retries.
        requests.HTTPError:       If the API returns a non-2xx status code.
        ValueError:               If ``HELIUS_API_KEY`` is not set.
    """
    if not HELIUS_API_KEY:
        raise ValueError("HELIUS_API_KEY is not set")

    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getTokenLargestAccounts",
        "params":  [token_address],
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()

    data = resp.json()
    if "error" in data:
        log.error("Helius RPC error for %s: %s", token_address[:8], data["error"])
        return []

    return data.get("result", {}).get("value", [])


# ── Local JSON snapshot helpers ───────────────────────────────────────────────
# Local snapshots are kept in git (committed by GitHub Actions) so the diff
# mechanism works across runs even if Supabase is not yet queried for history.

def load_snapshot(symbol: str) -> dict[str, Any] | None:
    """Load the most recent local holder snapshot for diff comparison."""
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not load local snapshot for %s: %s", symbol, exc)
        return None


def save_snapshot(symbol: str, holders: list[dict[str, Any]]) -> None:
    """Persist the current holder list to the local JSON snapshot file."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{symbol}_holders.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"timestamp": datetime.now(timezone.utc).isoformat(), "holders": holders},
                fh,
                indent=2,
            )
    except OSError as exc:
        log.error("Could not save local snapshot for %s: %s", symbol, exc)


# ── Holder comparison ─────────────────────────────────────────────────────────

def get_amount(holder: dict[str, Any]) -> float:
    """Return the decimal-adjusted token amount from a holder dict."""
    ui = holder.get("uiAmount")
    return float(ui) if ui is not None else float(holder.get("amount", 0))


def compare_holders(
    old_holders: list[dict[str, Any]],
    new_holders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Diff two holder lists and return significant changes.

    Change types:
        NEW   — wallet entered the top-20.
        EXIT  — wallet left the top-20.
        MOVE  — existing wallet changed supply % by ≥ ``MOVE_THRESHOLD_PCT``.

    Returns:
        List of change dicts with keys:
        ``type``, ``address``, ``old_pct``, ``new_pct``, ``delta``.
    """
    old_map   = {h["address"]: h for h in old_holders}
    new_map   = {h["address"]: h for h in new_holders}
    old_total = sum(get_amount(h) for h in old_holders) or 1.0
    new_total = sum(get_amount(h) for h in new_holders) or 1.0

    changes: list[dict[str, Any]] = []

    for addr, h in new_map.items():
        if addr not in old_map:
            pct = get_amount(h) / new_total * 100
            changes.append({"type": "NEW",  "address": addr,
                            "old_pct": None, "new_pct": pct,  "delta": pct})

    for addr, h in old_map.items():
        if addr not in new_map:
            pct = get_amount(h) / old_total * 100
            changes.append({"type": "EXIT", "address": addr,
                            "old_pct": pct,  "new_pct": None, "delta": -pct})

    for addr in set(old_map) & set(new_map):
        old_amt = get_amount(old_map[addr])
        new_amt = get_amount(new_map[addr])
        old_pct = old_amt / old_total * 100
        new_pct = new_amt / new_total * 100
        delta   = new_pct - old_pct
        token_delta = abs(new_amt - old_amt)

        pct_triggered   = abs(delta) >= MOVE_THRESHOLD_PCT
        token_triggered = token_delta >= MIN_HOLDER_CHANGE_TOKENS

        if pct_triggered or token_triggered:
            changes.append({
                "type":          "MOVE",
                "address":       addr,
                "old_pct":       old_pct,
                "new_pct":       new_pct,
                "delta":         delta,
                "token_delta":   token_delta,
                "trigger":       "pct+tokens" if (pct_triggered and token_triggered)
                                 else ("pct" if pct_triggered else "tokens"),
            })

    return changes


def format_holder_alert(
    symbol: str,
    changes: list[dict[str, Any]],
    snapshot_ts: str,
) -> str:
    """Build an HTML-formatted Telegram message summarising holder changes."""
    lines = [
        f"🚨 <b>Holder Alert — {symbol}</b>",
        f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"vs snapshot: {snapshot_ts[:16].replace('T', ' ')} UTC\n",
    ]
    for c in changes:
        addr = f"{c['address'][:6]}...{c['address'][-4:]}"
        if c["type"] == "NEW":
            lines.append(
                f"🆕 <b>NEW</b> wallet entered top 20\n"
                f"   <code>{addr}</code> → {c['new_pct']:.2f}%"
            )
        elif c["type"] == "EXIT":
            lines.append(
                f"🚪 <b>EXIT</b> wallet left top 20\n"
                f"   <code>{addr}</code> was {c['old_pct']:.2f}%"
            )
        else:
            arrow   = "📈" if c["delta"] > 0 else "📉"
            trigger = c.get("trigger", "pct")
            tok_str = (
                f"  {c['token_delta']:,.0f} tokens" if "token" in trigger else ""
            )
            lines.append(
                f"{arrow} <b>MOVE</b>  <code>{addr}</code>\n"
                f"   {c['old_pct']:.2f}% → {c['new_pct']:.2f}% ({c['delta']:+.2f}%){tok_str}"
            )
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_holder_monitor() -> None:
    """
    Check holder concentration for every token in the registry.

    For each token:
    - Fetches the current top-20 holders via Helius RPC.
    - Writes the snapshot to Supabase ``wallet_snapshots``.
    - Diffs against the previous local JSON snapshot.
    - For each significant change: writes to Supabase ``whale_alerts``
      and sends a Telegram alert.
    - Saves the updated local JSON snapshot for the next diff run.
    """
    log.info(
        "── Holder monitor starting — %s  threshold=%.1f%%",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        MOVE_THRESHOLD_PCT,
    )

    for symbol, address in TOKENS.items():
        log.info("Checking %s (%s...)", symbol, address[:8])
        try:
            current = fetch_holders(address)
        except Exception as exc:
            log.error("fetch_holders failed for %s after retries: %s — skipping", symbol, exc)
            continue

        if not current:
            log.warning("Empty holder list for %s — skipping", symbol)
            continue

        log.info("  Fetched %d holders", len(current))

        # Write snapshot to Supabase regardless of whether changes are detected.
        write_snapshot_to_supabase(symbol, current)

        snapshot = load_snapshot(symbol)

        if snapshot:
            try:
                changes = compare_holders(snapshot["holders"], current)
            except (KeyError, TypeError) as exc:
                log.error("Snapshot comparison failed for %s: %s", symbol, exc)
                save_snapshot(symbol, current)
                continue

            if changes:
                log.info("  %d change(s) detected", len(changes))
                msg = format_holder_alert(symbol, changes, snapshot["timestamp"])

                # Send Telegram alert.
                telegram_sent = False
                try:
                    ok, err = send_telegram(msg)
                    telegram_sent = ok
                    if not ok:
                        log.error("Telegram delivery failed: %s", err)
                except Exception as exc:
                    log.error("Unexpected Telegram error for %s: %s", symbol, exc)

                # Write each individual change to Supabase whale_alerts.
                for change in changes:
                    write_alert_to_supabase(symbol, change, telegram_sent)
            else:
                log.info("  No significant changes")
        else:
            log.info("  No previous snapshot — creating baseline")
            try:
                send_telegram(
                    f"📸 <b>{symbol}</b> — baseline snapshot created.\n"
                    f"Tracking {len(current)} holders.\n"
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                )
            except Exception as exc:
                log.error("Failed to send baseline notification for %s: %s", symbol, exc)

        save_snapshot(symbol, current)
        log.info("  Local snapshot saved → snapshots/%s_holders.json", symbol)


def run() -> None:
    """
    Entry point — run the holder monitor.

    Designed to be called from GitHub Actions cron or any scheduler.
    Never raises; all errors are logged.
    """
    run_holder_monitor()
    log.info("── Monitor complete")


if __name__ == "__main__":
    run()
