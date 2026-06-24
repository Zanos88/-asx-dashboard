"""
Offline local dev runner for monitor.py — simulates one run_holder_monitor()
cycle without hitting Helius, Telegram, Supabase, DexScreener, or Anthropic.

Twelve module-level callables in monitor.py are replaced with stubs for the
duration of the run. Every stub that could mask a live network call tracks its
invocation count; hard assertions verify the expected counts so that a new
code path accidentally bypassing a stub causes an explicit FAIL rather than a
silent real call.

Synthetic "before" snapshot: ALL_19[:-1] from the real Helius fixture
(18 holders). fetch_holders returns all 19. compare_holders() detects exactly
one NEW entry (the dropped holder), which drives format_quant_alert() and
produces a captured would-be Telegram alert.

Usage:
    python local_runner.py
Exit 0 = run_holder_monitor() completed without exception AND all hard
         assertions pass.
Exit 1 = any assertion failed or uncaught exception.
"""
from __future__ import annotations

import json
import os
import sys
import io
from datetime import datetime, timezone

# Force UTF-8 output so emoji in captured alerts print correctly on Windows
# consoles that default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import monitor

# -- Fixtures ------------------------------------------------------------------
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_rpc_payloads")
with open(os.path.join(_FIX, "helius_getTokenLargestAccounts_alon.json")) as _fh:
    _HELIUS = json.load(_fh)

ALON     = "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"
ALL_19   = _HELIUS["result"]["value"]   # 19 holders, Helius raw format (real data)
BEFORE_18 = ALL_19[:-1]                 # drop last holder → synthetic "before"
NEW_ADDR  = ALL_19[-1]["address"]       # 6BwicEuXm2CAKn7MjM2vvksKuecEkd5FPRwmsAvVn87L  # TEST FIXTURE

SYNTHETIC_BEFORE = {
    "timestamp": "2026-06-23T00:00:00+00:00",
    "holders": BEFORE_18,               # same shape as save_snapshot() writes
}

_FIXED_PRICE = {"price": 0.00001234, "change_1h": 1.5, "change_24h": -3.2}

# -- Stub call counters --------------------------------------------------------
# Every stub that could mask a real network call gets a counter.
# Hard assertions below verify the exact expected count for each.
_calls: dict[str, int] = {
    "fetch_holders":              0,
    "classify_and_filter":        0,
    "fetch_token_supply":         0,
    "fetch_price_context":        0,
    "fetch_wallet_intel":         0,
    "run_relationship_detection": 0,
    "send_alert":                 0,
    "send_telegram":              0,
}
_captured: list[str] = []   # full text of every would-be Telegram send


# -- Stubs ---------------------------------------------------------------------

def _stub_fetch_holders(token_address: str) -> list:
    _calls["fetch_holders"] += 1
    return list(ALL_19)   # defensive copy — prevents in-place mutation


def _stub_classify_and_filter(raw, rpc_url, resolve_fn, supabase):
    # Replaces the 5-layer LP/program filter (which internally calls
    # getAccountInfo on each address). All fixture holders are real wallets
    # (reconstructed from post-filter snapshot data) — pass all through.
    _calls["classify_and_filter"] += 1
    return {"real_holders": list(raw), "excluded": [], "lp_pct": 0.0}


def _stub_fetch_token_supply(token_address: str) -> float:
    _calls["fetch_token_supply"] += 1
    return 1_000_000_000.0


def _stub_fetch_price_context(token_address: str) -> dict:
    _calls["fetch_price_context"] += 1
    return dict(_FIXED_PRICE)


def _stub_fetch_wallet_intel(wallet_address: str, current_symbol: str) -> dict:
    _calls["fetch_wallet_intel"] += 1
    return {}


def _stub_run_relationship_detection(**kwargs) -> list:
    # wallet_relationship_engine makes Helius RPC calls internally; stub entire
    # function. Returns empty list → _all_clusters["ALON"] = [], so
    # _notify_new_cluster is never reached and no extra send_alert calls fire.
    _calls["run_relationship_detection"] += 1
    return []


def _stub_send_alert(msg: str, reply_markup=None) -> tuple[bool, str]:
    _calls["send_alert"] += 1
    _captured.append(msg)
    return True, ""


def _stub_send_telegram(msg: str, reply_markup=None, *, chat_id: str = "") -> tuple[bool, str]:
    _calls["send_telegram"] += 1
    _captured.append(f"[send_telegram] {msg}")
    return True, ""


def _stub_load_snapshot(symbol: str) -> dict | None:
    # Return synthetic before for every call:
    #   · Pre-pass check (if not load_snapshot): truthy → no Supabase fallback
    #   · Pass 2: provides the "before" state for compare_holders()
    return SYNTHETIC_BEFORE


def _stub_save_snapshot(symbol: str, holders: list) -> None:
    pass   # prevent overwriting real snapshots/ALON_holders.json during test


def _stub_get_cluster_for_wallet(addr, supabase):
    # Both call sites wrap in try/except: pass, so this would fail silently
    # without stubbing, but would emit log noise. Return None directly.
    return None


def _stub_get_wallet_last_moved(addr: str, symbol: str):
    # The dormant-wallet check calls compare_holders() a second time at
    # threshold=0.001%. With 18→19 holders, all 18 common holders show a
    # ~0.6% downward shift (denominator grows). Without this stub, None is
    # returned (_supabase=None) → _days=999 ≥ 7 → 18 spurious dormant alerts.
    # Returning now() gives _days=0, so no dormant alerts fire.
    return datetime.now(timezone.utc)


# -- Install / restore ---------------------------------------------------------

def _install_stubs() -> dict:
    """Swap I/O-bound callables in monitor's namespace; return originals."""
    orig = {
        "fetch_holders":              monitor.fetch_holders,
        "classify_and_filter":        monitor.classify_and_filter,
        "fetch_token_supply":         monitor.fetch_token_supply,
        "fetch_price_context":        monitor.fetch_price_context,
        "fetch_wallet_intel":         monitor.fetch_wallet_intel,
        "run_relationship_detection": monitor.run_relationship_detection,
        "send_alert":                 monitor.send_alert,
        "send_telegram":              monitor.send_telegram,
        "load_snapshot":              monitor.load_snapshot,
        "save_snapshot":              monitor.save_snapshot,
        "get_cluster_for_wallet":     monitor.get_cluster_for_wallet,
        "_get_wallet_last_moved":     monitor._get_wallet_last_moved,
        # module-level state
        "_DRY_RUN":               monitor.DRY_RUN,
        "_TOKENS":                dict(monitor.TOKENS),
        "_MOVE_THRESHOLD_PCT":    monitor.MOVE_THRESHOLD_PCT,
        "_MIN_HOLDER_CHANGE_TOKENS": monitor.MIN_HOLDER_CHANGE_TOKENS,
    }
    monitor.fetch_holders              = _stub_fetch_holders
    monitor.classify_and_filter        = _stub_classify_and_filter
    monitor.fetch_token_supply         = _stub_fetch_token_supply
    monitor.fetch_price_context        = _stub_fetch_price_context
    monitor.fetch_wallet_intel         = _stub_fetch_wallet_intel
    monitor.run_relationship_detection = _stub_run_relationship_detection
    monitor.send_alert                 = _stub_send_alert
    monitor.send_telegram              = _stub_send_telegram
    monitor.load_snapshot              = _stub_load_snapshot
    monitor.save_snapshot              = _stub_save_snapshot
    monitor.get_cluster_for_wallet     = _stub_get_cluster_for_wallet
    monitor._get_wallet_last_moved     = _stub_get_wallet_last_moved
    monitor.DRY_RUN = True
    monitor.TOKENS  = {"ALON": ALON}
    # Pin threshold to 1.0% so only the real NEW entry triggers an alert.
    # config.json in this repo sets move_threshold_pct=0.01, which would cause
    # all 18 common holders to fire MOVE alerts due to denominator-shift (~0.1-0.6%),
    # making call-count assertions non-deterministic across config changes.
    monitor.MOVE_THRESHOLD_PCT       = 1.0
    monitor.MIN_HOLDER_CHANGE_TOKENS = 0
    return orig


def _restore_stubs(orig: dict) -> None:
    monitor.fetch_holders              = orig["fetch_holders"]
    monitor.classify_and_filter        = orig["classify_and_filter"]
    monitor.fetch_token_supply         = orig["fetch_token_supply"]
    monitor.fetch_price_context        = orig["fetch_price_context"]
    monitor.fetch_wallet_intel         = orig["fetch_wallet_intel"]
    monitor.run_relationship_detection = orig["run_relationship_detection"]
    monitor.send_alert                 = orig["send_alert"]
    monitor.send_telegram              = orig["send_telegram"]
    monitor.load_snapshot              = orig["load_snapshot"]
    monitor.save_snapshot              = orig["save_snapshot"]
    monitor.get_cluster_for_wallet     = orig["get_cluster_for_wallet"]
    monitor._get_wallet_last_moved     = orig["_get_wallet_last_moved"]
    monitor.DRY_RUN                  = orig["_DRY_RUN"]
    monitor.TOKENS                   = orig["_TOKENS"]
    monitor.MOVE_THRESHOLD_PCT       = orig["_MOVE_THRESHOLD_PCT"]
    monitor.MIN_HOLDER_CHANGE_TOKENS = orig["_MIN_HOLDER_CHANGE_TOKENS"]


# -- Result tracking -----------------------------------------------------------
_results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append(ok)
    status = "PASS" if ok else "FAIL"
    print(f"  {label} ... {status}" + (f"  ({detail})" if detail and not ok else ""))


# -- Main ----------------------------------------------------------------------

def main() -> None:
    print(f"\nlocal_runner.py — offline monitor pipeline simulation")
    print("=" * 56)
    print(f"  Fixture:  {len(ALL_19)} holders from helius_getTokenLargestAccounts_alon.json")
    print(f"  Before:   {len(BEFORE_18)} holders (dropped {NEW_ADDR[:8]}…)")
    print(f"  Expected: 1 NEW entry detected => 1 whale alert + 1 hourly digest")

    # -- Run run_holder_monitor() with all stubs installed -----------------
    orig = _install_stubs()
    run_ok = False
    exc_caught: Exception | None = None
    try:
        monitor.run_holder_monitor()
        run_ok = True
    except Exception as exc:
        exc_caught = exc
    finally:
        _restore_stubs(orig)

    # -- Print captured alert output in full -------------------------------
    sep = "-" * 56
    print(f"\n{sep}")
    print(f"  Captured alerts ({len(_captured)} total — would have gone to Telegram)")
    print(sep)
    for i, msg in enumerate(_captured, 1):
        print(f"\n-- Alert {i} of {len(_captured)} {'-' * 40}")
        print(msg)
    print(f"\n{sep}")

    # -- Hard assertions ---------------------------------------------------
    print(f"\n-- Hard assertions {'-' * 37}")

    check("run_holder_monitor() completed without exception",
          run_ok,
          f"{type(exc_caught).__name__}: {exc_caught}" if exc_caught else "")

    check("fetch_holders called exactly 1× — no raw Helius calls",
          _calls["fetch_holders"] == 1,
          f"calls={_calls['fetch_holders']}")

    check("classify_and_filter called exactly 1× — no getAccountInfo calls",
          _calls["classify_and_filter"] == 1,
          f"calls={_calls['classify_and_filter']}")

    check("fetch_token_supply called exactly 1× — no getTokenSupply calls",
          _calls["fetch_token_supply"] == 1,
          f"calls={_calls['fetch_token_supply']}")

    check("fetch_price_context called exactly 1× — no DexScreener calls",
          _calls["fetch_price_context"] == 1,
          f"calls={_calls['fetch_price_context']}")

    check("fetch_wallet_intel called exactly 1× — NEW entry triggers intel, stub path only",
          _calls["fetch_wallet_intel"] == 1,
          f"calls={_calls['fetch_wallet_intel']}")

    check("run_relationship_detection called exactly 1× — no wallet_relationship_engine RPC",
          _calls["run_relationship_detection"] == 1,
          f"calls={_calls['run_relationship_detection']}")

    check("send_alert called exactly 2× — whale alert + hourly digest, no real Telegram",
          _calls["send_alert"] == 2,
          f"calls={_calls['send_alert']}")

    check("send_telegram called 0× — no baseline snapshot message, DRY_RUN=True elsewhere",
          _calls["send_telegram"] == 0,
          f"calls={_calls['send_telegram']}")

    check("at least 1 captured alert produced",
          len(_captured) >= 1,
          f"captured={len(_captured)}")

    addr_in_alert = NEW_ADDR[:8] in _captured[0] if _captured else False
    check(
        f"first alert contains {NEW_ADDR[:8]}… — diff resolved the correct holder",
        addr_in_alert,
        f"addr_present={addr_in_alert}",
    )

    passed, total = sum(_results), len(_results)
    print(f"{sep}")
    if exc_caught:
        print(f"  Exception: {type(exc_caught).__name__}: {exc_caught}")
    print(f"{'✅ All' if passed == total else '❌'} {passed}/{total} passed\n")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
