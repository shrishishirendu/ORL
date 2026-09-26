# Wireframe vs real process flow: audit (2026-09-25)

A read-only audit of the 18-screen wireframe canvas ([UI_REDESIGN.md](UI_REDESIGN.md)) against the real
backend behaviour and the award-engine integration ([AWARD_ENGINE_CONTRACT.md](AWARD_ENGINE_CONTRACT.md)).
**Status: findings only; no fixes applied to the canvas yet.**

Mechanical check: there are no broken links, and every screen can be reached from the sidebar. The problems are
in the *process*: missing hand-offs, missing states, and states that contradict the backend.

## Process verdicts

| # | Step | Screen | Verdict | Key issue |
|---|---|---|---|---|
| 0a | Award loaded, engine healthy | AwardInterpretation | GAP | No engine status or pinned commit anywhere |
| 0b | Workers classified (award, level, employment type) | Workers ↔ AwardInterpretation | **WRONG** | The two screens link to each other in a circle and neither has a form. Workers shows an invented "$/h pay basis" |
| 0c | Sites and travel matrix | Shifts & Sites | OK | — |
| 0d | Agreement ingestion (AI edge 1) | AwardExtract | OK / minor | Copy implies accepted AI clauses change costs directly; they must go through the engine owner and a new pin |
| 1a | Import dry-run | DataImport | OK | Needs award columns and an "unclassified new workers" warning |
| 1b | Commit → next step | DataImport | GAP | Dead end. No committed state, no "Sync award costs" or "Solve" CTA |
| 2 | Award costing (placeholder vs engine) | AwardMatrix | **MISSING** | No "Sync from award engine", no unresolved cells with reasons, no link to Solve |
| 3 | Tier 1 solve | RosterSolve | GAP + WRONG | No *solved* state. Shows 2 blockers, but the backend reports only 0-candidate shifts first. The Shift 47 back-to-back "double-booking" example can't happen |
| 4 | Reconciliation (engine exact vs solver estimate) | Planner, Main, PayRun | **MISSING** | Every screen shows the solver objective as "the cost". No `cost_status` |
| 5 | Publish / view roster | RosterPlanner | GAP | No publish state in the backend (several solved rosters per period). No "Dispatch →" link |
| 6 | Tier 2 dispatch | DispatchBoard, RouteDetail | GAP | "Solve route" (Sophie) opens Priya's route. No job states. The failed route has no fix path. No link to Live |
| 7a | Tier 3 in-scope events | ReportDisruption, LiveOps | OK | Add a worker_sick post-submit variant |
| 7b | Escalation → day re-roster → re-dispatch | LiveOps, SolverJobs | **WRONG** | Loop never closes. Hides that the re-roster covers the whole day as a new roster, **doesn't exclude the sick worker**, and doesn't re-dispatch |
| 8 | Timesheets | TimeEntry | **WRONG** | "Send to Pay Run" is live with approvals pending and the period still open |
| 9 | Pay run | PayRun | **WRONG** | Stepper marks steps done that aren't. "Approve calculation" is live with [amount] totals. `/engine/pay-run` isn't in contract v1 |
| 10 | Payslip dispatch | PayRun | OK | Add "period closed" as a blocker |
| 11 | Advisory engines | PayRisk | OK / minor | Relabel "AI ENGINES" to "ADVISORY CHECKS · FLAG ONLY" and add "Acknowledge" |

## Top issues (ranked)

1. **Blocker: the award-engine integration is invisible.** Add the sync action and unresolved cells (AwardMatrix),
   cost-source pre-flight rows (RosterSolve), and one reusable "reconciled cost" widget (engine exact +
   solver estimate + `cost_status` badge) on Planner, Main and PayRun.
2. **Blocker: nowhere to classify workers.** Add an editable award-classification block in the Workers profile.
   Use one consistent demo state (15/15 mapped, matching the security seed).
3. **Blocker: sick → re-roster → re-dispatch loop.** Add completed and failed states for the chained job, a diff
   against the week roster, and "Re-dispatch changed assignments". Tag "exclude sick worker" as Needs API.
4. **Major: pay can run too early.** Gate Send to Pay Run and Approve calculation, and make the stepper honest.
5. **Major: invented per-worker $/h and the solver objective shown as cost.** This breaks the honesty rule. Show the
   engine exact figure, or the placeholder $40/h clearly marked.
6. **Major: failed-solve display contradicts the backend.** Show only the first blockers, with a hint that more may follow.
7. **Major: missing forward hand-offs.** Import→Sync/Solve, Matrix→Solve, Solve→Planner→Dispatch,
   Dispatch→Live, chained job→day roster.
8. **Major: Week 1 data contradictions.** Placeholder shift #74 vs "25/25 covered". The placeholder row count
   doesn't match skill filtering.
9. **Minor: Tier 2 card interactions.** Wrong route opened, no job states, no overrun banner.
10. **Minor: agent-boundary wording.** In AwardExtract, PayRisk, TimeEntry and AwardMatrix. There are no actual violations.

**Security re-theme:** every Plan, Dispatch, Live, Workforce and Pay screen, plus Main, SolverJobs and DataImport,
still shows nursing/cleaning/driving, "Community Centre" sites and $36–50/h rates. Swap to the seed:
- skills `static_guard / mobile_patrol / crowd_control / first_aid / cctv_monitoring`;
- sites Northside Distribution Centre / Southside Retail Park / CBD Office Tower;
- mobile patrol runs as the multi-stop routes;
- levels L1–L5 and the employment-type mix.

## Fix approach

No new screens are needed; everything is a new **state or panel** on an existing screen. See the "New states"
list in the audit discussion: Workers classification form, AwardMatrix sync/unresolved/degraded
states, reconciled-cost widget, RosterSolve solved state, DataImport committed state, chained-job
states, Dispatch card job strip, RouteDetail re-sequenced banner, worker_sick submit state, and gated
TimeEntry/PayRun.

## Backend gaps surfaced (add to Needs API)

- `AwardCostMatrix.reasons` (the reason stored per cell)
- a sync-matrix result summary
- `engine_total_cost` / `cost_status` / `cost_detail` on roster reads
- worker availability for Tier 3 re-rosters (sick worker exclusion)
- a current/published roster per period
- an automatic re-dispatch after an escalation re-roster
- engine pay-run (contract v2)
