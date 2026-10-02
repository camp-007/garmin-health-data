"""
Pool unit metadata survives processing without an extractor schema change.
"""

import copy

import pytest
from sqlalchemy import select

from garmin_health_data.models import SupplementalActivityMetric, SwimmingAggMetrics
from tests.test_virtual_ride_summary import activity_payload, processor_for


@pytest.mark.parametrize(
    "unit,factor,expected",
    [
        ("meter", 100, 1),
        ("yard", 91.44, 0.9144),
        ("yard", 100, None),
        ("unknown", 91.44, None),
        ("meter", True, None),
        ("meter", None, None),
        ([], 100, None),
    ],
)
def test_recorded_pool_unit_is_preserved_only_when_reviewed(
    db_session, unit, factor, expected
):
    processor = processor_for(db_session)
    payload = activity_payload("lap_swimming")
    payload.update(
        poolLength=2286,
        activeLengths=40,
        strokes=600,
        averageSwimCadenceInStrokesPerMinute=24,
        averageSwolf=35,
        unitOfPoolLength={"unitKey": unit, "factor": factor},
    )
    for _ in range(2):
        processor._process_single_activity(copy.deepcopy(payload), db_session)
        db_session.flush()
    summary = db_session.scalars(select(SwimmingAggMetrics)).one()
    assert (summary.pool_length, summary.active_lengths, summary.strokes) == (
        2286,
        40,
        600,
    )
    assert (summary.avg_swim_cadence, summary.avg_swolf) == (24, 35)
    preserved = db_session.scalars(
        select(SupplementalActivityMetric).where(
            SupplementalActivityMetric.metric == "pool_length_unit_meters"
        )
    ).one()
    assert preserved.value == expected


@pytest.mark.parametrize(
    "next_unit", [None, {}, {"unitKey": "unsupported", "factor": 91.44}]
)
def test_missing_or_unknown_unit_clears_previous_descriptor(db_session, next_unit):
    processor = processor_for(db_session)
    payload = activity_payload("lap_swimming")
    payload.update(
        poolLength=2286, unitOfPoolLength={"unitKey": "yard", "factor": 91.44}
    )
    processor._process_single_activity(copy.deepcopy(payload), db_session)
    payload["unitOfPoolLength"] = next_unit
    processor._process_single_activity(payload, db_session)
    db_session.flush()
    db_session.expire_all()
    assert (
        db_session.scalars(
            select(SupplementalActivityMetric).where(
                SupplementalActivityMetric.metric == "pool_length_unit_meters"
            )
        )
        .one()
        .value
        is None
    )
