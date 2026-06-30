# Bullphoric — Master Sequential Roadmap
**Repo:** Zanos88/-asx-dashboard | **Supabase:** icmuwbvwuhtvqeammxjx

Goal: an intelligence tool used to make money in bull-market conditions, via wallet/coin
alpha hunting + alerts. Build strictly sequential — each phase gated on the prior one
producing **verified real data**, not "success" messages.

Core discipline (applies to every phase):
- Verify against Supabase directly (counts, MAX timestamps) before trusting any run.
- No silent failures — every run reports rows_written; 0 rows + no error = investigate.
- No dashboard/feature built on unverified numbers — visualizing a bug makes it authoritative.
- "Shipped" = code on `main` **and** confirmed running on Railway (`/health` commit == main)
  **and** live-re-tested — not merely merged.

> Completed work is archived in **`COMPLETED_LOG.md`** (do not re-surface it here).

---

## CURRENT (in progress)

- **Deployment-vs-code gap + `/crosswallets deep <N>`.** Diagnosis (git-verified): `/crosswallets`
  + Jupiter fallback are **unmerged** on branch `feat/crosswallets-and-jupiter-price` (`945e19c`)
  = code gap; and `/relationships ansem` still shows pre-fix behaviour though the
  `get_live_tracked_tokens` fix is on `main` (#17/#18) = Railway hasn't redeployed = deploy gap.
  Work: merge the branch; add `set_my_commands` post_init (auto-sync pinned menu); expose commit
  SHA in `/health`; add `/crosswallets deep <N>` widening the candidate pull to top-N per token
  via Helius DAS (reuse `fetch_wallet_token_amounts`); then Railway redeploy + live Telegram re-test.

- **A6 — win-rate/PnL backfill (Phase A blocker).** `smart_wallets` `trade_count`/`win_rate`/PnL
  stuck at 0 while `trades_90d` populates. Blocks all wallet performance scoring (Phase B re-seed,
  C5, D4). Must show non-zero, non-NULL win_rate/PnL before Phase B.

- **Ops — cron/Telegram alert cadence.** Investigate why scheduled cron + regular Telegram alerts
  aren't firing on cadence (under investigation).

---

## NEAR-TERM / NOT STARTED (one line each; full spec written when the item starts)

**Phase B — signal foundation**
- B3 re-seed `smart_wallets` via retro-winner scan over `discovered_tokens`, gated on A6.

**Phase C — detection feature layer**
- C1 insider / deployer-link detection (flag wallets funded by / first-3-blocks of deployer).
- C2 rugpull risk score gate (mint/freeze, LP lock, top-10-ex-LP concentration, insider_linked).
- C3 concentration metrics (Gini/HHI) on holder distribution; feeds C2 + alerts.
- C4 buy/sell pressure ratio (rolling window) into signal_engine score.
- C5 alpha score composite (win_rate + tier + rugpull-inverse + buy_pressure) — the alert gate.
- C6 new-token alerts (discovered_tokens → rugpull-scored → promote) — only after C1/C2.

**Phase D — Streamlit dashboard (test/learn, behind password gate, separated from ASX)**
- Call-history, holder-concentration, cluster-map, wallet-performance (insider list, "hint not thesis").

**Phase E — Vercel custom UI**
- Port only proven-useful Phase D views; reuse Railway FastAPI as single source of truth.

**Local dev loop (parallel track)** — Helius single-call only, enforce DRY_RUN/read-only, branch
so local never auto-deploys to Railway.

---

### Sequencing
A (data fixes) → B (signal foundation) → C (detection features) → D (Streamlit test/learn)
→ E (Vercel productize). Do not skip gates. Each gate = verified real data in Supabase.
