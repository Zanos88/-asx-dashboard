# Completed Work — Archive

Terse archive of shipped/merged work so the active roadmap (`BULLPHORIC_MASTER_ROADMAP.md`)
stays scoped to current + near-term only. One line per item with its PR/commit reference.
Do not re-surface these in status reports unless history review is explicitly requested.

## Data-integrity / pre-Phase-1D
- wallet_clusters 1-row-per-cluster + evidence write path for non-tx relationship types — `8cae4ef` (Migrations A/B)
- Synthetic deterministic `tx_signature` for null-tx evidence rows — `f49ff7a` (Migration C: full unique index)
- Cross-coin whale % true-supply denominator + token_discovery batch dedupe — #5 `00ddf38`
- Stable `total_supply` denominator in `compare_holders` — #7 `d464d02`
- `bot_config.updated_at` BEFORE-UPDATE trigger `bot_config_set_updated_at` — Migration D (Supabase MCP)
- Token discovery: Pump.fun Cloudflare bypass + Raydium zero-results fix — `1b56564`
- Phase 1C token-discovery pipeline + `is_cluster_member()` gate — `def59b3`

## Alert pipeline P1–P5 (defaulting-on-failure → skip+log)
- P1 centralize %-of-supply on true supply; skip+log on failure (`supply_utils.py`) — #8 `4ffdd85`
- P2 real on-chain lookups for dormant + EXIT (`fetch_last_activity`, `dormant_alerts`) — #9 `c5f059d`
- P3/P4 skip whale alert on price-fetch failure — #10 `ffc3031`
- P4 show "USD N/A" instead of ~$0 when price unavailable — #11 `9b9ccda`
- Manual verify-wallet workflow (independent public-RPC cross-check) — #12 `da323a3`
- Controlled test-dormant workflow — #13 `a8329c3`

## Detection / data layer
- INTER_TRANSFER `relationship_evidence` write path (0% → real per-tx proof) — #14 `5b70a7f`
- 1C log-only webhook token-discovery source + volume safety switch — #15 `17028bd`
- `rugpull_scorer` (transparent partial score, cap 75, pending C1 component) + read-only dashboard API (4 endpoints, fail-closed auth) — #16 `8df1335`

## Three-Fix Pass
- Centralize tracked_tokens (`get_live_tracked_tokens`) + per-token mute (`/mute` `/unmute` `/muted`) + wallet-intel full value — #17 `af51bc2`
- Remaining `bot_commands` validators read live tracked_tokens — #18 `28a2630`

## Docs
- Bullphoric master sequential roadmap A→E with execution status — #6 `bc67a0a`

## Earlier fixes
- Suppress false-alert storm from token-account vs owner-wallet address mismatch — `c480f78`
- RPC owner resolution via single calls (Helius free plan blocks batch arrays) — `4357f97`
- Wallet delta NameError, digest LP filter, AI no-movement shortcut, tighter directional prompt — `c5e5dcd`
- `/run` silent failure, `/removetoken` Supabase write, `wallet_tx_events` 500s — `696204f`

---

## NOT in this archive (still open — see active roadmap)
- **On-demand `/crosswallets` (fast + deep) + Jupiter price fallback** — code-complete on branch
  `feat/crosswallets-and-jupiter-price` (`945e19c`) but **NOT merged to main and NOT deployed**.
  Tracked as current work, not shipped.
