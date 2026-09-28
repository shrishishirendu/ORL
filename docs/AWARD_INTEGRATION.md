# Award Intelligence ↔ ORL integration: assessment and proposed plan

Status: **in progress on branch `feature/award-integration`** (2026-09-25). Demo scoped to MA000016 Security (NSW). The engine service is built and tested; see [AWARD_ENGINE_CONTRACT.md](AWARD_ENGINE_CONTRACT.md). Based on a
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

## Engine findings for the engine owner (found while wrapping, 2026-09-25)

These are inside the deterministic engine. Under the agent boundary they are reported, not worked
around in ORL. The service normalises *input order* and flags the rest in `warnings`.

1. **Input-order dependence.** Weekly overtime is assigned to the *last shifts in the input array*, not the
   latest in time. The same 5-shift week priced $1,676.78 vs $1,591.52 depending on order. The service
   sorts chronologically. The engine should do this itself.
2. **Jurisdiction has no effect on MA000016 pay.** The security engine applies public-holiday rates only
   when a shift carries the PH flag, and never consults the calendar. NSW gazetted holidays aren't
   registered, and `marginalCost` and `calcRow` never pass jurisdiction. The service flags holiday shifts
   ("cost may be understated").
   **Fix raised 2026-09-28:** [award-intelligence PR #2](https://github.com/shreeyanshujha/award-intelligence/pull/2)
   bundles the verified NSW statewide list (2024–26) into the calendar and has MA000016 segments check
   it. Other states stay incomplete, with a warning. Once it merges and the pin is bumped, the service's
   holiday flag should no longer fire for NSW in those years.
3. **Penalties use the award minimum, not the over-award rate.** A casual L3 at $31.50 on a Sunday prices
   exactly as $29.73 × 2.25 × 7.5, so the over-award adds nothing. This may be award-correct; **owner to
   confirm**.
4. **Rate validity is always "unknown".** The bundled MA000016 library has no amendment date, although the
   rule registry is versioned.
5. **Gaps raised on ordinary shifts:** a minimum-engagement gap on every full-time shift under 7.6 h, and
   a break-record gap on every shift of 5 h or more unless `break_recorded` is set.
6. **Generic path ignores `penaltyRates` shift loadings, and the parsed loadings are wrong.** MA000034
   was parsed as a single night 15% row on a 12:00–18:00 window, with the afternoon loading missing.
   MA000018 was parsed as night −85% over 10:00–13:00. **Fix raised 2026-09-28:**
   [award-intelligence PR #3](https://github.com/shreeyanshujha/award-intelligence/pull/3) pays loadings
   only from rules verified against the award text (MA000034 cl. 20 so far). Parsed rows are never paid.
   Shifts that may attract a loading under an unverified award are reported in `validationErrors`.
7. **Minor:**
   - Employment type is read from the timesheet employee, not the profile.
   - The "Ordinary time" item has no clause reference.
   - The award-intelligence checkout is dirty: `package-lock.json` and `_claude_tmp/`. The pin check
     reads HEAD only.

## Phased plan (after decisions 1–3)

1. **Engine service + contract tests.** Golden tests prove that wrapper output equals the direct
   engine output for the same inputs. No engine changes.
2. **Worker award fields + matrix population.** Tier 1 is unchanged and just receives real costs.
3. **Post-solve reconciliation and fixed-point option.** The UI shows the exact cost and the solver estimate.
4. **Timesheets from roster + Tier 3 actuals → deterministic pay run** via the engine.
5. **Unified UI shell and retirement of the duplicate roster tools.**

## Owner decisions, 2026-09-28 (unification)

- **UI shell:** award-intelligence's React app becomes the single product UI. ORL's Plan / Dispatch /
  Live / Workforce screens are added to it as pages that call ORL's API, styled to the approved
  wireframes. ORL's static `/admin/` dashboard is retired once those pages reach parity.
- **Repo access:** changes to award-intelligence are made on feature branches and raised as PRs for
  its owner to review. This includes the engine findings above (holidays/jurisdiction,
  `penaltyRates` in the generic path, input-order overtime). ORL still never patches around them.
- **Data ownership:** both systems keep their own data. Employees are **synced** between ORL
  `Worker` (keyed by `employee_code`) and the award-intelligence employee master (keyed by
  `employeeId`). The sync must be explicit and must report conflicts, never overwrite them silently.
- **Employee sync (built 2026-09-29).** award-intelligence has no employee store that accepts writes, so
  the sync is a read on its side and a report on ORL's:
  - award-intelligence exposes `GET /api/employee-master`
    ([PR #4](https://github.com/shreeyanshujha/award-intelligence/pull/4)). It returns the employee master
    behind the latest payroll import, with **raw payroll IDs** (owner decision, 2026-09-29) and the API
    token always required. It carries no names, no pay amounts, and no date of birth or gender.
  - ORL runs `POST /workers/sync-employees` (`app/services/employee_sync/`). It matches
    `employee_code` == `employeeId` exactly and compares `employment_type` and `award_code`
    ("MA000016-NSW" → `MA000016`, with a note if the state differs from `AWARD_JURISDICTION`).
  - Preview is the default. **Apply only fills blank ORL fields** (owner decision, 2026-09-29). Differing
    values are reported as conflicts and never overwritten. Employees that exist only in
    award-intelligence are reported for onboarding, because skills and a home site don't exist there.
    `classification_level` isn't synced, since the master has no engine level key.
  - Settings: `AWARD_INTELLIGENCE_URL`, `AWARD_INTELLIGENCE_API_TOKEN`. UI: the Workforce → Employee
    Sync page in the React shell (award-intelligence PR #1).
- **Verified end to end (local, 2026-09-28):** security seed → `sync-matrix` (405/405 cells
  engine-priced, 0 placeholders) → Tier 1 week 1 solved 25/25 → engine exact $8,598.90 vs solver
  estimate $8,365.57, `cost_status=engine_exact`.
