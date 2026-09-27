"""Populate ``AwardCostMatrix`` from the Award Engine service.

This is the "how the table gets filled" half of ARCHITECTURE.md's
"AwardCostMatrix boundary": for a period, every active worker that has
award fields is priced against every shift in the period via
``POST /engine/cost-matrix``, and each returned cell is upserted as an
``AwardCostMatrix`` row with ``is_placeholder=False``. Tier 1 is not
touched; it keeps reading the table exactly as before.

**Cell semantics.** Each cell is the engine's *marginal* cost of adding the
shift to the worker's baseline week (docs/AWARD_INTEGRATION.md, "cost
depends on the whole week"). The **baseline** is the worker's assignments
in the most recently generated ``SOLVED`` roster whose period overlaps
``period_start..period_end``, restricted to shifts dated inside the period;
with no such roster it is an empty week. (The engine prices a shift that is
already in the baseline against the *rest* of the week, so re-syncing after
a solve is the "fixed-point pass" of the integration plan, not a double
count.)

**Honest gaps.** An ``unresolved`` cell becomes ``eligible=False`` with
``pay_cost=0`` -- ``pay_cost`` is ``NOT NULL`` on the table, and since the row
is ineligible Tier 1 can never pick it, so the 0 is never used as a price.
The engine's reasons are returned in the sync summary (the table has no
column for them). A resolved cell the engine marks ``eligible=false`` (a
shift-level hard limit, e.g. maximum shift length) is written as-is.

**Skill/region filtering is not done here.** Every award worker is priced
against every shift (the contract's default ``pairs``); Tier 1's own
skill/region eligibility filter still decides who may work what, exactly as
today. That costs some engine work on pairs Tier 1 will discard, in exchange
for not duplicating Tier 1's eligibility rules in a second place.

**Mixing policy (engine rows vs placeholder rows).** The contract says ORL
must never *silently* mix engine and placeholder costs in one solve. What
this module does:

- Workers with award fields: all of their rows in the period are replaced
  by engine rows -- including any existing placeholder row, and any
  hand-authored demo row -- so a priced worker's rows are never a mix.
- Workers without award fields (or failing pre-flight, below) are left
  alone: their placeholder rows stay, and they are listed in the summary
  (``workers_without_award_fields`` / ``workers_invalid_award_fields``),
  with ``cost_basis`` = ``"mixed"`` whenever both kinds now exist.
- The mix is then flagged, never hidden, on each roster solved from it:
  ``app/services/award_engine/reconcile.py`` sets
  ``Roster.cost_status = placeholder_estimate`` whenever the solved roster
  assigns a worker the engine cannot price, and records which workers.

**Pre-flight validation.** The engine answers 422 for the *whole request*
if any worker carries a non-null but invalid award field (unknown level key,
part_time without its hours -- contract v1.1 "Null vs invalid"). So one bad
worker record would block pricing for everyone. Before building requests,
ORL fetches ``GET /engine/awards`` and runs ``award_field_problems`` -- a
shape check against the engine's own published level keys, not an award
rule -- and leaves failing workers out of the request (their rows are
untouched, and they are reported in ``workers_invalid_award_fields``).

**Release-blocking gaps** (v1.1) don't block pricing: a resolved cell's
cost is written as-is, and the gaps are counted and listed (distinct,
capped) in the summary so they are shown rather than hidden.

If the engine is unavailable the whole sync fails *before* any row is
written (all engine calls complete before the first upsert), so the table is
left exactly as it was -- placeholders included -- never half-engine,
half-placeholder for the same worker.

This module never commits; the caller owns the transaction (see
``app/workers/tasks.py``'s ``sync_award_matrix_task``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as date_
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.award_cost_matrix import AwardCostMatrix
from app.models.enums import RosterStatus
from app.models.roster import Roster, RosterAssignment
from app.models.shift import Shift
from app.models.worker import Worker
from app.services.award_engine.client import (
    AwardEngineClient,
    CostMatrixRequest,
    CostMatrixResponse,
    EngineContext,
    EngineShift,
    EngineWorker,
    context_from_settings,
)

# ORL's ``Shift`` has no break column, so every shift is sent with
# ``break_minutes=0`` (the contract requires the field). FLAG FOR REVIEW:
# an unpaid break ORL doesn't know about makes the engine's figure an
# over-estimate for that shift; the fix is a ``Shift.break_minutes`` column,
# not a guessed default here.
DEFAULT_BREAK_MINUTES = 0

# Upper bound on worker x shift cells per ``cost-matrix`` request. The
# engine service refuses requests above its own limit (``ENGINE_MAX_CELLS``,
# 20000 by default); requests are split by worker to stay under this.
MAX_CELLS_PER_REQUEST = 20000

# Upsert batch size: 8 bound parameters per row, well under asyncpg's
# 32767-parameter limit.
_UPSERT_BATCH = 1000

# Caps on how much detail the summary echoes back.
_UNRESOLVED_SAMPLE_LIMIT = 50
_RELEASE_GAP_LIMIT = 20


def has_award_fields(worker: Worker) -> bool:
    """Whether the engine can be asked to price ``worker`` at all: the three
    fields the contract's Worker shape cannot do without. Whether their
    *values* are acceptable is ``award_field_problems``' job.
    """
    return bool(worker.award_code and worker.classification_level and worker.employment_type)


def award_field_problems(
    worker: Worker, level_keys_by_award: Mapping[str, frozenset[str]]
) -> list[str]:
    """Reasons the engine would 422 a request carrying ``worker`` (which must
    satisfy ``has_award_fields``) -- the engine service's own worker
    validation (``engine-service/src/validate.js``), mirrored as a pure
    shape check against ``GET /engine/awards`` so one bad record can't block
    a whole request. Empty list = OK to send.
    """
    problems: list[str] = []
    levels = level_keys_by_award.get(worker.award_code)
    if levels is None:
        problems.append(f"award_code {worker.award_code!r} is not offered by the engine")
    elif worker.classification_level not in levels:
        problems.append(
            f"classification_level {worker.classification_level!r} is not a level key of "
            f"{worker.award_code}"
        )
    for name in ("over_award_rate", "ordinary_hours_per_week", "agreed_ordinary_hours_per_shift"):
        value = getattr(worker, name)
        if value is not None and not value > 0:
            problems.append(f"{name} must be greater than 0")
    if str(worker.employment_type) == "part_time":
        if worker.ordinary_hours_per_week is None:
            problems.append("part_time requires ordinary_hours_per_week")
        if worker.agreed_ordinary_hours_per_shift is None:
            problems.append("part_time requires agreed_ordinary_hours_per_shift")
    return problems


async def fetch_level_keys(client: AwardEngineClient) -> dict[str, frozenset[str]]:
    """``GET /engine/awards`` -> ``{award_code: {level keys}}``."""
    awards = await client.awards()
    return {a.code: frozenset(level.key for level in a.levels) for a in awards.awards}


def to_engine_worker(worker: Worker) -> EngineWorker:
    """Map an ORL ``Worker`` (which must satisfy ``has_award_fields``) to the
    contract's Worker shape. ``roster_cycle_weeks=1`` / ``roster_cycle_start
    =None``: ORL has no roster-cycle data; the contract's own example values.
    """
    return EngineWorker(
        worker_id=worker.id,
        name=worker.name,
        award_code=worker.award_code,
        classification_level=worker.classification_level,
        employment_type=str(worker.employment_type),
        over_award_rate=worker.over_award_rate,
        ordinary_hours_per_week=worker.ordinary_hours_per_week,
        agreed_ordinary_hours_per_shift=worker.agreed_ordinary_hours_per_shift,
    )


def to_engine_shift(shift: Shift) -> EngineShift:
    """Map an ORL ``Shift`` (with ``.site`` loaded) to the contract's Shift shape."""
    return EngineShift(
        shift_id=shift.id,
        date=shift.date,
        start_time=shift.start_time.strftime("%H:%M"),
        end_time=shift.end_time.strftime("%H:%M"),
        break_minutes=DEFAULT_BREAK_MINUTES,
        site_code=shift.site.code,
    )


def shift_sort_key(shift: Shift) -> tuple[date_, Any, int]:
    """Chronological, then by id -- the one ordering every engine payload uses."""
    return (shift.date, shift.start_time, shift.id)


def build_cost_matrix_requests(
    workers: Sequence[Worker],
    shifts: Sequence[Shift],
    baseline: dict[int, list[Shift]],
    context: EngineContext,
    *,
    max_cells: int = MAX_CELLS_PER_REQUEST,
) -> list[CostMatrixRequest]:
    """Deterministically build the ``cost-matrix`` request(s) for a sync.

    One request per ``award_code`` (the contract's request carries a single
    ``award_code``), in sorted code order; within a code, workers are split
    into consecutive id-ordered chunks so no request exceeds ``max_cells``.
    Workers are ordered by id, shifts (and each baseline) chronologically,
    and baseline keys ascending -- so the same DB state always yields
    byte-identical request bodies, which is what lets the engine's own
    determinism guarantee carry through end to end.
    """
    if not shifts:
        return []
    ordered_shifts = sorted(shifts, key=shift_sort_key)
    engine_shifts = [to_engine_shift(s) for s in ordered_shifts]
    per_request = max(1, max_cells // len(engine_shifts))

    by_code: dict[str, list[Worker]] = defaultdict(list)
    for worker in workers:
        if has_award_fields(worker):
            by_code[worker.award_code].append(worker)

    requests: list[CostMatrixRequest] = []
    for award_code in sorted(by_code):
        code_workers = sorted(by_code[award_code], key=lambda w: w.id)
        for start in range(0, len(code_workers), per_request):
            chunk = code_workers[start : start + per_request]
            chunk_baseline = {
                str(w.id): [to_engine_shift(s) for s in sorted(baseline[w.id], key=shift_sort_key)]
                for w in chunk
                if baseline.get(w.id)
            }
            requests.append(
                CostMatrixRequest(
                    award_code=award_code,
                    context=context,
                    workers=[to_engine_worker(w) for w in chunk],
                    shifts=engine_shifts,
                    baseline=chunk_baseline,
                )
            )
    return requests


@dataclass(frozen=True)
class MatrixRowValues:
    """One ``AwardCostMatrix`` row's worth of values, straight from an engine cell."""

    worker_id: int
    day: date_
    shift_id: int
    pay_cost: Decimal
    eligible: bool
    min_hours: Decimal | None
    max_hours: Decimal | None
    resolved: bool
    reasons: tuple[str, ...]
    release_gaps: tuple[str, ...] = ()

    def as_insert_values(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "day": self.day,
            "shift_id": self.shift_id,
            "pay_cost": self.pay_cost,
            "eligible": self.eligible,
            "min_hours": self.min_hours,
            "max_hours": self.max_hours,
            "is_placeholder": False,
        }


def rows_from_response(response: CostMatrixResponse) -> list[MatrixRowValues]:
    """Engine cells -> row values, with no arithmetic on any figure.

    ``unresolved`` cells (``pay_cost: null``) are forced to
    ``eligible=False, pay_cost=0`` -- see module docstring, "Honest gaps".
    Output is sorted by (worker_id, shift_id), the contract's own order.
    """
    rows: list[MatrixRowValues] = []
    for cell in response.rows:
        resolved = cell.status == "resolved" and cell.pay_cost is not None
        rows.append(
            MatrixRowValues(
                worker_id=cell.worker_id,
                day=cell.day,
                shift_id=cell.shift_id,
                pay_cost=cell.pay_cost if resolved else Decimal("0"),
                eligible=cell.eligible if resolved else False,
                min_hours=cell.min_hours,
                max_hours=cell.max_hours,
                resolved=resolved,
                reasons=tuple(cell.reasons),
                release_gaps=tuple(str(g) for g in cell.release_blocking_gaps),
            )
        )
    rows.sort(key=lambda r: (r.worker_id, r.shift_id))
    return rows


@dataclass
class MatrixSyncSummary:
    """What ``populate_award_cost_matrix`` reports back (JSON-safe via ``as_dict``)."""

    period_start: date_
    period_end: date_
    rows_written: int = 0
    resolved_count: int = 0
    unresolved_count: int = 0
    # Resolved cells the engine itself marked ineligible (hard limit breached).
    engine_ineligible_count: int = 0
    engine_commit: str | None = None
    instrument_versions: list[str] = field(default_factory=list)
    baseline_roster_id: int | None = None
    workers_priced: list[int] = field(default_factory=list)
    workers_without_award_fields: list[int] = field(default_factory=list)
    # Workers left out by pre-flight validation: [{"worker_id", "problems"}].
    workers_invalid_award_fields: list[dict[str, Any]] = field(default_factory=list)
    # Resolved cells that carry v1.1 release-blocking gaps, and the distinct gaps.
    release_gap_cell_count: int = 0
    release_gaps: list[str] = field(default_factory=list)
    # Placeholder rows still present in the period after the sync.
    placeholder_rows_remaining: int = 0
    # "engine" | "mixed" | "placeholder" -- see module docstring.
    cost_basis: str = "placeholder"
    unresolved_samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "rows_written": self.rows_written,
            "resolved_count": self.resolved_count,
            "unresolved_count": self.unresolved_count,
            "engine_ineligible_count": self.engine_ineligible_count,
            "engine_commit": self.engine_commit,
            "instrument_versions": self.instrument_versions,
            "baseline_roster_id": self.baseline_roster_id,
            "workers_priced": self.workers_priced,
            "workers_without_award_fields": self.workers_without_award_fields,
            "workers_invalid_award_fields": self.workers_invalid_award_fields,
            "release_gap_cell_count": self.release_gap_cell_count,
            "release_gaps": self.release_gaps,
            "placeholder_rows_remaining": self.placeholder_rows_remaining,
            "cost_basis": self.cost_basis,
            "unresolved_samples": self.unresolved_samples,
        }


async def load_baseline(
    session: AsyncSession, period_start: date_, period_end: date_, worker_ids: Sequence[int]
) -> tuple[int | None, dict[int, list[Shift]]]:
    """The baseline week for each worker (see module docstring) and the id of
    the roster it came from (``None`` when there is none -> empty baselines).
    """
    roster_id = (
        await session.execute(
            select(Roster.id)
            .where(
                Roster.status == RosterStatus.SOLVED,
                Roster.period_start <= period_end,
                Roster.period_end >= period_start,
            )
            .order_by(Roster.generated_at.desc(), Roster.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    baseline: dict[int, list[Shift]] = {}
    if roster_id is None or not worker_ids:
        return roster_id, baseline

    assignments = (
        (
            await session.execute(
                select(RosterAssignment)
                .options(selectinload(RosterAssignment.shift).selectinload(Shift.site))
                .where(
                    RosterAssignment.roster_id == roster_id,
                    RosterAssignment.worker_id.in_(worker_ids),
                    RosterAssignment.day >= period_start,
                    RosterAssignment.day <= period_end,
                )
            )
        )
        .scalars()
        .all()
    )
    for assignment in assignments:
        baseline.setdefault(assignment.worker_id, []).append(assignment.shift)
    for shifts in baseline.values():
        shifts.sort(key=shift_sort_key)
    return roster_id, baseline


async def _upsert_rows(session: AsyncSession, rows: Sequence[MatrixRowValues]) -> None:
    for start in range(0, len(rows), _UPSERT_BATCH):
        batch = [r.as_insert_values() for r in rows[start : start + _UPSERT_BATCH]]
        stmt = pg_insert(AwardCostMatrix).values(batch)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_award_cost_matrix_key",
            set_={
                "pay_cost": stmt.excluded.pay_cost,
                "eligible": stmt.excluded.eligible,
                "min_hours": stmt.excluded.min_hours,
                "max_hours": stmt.excluded.max_hours,
                "is_placeholder": False,
            },
        )
        await session.execute(stmt)


async def populate_award_cost_matrix(
    session: AsyncSession,
    period_start: date_,
    period_end: date_,
    client: AwardEngineClient,
    *,
    context: EngineContext | None = None,
    max_cells: int = MAX_CELLS_PER_REQUEST,
) -> MatrixSyncSummary:
    """Price ``period_start..period_end`` with the engine and upsert the rows.

    Raises ``AwardEngineError`` (from ``client.py``) if any engine call
    fails -- before anything is written. Does not commit.
    """
    context = context or context_from_settings()
    summary = MatrixSyncSummary(period_start=period_start, period_end=period_end)

    workers = (
        (await session.execute(select(Worker).where(Worker.active.is_(True)).order_by(Worker.id)))
        .scalars()
        .all()
    )
    candidates = [w for w in workers if has_award_fields(w)]
    summary.workers_without_award_fields = [w.id for w in workers if not has_award_fields(w)]

    shifts = (
        (
            await session.execute(
                select(Shift)
                .options(selectinload(Shift.site))
                .where(Shift.date >= period_start, Shift.date <= period_end)
            )
        )
        .scalars()
        .all()
    )

    priced: list[Worker] = []
    if candidates and shifts:
        level_keys = await fetch_level_keys(client)
        for worker in candidates:
            problems = award_field_problems(worker, level_keys)
            if problems:
                summary.workers_invalid_award_fields.append(
                    {"worker_id": worker.id, "problems": problems}
                )
            else:
                priced.append(worker)
    summary.workers_priced = [w.id for w in priced]

    summary.baseline_roster_id, baseline = await load_baseline(
        session, period_start, period_end, [w.id for w in priced]
    )
    requests = build_cost_matrix_requests(priced, shifts, baseline, context, max_cells=max_cells)

    # Every engine call happens before the first write (module docstring).
    responses = [await client.cost_matrix(request) for request in requests]

    rows: list[MatrixRowValues] = []
    commits: set[str] = set()
    versions: set[str] = set()
    for response in responses:
        rows.extend(rows_from_response(response))
        commits.add(response.engine_commit)
        versions.update(response.instrument_versions)
    rows.sort(key=lambda r: (r.worker_id, r.shift_id))

    if rows:
        await _upsert_rows(session, rows)
        await session.flush()

    summary.rows_written = len(rows)
    summary.resolved_count = sum(1 for r in rows if r.resolved)
    summary.unresolved_count = len(rows) - summary.resolved_count
    summary.engine_ineligible_count = sum(1 for r in rows if r.resolved and not r.eligible)
    # One commit per sync in practice (one engine process); if requests were
    # somehow served by different commits, say so rather than pick one.
    summary.engine_commit = ",".join(sorted(commits)) if commits else None
    summary.instrument_versions = sorted(versions)
    summary.unresolved_samples = [
        {"worker_id": r.worker_id, "shift_id": r.shift_id, "reasons": list(r.reasons)}
        for r in rows
        if not r.resolved
    ][:_UNRESOLVED_SAMPLE_LIMIT]
    gap_rows = [r for r in rows if r.release_gaps]
    summary.release_gap_cell_count = len(gap_rows)
    summary.release_gaps = sorted({g for r in gap_rows for g in r.release_gaps})[
        :_RELEASE_GAP_LIMIT
    ]

    summary.placeholder_rows_remaining = (
        await session.execute(
            select(func.count())
            .select_from(AwardCostMatrix)
            .where(
                AwardCostMatrix.day >= period_start,
                AwardCostMatrix.day <= period_end,
                AwardCostMatrix.is_placeholder.is_(True),
            )
        )
    ).scalar_one()
    only_engine = (
        summary.placeholder_rows_remaining == 0
        and not summary.workers_without_award_fields
        and not summary.workers_invalid_award_fields
    )
    if rows and only_engine:
        summary.cost_basis = "engine"
    elif rows:
        summary.cost_basis = "mixed"
    else:
        summary.cost_basis = "placeholder"
    return summary
