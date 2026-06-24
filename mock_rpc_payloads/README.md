# mock_rpc_payloads/

Recorded RPC/API responses for offline testing of `executor.py` (cap → filter →
gate → quote chain) and the `ToxicFlowFilter` in `trader.py`, without hitting
live Jupiter or Helius. Per AGENTS.md §4, "clean, passing code" runs against
these fixtures — never against live calls during local dev.

**Payload files are kept verbatim to the real response shape.** All provenance,
caveats, and synthetic notes live here, not inside the JSON.

## Provenance

| File | Source | How obtained |
|------|--------|--------------|
| `jupiter_quote_v6_sol_to_alon.json` | **Real, captured live** | `GET https://lite-api.jup.ag/swap/v1/quote` — 0.05 SOL (`amount=50000000`) WSOL→ALON, `slippageBps=50`. Verbatim response. Schema is identical to the documented Jupiter v6 quote response. |
| `helius_getTokenLargestAccounts_alon.json` | **Reconstructed from real data** | Raw `getTokenLargestAccounts` envelope. The `value[].address` (SPL token accounts), `amount`, `decimals`, `uiAmount`, `uiAmountString` are the real values from `snapshots/ALON_holders.json` (captured 2026-06-23). The `result.context` slot/apiVersion are illustrative. |

## Important shape notes

- **Helius `address` = SPL token account, NOT the owner wallet.** The raw RPC
  response identifies token accounts; owner-wallet resolution happens later in
  `address_filters.classify_and_filter`. (The `snapshots/` file is the *post-filter*
  shape — owner wallets, reranked, with an added `token_account` field. Do not
  confuse the two.)
- **No LP-pool entry in the Helius fixture.** It was reconstructed from the
  post-filter snapshot, which has already had LP/program accounts removed. So this
  fixture exercises the "all real wallets" path only. To test LP exclusion, prepend
  a known LP token account — e.g. the real ALON Raydium LP
  `Gi1VCbPL6Sdcytjp6f1uG1PvCHq25FuNgnVqySHBnKNk` (see
  `address_filters.KNOWN_LP_ADDRESSES`) — with an illustrative balance, and assert
  it lands in `excluded`.

## Caveats that affect executor.py

- **Jupiter host:** `executor.py`'s `JUP_QUOTE_URL` now points at
  `https://lite-api.jup.ag/swap/v1/quote` (HTTP 200 — the host this fixture was
  captured from). The older `quote-api.jup.ag/v6` host was **unreachable (HTTP 000)**
  from this environment. The live path is still gated off in the scaffold.
- **Token decimals / `out_ui`:** for a `buy`, `executor._jupiter_quote` returns
  `out_ui = float(outAmount)` = `4535836288` raw atomic units. ALON has 6 decimals,
  so the true UI amount is `4535.836288` ALON. The executor intentionally leaves
  token decimals unresolved (SOL output is the only branch converted to UI units).
  A fixture-driven test is the right place to decide whether to resolve decimals.

## Not yet built (next task)

A test harness that monkeypatches `requests.get` / `requests.post` (or
`trader._rpc`) to serve these files, then drives `TradeExecutor.execute_swap` and
`ToxicFlowFilter.check_all` and asserts on the resulting statuses/verdicts.
`executor.py` deliberately carries no test-mode branching — the seam is the
network client, not the executor.
