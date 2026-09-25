# Award Intelligence ↔ ORL integration: assessment and proposed plan

Status: **proposal, 2026-09-25. Not started, and it needs the owner decisions below.** Based on a
read-only review of `C:\dev\award-intelligence` (origin `github.com/shreeyanshujha/award-intelligence`,
3 commits, head `ba3aaee`). Respects the agent boundary in [UI_REDESIGN.md](UI_REDESIGN.md) and the
`AwardCostMatrix` seam in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Verdict

**Integration is feasible, and there is no hard technical blocker.** The award engine is a set of pure,
deterministic JS functions, so it can be wrapped unchanged, which is what the agent boundary
requires. Three things need a decision before building (see Blockers), and one real modelling issue
has to be designed around: **per-shift pay cost is not independent**.

## What award-intelligence is

- **Stack:** Node 22 + React 18/Vite SPA, and an optional Express 5 server. ORL is Python/FastAPI, so
  the integration is **two services, one product**, not a code merge.
- **Engine:** `calculateTimesheetResults(parsedCache, timesheetData, {jurisdiction})`
  (`src/domain/payCalculator.js:661`). It is synchronous, has no I/O, and is deterministic. It prices a
  **whole pay period per employee** and returns pay lines with clause references. MA000016 has a
  dedicated versioned segment engine (`src/domain/payEngine/`).
- **AI:** the Anthropic calls are explanation, Q&A and classification *suggestions*. Today no AI output
  feeds a dollar figure (the offline `augmentAwardLibrary` merge is guarded and currently unused).
  This is consistent with the agent boundary.
- **Its own roster tools:** Roster Optimiser (advisory local search), Unallocated Duty and Bulk Ad-Hoc.
  They overlap with ORL Tier 1. The useful idea is **marginal cost against a baseline**:
  `marginalCost()` in `src/engines/coverage.js:113` = `calc(existing ∪ new) − calc(existing)`.
- **Awards bundled:** MA000016 Security (the only one with a verified, versioned rule registry), MA000034
  Nurses, MA000027 Health Professionals, MA000018 Aged Care, MA000031 Medical Practitioners,
  MA000098 Ambulance, MA000012 Pharmacy. **No SCHADS (MA000100) and no Cleaning (MA000022).**

## The key modelling issue: cost depends on the whole week

Weekly and roster-cycle overtime, overtime displacing penalties, weekly-capped allowances and
work-period minimum engagement all make `pay_cost(W, D, S)` depend on W's *other* assignments. A
fixed `AwardCostMatrix` is therefore a linear approximation of the true cost.

**Proposed handling:**
- It keeps the matrix shape and ORL's consume-only contract.
- It never re-derives award rules in ORL.
- It keeps every figure deterministic.

The steps:
1. **Price marginally against a baseline.** Each matrix cell = the engine's marginal cost of
   adding that shift to the worker's baseline week (default baseline: the worker's shifts in the
   previous solved roster for the period, else an empty week).
2. **Reconcile after the solve (mandatory).** Re-price the solved roster with the engine's full
   pay-period calculation. The **reported roster cost is always the engine's exact figure**. The
   solver objective is shown only as "solver estimate".
3. **Optional fixed-point pass.** Re-price with the solved roster as the new baseline and re-solve, at most N
   times, stopping when the assignment is unchanged. This is deterministic.

## Proposed architecture

```
ORL (Python/FastAPI)                           Award Engine service (Node)
 Worker + award fields ─┐                       wraps award-intelligence engine UNCHANGED
 Shift, Roster, Tier 3 ─┼─ AwardEngineClient ──► POST /engine/cost-matrix   → AwardCostMatrix rows
                        │   (HTTP, typed)       POST /engine/price-roster  → exact pay per worker
 Tier 1 CP-SAT ◄── AwardCostMatrix (seam)       POST /engine/pay-run       → pay lines + clause refs
 Tier 2/3 unchanged                              GET  /engine/awards        → codes, levels, validity
 Timesheets = roster + Tier 3 actuals ────────► pay run (deterministic)
```

- **Engine service:** a thin Node HTTP wrapper that imports the engine modules from a **pinned
  commit** of award-intelligence. The engine code itself is not modified. It also builds `parsedCache`
  from structured data, memoises baselines, and returns `instrumentVersions` and `rateValidity` with
  every result.
- **ORL changes:**
  - Worker gains `award_code`, `classification_level`, `employment_type`, `over_award_rate`
    and `jurisdiction`.
  - An `AwardEngineClient` fills `AwardCostMatrix` with `is_placeholder=false`.
  - Unresolved cells become `eligible=false` with a reason.
  - Post-solve reconciliation stores the exact cost next to the solver estimate.
  - Placeholder rows remain only as the fallback when the engine is unavailable, and are flagged.
- **Retire overlapping tools:** Roster Optimiser, Unallocated Duty and Bulk Ad-Hoc become views
  on ORL Tier 1 (or stay advisory), so two solvers never disagree. Rest and weekly-cap rules move
  into ORL's constraints, sourced from engine rule data rather than re-derived.
- **One product UI:** the wireframed shell, served same-origin behind one reverse proxy. The award
  screens are ported into the shell over time.

## Blockers and decisions needed

| # | Item | Type | Needs |
|---|---|---|---|
| 1 | award-intelligence is owned by another GitHub account (`shreeyanshujha`) | **Access / agreement** | Permission to use the code at a pinned commit, and ideally to add an official `buildParsedCache(structured)` + cost-matrix entry point upstream (today ORL would have to build an internal, unofficial shape) |
| 2 | No SCHADS or Cleaning award; the demo workforce is nursing / cleaning / driving | **Owner decision** | Pick awards: MA000034 Nurses covers nursing; driving and cleaning need an award pack (or a demo scoped to Security, which is the best-verified award) |
| 3 | Engine gaps affect cost accuracy: `jurisdiction` is never passed (state public holidays are missed); the generic path ignores `penaltyRates` (evening/night loadings are understated for non-security awards); allowances are triggered by regex over free-text notes | **Engine owner** | These are fixes *inside* the deterministic engine. They must be made and reviewed by its owner, not worked around in ORL. Until then, flag affected costs |
| 4 | Per-shift cost is order-dependent | Design (handled above) | Agree on "marginal vs baseline + mandatory post-solve reconciliation" |
| 5 | Performance: 2 engine runs per matrix cell, no memoisation | Engineering | Memoise baselines in the wrapper and batch per worker |
| 6 | Security release gaps (legal employer, work type, roster cycle) return `unresolved` with no $ | Policy | Treat as `eligible=false` with the reason shown in the UI |
| 7 | Repo hygiene: 3 commits, uncommitted `package-lock.json`, untracked zip, no Docker/CI | Minor | Clean up before pinning |

## Phased plan (after decisions 1–3)

1. **Engine service + contract tests.** Golden tests prove that wrapper output equals the direct
   engine output for the same inputs. No engine changes.
2. **Worker award fields + matrix population.** Tier 1 is unchanged and just receives real costs.
3. **Post-solve reconciliation and fixed-point option.** The UI shows the exact cost and the solver estimate.
4. **Timesheets from roster + Tier 3 actuals → deterministic pay run** via the engine.
5. **Unified UI shell and retirement of the duplicate roster tools.**
