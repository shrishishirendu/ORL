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

### Running tests

```bash
pytest
```

## Folder layout

```
app/
  api/                  FastAPI routers (HTTP surface of the service layer)
  core/                 Settings (pydantic-settings) and the async DB engine/session factory
  models/               SQLAlchemy ORM models (declarative Base only for now)
  schemas/              Pydantic request/response schemas
  services/
    rostering/          Tier 1 — batch rostering (OR-Tools CP-SAT)
    dispatch/           Tier 2 — per-shift dispatch/routing (OR-Tools Routing / VRPTW)
    reoptimization/     Tier 3 — event-driven intra-day re-optimization
  workers/              Async task queue workers (arq)
  main.py               FastAPI app entrypoint
tests/                  Test suite
Dockerfile              Multi-stage build for the API service
docker-compose.yml      Postgres, Redis, and the API service
```

No solver logic, domain models, or database migrations are implemented yet — this is the
project scaffold. See ARCHITECTURE.md for what each tier will do.
