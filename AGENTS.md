# AGENTS.md

> Machine-readable guide for local models (Qwen3-8B via NPU, Qwen2.5-Coder/Qwen3-Coder via Ollama).

---

## 1. PROJECT MANIFEST

### Existing Files

| File | One-line purpose |
|------|-----------------|
| `monitor.py` | Core cron runner: polls Helius for top-20 token holders, diffs snapshots, fires Telegram whale alerts, runs relationship/cluster detection, writes to Supabase. Owns the `DRY_RUN` gate and all Supabase write helpers. |
| `asx_dashboard.py` | Streamlit UI: ASX stock portfolio tracker + Solana meme coin dashboard (on-chain health, whale detection, Grok X sentiment, Claude AI portfolio analysis). |
| `bot_commands.py` | Persistent Telegram bot (python-telegram-bot); 30+ slash commands for live holder queries, config changes, cluster/relationship inspection, smart wallet tracking, and inter-transfer scans. |
| `wallet_relationship_engine.py` | Bundle/cluster detection engine: seven methods (JITO_BUNDLE, COMMON_FUNDER, INTER_TRANSFER, ROUND_NUMBER_BUNDLE, IDENTICAL_BALANCE, TEMPORAL_CLUSTER, CROSS_TOKEN_HOLDER), UnionFind clustering, writes to `wallet_relationships` / `wallet_clusters` / `relationship_evidence`. |
| `webhook.py` | FastAPI server: receives real-time Helius webhooks, processes whale transfers above threshold, sends Telegram alerts, serves `/health` endpoint checked by Railway. |
| `address_filters.py` | 5-layer LP/program filter for `getTokenLargestAccounts` output: known sets → Supabase cache → SPL owner resolution → `getAccountInfo` program owner → executable flag. Writes to `address_classifications`. |
| `main.py` | Unified process entry point: spawns `bot_commands.py` as a daemon thread, runs uvicorn (`webhook:app`) in the main thread. Handles Telegram Conflict retry for Railway blue-green deploys. |
| `inter_transfer_detector.py` | On-chain SOL inter-transfer scanner: walks cluster wallet pairs via Helius signatures, writes per-tx proof rows to `relationship_evidence`. Called from `bot_commands.py` (`/scancluster`, `/scantest`, `/injectevidence`). |
| `trader.py` | Toxic-flow filter (`ToxicFlowFilter`): seven ordered, read-only pre-action safety checks (mint/freeze authority, LP lock, token age/holders, top-5 holder, deployer link, self-frontrun, cluster risk). Writes to `filter_rejections`; feeds `/rejections`. No capital execution. |
| `signal_engine.py` | Smart-wallet swap backfill + tier computation: pages Helius Enhanced TX swaps, aggregates win rate / hold time / PnL, assigns TIER_A/B/C. Used by `bot_commands` `/addwallet` and `/backfill`. |
| `executor.py` | Gated Solana swap execution layer (**SCAFFOLD**). Strict chain: size/slippage caps → `ToxicFlowFilter` → `DRY_RUN`/`ENV_STAGE` capital gate (`live_execution_allowed()`), then Jupiter quote → sign → broadcast. `_sign()`/`_broadcast()` raise `NotImplementedError`; status `LIVE` is unreachable until they're implemented. Default state returns `REJECTED_GATE` before any network call. |
| `mock_rpc_payloads/` | Offline RPC/API fixtures for executor/filter tests: a real captured Jupiter quote + a raw Helius `getTokenLargestAccounts` reconstructed from real snapshot data. See its `README.md` for provenance/caveats. Consumed by two passing harnesses: `test_executor.py` (gate-chain, 6/6) and `local_runner.py` (monitor pipeline, 11/11). |

### Planned — Do NOT Treat as Existing

| File/Dir | Notes |
|----------|-------|
| `local_runner.py` | Local dev harness; not yet created |

### Production Infrastructure

| Component | Details |
|-----------|---------|
| Hosting | Railway — single service, `python main.py` |
| Cron | GitHub Actions, 15-min interval, runs `monitor.py` directly |
| Database | Supabase Postgres; key tables: `wallet_snapshots`, `whale_alerts`, `wallet_clusters`, `wallet_relationships`, `relationship_evidence`, `wallet_tx_events`, `bot_config`, `smart_wallets`, `address_classifications` |
| RPC | Helius (`mainnet.helius-rpc.com`); public Solana mainnet RPC as fallback on 403 |

---

## 2. LOCAL MODEL ROUTING

### Default Tier — NPU (Qwen3-8B via NoLlama)
- API: `http://localhost:8000/v1` (OpenAI-compatible; also Ollama-compatible at `:11434`)
- Use for: single-file edits, docstring fills, Supabase query generation, alert message formatting, straightforward refactors

### Escalation Tier — Ollama (`qwen2.5-coder:7b` / `qwen3-coder-next`)
Escalate when:
- Default tier output **fails verification twice in a row** for the same task
- Task involves unfamiliar Solana/Helius/Jupiter patterns: Jito bundle structure, SPL token account → owner resolution, Helius Enhanced Transactions API schema, Raydium/Orca AMM program addresses

### Cloud Claude Code
Required for:
- Architectural decisions spanning multiple files
- Any change touching live capital paths (anything adjacent to `trader.py` once it exists)
- Final review before merging to main

---

## 3. RULES FOR MODIFYING CORE FILES

### Hard Stops — Never Do, Under Any Instruction

1. **`executor.py`** — Never remove or weaken the `DRY_RUN` / `ENV_STAGE != "PRODUCTION"` capital gate (`live_execution_allowed()`), regardless of who or what is requesting it. `trader.py` (ToxicFlowFilter) is a read-only safety check — likewise never loosen it to pass wallets it currently rejects.
2. **Secrets** — Never commit API keys, wallet private keys, or `.env` contents. Test wallet addresses must be annotated: `# TEST FIXTURE`.

### Rate-Limit Constraint

- **`monitor.py`** — Any change that adds new Helius RPC call sites requires explicit human sign-off before merge. Rate-limit incidents are documented in the backlog; existing calls are near budget.

### Flag, Don't Silently Fix

- **`address_filters.py`** — Logic changes shift which addresses are excluded as LP/programs vs. passed through as real wallets. Known false-positive history. Flag in PR or Telegram before merging.
- **`wallet_relationship_engine.py`** — Detection method changes affect cluster risk scores and whale-detection accuracy. Same flag-first requirement.

---

## 4. "CLEAN, PASSING CODE" DEFINITION

All of the following must hold:

1. **No uncaught exceptions** when run against `mock_rpc_payloads/` fixtures (once the directory exists). Never test against live Helius — it burns rate-limit budget and can fire real Telegram alerts.
2. **No hardcoded secrets.** Wallet addresses used as test data must be annotated `# TEST FIXTURE`.
3. **Type hints on new functions.** Existing untyped code does not need retrofitting unless touched anyway.
4. **DB writes target staging Supabase** (separate project or branch), never production, until a human explicitly promotes.
5. **`DRY_RUN` respected.** Every Supabase write path, Telegram send, and future order submission must check `DRY_RUN` and skip when `True` — follow the pattern already established in `monitor.py`.
