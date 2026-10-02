"""
Virtual rides retain recorded cycling summaries through both input paths.
"""

import copy

import pytest
from sqlalchemy import select

from garmin_health_data.models import CyclingAggMetrics, User
from garmin_health_data.processor import GarminProcessor
from garmin_health_data.processor_helpers import FileSet


def activity_payload(sport="virtual_ride"):
    return {
        "activityId": 101,
        "activityType": {"typeId": 1, "typeKey": sport},
        "eventType": {"typeId": 1, "typeKey": "training"},
        "startTimeGMT": "2026-08-10T12:00:00",
        "startTimeLocal": "2026-08-10T07:00:00",
        "duration": 1800.0,
        "distance": 12000.0,
        **{
            key: False
            for key in (
                "parent",
                "purposeful",
                "favorite",
                "pr",
                "hasPolyline",
                "hasImages",
                "hasVideo",
                "hasHeatMap",
                "manualActivity",
                "autoCalcCalories",
            )
        },
    }


def processor_for(session):
    session.add(User(user_id=1, full_name="Synthetic athlete"))
    session.flush()
    processor = GarminProcessor(FileSet(file_paths=[], files={}), session)
    processor.user_id = 1
    return processor


@pytest.mark.parametrize(
    "sport",
    ["virtual_ride", "VIRTUAL_RIDE", "cycling", "road_biking", "indoor_cycling"],
)
@pytest.mark.parametrize("power", [0, 240])
def test_recorded_cycling_summary_and_replay(db_session, sport, power):
    processor = processor_for(db_session)
    payload = activity_payload(sport)
    payload.update(
        avgPower=power,
        maxPower=400,
        normPower=255,
        averageBikingCadenceInRevPerMinute=85,
        maxBikingCadenceInRevPerMinute=110,
        elevationGain=75,
    )
    for _ in range(2):
        processor._process_single_activity(copy.deepcopy(payload), db_session)
        db_session.flush()
    summary = db_session.scalars(select(CyclingAggMetrics)).one()
    assert summary.activity_id == 101
    assert (summary.avg_power, summary.max_power, summary.normalized_power) == (
        power,
        400,
        255,
    )
    assert (
        summary.avg_biking_cadence,
        summary.max_biking_cadence,
        summary.elevation_gain,
    ) == (85, 110, 75)


def test_absent_power_is_not_inferred_from_other_metrics(db_session):
    processor = processor_for(db_session)
    payload = activity_payload()
    payload.update(averageBikingCadenceInRevPerMinute=85, elevationGain=0)
    processor._process_single_activity(payload, db_session)
    db_session.flush()
    summary = db_session.scalars(select(CyclingAggMetrics)).one()
    assert (summary.avg_power, summary.max_power, summary.normalized_power) == (
        None,
        None,
        None,
    )
    assert summary.avg_biking_cadence == 85 and summary.elevation_gain == 0


def test_unknown_virtual_type_does_not_become_cycling(db_session):
    processor = processor_for(db_session)
    payload = activity_payload("virtual_unknown")
    payload.update(avgPower=240, normPower=255)
    processor._process_single_activity(payload, db_session)
    db_session.flush()
    assert db_session.scalars(select(CyclingAggMetrics)).all() == []


def test_virtual_leg_detail_summary_keeps_recorded_values_on_partial_update(db_session):
    processor = processor_for(db_session)
    processor._process_single_activity(activity_payload(), db_session)
    processor._write_multisport_leg_agg(
        101,
        "virtual_ride",
        {
            "averagePower": 240,
            "maxPower": 400,
            "normalizedPower": 255,
            "averageBikeCadence": 85,
            "elevationGain": 75,
        },
        db_session,
    )
    processor._write_multisport_leg_agg(
        101, "virtual_ride", {"maxPower": 420}, db_session
    )
    db_session.flush()
    db_session.expire_all()
    summary = db_session.scalars(select(CyclingAggMetrics)).one()
    assert (summary.avg_power, summary.max_power, summary.normalized_power) == (
        240,
        420,
        255,
    )
    assert summary.avg_biking_cadence == 85 and summary.elevation_gain == 75
