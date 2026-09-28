"""Unit tests for shift breaks: fit validation and the engine mapping."""

from __future__ import annotations

from datetime import date, time

import pytest

from app.models.shift import Shift
from app.models.site import Site
from app.services.admin_data.shifts import break_problem
from app.services.award_engine.matrix import to_engine_shift


@pytest.mark.parametrize(
    ("start", "end", "minutes", "at"),
    [
        (time(8), time(16), None, None),  # unknown
        (time(8), time(16), 30, None),  # unpositioned
        (time(8), time(16), 30, time(12)),
        (time(22), time(6), 30, time(2)),  # overnight, break after midnight
        (time(22), time(6), 30, time(5, 30)),  # ends exactly at the finish
        (time(8), time(16), 0, None),
    ],
)
def test_breaks_that_fit(start, end, minutes, at) -> None:
    assert break_problem(start, end, minutes, at) is None


@pytest.mark.parametrize(
    ("start", "end", "minutes", "at", "message"),
    [
        (time(8), time(16), None, time(12), "break_minutes is missing"),
        (time(8), time(16), -5, None, "zero or more"),
        (time(8), time(16), 480, None, "shorter than the shift"),
        (time(8), time(16), 30, time(15, 45), "within the shift"),
        (time(8), time(16), 30, time(7), "within the shift"),
        (time(22), time(6), 30, time(6), "within the shift"),
        (time(8), time(16), 0, time(12), "break_minutes is 0"),
    ],
)
def test_breaks_that_dont_fit(start, end, minutes, at, message) -> None:
    assert message in break_problem(start, end, minutes, at)


def shift(**fields) -> Shift:
    return Shift(
        id=7,
        date=date(2026, 10, 5),
        start_time=time(22),
        end_time=time(6),
        required_skill="guard",
        site_id=1,
        is_multi_stop=False,
        site=Site(code="STH-01", name="South"),
        **fields,
    )


def test_engine_shift_carries_a_known_break() -> None:
    body = to_engine_shift(shift(break_minutes=30, break_start=time(2))).model_dump(mode="json")
    assert body["break_minutes"] == 30
    assert body["break_start"] == "02:00"


def test_engine_shift_sends_zero_and_no_start_when_the_break_is_unknown() -> None:
    body = to_engine_shift(shift()).model_dump(mode="json")
    assert body["break_minutes"] == 0
    assert "break_start" not in body
