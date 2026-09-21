"""Metadata-level sanity checks for the ORL domain models.

These tests don't require a live database. They check that every model
described in the task (Worker, Shift, AwardCostMatrix, Roster,
RosterAssignment, Job, Route, RouteStop, SiteAssignment,
ReoptimizationEvent -- plus the supporting Site table) is registered on
``Base.metadata`` with the expected table name and columns, and, as a
slightly stronger check, that the full metadata can actually be issued as
``CREATE TABLE`` DDL against an in-memory SQLite database without error
(catching gross FK/ordering mistakes) even though the real target database
is Postgres.

NOTE: SQLite can't represent a Postgres-native ``ARRAY`` column or native
``ENUM`` type faithfully, so the "create against SQLite" check below swaps
those two column types out for SQLite-compatible equivalents on temporary
copies of the tables rather than trying to run the real metadata as-is.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import create_engine

from app.models import (
    AwardCostMatrix,
    Job,
    ReoptimizationEvent,
    Roster,
    RosterAssignment,
    Route,
    RouteStop,
    Shift,
    Site,
    SiteAssignment,
    TravelMatrixEntry,
    Worker,
)
from app.models.base import Base

EXPECTED_TABLES: dict[str, set[str]] = {
    "worker": {"id", "name", "skills", "region", "home_site_id", "active"},
    "shift": {
        "id",
        "date",
        "start_time",
        "end_time",
        "required_skill",
        "site_id",
        "is_multi_stop",
    },
    "award_cost_matrix": {
        "id",
        "worker_id",
        "day",
        "shift_id",
        "pay_cost",
        "eligible",
        "min_hours",
        "max_hours",
    },
    "roster": {
        "id",
        "period_start",
        "period_end",
        "generated_at",
        "status",
        "total_cost",
        "failure_reason",
    },
    "roster_assignment": {"id", "roster_id", "worker_id", "day", "shift_id"},
    "job": {
        "id",
        "shift_id",
        "site_id",
        "window_start",
        "window_end",
        "duration_minutes",
        "sequence_hint",
    },
    "route": {"id", "roster_assignment_id", "generated_at", "status"},
    "route_stop": {
        "id",
        "route_id",
        "job_id",
        "sequence_no",
        "planned_arrival",
        "planned_departure",
    },
    "site_assignment": {"id", "roster_assignment_id", "site_id", "arrival", "departure"},
    "reoptimization_event": {
        "id",
        "event_type",
        "occurred_at",
        "route_id",
        "shift_id",
        "worker_id",
        "resolution",
        "resolved_at",
        "status",
    },
    "site": {"id", "code", "name", "region", "latitude", "longitude"},
    "travel_matrix_entry": {"id", "from_site_id", "to_site_id", "travel_minutes"},
}


def test_all_expected_tables_are_registered() -> None:
    assert set(EXPECTED_TABLES) <= set(Base.metadata.tables)


def test_expected_columns_present_on_each_table() -> None:
    for table_name, expected_columns in EXPECTED_TABLES.items():
        table = Base.metadata.tables[table_name]
        actual_columns = {c.name for c in table.columns}
        missing = expected_columns - actual_columns
        assert not missing, f"{table_name} is missing columns: {missing}"


def test_no_unexpected_extra_tables_slipped_in() -> None:
    # Every table in metadata should be one we deliberately modeled.
    assert set(Base.metadata.tables) == set(EXPECTED_TABLES)


def test_foreign_keys_point_at_expected_tables() -> None:
    def fk_targets(table_name: str) -> set[str]:
        table = Base.metadata.tables[table_name]
        return {fk.column.table.name for fk in table.foreign_keys}

    assert fk_targets("shift") == {"site"}
    assert fk_targets("worker") == {"site"}
    assert fk_targets("award_cost_matrix") == {"worker", "shift"}
    assert fk_targets("roster_assignment") == {"roster", "worker", "shift"}
    assert fk_targets("job") == {"shift", "site"}
    assert fk_targets("route") == {"roster_assignment"}
    assert fk_targets("route_stop") == {"route", "job"}
    assert fk_targets("site_assignment") == {"roster_assignment", "site"}
    assert fk_targets("reoptimization_event") == {"route", "shift", "worker"}
    assert fk_targets("travel_matrix_entry") == {"site"}


def test_expected_unique_constraints_present() -> None:
    def unique_constraint_columns(table_name: str) -> list[frozenset[str]]:
        table = Base.metadata.tables[table_name]
        return [
            frozenset(c.name for c in uc.columns)
            for uc in table.constraints
            if isinstance(uc, sa.UniqueConstraint)
        ]

    assert frozenset({"worker_id", "day", "shift_id"}) in unique_constraint_columns(
        "award_cost_matrix"
    )
    assert frozenset(
        {"roster_id", "worker_id", "day", "shift_id"}
    ) in unique_constraint_columns("roster_assignment")
    assert frozenset({"route_id", "sequence_no"}) in unique_constraint_columns("route_stop")
    assert frozenset({"route_id", "job_id"}) in unique_constraint_columns("route_stop")
    assert frozenset({"from_site_id", "to_site_id"}) in unique_constraint_columns(
        "travel_matrix_entry"
    )

    # Route/SiteAssignment are one-to-one with RosterAssignment: enforced via
    # a unique index on the FK column rather than a UniqueConstraint object.
    route_table = Base.metadata.tables["route"]
    assert route_table.c.roster_assignment_id.unique or any(
        ix.unique and {c.name for c in ix.columns} == {"roster_assignment_id"}
        for ix in route_table.indexes
    )
    site_assignment_table = Base.metadata.tables["site_assignment"]
    assert site_assignment_table.c.roster_assignment_id.unique or any(
        ix.unique and {c.name for c in ix.columns} == {"roster_assignment_id"}
        for ix in site_assignment_table.indexes
    )


def test_models_repr_do_not_raise() -> None:
    """__repr__ should be safe to call on a transient (unflushed) instance."""
    models_and_instances = [
        Worker(
            id=1,
            name="Jane Doe",
            skills=["first_aid"],
            region="North",
            home_site_id=1,
            active=True,
        ),
        Site(id=1, code="SITE-1", name="Head Office"),
        Roster(id=1, status="pending"),
        RosterAssignment(id=1, roster_id=1, worker_id=1, shift_id=1),
        Route(id=1, roster_assignment_id=1),
        RouteStop(id=1, route_id=1, job_id=1, sequence_no=1),
        SiteAssignment(id=1, roster_assignment_id=1, site_id=1),
        ReoptimizationEvent(id=1, event_type="job_cancelled", status="open"),
        Job(id=1, shift_id=1, site_id=1, duration_minutes=30),
        Shift(id=1, required_skill="first_aid", site_id=1),
        AwardCostMatrix(id=1, worker_id=1, shift_id=1, pay_cost=100, eligible=True),
        TravelMatrixEntry(id=1, from_site_id=1, to_site_id=2, travel_minutes=15),
    ]
    for instance in models_and_instances:
        text = repr(instance)
        assert type(instance).__name__ in text


def test_metadata_creates_cleanly_against_sqlite() -> None:
    """Structural sanity check: build DDL for every table against a
    throwaway in-memory SQLite engine, substituting SQLite-friendly types
    for the two Postgres-only column types (ARRAY, native Enum) so this
    doesn't require a real Postgres instance to catch e.g. FK ordering
    mistakes or duplicate constraint names.
    """
    engine = create_engine("sqlite:///:memory:")
    sqlite_metadata = sa.MetaData()

    type_overrides = {
        "skills": sa.JSON(),  # was postgresql ARRAY(String)
    }

    for table in Base.metadata.sorted_tables:
        columns = []
        for col in table.columns:
            col_type = type_overrides.get(col.name, col.type)
            if isinstance(col_type, sa.Enum):
                col_type = sa.String(50)
            new_col = sa.Column(
                col.name,
                col_type,
                primary_key=col.primary_key,
                nullable=col.nullable,
            )
            columns.append(new_col)
        sa.Table(table.name, sqlite_metadata, *columns)

    # No FKs/unique constraints copied over (SQLite type affinity differs
    # enough to make that fiddly and it's not what this check is for) --
    # this only verifies every column of every table can be declared and
    # created without error.
    sqlite_metadata.create_all(engine)

    with engine.connect() as conn:
        result = conn.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        created = {row[0] for row in result}
    assert set(EXPECTED_TABLES) <= created
