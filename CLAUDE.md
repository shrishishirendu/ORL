# ORL — notes for Claude

- Architecture and tier design: [ARCHITECTURE.md](ARCHITECTURE.md). Running and testing: [README.md](README.md).
- **UI/UX redesign in progress:** read [docs/UI_REDESIGN.md](docs/UI_REDESIGN.md) before changing
  `app/web/static/` or discussing the UI. It links the approved wireframes and design system, and
  records the demo story, the colour rule (blue = Tier 1 batch, orange = Tiers 2–3 live) and the
  award-data honesty rule.
- **Agent boundary:** no agent computes a $ figure that feeds a payslip, and no agent performs
  the CP-SAT/VRPTW solve. The award module's calculation output is a contract to preserve: wrap
  it, don't rebuild it. Agents only work at the edges: award-rule drafting with mandatory human
  review, natural language → the solver's existing typed constraints, read-only explanations of
  solver output, and advisory compliance QA that flags and never auto-corrects. Details are in
  docs/UI_REDESIGN.md.
- **Award module integration:** [docs/AWARD_INTEGRATION.md](docs/AWARD_INTEGRATION.md) holds the
  assessment and proposed plan for integrating `C:\dev\award-intelligence` (Node/React, a separate
  repo) via a Node engine service behind the AwardCostMatrix seam. It's a proposal that needs
  owner decisions before building.
- Never invent award rates or clause numbers. Use `[rate]` / `cl. [ref]` placeholders unless the
  value comes from the Award Interpretation engine.
