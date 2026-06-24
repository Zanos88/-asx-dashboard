"""
Offline test harness for executor.py — proves the strict gate chain
(caps → ToxicFlowFilter → DRY_RUN/ENV_STAGE gate → Jupiter) against the
recorded mock_rpc_payloads/ fixtures. No network, Telegram, or Supabase.

The seam is the network client, not the executor:
  - trader._rpc            is replaced to serve the Helius fixture / forced failures
  - executor.requests.get  is replaced with a call-counting Jupiter spy

Usage:
  python test_executor.py
Exit code 0 = all pass, 1 = any failure.
"""
from __future__ import annotations

import json
import os
import sys

import executor
import trader

# ── Fixtures ──────────────────────────────────────────────────────────────────
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_rpc_payloads")
with open(os.path.join(_FIX, "helius_getTokenLargestAccounts_alon.json")) as fh:
    HELIUS = json.load(fh)
with open(os.path.join(_FIX, "jupiter_quote_v6_sol_to_alon.json")) as fh:
    JUPITER = json.load(fh)

ALON        = "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"
TEST_WALLET = "TestWa11etNotInFixture11111111111111111111"   # absent from fixture top-5


# ── Fake network clients ──────────────────────────────────────────────────────

def _rpc_all_pass(method, params, rpc_url, timeout=10):
    """trader._rpc replacement that drives ToxicFlowFilter to a PASS."""
    if method == "getParsedAccountInfo":   # Check A — authorities revoked
        return {"value": {"data": {"parsed": {"info":
                {"mintAuthority": None, "freezeAuthority": None}}}}}
    if method == "getTokenLargestAccounts":  # Checks C + D — real fixture holders
        return HELIUS["result"]
    if method == "getSignaturesForAddress":  # Check E — no creation tx
        return []
    return None


def _rpc_bad_authority(method, params, rpc_url, timeout=10):
    """Same as all-pass but mint authority NOT revoked → Check A fails."""
    if method == "getParsedAccountInfo":
        return {"value": {"data": {"parsed": {"info":
                {"mintAuthority": "Mint1111111111111111111111111111111111111111",
                 "freezeAuthority": None}}}}}
    if method == "getTokenLargestAccounts":
        return HELIUS["result"]
    if method == "getSignaturesForAddress":
        return []
    return None


class _FakeResp:
    def __init__(self, payload): self._payload = payload
    def raise_for_status(self): pass
    def json(self): return self._payload


class _JupiterSpy:
    """Replacement for executor.requests.get — serves the fixture, counts calls."""
    def __init__(self, payload): self.payload, self.calls = payload, 0
    def __call__(self, url, params=None, timeout=None):
        self.calls += 1
        return _FakeResp(self.payload)


# ── Install / restore helpers ─────────────────────────────────────────────────

def _install_rpc(fake):
    orig = trader._rpc
    trader._rpc = fake
    return lambda: setattr(trader, "_rpc", orig)


def _install_jupiter_spy():
    spy  = _JupiterSpy(JUPITER)
    orig = executor.requests.get
    executor.requests.get = spy
    return spy, lambda: setattr(executor.requests, "get", orig)


def _set_stage(dry_run, env_stage):
    od, oe = executor.DRY_RUN, executor.ENV_STAGE
    executor.DRY_RUN, executor.ENV_STAGE = dry_run, env_stage
    def restore():
        executor.DRY_RUN, executor.ENV_STAGE = od, oe
    return restore


# ── Result tracking ───────────────────────────────────────────────────────────
_results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append(ok)
    status = "PASS" if ok else "FAIL"
    print(f"  {label} ... {status}" + (f"  ({detail})" if detail and not ok else ""))


# ── Cases ─────────────────────────────────────────────────────────────────────

def case1_rejected_cap_size():
    restore_rpc = _install_rpc(_rpc_all_pass)
    spy, restore_jup = _install_jupiter_spy()
    try:
        ex  = executor.TradeExecutor()
        res = ex.execute_swap(ALON, amount_sol=5.0, side="buy",
                              token_symbol="ALON", filter_wallet=TEST_WALLET)
        check("1. REJECTED_CAP (size 5.0 > 0.5)",
              res.status == "REJECTED_CAP" and spy.calls == 0,
              f"status={res.status} jup_calls={spy.calls}")
    finally:
        restore_jup(); restore_rpc()


def case2_rejected_cap_slippage():
    restore_rpc = _install_rpc(_rpc_all_pass)
    spy, restore_jup = _install_jupiter_spy()
    try:
        ex  = executor.TradeExecutor()
        res = ex.execute_swap(ALON, amount_sol=0.05, slippage_bps=500, side="buy",
                              token_symbol="ALON", filter_wallet=TEST_WALLET)
        check("2. REJECTED_CAP (slippage 500 > 100)",
              res.status == "REJECTED_CAP" and spy.calls == 0,
              f"status={res.status} jup_calls={spy.calls}")
    finally:
        restore_jup(); restore_rpc()


def case3_rejected_filter():
    restore_rpc = _install_rpc(_rpc_bad_authority)
    spy, restore_jup = _install_jupiter_spy()
    try:
        ex  = executor.TradeExecutor()
        res = ex.execute_swap(ALON, amount_sol=0.05, side="buy",
                              token_symbol="ALON", filter_wallet=TEST_WALLET)
        ok = (res.status == "REJECTED_FILTER"
              and (res.reason or "").startswith("[A]")
              and spy.calls == 0)
        check("3. REJECTED_FILTER (Check A: mint authority not revoked)",
              ok, f"status={res.status} reason={res.reason!r} jup_calls={spy.calls}")
    finally:
        restore_jup(); restore_rpc()


def case4_rejected_gate():
    restore_rpc = _install_rpc(_rpc_all_pass)
    spy, restore_jup = _install_jupiter_spy()
    restore_stage = _set_stage(True, "DEVELOPMENT")   # DRY_RUN active
    try:
        ex  = executor.TradeExecutor()
        res = ex.execute_swap(ALON, amount_sol=0.05, side="buy",
                              token_symbol="ALON", filter_wallet=TEST_WALLET)
        ok = (res.status == "REJECTED_GATE"
              and res.reason == "EXECUTOR_DRY_RUN active"
              and res.expected_out is None
              and spy.calls == 0)
        check("4. REJECTED_GATE (caps+filter pass, DRY_RUN blocks before Jupiter)",
              ok, f"status={res.status} reason={res.reason!r} "
                  f"expected_out={res.expected_out} jup_calls={spy.calls}")
    finally:
        restore_stage(); restore_jup(); restore_rpc()


def case5_filter_drives_off_fixture():
    # PASS verdict with authorities revoked, REJECT (code A) when not revoked.
    restore_rpc = _install_rpc(_rpc_all_pass)
    try:
        f = trader.ToxicFlowFilter(supabase=None, helius_rpc="http://x", fetch_dexscreener=None)
        v_pass = f.check_all(TEST_WALLET, ALON, "ALON")
    finally:
        restore_rpc()
    restore_rpc = _install_rpc(_rpc_bad_authority)
    try:
        f = trader.ToxicFlowFilter(supabase=None, helius_rpc="http://x", fetch_dexscreener=None)
        v_fail = f.check_all(TEST_WALLET, ALON, "ALON")
    finally:
        restore_rpc()
    ok = v_pass.passed and (not v_fail.passed) and v_fail.code == "A"
    check("5. ToxicFlowFilter driven by real Helius fixture (pass + Check-A reject)",
          ok, f"pass={v_pass.passed} fail.passed={v_fail.passed} fail.code={v_fail.code}")


def case6_live_path_quotes_then_blocks():
    restore_rpc = _install_rpc(_rpc_all_pass)
    spy, restore_jup = _install_jupiter_spy()
    restore_stage = _set_stage(False, "PRODUCTION")   # both gates OPEN
    raised = False
    try:
        ex = executor.TradeExecutor()
        try:
            ex.execute_swap(ALON, amount_sol=0.05, side="buy",
                            token_symbol="ALON", filter_wallet=TEST_WALLET)
        except NotImplementedError:
            raised = True
        check("6. Live path: gates open → quote served → broadcast STILL blocked",
              raised and spy.calls == 1,
              f"raised_NotImplemented={raised} jup_calls={spy.calls}")
    finally:
        restore_stage(); restore_jup(); restore_rpc()


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\nexecutor.py offline gate-chain tests\n{'─'*46}")
    for case in (
        case1_rejected_cap_size,
        case2_rejected_cap_slippage,
        case3_rejected_filter,
        case4_rejected_gate,
        case5_filter_drives_off_fixture,
        case6_live_path_quotes_then_blocks,
    ):
        try:
            case()
        except Exception as exc:
            _results.append(False)
            print(f"  {case.__name__} ... ERROR ({exc})")
    passed, total = sum(_results), len(_results)
    print(f"{'─'*46}")
    print(f"{'✅ All' if passed == total else '❌'} {passed}/{total} passed\n")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
