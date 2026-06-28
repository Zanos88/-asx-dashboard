# Bullphoric — Master Sequential Roadmap
**Repo:** Zanos88/-asx-dashboard | **Supabase:** icmuwbvwuhtvqeammxjx | **Date:** 2026-06-28

Goal: an intelligence tool used to make money in bull-market conditions, via wallet/coin
alpha hunting + alerts. Build strictly sequential — each phase gated on the prior one
producing **verified real data**, not "success" messages.

Core discipline (applies to every phase):
- Verify against Supabase directly (counts, MAX timestamps) before trusting any run.
- No silent failures — every run reports rows_written; 0 rows + no error = investigate.
- No dashboard/feature built on unverified numbers — visualizing a bug makes it authoritative.

---

## EXECUTION STATUS — verified 2026-06-28 (do not trust without re-query)

Live reconciliation of Phase A + early Phase B against Supabase `icmuwbvwuhtvqeammxjx`.
Verified via direct SQL, not run logs.

| Item | Status | Evidence |
|------|--------|----------|
| A1 wallet_clusters dedup | ✅ DONE | 11 rows = 11 distinct cluster_ids, **0 duplicates**. The "11 rows" is NOT a regression — more clusters formed across ALON/TROLL/ANSEM + discovered tokens. 1-row-per-cluster_id rule holds. |
| A2 evidence synthetic tx + Migration C | ✅ DONE | Synthetic `tx_signature` deterministic (md5 of `min/max(wallet)`+type+token, no rng/time). Migration C applied — `relationship_evidence_tx` is now a FULL unique index. `relationship_evidence` = 10 rows (COMMON_FUNDER 2, ROUND_NUMBER_BUNDLE 3, IDENTICAL_BALANCE 5). |
| A3 branch merged to main | ✅ DONE | All fix-content on `main` via PR #3 + PR #5 (squash). `claude/remote-control-OsB9K` stale/superseded. |
| A4 cross-coin % bug | ✅ DONE | `build_cross_holdings()` now divides by true `total_supply` (PR #5). Verified wallet `9ZPsRWG…`: ALON **1.1934%**, TROLL **1.8477%** (were 4.15% / 5.54%). |
| A5 bot_config.updated_at | ✅ DONE | Migration D added BEFORE-UPDATE trigger `bot_config_set_updated_at`. Live proof: `token_discovery_last_polled.updated_at` bumped to 2026-06-27 15:53 on next write (was stuck June 19). |
| **A6 win-rate/PnL backfill** | ❌ **OUTSTANDING — current blocker** | Confirmed real: all 5 `smart_wallets` have `trade_count=0` AND `win_rate=0`; only 1 has `trades_90d>0`; 0 non-zero PnL. trades_90d populates but trade_count/win_rate/PnL don't. Blocks all wallet performance scoring (C5, D4). |
| B2 discovered_tokens active | ✅ VERIFIED | 268 rows, latest 2026-06-28 01:32 (cron healthy, ~15-min cadence). Not a stale one-time fill. |

**Phase A gate is NOT yet clear** — A6 must be fixed and verified (smart_wallets showing
non-zero, non-NULL win_rate/PnL) before Phase B re-seeding and everything downstream.

Known non-blocking debt:
- Monitor "Commit updated snapshots" step can fail on a git rebase conflict when a manual
  dispatch races the scheduled cron (add/add on `snapshots/*.json`). DB writes unaffected.
  Harden later (concurrency guard or `-X ours` on the snapshot commit).
- Stale inflated CROSS_TOKEN_HOLDER rows (`token_address` NULL, 4.14/5.53) linger in
  `wallet_relationships` from before the A4 fix — cosmetic; safe to delete.

---

## PHASE A — DATA INTEGRITY FIXES (blocking everything)
Must complete + verify before any new feature or dashboard work.

A1. Confirm wallet_clusters dedup migration applied (currently shows 11 rows again —
    regressed or new clusters; reconfirm 1-row-per-cluster_id rule, re-dedup if needed).
A2. Confirm synthetic tx_signature for relationship_evidence is deterministic
    (same inputs → same string), then confirm Migration C applied.
A3. Confirm claude/remote-control-OsB9K merged to main.
A4. FIX cross-coin Whales % bug — displayed ~3-3.5x inflated vs wallet_snapshots.pct_supply
    (verified: wallet 9ZPsRWG... ALON 1.19% shown as 4.15%, TROLL 1.85% shown as 5.54%).
    Find the calc, fix denominator, re-verify against known-correct values.
A5. FIX bot_config.updated_at not bumping on write (config saves silently, can't trust
    "did my change save").
A6. FIX win-rate/PnL backfill — trades_90d populated but trade_count/win_rate = 0
    (calculation or pagination bug found on seeded wallets). This blocks ALL wallet
    performance scoring downstream.

GATE: re-run 5-check verification. All pass → Phase B.

---

## PHASE B — TROLL DECISION + SIGNAL FOUNDATION
B1. With cross-coin % fixed, pull TROLL's genuine whale activity using corrected calc.
    Decide keep/remove based on real numbers. (User instruction: only track with merit.)
    [STATUS 2026-06-28: user decided KEEP — tracked_tokens = ALON + TROLL + ANSEM.
    Corrected TROLL cross-holding 1.85%; whale_alerts all >5wk stale. Revisit if dormant.]
B2. Confirm discovered_tokens pipeline actively appending (268 rows present — verify
    recency, not a stale one-time fill). [STATUS: ✅ verified active, latest 06-28 01:32.]
B3. Re-seed smart_wallets via Strategy 1 (retro-winner scan) NOW that discovered_tokens
    has data — the alert-frequency seed method is a proven dead end (all dormant/LP/exchange).
    Score early buyers across multiple pumped tokens, gate via is_cluster_member().
    [BLOCKED ON A6 — re-seeding is pointless until win_rate/PnL scoring works.]

GATE: smart_wallets contains wallets with real (non-zero, non-NULL) win_rate/PnL. → Phase C.

---

## PHASE C — DETECTION FEATURE LAYER (build order by evidence value)
Each feeds the dashboard later. Build + verify each before next.

C1. Insider / deployer-link detection (HIGHEST — market research shows insider-linked
    wallets capture majority of memecoin profit). Flag wallets funded by / bought within
    first 3 blocks of deployer. Hard-exclude from copy eligibility. insider_linked column.
C2. Rugpull risk score (0-100): mint/freeze revoked, LP lock %, top-10-ex-LP concentration,
    insider_linked. Gate auto-promotion of discovered_tokens on this.
C3. Concentration metrics (Gini/HHI) on holder distribution — feeds C2, surfaced in alerts.
C4. Buy/sell pressure ratio (rolling window) — momentum signal into signal_engine score.
C5. Alpha score composite (0-100): win_rate + tier + rugpull (inverse) + buy_pressure.
    The live execution/alert gate.
C6. New token alerts (discovered_tokens → rugpull-scored → promote) — only after C1/C2.

GATE: features producing verified output. → Phase D.

---

## PHASE D — DASHBOARD STAGE 1 (Streamlit, test-and-learn)
Add to existing ASX Streamlit app as clearly separated section (see UI note below).
Purpose: validate WHAT data/layouts are actually useful before investing in Vercel.

UI separation requirement: top-level mode switch or distinct nav (e.g. "ASX" vs
"Bullphoric / Crypto") so the two unrelated domains never visually bleed. Keep all
crypto tabs behind the existing password gate.

D1. Call History tab — whale_alerts (424 rows): filterable table, token/wallet/date,
    Solscan/DexScreener links. The reviewable "what did the bot flag" record.
D2. Holder Concentration tab — wallet_snapshots: top holders per token, position-change
    time-series, Gini/HHI from C3.
D3. Cluster Map tab — wallet_clusters + relationships + evidence: member wallets,
    detection method, risk, on-chain proof.
D4. Wallet Performance / Insider List tab — ONLY once Phase A6 + B3 + C1 give real data.
    Nansen-style: wallet, win rate, realized/unrealized PnL, hold time, trade count, tier,
    insider flag. MUST label memecoin data "hint not thesis" + show data-window length
    so one-lucky-trade wallets aren't mistaken for skill.
D5. Alerts/notifications — push meaningful signals (high alpha score, insider entry,
    new low-rug token) to Telegram (already wired) + surface in dashboard.

GATE: 2-4 weeks live use. Capture which tabs/views you actually use for decisions,
what's missing, what's noise. These learnings define Phase E scope. → Phase E.

---

## PHASE E — DASHBOARD STAGE 2 (Vercel custom UI)
Port ONLY the proven-useful views from Phase D into a quality custom frontend.

E1. Backend decision (recommended: reuse Railway FastAPI as single source of truth so
    bot + dashboard share identical scoring logic — avoids duplicate-calc bugs like the
    cross-coin % issue). Alternatives: Supabase REST+RLS (least code, read-only simple),
    Vercel serverless (most flexible, most code).
E2. Build React/Vercel UI consuming that backend. Real-time-friendly for live bull-market
    trade decisions (the clunk Streamlit can't do well).
E3. Migrate alerts + performance + call history into polished interface.
E4. (Future, separate decision) trade execution from UI — only after trader.py live-tested.

---

## LOCAL DEV LOOP (parallel track — Lenovo Aura / BoneAppetit)
Not blocking the above; enables faster iteration. Scope already drafted for Gemini research.
Key constraints baked in: Helius single-call only (free tier blocks batch), enforce
DRY_RUN/read-only locally, secrets never in shared repo, branch so local never auto-deploys
to Railway via main.

---

### Sequencing summary
A (data fixes) → B (signal foundation) → C (detection features) → D (Streamlit test/learn)
→ E (Vercel productize). Local dev loop runs alongside from any point.

Do not skip gates. Each gate = verified real data in Supabase, not a success message.
