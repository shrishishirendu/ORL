"""Tier 2 — the pure VRPTW solver.

Per ``ARCHITECTURE.md``'s Tier 2 section, this module solves **one shift's
single worker route**: given the job list for that shift (each a stop with
a site, a time window, and a service duration) and a cached travel-time
matrix between sites, it produces an ordered stop sequence with planned
arrival/departure times, or a clean, fast "infeasible" signal naming the
job(s) that cannot be fit.

This is deliberately a **single-vehicle** VRPTW, not a general multi-vehicle
VRP: ARCHITECTURE.md is explicit that "Tier 2 takes the Roster as fixed
input: it does not reassign which worker is on shift, only how a multi-stop
worker's jobs are ordered and timed within that shift." Each
``RosterAssignment`` (one worker, one shift) gets its own independent solve.
If a future requirement needs to *reassign* jobs across multiple workers
within a shift, that is a materially different (multi-vehicle) problem and
does not belong in this function -- flag that as a design fork rather than
silently generalizing this solver.

FLAG FOR REVIEW -- things ARCHITECTURE.md/the models do not pin down that
this module had to decide on:

- **Travel-time matrix representation**: modeled here as a plain
  ``{(from_site_id, to_site_id): minutes}`` mapping wrapped in
  ``TravelTimeMatrix`` (see below), not as a 2D array, since site ids are
  sparse integers, not a dense contiguous range. A missing entry raises
  ``MissingTravelTimeError`` rather than silently defaulting to 0 --
  defaulting would let a route sequence past an actually-uncached site pair
  and quietly produce a wrong/optimistic schedule.
- **Worker start location**: neither ``Worker`` nor ``RosterAssignment`` has
  a "home site" column, so there is nothing in the schema to anchor where
  the worker is *before* their first stop. ``start_site_id`` is therefore an
  optional caller-supplied parameter (the natural candidate is the shift's
  own ``Shift.site_id``, if that is meant to represent a depot/home site for
  multi-stop shifts) -- when omitted, the solver assumes zero travel time to
  the first stop (the worker is already wherever the route needs to begin).
- **Shift start anchor**: solving needs a zero point for the time axis.
  ``shift_start`` is optional; if omitted, the earliest job's
  ``window_start`` across the job list is used as time zero, but that will
  under-constrain a shift where the worker's official start is later.
  Callers that have ``Shift.date`` + ``Shift.start_time`` should build a
  ``datetime`` from those and pass it in.
- **Objective beyond feasibility**: ARCHITECTURE.md only specifies Tier 2's
  output as "an ordered stop sequence with arrival times", not an
  optimization objective. Feasibility (hitting every time window) is
  primary; among feasible sequences we secondarily minimize the total route
  span (last departure minus shift start), which in practice also minimizes
  idle waiting and backtracking. A different secondary objective (e.g. pure
  travel-time minimization, or lateness-tolerant soft windows) would need
  product input.
- **Search time limit**: ARCHITECTURE.md's only hint is Tier 3 running "on
  the order of seconds". ``time_limit_seconds`` defaults to 5 seconds per
  OR-tools solve; a scoped Tier 3 re-solve over a handful of remaining stops
  should finish well under that.
- **Time granularity**: all datetimes are converted to whole minutes
  (rounded) since ``Job.duration_minutes`` and the assumed travel-matrix
  units are both plain integer minutes -- sub-minute precision is not
  representable in this model.

Nothing in this module touches the database; it is pure input -> output so
it can be unit tested directly (see ``tests/test_dispatch.py``) and reused
unchanged by whatever later wires it to ``RosterAssignment``/``Job`` rows
and to Tier 3's scoped re-solves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# Large but finite: bigger than any realistic single-shift horizon, used as
# a safety fallback when no jobs exist to derive one from.
_DEFAULT_HORIZON_MINUTES = 24 * 60


class MissingTravelTimeError(KeyError):
    """Raised when the travel matrix has no entry for a required site pair.

    Deliberately *not* silently defaulted to 0 -- a missing entry means the
    cached Travel Matrix (a supporting component per ARCHITECTURE.md) is
    incomplete for this shift's sites, and routing through it would produce
    a schedule that looks feasible but is not.
    """

    def __init__(self, from_site_id: int, to_site_id: int) -> None:
        super().__init__(from_site_id, to_site_id)
        self.from_site_id = from_site_id
        self.to_site_id = to_site_id

    def __str__(self) -> str:
        return f"no cached travel time from site {self.from_site_id} to site {self.to_site_id}"


@dataclass(frozen=True)
class TravelTimeMatrix:
    """A cached travel-time lookup between sites, in whole minutes.

    Thin wrapper around a ``{(from_site_id, to_site_id): minutes}`` mapping
    (see the module docstring for why this shape was chosen over a dense
    2D array). Same-site lookups always return 0 without needing an entry.
    """

    minutes: Mapping[tuple[int, int], int]

    def get(self, from_site_id: int, to_site_id: int) -> int:
        if from_site_id == to_site_id:
            return 0
        try:
            return self.minutes[(from_site_id, to_site_id)]
        except KeyError as exc:
            raise MissingTravelTimeError(from_site_id, to_site_id) from exc

    @classmethod
    def from_symmetric_pairs(cls, pairs: Mapping[tuple[int, int], int]) -> TravelTimeMatrix:
        """Build a matrix from one direction's entries, mirroring each pair.

        Convenience for the common case (and for tests) where travel time is
        assumed symmetric; real cached data may not be, hence this is a
        constructor rather than default ``get()`` behaviour.
        """
        full: dict[tuple[int, int], int] = {}
        for (a, b), minutes in pairs.items():
            full[(a, b)] = minutes
            full.setdefault((b, a), minutes)
        return cls(full)


@dataclass(frozen=True)
class JobSpec:
    """One stop to sequence -- the solver-facing shape of a ``Job`` row.

    Deliberately not the ``Job`` ORM model itself, so this module stays free
    of any DB/session dependency; the caller maps ``Job`` rows to this.
    """

    job_id: int
    site_id: int
    window_start: datetime
    window_end: datetime
    duration_minutes: int

    def __post_init__(self) -> None:
        if self.window_end < self.window_start:
            raise ValueError(
                f"job {self.job_id}: window_end ({self.window_end}) is before "
                f"window_start ({self.window_start})"
            )
        if self.duration_minutes < 0:
            raise ValueError(f"job {self.job_id}: duration_minutes must be >= 0")


@dataclass(frozen=True)
class RouteStopPlan:
    """One solved stop: a job placed at a sequence position with planned times.

    Field names deliberately mirror ``RouteStop`` (``sequence_no``,
    ``planned_arrival``, ``planned_departure``) so a caller can persist a
    feasible ``RouteSolveResult`` by mapping each ``RouteStopPlan`` to a
    ``RouteStop`` row with no field-name translation.
    """

    job_id: int
    site_id: int
    sequence_no: int
    planned_arrival: datetime
    planned_departure: datetime


@dataclass(frozen=True)
class RouteSolveResult:
    """The solver's output: either a feasible sequence, or a clean infeasible signal.

    ``infeasible_job_ids`` is always populated (never left for the caller to
    infer) whenever ``feasible`` is False, so Tier 3's "escalate if
    infeasible" behaviour has a fast, clear signal to check without having
    to re-derive it.
    """

    feasible: bool
    stops: list[RouteStopPlan] = field(default_factory=list)
    infeasible_job_ids: list[int] = field(default_factory=list)
    reason: str | None = None


def solve_shift_route(
    jobs: Sequence[JobSpec],
    travel_matrix: TravelTimeMatrix,
    *,
    shift_start: datetime | None = None,
    start_site_id: int | None = None,
    time_limit_seconds: float = 5.0,
) -> RouteSolveResult:
    """Solve a single worker's multi-stop route for one shift (Tier 2 VRPTW).

    Args:
        jobs: the shift's job list (stops to sequence). A 0- or 1-job list
            short-circuits without invoking OR-tools at all -- per
            ARCHITECTURE.md, single-site roles should never reach this
            function in the first place (they take the ``SiteAssignment``
            bypass), but a multi-stop shift can still transiently have 0 or
            1 remaining jobs (e.g. after Tier 3 cancellations), so this is
            handled cheaply rather than raising.
        travel_matrix: cached travel times between the jobs' sites.
        shift_start: the worker's shift start time, used as the route's time
            zero. If omitted, the earliest job's ``window_start`` is used
            instead (see the module docstring's flag on this).
        start_site_id: the site the worker starts the shift at (e.g. the
            shift's home/depot site), used to charge travel time to the
            first stop. If omitted, the first stop is assumed reachable with
            zero travel time (see the module docstring's flag on this).
        time_limit_seconds: OR-tools search time budget per solve attempt.
            An infeasible multi-job case may run this multiple times (once
            per candidate culprit job) to isolate which job(s) block a
            feasible sequence -- see the module docstring.

    Returns:
        A ``RouteSolveResult``. Never raises for an infeasible input; only
        raises (``MissingTravelTimeError``) when the travel matrix itself is
        missing a required site pair, which is a caller data problem, not a
        routing outcome.
    """
    if not jobs:
        return RouteSolveResult(feasible=True, stops=[])

    epoch = shift_start if shift_start is not None else min(j.window_start for j in jobs)

    if len(jobs) == 1:
        return _solve_single_job(jobs[0], epoch, travel_matrix, start_site_id)

    hard_infeasible_ids: list[int] = []
    viable: list[JobSpec] = []
    for job in jobs:
        window_end_offset = _minutes_between(epoch, job.window_end)
        if window_end_offset < 0:
            hard_infeasible_ids.append(job.job_id)
        else:
            viable.append(job)

    if len(viable) <= 1:
        return _finish_reduced_to_at_most_one(
            viable, hard_infeasible_ids, epoch, travel_matrix, start_site_id
        )

    solved_stops = _solve_mandatory_vrptw(
        viable, epoch, travel_matrix, start_site_id, time_limit_seconds
    )
    if solved_stops is not None:
        feasible = not hard_infeasible_ids
        return RouteSolveResult(
            feasible=feasible,
            stops=solved_stops,
            infeasible_job_ids=hard_infeasible_ids,
            reason=(
                None
                if feasible
                else (
                    f"job(s) {hard_infeasible_ids} have windows that end before shift "
                    "start; remaining jobs were routed successfully"
                )
            ),
        )

    # The full set is infeasible. Isolate culprit(s) by trying each viable
    # job's removal in turn -- a job whose removal alone makes the rest
    # solvable is reported as a blocker. This is O(n) extra solves, each
    # over at most n-1 jobs, which is cheap for the small per-shift job
    # counts Tier 2/3 operate on.
    culprits: set[int] = set()
    for i, candidate in enumerate(viable):
        remainder = viable[:i] + viable[i + 1 :]
        if _is_feasible_subset(remainder, epoch, travel_matrix, start_site_id, time_limit_seconds):
            culprits.add(candidate.job_id)

    if culprits:
        ids = sorted(culprits)
        return RouteSolveResult(
            feasible=False,
            infeasible_job_ids=sorted(set(hard_infeasible_ids) | set(ids)),
            reason=(
                f"job(s) {ids} cannot be fit into a common feasible sequence with the "
                "rest of the shift's jobs given travel times between sites"
            ),
        )

    # No single job's removal fixes it: the conflict is joint across
    # multiple jobs. We cannot cheaply name a minimal culprit set, so report
    # everything still in play -- still a clean, fast "no" rather than a
    # guess or an exception.
    all_ids = sorted(set(hard_infeasible_ids) | {j.job_id for j in viable})
    return RouteSolveResult(
        feasible=False,
        infeasible_job_ids=all_ids,
        reason=(
            "no feasible sequence exists for this job set; the conflict involves "
            "multiple jobs jointly (no single job's removal resolves it)"
        ),
    )


def _minutes_between(epoch: datetime, dt: datetime) -> int:
    return round((dt - epoch).total_seconds() / 60)


def _solve_single_job(
    job: JobSpec,
    epoch: datetime,
    travel_matrix: TravelTimeMatrix,
    start_site_id: int | None,
) -> RouteSolveResult:
    stop = _single_stop_plan(job, epoch, travel_matrix, start_site_id)
    if stop is None:
        return RouteSolveResult(
            feasible=False,
            infeasible_job_ids=[job.job_id],
            reason=(
                f"job {job.job_id} cannot be reached within its time window given "
                "travel time from the shift start"
            ),
        )
    return RouteSolveResult(feasible=True, stops=[stop])


def _finish_reduced_to_at_most_one(
    viable: list[JobSpec],
    hard_infeasible_ids: list[int],
    epoch: datetime,
    travel_matrix: TravelTimeMatrix,
    start_site_id: int | None,
) -> RouteSolveResult:
    if not viable:
        return RouteSolveResult(
            feasible=False,
            infeasible_job_ids=hard_infeasible_ids,
            reason="all jobs' time windows end before the shift starts",
        )

    job = viable[0]
    stop = _single_stop_plan(job, epoch, travel_matrix, start_site_id)
    if stop is None:
        return RouteSolveResult(
            feasible=False,
            infeasible_job_ids=sorted({*hard_infeasible_ids, job.job_id}),
            reason=(
                f"job {job.job_id} cannot be reached within its time window given "
                "travel time from the shift start"
            ),
        )
    feasible = not hard_infeasible_ids
    return RouteSolveResult(
        feasible=feasible,
        stops=[stop],
        infeasible_job_ids=hard_infeasible_ids,
        reason=(
            None
            if feasible
            else f"job(s) {hard_infeasible_ids} have windows that end before shift start"
        ),
    )


def _single_stop_plan(
    job: JobSpec,
    epoch: datetime,
    travel_matrix: TravelTimeMatrix,
    start_site_id: int | None,
) -> RouteStopPlan | None:
    window_start_offset = _minutes_between(epoch, job.window_start)
    window_end_offset = _minutes_between(epoch, job.window_end)
    travel_from_start = (
        0 if start_site_id is None else travel_matrix.get(start_site_id, job.site_id)
    )
    arrival_offset = max(0, travel_from_start, window_start_offset)
    if arrival_offset > window_end_offset:
        return None
    departure_offset = arrival_offset + job.duration_minutes
    return RouteStopPlan(
        job_id=job.job_id,
        site_id=job.site_id,
        sequence_no=1,
        planned_arrival=epoch + timedelta(minutes=arrival_offset),
        planned_departure=epoch + timedelta(minutes=departure_offset),
    )


def _is_feasible_subset(
    jobs: list[JobSpec],
    epoch: datetime,
    travel_matrix: TravelTimeMatrix,
    start_site_id: int | None,
    time_limit_seconds: float,
) -> bool:
    if not jobs:
        return True
    if len(jobs) == 1:
        return _single_stop_plan(jobs[0], epoch, travel_matrix, start_site_id) is not None
    solved = _solve_mandatory_vrptw(jobs, epoch, travel_matrix, start_site_id, time_limit_seconds)
    return solved is not None


def _validate_travel_coverage(
    jobs: list[JobSpec],
    travel_matrix: TravelTimeMatrix,
    start_site_id: int | None,
) -> None:
    """Raise ``MissingTravelTimeError`` for any pair the solver might need.

    The VRPTW solver can choose to visit these jobs' sites in any order, so
    every ordered pair of distinct sites among them (plus the start site, if
    given, to each site) must be covered -- not just the pairs a particular
    candidate sequence happens to use.
    """
    sites = {job.site_id for job in jobs}
    if start_site_id is not None:
        for site_id in sites:
            travel_matrix.get(start_site_id, site_id)
    for from_site in sites:
        for to_site in sites:
            if from_site != to_site:
                travel_matrix.get(from_site, to_site)


def _solve_mandatory_vrptw(
    jobs: list[JobSpec],
    epoch: datetime,
    travel_matrix: TravelTimeMatrix,
    start_site_id: int | None,
    time_limit_seconds: float,
) -> list[RouteStopPlan] | None:
    """Single-vehicle VRPTW: every job in ``jobs`` must be visited.

    Returns the ordered stops if a feasible sequence exists, else ``None``.

    Node layout: node 0 is a single depot used as both the route's start and
    end (an "open" route -- no return-to-depot cost is charged, since
    ARCHITECTURE.md's Tier 2 output is just "an ordered stop sequence with
    arrival times", not a round trip). Nodes 1..n are the jobs, in the order
    given by ``jobs``.
    """
    # OR-tools' SWIG/pybind callback bridge does not propagate Python
    # exceptions raised inside a registered transit callback (they are
    # silently swallowed and the model proceeds as if nothing happened), so
    # a missing travel-matrix entry cannot be allowed to surface for the
    # first time from inside `time_callback` below -- it would be lost.
    # Validate full coverage up front instead, where a raise behaves
    # normally.
    _validate_travel_coverage(jobs, travel_matrix, start_site_id)

    n = len(jobs)
    num_nodes = n + 1
    depot = 0

    manager = pywrapcp.RoutingIndexManager(num_nodes, 1, depot)
    routing = pywrapcp.RoutingModel(manager)

    site_by_node = [None] + [job.site_id for job in jobs]
    service_by_node = [0] + [job.duration_minutes for job in jobs]

    def time_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        service = service_by_node[from_node]
        if from_node == depot:
            if start_site_id is None:
                travel = 0
            else:
                travel = travel_matrix.get(start_site_id, site_by_node[to_node])
        elif to_node == depot:
            travel = 0  # open route: no charge for "returning" to the depot
        else:
            travel = travel_matrix.get(site_by_node[from_node], site_by_node[to_node])
        return service + travel

    transit_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_index)

    window_ends = [_minutes_between(epoch, job.window_end) for job in jobs]
    max_travel = max(travel_matrix.minutes.values(), default=0) if travel_matrix.minutes else 0
    horizon = max(window_ends, default=0) + sum(service_by_node) + max_travel * (n + 1) + 1

    routing.AddDimension(transit_index, horizon, horizon, False, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")
    time_dimension.CumulVar(routing.Start(0)).SetRange(0, 0)

    for i, job in enumerate(jobs):
        node = i + 1
        index = manager.NodeToIndex(node)
        window_start_offset = _minutes_between(epoch, job.window_start)
        window_end_offset = _minutes_between(epoch, job.window_end)
        lower_bound = max(0, window_start_offset)
        time_dimension.CumulVar(index).SetRange(lower_bound, window_end_offset)

    # Secondary objective (feasibility is primary and enforced above via the
    # hard time-window bounds): minimize total route span, i.e. finish as
    # early as possible / avoid unnecessary waiting. See the module
    # docstring's flag on this choice.
    time_dimension.SetSpanCostCoefficientForVehicle(1, 0)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.FromMilliseconds(max(1, int(time_limit_seconds * 1000)))

    solution = routing.SolveWithParameters(search_parameters)
    if solution is None:
        return None

    stops: list[RouteStopPlan] = []
    index = routing.Start(0)
    sequence_no = 1
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != depot:
            job = jobs[node - 1]
            arrival_offset = solution.Value(time_dimension.CumulVar(index))
            departure_offset = arrival_offset + job.duration_minutes
            stops.append(
                RouteStopPlan(
                    job_id=job.job_id,
                    site_id=job.site_id,
                    sequence_no=sequence_no,
                    planned_arrival=epoch + timedelta(minutes=arrival_offset),
                    planned_departure=epoch + timedelta(minutes=departure_offset),
                )
            )
            sequence_no += 1
        index = solution.Value(routing.NextVar(index))
    return stops
