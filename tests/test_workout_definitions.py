"""Tests for the public workout definition contract and Garmin renderer."""

import copy

import pytest

from garmin_health_data.workouts import (
    WorkoutDefinitionError,
    calculate_duration,
    definition_hash,
    render_garmin_workout,
    validate_definition,
)


def definition() -> dict:
    return {
        "schema_version": 1,
        "workout": {
            "key": "threshold-2x3-v1",
            "name": "2 x 3 min Threshold",
            "description": "Contract test",
            "sport": "running",
            "steps": [
                {
                    "type": "warmup",
                    "duration": {"type": "time", "seconds": 600},
                    "target": {"type": "heart_rate_zone", "zone": 2},
                },
                {
                    "type": "repeat",
                    "count": 2,
                    "steps": [
                        {
                            "type": "interval",
                            "duration": {"type": "time", "seconds": 180},
                            "target": {
                                "type": "pace",
                                "min_seconds_per_km": 285,
                                "max_seconds_per_km": 300,
                            },
                        },
                        {
                            "type": "recovery",
                            "duration": {"type": "distance", "meters": 200},
                            "target": {"type": "open"},
                        },
                    ],
                },
                {
                    "type": "cooldown",
                    "duration": {"type": "lap_button"},
                    "target": {"type": "heart_rate", "min_bpm": 110, "max_bpm": 140},
                },
            ],
        },
    }


def test_validate_is_strict_and_normalizes() -> None:
    normalized = validate_definition(definition())
    assert normalized["workout"]["key"] == "threshold-2x3-v1"
    assert normalized["workout"]["estimated_duration_seconds"] == 0
    assert definition_hash(normalized) == definition_hash(copy.deepcopy(normalized))

    invalid = definition()
    invalid["workout"]["unexpected"] = True
    with pytest.raises(WorkoutDefinitionError, match="unknown field"):
        validate_definition(invalid)


def test_catalog_family_version_identity_is_preserved() -> None:
    raw = definition()
    raw["workout"].update({"family": "threshold-2x3", "version": 1})
    normalized = validate_definition(raw)
    assert normalized["workout"]["family"] == "threshold-2x3"
    assert normalized["workout"]["version"] == 1

    raw["workout"]["key"] = "wrong-v1"
    with pytest.raises(WorkoutDefinitionError, match="must equal"):
        validate_definition(raw)


def test_duration_calculation_handles_repeats_and_open_steps() -> None:
    steps = validate_definition(definition())["workout"]["steps"]
    assert calculate_duration(steps) is None

    deterministic = copy.deepcopy(definition())
    deterministic["workout"]["steps"] = deterministic["workout"]["steps"][:1]
    assert (
        validate_definition(deterministic)["workout"]["estimated_duration_seconds"]
        == 600
    )


def test_renderer_assigns_global_step_order_and_converts_pace() -> None:
    normalized = validate_definition(definition())
    payload = render_garmin_workout(normalized)
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepOrder"] == 1
    assert steps[1]["type"] == "RepeatGroupDTO"
    assert steps[1]["stepOrder"] == 2
    assert [child["stepOrder"] for child in steps[1]["workoutSteps"]] == [3, 4]
    assert steps[2]["stepOrder"] == 5
    pace = steps[1]["workoutSteps"][0]
    assert pace["targetType"]["workoutTargetTypeKey"] == "speed.zone"
    assert pace["targetValueOne"] == pytest.approx(1000 / 300)
    assert pace["targetValueTwo"] == pytest.approx(1000 / 285)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({"type": "heart_rate", "min_bpm": 160, "max_bpm": 150}, "min_bpm"),
        ({"type": "heart_rate_zone", "zone": 7}, "between 1 and 5"),
        ({"type": "power", "min_watts": 300, "max_watts": 250}, "min_watts"),
    ],
)
def test_invalid_targets_are_rejected(target, message) -> None:
    raw = definition()
    raw["workout"]["steps"][0]["target"] = target
    with pytest.raises(WorkoutDefinitionError, match=message):
        validate_definition(raw)


def test_pace_is_rejected_for_cycling() -> None:
    raw = definition()
    raw["workout"]["sport"] = "cycling"
    with pytest.raises(WorkoutDefinitionError, match="only for running"):
        validate_definition(raw)
