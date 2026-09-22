"use strict";

/**
 * ORL ops dashboard -- vanilla JS, no build step.
 *
 * Talks to the JSON API mounted at the site root (this page itself is
 * served under /admin/, but every fetch() call below uses an absolute
 * "/..." path so it hits the API regardless of the page's own path).
 */

const state = {
  rosters: [], // cached GET /rosters items, newest-generated first
  currentRosterId: null,
};

// ---------------------------------------------------------------------
// Small fetch helpers
// ---------------------------------------------------------------------

function showGlobalError(message) {
  const el = document.getElementById("global-error");
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearGlobalError() {
  const el = document.getElementById("global-error");
  el.classList.add("hidden");
  el.textContent = "";
}

async function apiGet(path) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (err) {
    throw new Error(`Network error calling ${path}: ${err.message}`);
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body.detail ? `: ${JSON.stringify(body.detail)}` : "";
    } catch (_) {
      /* ignore -- non-JSON error body */
    }
    const err = new Error(`GET ${path} failed (${response.status})${detail}`);
    err.status = response.status;
    throw err;
  }
  return response.json();
}

async function apiPost(path, payload) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    throw new Error(`Network error calling ${path}: ${err.message}`);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail ? `: ${JSON.stringify(body.detail)}` : "";
    const err = new Error(`POST ${path} failed (${response.status})${detail}`);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
}

/** Poll GET {jobsPath}/{job_id} until it reaches a terminal arq status. */
async function pollJob(jobsPath, jobId, { intervalMs = 1000, maxAttempts = 60 } = {}) {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const status = await apiGet(`${jobsPath}/${jobId}`);
    if (status.status === "complete" || status.status === "not_found") {
      return status;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`Timed out waiting for job ${jobId} on ${jobsPath}`);
}

// ---------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------

function fmtDateTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtTime(t) {
  // "08:00:00" -> "08:00"
  return typeof t === "string" ? t.slice(0, 5) : t;
}

function statusBadgeClass(status) {
  const s = (status || "").toLowerCase();
  if (["solved", "resolved", "complete", "ok"].includes(s)) return "ok";
  if (["failed", "escalated", "bad"].includes(s)) return "warn";
  if (["open", "resolving", "pending"].includes(s)) return "neutral";
  return "";
}

function badge(text, cls) {
  const span = document.createElement("span");
  span.className = `badge ${cls || statusBadgeClass(text)}`.trim();
  span.textContent = text;
  return span;
}

function td(content) {
  const cell = document.createElement("td");
  if (content instanceof Node) {
    cell.appendChild(content);
  } else {
    cell.textContent = content == null ? "–" : String(content);
  }
  return cell;
}

// ---------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------

const VIEWS = ["dashboard", "rosters", "route", "events"];

function setView(name, params) {
  if (!VIEWS.includes(name)) name = "dashboard";
  for (const v of VIEWS) {
    document.getElementById(`view-${v}`).classList.toggle("active", v === name);
  }
  for (const btn of document.querySelectorAll("#nav-tabs button")) {
    btn.classList.toggle("active", btn.dataset.view === name);
  }
  clearGlobalError();
  if (name === "dashboard") loadDashboard();
  if (name === "rosters") loadRosters();
  if (name === "route" && params && params.assignmentId) {
    document.getElementById("route-assignment-id").value = params.assignmentId;
    loadRoute(params.assignmentId);
  }
  if (name === "events") {
    if (params && params.shiftId) {
      document.getElementById("events-filter-shift").value = params.shiftId;
    }
    if (params && params.workerId) {
      document.getElementById("event-worker-id").value = params.workerId;
    }
    if (params && params.shiftId) {
      document.getElementById("event-shift-id").value = params.shiftId;
    }
    loadEvents();
  }
}

document.getElementById("nav-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  setView(btn.dataset.view);
});

// ---------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------

function nextMonday(from) {
  const d = new Date(from);
  const day = d.getDay(); // 0=Sun..6=Sat
  const daysAhead = ((8 - day) % 7) || 7; // strictly after today, landing on Monday
  d.setDate(d.getDate() + daysAhead);
  return d;
}

function toDateInputValue(d) {
  // Build the YYYY-MM-DD string from LOCAL date components -- d's Y/M/D
  // were set via local-timezone arithmetic (nextMonday/setDate), so
  // formatting via d.toISOString() (which converts to UTC first) can shift
  // the date by a day for any timezone ahead of UTC. This mirrors what a
  // native <input type="date"> expects: the calendar date the user sees.
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function initSolveForm() {
  const start = nextMonday(new Date());
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  document.getElementById("solve-period-start").value = toDateInputValue(start);
  document.getElementById("solve-period-end").value = toDateInputValue(end);
}

async function loadDashboard() {
  clearGlobalError();
  try {
    const [workers, shifts, rosters] = await Promise.all([
      apiGet("/workers?limit=1"),
      apiGet("/shifts?limit=1"),
      apiGet("/rosters?limit=1"),
    ]);
    document.getElementById("card-workers").textContent = workers.total;
    document.getElementById("card-shifts").textContent = shifts.total;
    document.getElementById("card-rosters").textContent = rosters.total;
    const latest = rosters.items[0];
    const latestEl = document.getElementById("card-latest-roster");
    latestEl.textContent = "";
    if (latest) {
      latestEl.appendChild(badge(latest.status, statusBadgeClass(latest.status)));
    } else {
      latestEl.textContent = "none yet";
    }
  } catch (err) {
    showGlobalError(err.message);
  }
}

document.getElementById("solve-roster-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("solve-roster-btn");
  const statusEl = document.getElementById("solve-roster-status");
  const resultEl = document.getElementById("solve-roster-result");
  resultEl.classList.add("hidden");
  const periodStart = document.getElementById("solve-period-start").value;
  const periodEnd = document.getElementById("solve-period-end").value;
  if (!periodStart || !periodEnd) return;

  btn.disabled = true;
  statusEl.textContent = "Enqueuing solve…";
  try {
    const enqueued = await apiPost("/rostering/solve", {
      period_start: periodStart,
      period_end: periodEnd,
    });
    statusEl.textContent = `Job ${enqueued.job_id} queued -- waiting for the arq worker…`;
    const finalStatus = await pollJob("/rostering/jobs", enqueued.job_id);
    statusEl.textContent = `Job ${enqueued.job_id}: ${finalStatus.status}`;
    resultEl.textContent = JSON.stringify(finalStatus.result ?? finalStatus, null, 2);
    resultEl.classList.remove("hidden");
    await loadDashboard();
  } catch (err) {
    statusEl.textContent = "";
    showGlobalError(err.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------
// Rosters view
// ---------------------------------------------------------------------

async function loadRosters() {
  clearGlobalError();
  try {
    const page = await apiGet("/rosters?limit=100");
    state.rosters = page.items;
    const select = document.getElementById("roster-select");
    select.innerHTML = "";
    if (page.items.length === 0) {
      const opt = document.createElement("option");
      opt.textContent = "No rosters yet";
      select.appendChild(opt);
      document.getElementById("roster-summary").textContent = "";
      renderAssignments(null);
      return;
    }
    for (const roster of page.items) {
      const opt = document.createElement("option");
      opt.value = roster.id;
      opt.textContent = `#${roster.id}  ${roster.period_start} → ${roster.period_end}  (${roster.status})`;
      select.appendChild(opt);
    }
    const targetId = state.currentRosterId || page.items[0].id;
    select.value = targetId;
    await loadRosterDetail(Number(select.value));
  } catch (err) {
    showGlobalError(err.message);
  }
}

document.getElementById("roster-select").addEventListener("change", (e) => {
  loadRosterDetail(Number(e.target.value));
});
document.getElementById("roster-refresh-btn").addEventListener("click", () => loadRosters());

async function loadRosterDetail(rosterId) {
  if (!rosterId) return;
  clearGlobalError();
  state.currentRosterId = rosterId;
  try {
    const roster = await apiGet(`/rosters/${rosterId}`);
    const summaryEl = document.getElementById("roster-summary");
    summaryEl.textContent = "";
    summaryEl.append(
      `Status: `,
      badge(roster.status, statusBadgeClass(roster.status)),
      document.createTextNode(
        `  ·  Total cost: ${roster.total_cost != null ? "$" + roster.total_cost.toFixed(2) : "–"}`
      )
    );
    if (roster.failure_reason) {
      summaryEl.append(document.createTextNode(`  ·  ${roster.failure_reason}`));
    }
    renderAssignments(roster.assignments);
  } catch (err) {
    showGlobalError(err.message);
  }
}

function renderAssignments(assignments) {
  const body = document.getElementById("roster-assignments-body");
  body.innerHTML = "";
  if (!assignments || assignments.length === 0) {
    const row = document.createElement("tr");
    row.appendChild(td("No assignments."));
    body.appendChild(row);
    return;
  }
  for (const a of assignments) {
    const row = document.createElement("tr");
    row.appendChild(td(a.worker.name));
    row.appendChild(td(a.day));
    row.appendChild(
      td(`${a.shift.date} ${fmtTime(a.shift.start_time)}–${fmtTime(a.shift.end_time)}`)
    );
    row.appendChild(td(a.shift.required_skill));
    row.appendChild(td(`${a.shift.site.name} (${a.shift.site.region ?? "–"})`));
    row.appendChild(td(badge(a.shift.is_multi_stop ? "multi-stop" : "single-site")));
    const viewBtn = document.createElement("button");
    viewBtn.className = "link";
    viewBtn.textContent = "View route →";
    viewBtn.addEventListener("click", () => setView("route", { assignmentId: a.id }));
    row.appendChild(td(viewBtn));
    body.appendChild(row);
  }
}

// ---------------------------------------------------------------------
// Route view
// ---------------------------------------------------------------------

document.getElementById("route-picker-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const id = document.getElementById("route-assignment-id").value;
  if (id) loadRoute(Number(id));
});

document.getElementById("route-solve-btn").addEventListener("click", () => {
  const id = document.getElementById("route-assignment-id").value;
  if (id) solveRoute(Number(id));
});

/**
 * POST /dispatch/solve for a roster assignment (Tier 2), poll it to
 * completion, then reload the route view -- the same
 * enqueue-then-poll-then-refresh pattern as the Dashboard's "Solve Roster"
 * button (see pollJob above), just targeting a single assignment instead of
 * a whole period.
 */
async function solveRoute(assignmentId) {
  clearGlobalError();
  const btn = document.getElementById("route-solve-btn");
  const statusEl = document.getElementById("route-solve-status");
  btn.disabled = true;
  statusEl.textContent = "Enqueuing dispatch solve…";
  try {
    const enqueued = await apiPost("/dispatch/solve", { roster_assignment_id: assignmentId });
    statusEl.textContent = `Job ${enqueued.job_id} queued -- waiting for the arq worker…`;
    const finalStatus = await pollJob("/dispatch/jobs", enqueued.job_id);
    statusEl.textContent = `Job ${enqueued.job_id}: ${finalStatus.status}`;
    await loadRoute(assignmentId);
  } catch (err) {
    statusEl.textContent = "";
    showGlobalError(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function loadRoute(assignmentId) {
  clearGlobalError();
  const content = document.getElementById("route-content");
  content.innerHTML = '<p class="muted">Loading…</p>';
  try {
    const data = await apiGet(`/routes/${assignmentId}`);
    content.innerHTML = "";
    if (data.mode === "site_assignment") {
      const p = document.createElement("p");
      p.append(badge("single-site"), document.createTextNode(" -- no routing needed."));
      content.appendChild(p);

      const table = document.createElement("table");
      table.innerHTML = `<thead><tr><th>Site</th><th>Arrival</th><th>Departure</th></tr></thead>`;
      const tbody = document.createElement("tbody");
      const row = document.createElement("tr");
      row.appendChild(td(`${data.site.name} (${data.site.code})`));
      row.appendChild(td(fmtDateTime(data.arrival)));
      row.appendChild(td(fmtDateTime(data.departure)));
      tbody.appendChild(row);
      table.appendChild(tbody);
      content.appendChild(table);
    } else {
      const p = document.createElement("p");
      p.append(
        badge("routed"),
        document.createTextNode(` Route #${data.route_id} -- status: `),
        badge(data.status, statusBadgeClass(data.status))
      );
      content.appendChild(p);

      const table = document.createElement("table");
      table.innerHTML =
        "<thead><tr><th>#</th><th>Site</th><th>Planned arrival</th><th>Planned departure</th></tr></thead>";
      const tbody = document.createElement("tbody");
      if (data.stops.length === 0) {
        const row = document.createElement("tr");
        row.appendChild(td("No stops (infeasible or empty route)."));
        tbody.appendChild(row);
      }
      for (const stop of data.stops) {
        const row = document.createElement("tr");
        row.appendChild(td(stop.sequence_no));
        row.appendChild(td(`${stop.site.name} (${stop.site.code})`));
        row.appendChild(td(fmtDateTime(stop.planned_arrival)));
        row.appendChild(td(fmtDateTime(stop.planned_departure)));
        tbody.appendChild(row);
      }
      table.appendChild(tbody);
      content.appendChild(table);

      const returnP = document.createElement("p");
      returnP.className = "muted";
      returnP.textContent = `Return to home: ${fmtDateTime(data.return_to_home_time)}`;
      content.appendChild(returnP);
    }
  } catch (err) {
    if (err.status === 404) {
      content.innerHTML = "";
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = err.message;
      content.appendChild(p);
      // Not dispatched yet is the expected, common case for a freshly
      // solved roster (Tier 1 assigns workers to shifts; Tier 2 routing is
      // a separate, per-assignment step) -- surface the fix right here
      // instead of making the person go back to the form's button.
      if (/has not been dispatched yet/i.test(err.message)) {
        const solveBtn = document.createElement("button");
        solveBtn.className = "primary";
        solveBtn.textContent = "Solve Route (Tier 2) now";
        solveBtn.addEventListener("click", () => solveRoute(assignmentId));
        content.appendChild(solveBtn);
      }
    } else {
      content.innerHTML = "";
      showGlobalError(err.message);
    }
  }
}

// ---------------------------------------------------------------------
// Events view
// ---------------------------------------------------------------------

document.getElementById("events-filter-form").addEventListener("submit", (e) => {
  e.preventDefault();
  loadEvents();
});

async function loadEvents() {
  clearGlobalError();
  const body = document.getElementById("events-body");
  body.innerHTML = '<tr><td colspan="8" class="muted">Loading…</td></tr>';
  const shiftId = document.getElementById("events-filter-shift").value;
  const resolved = document.getElementById("events-filter-resolved").value;
  const params = new URLSearchParams();
  if (shiftId) params.set("shift_id", shiftId);
  if (resolved) params.set("resolved", resolved);
  params.set("limit", "100");
  try {
    const page = await apiGet(`/events?${params.toString()}`);
    body.innerHTML = "";
    if (page.items.length === 0) {
      const row = document.createElement("tr");
      const cell = td("No events match this filter.");
      cell.colSpan = 8;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const ev of page.items) {
      const row = document.createElement("tr");
      row.appendChild(td(ev.id));
      row.appendChild(td(ev.event_type));
      row.appendChild(td(fmtDateTime(ev.occurred_at)));
      row.appendChild(td(ev.shift_id));
      row.appendChild(td(ev.worker_id));
      row.appendChild(td(ev.route_id));
      row.appendChild(td(badge(ev.status, statusBadgeClass(ev.status))));
      row.appendChild(td(ev.resolution ? badge(ev.resolution, statusBadgeClass(ev.resolution)) : "–"));
      body.appendChild(row);
    }
  } catch (err) {
    body.innerHTML = "";
    showGlobalError(err.message);
  }
}

function updateEventFormFields() {
  const type = document.getElementById("event-type").value;
  const cancelledField = document.getElementById("event-field-cancelled-job");
  const currentSiteField = document.getElementById("event-field-current-site");
  const currentTimeField = document.getElementById("event-field-current-time");
  const plannedTimeField = document.getElementById("event-field-planned-time");

  cancelledField.classList.toggle("hidden", type !== "job_cancelled");
  plannedTimeField.classList.toggle("hidden", type !== "visit_overran");

  // current_site_id/current_time are always-optional for job_cancelled and
  // worker_sick, but required for visit_overran -- see app/schemas/events.py.
  const required = type === "visit_overran";
  document.getElementById("event-current-site-id").required = required;
  document.getElementById("event-current-time").required = required;
  currentSiteField.classList.remove("hidden");
  currentTimeField.classList.remove("hidden");
}

document.getElementById("event-type").addEventListener("change", updateEventFormFields);
updateEventFormFields();

document.getElementById("event-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("event-submit-btn");
  const statusEl = document.getElementById("event-submit-status");
  const resultEl = document.getElementById("event-submit-result");
  resultEl.classList.add("hidden");

  const type = document.getElementById("event-type").value;
  const shiftId = Number(document.getElementById("event-shift-id").value);
  const workerId = Number(document.getElementById("event-worker-id").value);
  const completedRaw = document.getElementById("event-completed-jobs").value.trim();
  const completedJobIds = completedRaw
    ? completedRaw.split(",").map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n))
    : [];

  const payload = { event_type: type, shift_id: shiftId, worker_id: workerId, completed_job_ids: completedJobIds };

  const currentSiteRaw = document.getElementById("event-current-site-id").value;
  const currentTimeRaw = document.getElementById("event-current-time").value;
  const plannedTimeRaw = document.getElementById("event-planned-time").value;
  if (currentSiteRaw) payload.current_site_id = Number(currentSiteRaw);
  if (currentTimeRaw) payload.current_time = new Date(currentTimeRaw).toISOString();
  if (type === "visit_overran" && plannedTimeRaw) {
    payload.planned_time = new Date(plannedTimeRaw).toISOString();
  }
  if (type === "job_cancelled") {
    const cancelledRaw = document.getElementById("event-cancelled-job-id").value;
    if (!cancelledRaw) {
      showGlobalError("job_cancelled requires a cancelled job ID.");
      return;
    }
    payload.cancelled_job_id = Number(cancelledRaw);
  }

  btn.disabled = true;
  statusEl.textContent = "Enqueuing event…";
  try {
    const enqueued = await apiPost("/events", payload);
    statusEl.textContent = `Job ${enqueued.job_id} queued -- waiting for the arq worker…`;
    const finalStatus = await pollJob("/events/jobs", enqueued.job_id);
    statusEl.textContent = `Job ${enqueued.job_id}: ${finalStatus.status}`;
    resultEl.textContent = JSON.stringify(finalStatus.result ?? finalStatus, null, 2);
    resultEl.classList.remove("hidden");
    await loadEvents();
  } catch (err) {
    statusEl.textContent = "";
    showGlobalError(err.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

initSolveForm();
setView("dashboard");
