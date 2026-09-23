# ORL — Operational Resource Logistics

ORL is a mobile-workforce rostering and dispatch system. It takes worker pay-and-eligibility
data from an external Award Interpretation Engine and, in three tiers, turns it into an
actual weekly roster, a per-shift dispatch plan for workers who visit multiple sites, and
live re-optimization when a shift is disrupted during the day. Tier 1 (rostering) runs as a
weekly/fortnightly batch job with OR-Tools CP-SAT to minimize total pay cost; Tier 2
(dispatch/routing) runs per shift with OR-Tools Routing (VRPTW) to sequence multi-stop visits,
while single-site roles skip straight to a simple site assignment; Tier 3 reacts to events
(a job cancelled, a worker calling in sick, a visit overrunning) with a fast, scoped re-solve,
escalating back to Tier 1 only if a shift becomes infeasible. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the full design.

## Running locally

### Option A: Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

This starts Postgres, Redis, and the FastAPI service. The API is available at
`http://localhost:8000`, with a health check at `http://localhost:8000/health`.

### Option B: Virtualenv + uvicorn

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

You will still need Postgres and Redis available locally (or via `docker compose up postgres redis`)
and `DATABASE_URL` / `REDIS_URL` set accordingly in `.env`.

### Database migrations (Alembic)

Schema changes are managed with Alembic. With Postgres up and `DATABASE_URL` set (`.env` or
the environment), bring the database to the latest schema:

```bash
alembic upgrade head
```

Run this once after `docker compose up postgres` / `docker compose up --build` for a fresh
database, and again any time you pull in new migrations under `alembic/versions/`.

### Starting the API

```bash
uvicorn app.main:app --reload
```

This serves the FastAPI app at `http://localhost:8000` (`--reload` is for local dev only).
A health check is at `GET /health`.

### Starting the arq worker

The `/rostering/solve`, `/dispatch/solve`, and `/events` endpoints enqueue work rather than
solving inline — an `arq` worker process is what actually picks jobs up and runs the Tier
1/2/3 services against the database. Run it alongside the API:

```bash
arq app.workers.tasks.WorkerSettings
```

### Running tests

```bash
pytest
```

The suite includes real Postgres + Redis end-to-end integration tests under
`tests/integration/` — start Postgres and Redis first (`docker compose up postgres redis`)
and run migrations (`alembic upgrade head`); those tests skip themselves if Postgres isn't
reachable.

### End-to-end example: submitting a rostering solve

With Postgres, Redis, the API (`uvicorn app.main:app --reload`), and a worker
(`arq app.workers.tasks.WorkerSettings`) all running, enqueue a Tier 1 batch solve for a
rostering period:

```bash
curl -X POST http://localhost:8000/rostering/solve \
  -H "Content-Type: application/json" \
  -d '{"period_start": "2026-09-28", "period_end": "2026-10-04"}'
# => {"job_id": "..."}
```

Poll the returned `job_id` for its status/result once the worker has picked it up:

```bash
curl http://localhost:8000/rostering/jobs/<job_id>
# => {"job_id": "...", "status": "complete", "success": true, "result": {"roster_id": ..., "status": "optimal", ...}}
```

`POST /dispatch/solve` (Tier 2, per-shift routing) and `POST /events` (Tier 3, event-driven
re-optimization — `job_cancelled` / `worker_sick` / `visit_overran`) follow the same
enqueue-then-poll pattern, at `GET /dispatch/jobs/{job_id}` and `GET /events/jobs/{job_id}`
respectively. See [ARCHITECTURE.md](ARCHITECTURE.md#api-surface-and-job-polling) for the
full request/response shapes.

## Folder layout

```
app/
  api/                  FastAPI routers (HTTP surface of the service layer)
  core/                 Settings (pydantic-settings) and the async DB engine/session factory
  models/               SQLAlchemy ORM models (Roster, Route, Shift, Worker, Site, ...)
  schemas/              Pydantic request/response schemas
  services/
    rostering/          Tier 1 — batch rostering (OR-Tools CP-SAT)
    dispatch/           Tier 2 — per-shift dispatch/routing (OR-Tools Routing / VRPTW)
    reoptimization/     Tier 3 — event-driven intra-day re-optimization
    admin_data/         Admin data-entry: Worker/Shift manual entry + bulk upload,
                         plus the placeholder AwardCostMatrix bridge (see ARCHITECTURE.md)
  workers/              Async task queue workers (arq)
  web/static/           Static (no-build-step) admin dashboard, served at /admin/
  main.py               FastAPI app entrypoint
alembic/                Database migrations (see "Database migrations" above)
tests/                  Test suite (tests/integration/ needs live Postgres/Redis)
Dockerfile              Multi-stage build for the API service
docker-compose.yml      Postgres, Redis, and the API service
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full tier design, the API surface, and the
job-status polling pattern used by all three `/solve`/`/events` endpoints.
