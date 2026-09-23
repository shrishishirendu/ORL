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
  sites: [], // cached GET /sites items, for the Manage tab's site dropdowns
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

async function apiPatch(path, payload) {
  let response;
  try {
    response = await fetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    throw new Error(`Network error calling ${path}: ${err.message}`);
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail ? `: ${JSON.stringify(body.detail)}` : "";
    const err = new Error(`PATCH ${path} failed (${response.status})${detail}`);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
}

/**
 * POST multipart/form-data (file uploads + form fields) -- used only by the
 * Manage tab's bulk-upload widget (`POST /admin/data/upload`). `formData`
 * is a plain object of {fieldName: File|string|boolean|undefined}; File
 * values are appended as files, everything else as a form field (skipped
 * if undefined/null so an omitted optional file/field is genuinely absent,
 * not sent as the string "undefined").
 */
async function apiPostForm(path, formData) {
  const body = new FormData();
  for (const [key, value] of Object.entries(formData)) {
    if (value === undefined || value === null || value === "") continue;
    body.append(key, value);
  }
  let response;
  try {
    response = await fetch(path, { method: "POST", headers: { Accept: "application/json" }, body });
  } catch (err) {
    throw new Error(`Network error calling ${path}: ${err.message}`);
  }
  const responseBody = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = responseBody.detail ? `: ${JSON.stringify(responseBody.detail)}` : "";
    const err = new Error(`POST ${path} failed (${response.status})${detail}`);
    err.status = response.status;
    err.body = responseBody;
    throw err;
  }
  return responseBody;
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

const VIEWS = ["dashboard", "rosters", "route", "events", "manage"];

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
  if (name === "manage") loadManage();
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
// Manage view -- Workers/Shifts manual entry + bulk upload.
// ---------------------------------------------------------------------

async function loadManage() {
  clearGlobalError();
  await loadSitesForManage();
  await Promise.all([loadManageWorkers(), loadManageShifts()]);
}

async function loadSitesForManage() {
  try {
    const page = await apiGet("/sites");
    state.sites = page.items;
  } catch (err) {
    showGlobalError(err.message);
    return;
  }
  for (const selectId of ["worker-form-home-site", "shift-form-site"]) {
    const select = document.getElementById(selectId);
    const previousValue = select.value;
    select.innerHTML = "";
    for (const site of state.sites) {
      const opt = document.createElement("option");
      opt.value = site.id;
      opt.textContent = `${site.name} (${site.code})${site.region ? " -- " + site.region : ""}`;
      select.appendChild(opt);
    }
    if (previousValue) select.value = previousValue;
  }
}

// --- Workers: table + add/edit form ---

async function loadManageWorkers() {
  const body = document.getElementById("manage-workers-body");
  body.innerHTML = '<tr><td colspan="8" class="muted">Loading…</td></tr>';
  try {
    const page = await apiGet("/workers?limit=500");
    body.innerHTML = "";
    if (page.items.length === 0) {
      const row = document.createElement("tr");
      const cell = td("No workers yet.");
      cell.colSpan = 8;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const w of page.items) {
      const row = document.createElement("tr");
      row.appendChild(td(w.id));
      row.appendChild(td(w.name));
      row.appendChild(td(w.skills.join(", ")));
      row.appendChild(td(w.region));
      row.appendChild(td(`${w.home_site.name} (${w.home_site.code})`));
      row.appendChild(td(w.employee_code));
      row.appendChild(td(badge(w.active ? "active" : "inactive", w.active ? "ok" : "neutral")));
      const editBtn = document.createElement("button");
      editBtn.className = "link";
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", () => startEditWorker(w));
      row.appendChild(td(editBtn));
      body.appendChild(row);
    }
  } catch (err) {
    body.innerHTML = "";
    showGlobalError(err.message);
  }
}

function resetWorkerForm() {
  document.getElementById("worker-form-id").value = "";
  document.getElementById("worker-form-name").value = "";
  document.getElementById("worker-form-skills").value = "";
  document.getElementById("worker-form-region").value = "";
  document.getElementById("worker-form-employee-code").value = "";
  document.getElementById("worker-form-active").value = "true";
  document.getElementById("worker-form-title").textContent = "Add worker";
  document.getElementById("worker-form-submit-btn").textContent = "Add worker";
  document.getElementById("worker-form-cancel-btn").classList.add("hidden");
}

function startEditWorker(worker) {
  document.getElementById("worker-form-id").value = worker.id;
  document.getElementById("worker-form-name").value = worker.name;
  document.getElementById("worker-form-skills").value = worker.skills.join(", ");
  document.getElementById("worker-form-region").value = worker.region || "";
  document.getElementById("worker-form-home-site").value = worker.home_site.id;
  document.getElementById("worker-form-employee-code").value = worker.employee_code || "";
  document.getElementById("worker-form-active").value = String(worker.active);
  document.getElementById("worker-form-title").textContent = `Edit worker #${worker.id}`;
  document.getElementById("worker-form-submit-btn").textContent = "Save changes";
  document.getElementById("worker-form-cancel-btn").classList.remove("hidden");
  document.getElementById("worker-form-name").scrollIntoView({ behavior: "smooth", block: "center" });
}

document.getElementById("worker-form-cancel-btn").addEventListener("click", () => resetWorkerForm());

document.getElementById("worker-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const statusEl = document.getElementById("worker-form-status");
  const workerId = document.getElementById("worker-form-id").value;
  const skills = document
    .getElementById("worker-form-skills")
    .value.split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const payload = {
    name: document.getElementById("worker-form-name").value.trim(),
    skills,
    region: document.getElementById("worker-form-region").value.trim() || null,
    home_site_id: Number(document.getElementById("worker-form-home-site").value),
    active: document.getElementById("worker-form-active").value === "true",
    employee_code: document.getElementById("worker-form-employee-code").value.trim() || null,
  };

  statusEl.textContent = workerId ? "Saving…" : "Adding…";
  try {
    if (workerId) {
      await apiPatch(`/workers/${workerId}`, payload);
      statusEl.textContent = `Worker #${workerId} updated.`;
    } else {
      const created = await apiPost("/workers", payload);
      statusEl.textContent = `Worker #${created.id} created.`;
    }
    resetWorkerForm();
    await loadManageWorkers();
  } catch (err) {
    statusEl.textContent = "";
    showGlobalError(err.message);
  }
});

// --- Shifts: table + add/edit form ---

function eligibilityBadge(count) {
  return count > 0
    ? badge(`${count} eligible`, "ok")
    : badge("no eligible workers yet", "warn");
}

async function loadManageShifts() {
  const body = document.getElementById("manage-shifts-body");
  body.innerHTML = '<tr><td colspan="8" class="muted">Loading…</td></tr>';
  try {
    const page = await apiGet("/shifts?limit=500");
    body.innerHTML = "";
    if (page.items.length === 0) {
      const row = document.createElement("tr");
      const cell = td("No shifts yet.");
      cell.colSpan = 8;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const s of page.items) {
      const row = document.createElement("tr");
      row.appendChild(td(s.id));
      row.appendChild(td(s.date));
      row.appendChild(td(`${fmtTime(s.start_time)}–${fmtTime(s.end_time)}`));
      row.appendChild(td(s.required_skill));
      row.appendChild(td(`${s.site.name} (${s.site.region ?? "–"})`));
      row.appendChild(td(badge(s.is_multi_stop ? "multi-stop" : "single-site")));
      row.appendChild(td(eligibilityBadge(s.eligible_worker_count)));
      const editBtn = document.createElement("button");
      editBtn.className = "link";
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", () => startEditShift(s));
      row.appendChild(td(editBtn));
      body.appendChild(row);
    }
  } catch (err) {
    body.innerHTML = "";
    showGlobalError(err.message);
  }
}

function resetShiftForm() {
  document.getElementById("shift-form-id").value = "";
  document.getElementById("shift-form-date").value = "";
  document.getElementById("shift-form-start").value = "";
  document.getElementById("shift-form-end").value = "";
  document.getElementById("shift-form-skill").value = "";
  document.getElementById("shift-form-title").textContent = "Add shift";
  document.getElementById("shift-form-submit-btn").textContent = "Add shift";
  document.getElementById("shift-form-cancel-btn").classList.add("hidden");
}

function startEditShift(shift) {
  if (shift.is_multi_stop) {
    showGlobalError(
      "This shift is multi-stop and can't be edited here -- this form only manages single-site shifts."
    );
    return;
  }
  document.getElementById("shift-form-id").value = shift.id;
  document.getElementById("shift-form-date").value = shift.date;
  document.getElementById("shift-form-start").value = fmtTime(shift.start_time);
  document.getElementById("shift-form-end").value = fmtTime(shift.end_time);
  document.getElementById("shift-form-skill").value = shift.required_skill;
  document.getElementById("shift-form-site").value = shift.site.id;
  document.getElementById("shift-form-title").textContent = `Edit shift #${shift.id}`;
  document.getElementById("shift-form-submit-btn").textContent = "Save changes";
  document.getElementById("shift-form-cancel-btn").classList.remove("hidden");
  document.getElementById("shift-form-date").scrollIntoView({ behavior: "smooth", block: "center" });
}

document.getElementById("shift-form-cancel-btn").addEventListener("click", () => {
  resetShiftForm();
  document.getElementById("shift-form-result").classList.add("hidden");
});

document.getElementById("shift-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const statusEl = document.getElementById("shift-form-status");
  const resultEl = document.getElementById("shift-form-result");
  resultEl.classList.add("hidden");
  const shiftId = document.getElementById("shift-form-id").value;
  const payload = {
    date: document.getElementById("shift-form-date").value,
    start_time: document.getElementById("shift-form-start").value,
    end_time: document.getElementById("shift-form-end").value,
    required_skill: document.getElementById("shift-form-skill").value.trim(),
    site_id: Number(document.getElementById("shift-form-site").value),
  };

  statusEl.textContent = shiftId ? "Saving…" : "Adding…";
  try {
    let result;
    if (shiftId) {
      result = await apiPatch(`/shifts/${shiftId}`, payload);
      statusEl.textContent = `Shift #${shiftId} updated.`;
    } else {
      result = await apiPost("/shifts", payload);
      statusEl.textContent = `Shift #${result.shift.id} created.`;
    }
    resultEl.textContent =
      `${result.shift.eligible_worker_count} worker(s) now eligible ` +
      `(${result.placeholder_award_rows_created} placeholder AwardCostMatrix row(s) just generated).`;
    resultEl.classList.remove("hidden");
    resetShiftForm();
    await loadManageShifts();
  } catch (err) {
    statusEl.textContent = "";
    showGlobalError(err.message);
  }
});

// --- Bulk upload (shared by Workers and Shifts -- same endpoint) ---

function renderUploadSummary(result) {
  const lines = [];
  lines.push(result.dry_run ? "PREVIEW (dry run -- nothing was saved)" : "Upload committed.");
  if (result.workers) {
    const w = result.workers;
    lines.push("");
    lines.push(
      `Workers: ${w.created} created, ${w.updated} updated, ${w.deactivated} deactivated`
    );
    lines.push(
      `  matched by employee_code: ${w.matched_by_employee_code}, ` +
        `matched by name: ${w.matched_by_name}`
    );
    if (w.matched_by_name_workers.length > 0) {
      lines.push(
        `  (less reliable) matched by name only: ${w.matched_by_name_workers.join(", ")}`
      );
    }
    if (w.deactivated_workers.length > 0) {
      lines.push(
        `  deactivated: ${w.deactivated_workers.map((x) => `${x.name} (#${x.id})`).join(", ")}`
      );
    }
  }
  if (result.shifts) {
    const s = result.shifts;
    lines.push("");
    lines.push(
      `Shifts (${s.period_start} .. ${s.period_end}): ${s.created} created, ` +
        `${s.deleted} deleted, ${s.placeholder_award_rows_created} placeholder AwardCostMatrix row(s) generated`
    );
    if (s.shifts_skipped.length > 0) {
      lines.push(`  left untouched (${s.shifts_skipped.length}):`);
      for (const skipped of s.shifts_skipped) {
        lines.push(`    #${skipped.id} ${skipped.date} @ ${skipped.site_code} -- ${skipped.reason}`);
      }
    }
  }
  lines.push("");
  lines.push(JSON.stringify(result, null, 2));
  return lines.join("\n");
}

async function runUpload(dryRun) {
  clearGlobalError();
  const statusEl = document.getElementById("upload-status");
  const resultEl = document.getElementById("upload-result");
  resultEl.classList.add("hidden");

  const combinedFile = document.getElementById("upload-combined-file").files[0];
  const workersCsv = document.getElementById("upload-workers-csv").files[0];
  const shiftsCsv = document.getElementById("upload-shifts-csv").files[0];
  const periodStart = document.getElementById("upload-period-start").value;
  const periodEnd = document.getElementById("upload-period-end").value;

  if (!combinedFile && !workersCsv && !shiftsCsv) {
    showGlobalError("Choose a combined .xlsx, a Workers CSV, and/or a Shifts CSV first.");
    return;
  }

  statusEl.textContent = dryRun ? "Validating (dry run)…" : "Uploading…";
  document.getElementById("upload-preview-btn").disabled = true;
  document.getElementById("upload-commit-btn").disabled = true;
  try {
    const result = await apiPostForm("/admin/data/upload", {
      combined_file: combinedFile,
      workers_csv: workersCsv,
      shifts_csv: shiftsCsv,
      period_start: periodStart,
      period_end: periodEnd,
      dry_run: dryRun ? "true" : "false",
    });
    statusEl.textContent = dryRun ? "Preview ready." : "Upload committed.";
    resultEl.textContent = renderUploadSummary(result);
    resultEl.classList.remove("hidden");
    if (!dryRun) {
      await Promise.all([loadManageWorkers(), loadManageShifts()]);
    }
  } catch (err) {
    statusEl.textContent = "";
    if (err.status === 422 && err.body && err.body.detail && err.body.detail.errors) {
      resultEl.textContent = "Validation errors:\n" + err.body.detail.errors.join("\n");
      resultEl.classList.remove("hidden");
    } else {
      showGlobalError(err.message);
    }
  } finally {
    document.getElementById("upload-preview-btn").disabled = false;
    document.getElementById("upload-commit-btn").disabled = false;
  }
}

document.getElementById("upload-preview-btn").addEventListener("click", () => runUpload(true));
document.getElementById("upload-commit-btn").addEventListener("click", () => runUpload(false));

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

initSolveForm();
setView("dashboard");
