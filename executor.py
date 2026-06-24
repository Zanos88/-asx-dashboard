"""
Executor — gated Solana swap execution layer (SCAFFOLD).

This is the ONLY module in the repo that may ever move capital. Everything else
(monitor.py, trader.py/ToxicFlowFilter, signal_engine.py, …) is read-only
monitoring, alerting, or pre-trade filtering.

Safety model — strict, gates-before-Jupiter. execute_swap() runs three
sequential, unbypassable gates and STOPS before any network call unless all
pass and the run is genuinely live:

    GATE 1  size + slippage caps           → REJECTED_CAP
    GATE 2  ToxicFlowFilter.check_all()     → REJECTED_FILTER   (trader.py)
    GATE 3  live_execution_allowed()        → REJECTED_GATE     (DRY_RUN + ENV_STAGE)
    ── hard stop ── no Jupiter quote, no signing, nothing past gate 3 ──
    LIVE PATH (only if all gates pass): jupiter_quote → sign → broadcast

In the default state (EXECUTOR_DRY_RUN=True) execute_swap() returns
REJECTED_GATE immediately and never touches the network. To exercise the
cap/filter/quote logic without hitting live Jupiter, use mock_rpc_payloads/
fixtures (see AGENTS.md §1/§4) — executor.py carries no test-mode branching.

⚠️ NEVER remove or weaken the DRY_RUN / ENV_STAGE gate. See AGENTS.md §3.

Environment variables:
    EXECUTOR_DRY_RUN            "true"/"false"  (default true — safe)
    ENV_STAGE                   "PRODUCTION" enables the live path (default DEVELOPMENT)
    EXECUTOR_MAX_POSITION_SOL   per-swap size cap in SOL (default 0.5)
    EXECUTOR_MAX_SLIPPAGE_BPS   max accepted slippage, basis points (default 100 = 1%)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from trader import ToxicFlowFilter

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
WSOL_MINT          = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL   = 1_000_000_000
JUP_QUOTE_URL      = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP_URL       = "https://lite-api.jup.ag/swap/v1/swap"

# ── Gate config (module-level, read once) ─────────────────────────────────────
DRY_RUN: bool   = os.environ.get("EXECUTOR_DRY_RUN", "true").strip().lower() != "false"
ENV_STAGE: str  = os.environ.get("ENV_STAGE", "DEVELOPMENT").strip().upper()

MAX_POSITION_SOL: float = float(os.environ.get("EXECUTOR_MAX_POSITION_SOL", "0.5"))
MAX_SLIPPAGE_BPS: int   = int(os.environ.get("EXECUTOR_MAX_SLIPPAGE_BPS", "100"))


def live_execution_allowed() -> tuple[bool, str]:
    """
    Single source of truth for whether a real on-chain broadcast may occur.
    BOTH conditions must hold. Returns (allowed, reason_blocked).

    ⚠️ Do not weaken this. execute_swap() funnels every live-capital path through
    here (GATE 3), and _broadcast() re-checks it so the gate cannot be bypassed.

    INVARIANT: status=="LIVE" is intentionally UNREACHABLE while _sign()/
    _broadcast() raise NotImplementedError — the scaffold can pass all three gates
    but still cannot broadcast. Re-verifying that this invariant holds (i.e. that
    nothing reaches a real broadcast unless you deliberately implement signing)
    MUST be the first thing checked before _sign/_broadcast are ever implemented
    for real.
    """
    if DRY_RUN:
        return False, "EXECUTOR_DRY_RUN active"
    if ENV_STAGE != "PRODUCTION":
        return False, f"ENV_STAGE={ENV_STAGE!r} != 'PRODUCTION'"
    return True, ""


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    # status=="LIVE" is unreachable in the scaffold — see live_execution_allowed().
    status: str            # REJECTED_CAP | REJECTED_FILTER | REJECTED_GATE | QUOTE_FAILED | LIVE
    side: str              # buy | sell
    token_address: str
    token_symbol: str
    amount_sol: float
    expected_out: float | None = None   # token/SOL units out — populated only on the live path
    route_label: str | None = None
    reason: str | None = None           # populated on any non-LIVE status
    signature: str | None = None        # populated only on a real broadcast (never in scaffold)

    @property
    def ok(self) -> bool:
        return self.status == "LIVE"


# ── Executor ──────────────────────────────────────────────────────────────────

class TradeExecutor:
    """
    Strict gated swap executor. Three sequential gates (caps → filter → capital
    gate) run before any Jupiter call. In DRY_RUN (default) or any non-PRODUCTION
    stage, execute_swap() returns REJECTED_GATE before touching the network. The
    live path is implemented only as far as the gate; _sign()/_broadcast() raise
    NotImplementedError on purpose.
    """

    def __init__(
        self,
        supabase: Any = None,
        helius_rpc: str | None = None,
        fetch_dexscreener: Callable[[str], dict] | None = None,
        bot_config: dict | None = None,
        notify: Callable[[str], None] | None = None,
        max_position_sol: float = MAX_POSITION_SOL,
        max_slippage_bps: int = MAX_SLIPPAGE_BPS,
    ) -> None:
        self._sb               = supabase
        self._notify           = notify          # optional fn(str) for Telegram/alerts
        self._max_position_sol = max_position_sol
        self._max_slippage_bps = max_slippage_bps
        self._filter = ToxicFlowFilter(
            supabase=supabase,
            helius_rpc=helius_rpc,
            fetch_dexscreener=fetch_dexscreener,
            bot_config=bot_config,
        )
        allowed, reason = live_execution_allowed()
        log.info(
            "TradeExecutor init — live_execution_allowed=%s (%s) | caps: %.3f SOL, %d bps",
            allowed, reason or "PRODUCTION", self._max_position_sol, self._max_slippage_bps,
        )

    # ── Public entry ──────────────────────────────────────────────────────────

    def execute_swap(
        self,
        token_address: str,
        amount_sol: float,
        *,
        side: str = "buy",
        token_symbol: str = "",
        slippage_bps: int = 50,
        filter_wallet: str | None = None,
    ) -> ExecutionResult:
        """
        Attempt a SOL<->token swap through the strict gate chain.

        side="buy"  spends `amount_sol` of SOL to acquire `token_address`.
        side="sell" is accepted by the gate chain; the live leg is unimplemented.

        Returns an ExecutionResult. In scaffold/dry-run mode the terminal status is
        a REJECTED_* reason; it never quotes Jupiter and never broadcasts.
        """
        res = ExecutionResult(
            status="REJECTED_GATE", side=side, token_address=token_address,
            token_symbol=token_symbol, amount_sol=amount_sol,
        )

        # ── GATE 1: size / slippage caps (cheapest, no I/O) ──────────────────
        cap_reason = self._assert_caps(amount_sol, slippage_bps)
        if cap_reason:
            res.status, res.reason = "REJECTED_CAP", cap_reason
            return self._finish(res)

        # ── GATE 2: toxic-flow filter (trader.py / ToxicFlowFilter) ──────────
        verdict = self._filter.check_all(
            filter_wallet or token_address, token_address, token_symbol
        )
        if not verdict.passed:
            res.status, res.reason = "REJECTED_FILTER", f"[{verdict.code}] {verdict.reason}"
            return self._finish(res)

        # ── GATE 3: capital gate (DRY_RUN + ENV_STAGE) ───────────────────────
        live, why = live_execution_allowed()
        if not live:
            res.status, res.reason = "REJECTED_GATE", why
            return self._finish(res)   # ◄── HARD STOP. No Jupiter call past here.

        # ══ LIVE PATH ══ reached only when caps + filter pass AND
        #                 DRY_RUN=False AND ENV_STAGE=="PRODUCTION".
        quote = self._jupiter_quote(token_address, amount_sol, slippage_bps, side=side)
        if not quote:
            res.status, res.reason = "QUOTE_FAILED", "no route from Jupiter"
            return self._finish(res)
        res.expected_out = quote["out_ui"]
        res.route_label  = quote["route_label"]

        unsigned_tx = self._build_swap_transaction(quote, owner_pubkey=None)
        signed_tx   = self._sign(unsigned_tx)
        signature   = self._broadcast(signed_tx)
        res.status, res.signature = "LIVE", signature   # unreachable in scaffold
        return self._finish(res)

    # ── GATE 1 helper ─────────────────────────────────────────────────────────

    def _assert_caps(self, amount_sol: float, slippage_bps: int) -> str | None:
        """Return a rejection reason if size/slippage exceed caps, else None."""
        if amount_sol <= 0 or amount_sol > self._max_position_sol:
            return f"amount_sol={amount_sol} outside (0, {self._max_position_sol}]"
        if slippage_bps <= 0 or slippage_bps > self._max_slippage_bps:
            return f"slippage_bps={slippage_bps} exceeds cap {self._max_slippage_bps}"
        return None

    # ── Jupiter quote (LIVE PATH ONLY — never called in dry-run) ──────────────

    def _jupiter_quote(
        self, token_address: str, amount_sol: float, slippage_bps: int, *, side: str
    ) -> dict[str, Any] | None:
        """Fetch a Jupiter v6 quote. Returns a normalised dict or None on failure."""
        if side == "buy":
            input_mint, output_mint = WSOL_MINT, token_address
        else:
            input_mint, output_mint = token_address, WSOL_MINT
        amount_atomic = int(amount_sol * LAMPORTS_PER_SOL)
        try:
            resp = requests.get(
                JUP_QUOTE_URL,
                params={
                    "inputMint":   input_mint,
                    "outputMint":  output_mint,
                    "amount":      amount_atomic,
                    "slippageBps": slippage_bps,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("Jupiter quote failed for %s: %s", token_address[:8], exc)
            return None

        out_atomic = data.get("outAmount")
        if out_atomic is None:
            return None
        labels = [
            (r.get("swapInfo") or {}).get("label", "?")
            for r in (data.get("routePlan") or [])
        ]
        # outAmount is in the output token's atomic units. We expose SOL output in
        # UI units; token output stays raw atomic (decimals unknown here).
        # TODO(decimals): out_ui for a BUY is RAW ATOMIC units, not human-scale —
        # token decimals are NOT resolved here (only the SOL/sell branch is divided
        # by LAMPORTS_PER_SOL). e.g. outAmount 4535836288 atomic == 4535.84 ALON
        # (6 decimals). This MUST be fixed — fetch and apply the output mint's
        # decimals — BEFORE _sign/_broadcast get real implementations, or position
        # sizing will be wrong by orders of magnitude.
        out_ui = float(out_atomic) / LAMPORTS_PER_SOL if side == "sell" else float(out_atomic)
        return {
            "raw":         data,
            "out_atomic":  int(out_atomic),
            "out_ui":      out_ui,
            "route_label": " → ".join(labels) or "direct",
        }

    # ── Live-path steps (UNIMPLEMENTED — scaffold) ────────────────────────────

    def _build_swap_transaction(self, quote: dict, owner_pubkey: str | None) -> Any:
        """
        Would POST the quote to Jupiter /swap to get the serialized swap tx.
        Unimplemented: requires a real owner pubkey and keypair handling.
        """
        raise NotImplementedError(
            "Live swap-tx build not wired. Implement Jupiter /swap + keypair handling "
            "deliberately before enabling the live path. See AGENTS.md §3."
        )

    def _sign(self, unsigned_tx: Any) -> Any:
        """Would sign with the executor keypair. Unimplemented by design."""
        raise NotImplementedError(
            "Transaction signing not wired. No keypair/solders handling in scaffold."
        )

    def _broadcast(self, signed_tx: Any) -> str:
        """
        Would send the signed tx and return its signature. Unimplemented by design.
        Re-asserts the capital gate at the lowest level — defense in depth.
        """
        allowed, reason = live_execution_allowed()
        if not allowed:
            raise RuntimeError(f"_broadcast blocked by gate: {reason}")
        raise NotImplementedError(
            "Broadcast not wired. Implement send + confirmation deliberately. "
            "Reaching this point means DRY_RUN=False and ENV_STAGE=PRODUCTION."
        )

    # ── Finalisation: log + optional notify ───────────────────────────────────

    def _finish(self, res: ExecutionResult) -> ExecutionResult:
        log.info(
            "Execute %s %s %.4f SOL → status=%s%s",
            res.side, res.token_symbol or res.token_address[:8], res.amount_sol,
            res.status, f" ({res.reason})" if res.reason else "",
        )
        if self._notify:
            try:
                self._notify(self._format_alert(res))
            except Exception as exc:
                log.debug("notify failed: %s", exc)
        return res

    @staticmethod
    def _format_alert(res: ExecutionResult) -> str:
        icon = "✅" if res.status == "LIVE" else "🚫"
        ts   = datetime.now(timezone.utc).strftime("%H:%M UTC")
        lines = [
            f"{icon} <b>EXECUTOR {res.status}</b> — {res.side.upper()} "
            f"{res.token_symbol or res.token_address[:8]}",
            f"Size: {res.amount_sol:.4f} SOL",
        ]
        if res.expected_out is not None:
            lines.append(f"Expected out: {res.expected_out:,.0f} units")
        if res.route_label:
            lines.append(f"Route: {res.route_label}")
        if res.reason:
            lines.append(f"Note: {res.reason}")
        lines.append(f"⏰ {ts}")
        return "\n".join(lines)


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    allowed, reason = live_execution_allowed()
    log.info("live_execution_allowed=%s (%s)", allowed, reason or "PRODUCTION")
    ex = TradeExecutor()  # no supabase/notify
    ALON = "8XtRWb4uAAJFMP4QQhoYYCWR6XXb7ybcCdiqPwz9s5WS"
    # In the default DRY_RUN state this returns REJECTED_GATE BEFORE any Jupiter
    # call — demonstrating the strict gate chain, not a simulated fill.
    result = ex.execute_swap(ALON, amount_sol=0.05, side="buy", token_symbol="ALON")
    log.info("result: %s", result)
    assert result.status != "LIVE", "LIVE must be unreachable while sign/broadcast are stubbed"
