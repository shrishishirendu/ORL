# ORL UI/UX redesign — status and handoff

Last updated: 2026-09-25. Status: **wireframes approved by the product owner; implementation not started.**

This is the working record of the UI/UX redesign so any Claude session or teammate can pick it
up. Read it before touching `app/web/static/` or proposing UI changes.

## Where things live

| What | Where |
|---|---|
| Approved wireframes (17 clickable screens + shared sidebar) | Claude Design canvas **"ORL — Product Wireframes (full set)"** — https://claude.ai/artifact/FacZqSAdUUmnrpxyX9icJY (private; owner shares it from its Share menu) |
| Design system (tokens: colours, type, spacing, radii) | Claude Design System **"ORL — Operational Resource Logistics"** — https://claude.ai/artifact/2KPs6uHpR3zozJhjW26YNQ |
| Separate earlier concept (not ours — do not overwrite) | https://claude.ai/artifact/LNVhSKP4sJpjC3ZLRVMhm7 — a parallel session's 5-screen "ORL · UI/UX" canvas (Data upload & run, Roster board, Intra-day dispatch, Timesheets → award pay, Nav). Useful reference, especially its timesheet screen. |
| Award Intelligence module reference screenshots | `Claude outputs/*.png` (untracked) |
| Current (pre-redesign) UI | `app/web/static/{index.html,app.js,style.css}`, served at `/admin/` |

## Product direction

ORL and the Award Intelligence module become **one product** with one shell: a dark grouped
sidebar, a light content area, KPI cards, "engine" pages (mono eyebrow + big title + honest
explanation + "Deterministic · explainable" badge) and guided empty states. The product story
is a four-stage loop: **Plan (Tier 1) → Dispatch (Tier 2) → Live (Tier 3) → Pay (award)**,
with Tier 3 escalating back to Tier 1 on infeasibility.

**Colour rule (the core of the visual language):** blue `#2f5fd6` = batch/planning (Tier 1);
orange `#e2661a` (text on white `#b8500f`) = fast/live (Tiers 2 and 3). Never mix them on one
element. Award/Pay uses neutral ink + green/success, never blue or orange. Sidebar `#141b29`,
page `#f4f6f9`, ink `#1b2333`, muted text `#5b6679`. Type: Inter, with JetBrains Mono for eyebrows,
codes and clause references.

## Information architecture (sidebar)

- **Command Centre** — KPIs, the Plan→Dispatch→Live→Pay pipeline, today's route timeline, "Needs attention"
- **Plan · Tier 1** — Roster Planner (workers × days grid), Solve & Coverage (pre-flight, live job, failed-roster diagnostics)
- **Dispatch · Tier 2** — Dispatch Board, Route Detail (schematic round-trip map + stop timeline)
- **Live · Tier 3** — Disruptions (event feed, before/after), Report Disruption (pickers instead of raw IDs)
- **Workforce** — Workers (directory + profile drawer incl. award classification), Shifts & Sites (+ travel times)
- **Award & Pay** — Award Interpretation, AI Award Extract, Pay & Eligibility Matrix (the AwardCostMatrix seam), Time Entry, Pay Run, Pay & Risk Engines (Alerts, Pay Anomalies, Labour Cost, Budget, Fatigue, Compliance)
- **Workspace** — Data Import (3-step wizard with a visual dry-run diff), Solver Activity (job history)

## UX behaviours the backend forces (design around these, don't hide them)

- Every solve is enqueue-then-poll (`POST` → `job_id` → `GET .../jobs/{id}`, status
  `deferred|queued|in_progress|complete|not_found`). Show queued / solving / done states.
- **Tier 1 is all-or-nothing.** A roster covers every shift in the period or fails with **no
  assignments** and a `failure_reason` with one diagnostic line per unfilled shift. The UI shows
  failure plainly, with a fix action per shift.
- Tier 2 routes one roster assignment at a time; single-site shifts skip routing (site assignment).
- Tier 3: `worker_sick` **always** escalates to a Tier 1 re-roster of that day;
  `job_cancelled` / `visit_overran` re-sequence in scope (2s budget) or escalate.
- Bulk upload: workers are a **full replace** (missing active workers are deactivated); shifts are a
  full replace within the period. Always dry-run first; commit needs explicit confirmation.

## Demo story used across all screens (keep consistent)

Sites NTH-01 / STH-01 / CBD-01; 15 seeded workers; period Mon 28 Sep – Sun 18 Oct 2026; "today" =
Tue 29 Sep, 10:40. Week 1 = Roster #12, solved, **$8,358.00**, 25/25 shifts. Week 2 solve failed
(Shift 41 first_aid — candidates ineligible per award; Shift 47 double-booking). Today's events:
Tom Anderson job_cancelled 09:12 (in scope, home 10:30); Priya Sharma visit_overran 10:25 at CBD-01
(absorbed — home 13:45 unchanged, **no pay impact**); Ella Osei worker_sick 10:34 (escalated →
re-roster job). Dev Kapoor +40 min on Mon with no event = the Pay Anomalies example. Pay run is at
step 4 (draft calculation) with 2 timesheet approvals pending.

## Agent boundary (deterministic engines stay deterministic)

Set by the product owner, 2026-09-25. It mirrors AXI-WFM's deterministic-financial-engine boundary.

- **Never agentic:** the Award Interpretation module's calculation path, and the Tier 1 CP-SAT and
  Tier 2/3 VRPTW solves. No agent computes a $ figure that feeds a payslip, and no agent performs
  a solve. The award module's current output is a **contract to preserve**: wrap it, don't rebuild it.
- **Agents are welcome only at these edges:**
  1. **Award rule authoring/maintenance:** draft or update award rules from the award text.
     **Mandatory human review** before anything is codified. The wireframe's *AI Award Extract*
     screen is this: it proposes, a person accepts, and publish stays locked until every candidate is reviewed.
  2. **Natural language → constraints:** translate roster preferences and bans into the solver's
     *existing typed inputs*. The solver still decides.
  3. **Plain-English explanations of solver output**, such as why a worker was excluded or why a
     roster came back infeasible. Read-only; writes nothing back.
  4. **Advisory compliance QA:** cross-check computed pay against award text. It **flags for
     human review only and never auto-corrects.** The *Pay & Risk Engines* screen must stay advisory.
- **UI implications:**
  - Label AI output as a suggestion.
  - Every AI action ends at a human decision.
  - Never show an AI-derived number as a pay or cost figure.
  - Status labels must not imply automatic pay correction. For example, Time Entry says
    "Matched to event", not "Auto-resolved".
- **Not yet in the wireframes (candidates, each needs owner approval):** a "Why?" explainer on
  Solve & Coverage failures and on excluded workers (edge 3), and a roster-preferences input that
  shows the typed constraint it produced before it is saved (edge 2).

## Award data honesty rule

Only these award values are real (from the Award Intelligence module): **MA000016 Security
Services Industry Award 2020** — Security Officer Level 1 base $28.42/hr (cl. 15.1 / Sch B);
casual loading 25% → $35.53/hr (cl. 11); ordinary hours 38 hrs/wk, max 10 hrs/day (cl. 13).
Every other rate, level or clause is shown as `[rate]`, `[amount]`, `cl. [ref]` until the award
engine supplies it. Planned pay costs come from ORL's `AwardCostMatrix` (rate × hours). Never
invent award figures in UI, fixtures or copy.

**Decided 2026-09-25:** the demo is scoped to **MA000016 Security Services (NSW)**, the
best-verified award in the engine. The demo workforce is being re-themed to security officers
(static guarding, mobile patrol, crowd control, first aid, CCTV), and mobile patrol runs are the
Tier 2 multi-stop routes. The wireframes still show the earlier nursing/cleaning demo data, so they
need a data refresh to match.

## Backend gaps the wireframes rely on ("Needs API" tags on the canvas)

Per-assignment `pay_cost` on RosterAssignment; worker min/max hours endpoint; `unfilled_shift_ids`
on `GET /rosters/{id}`; per-shift eligible-worker list + `is_placeholder`; AwardCostMatrix read
endpoint; filters on `/shifts` and `/workers`; `GET /workers/{id}` and `/shifts/{id}`; site
lat/lng in `SiteRead` (+ seed); `GET /travel-matrix` plus travel legs/idle time on routes; job
list per shift; dispatch status per assignment + "dispatch all"; persisted event
detail/escalation reason/escalation job id; route history for before/after; solver job history
(jobs live only in Redis today); optimal-vs-feasible and solve duration; structured upload errors
`{sheet,row,field}`; multi-stop shift/Job creation from the UI. Award & Pay also needs: worker
classification level, timesheets/actuals, leave, pay run and engine-findings APIs (arrives with
the Award Intelligence integration; the `AwardCostMatrix` shape stays the seam — see
ARCHITECTURE.md).

## Next steps

1. ~~Owner decides the award pack~~ → Security (MA000016) chosen; integration build in progress (see AWARD_INTEGRATION.md).
2. Optionally a visual QA pass on the canvas (screens were checked for data and markup, not rendered).
3. Rebuild `app/web/static/` to the wireframes, starting with screens that work on today's API
   (Command Centre, Roster Planner, Solve & Coverage, Dispatch Board, Route Detail, Disruptions,
   Workers, Shifts & Sites, Data Import). Keep it no-build-step unless the team decides otherwise.
   Build the backend gap endpoints in parallel.
