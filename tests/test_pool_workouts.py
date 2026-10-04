"""
Synthetic contract and provider-rendering checks; no personal templates.
"""

from copy import deepcopy

import pytest

from garmin_health_data.workouts import (
    validate_definition,
    render_garmin_workout,
    WorkoutDefinitionError,
)
from garmin_health_data.pool_workouts import summarize_pool_definition
from garmin_health_data.workout_publish import preview_workout


def definition(unit="yards", rest="time"):
    return {
        "schema_version": 2,
        "workout": {
            "key": "pool-example-v1",
            "family": "pool-example",
            "version": 1,
            "name": "Pool example",
            "sport": "swimming",
            "pool": {"length": 25, "unit": unit},
            "description": "Easy technique practice.",
            "steps": [
                {
                    "type": "repeat",
                    "count": 3,
                    "steps": [
                        {
                            "type": "interval",
                            "duration": {
                                "type": "distance",
                                "value": 100,
                                "unit": unit,
                            },
                            "swim": {
                                "stroke": "freestyle",
                                "drill": "pull",
                                "equipment": "paddles",
                            },
                            "notes": "Long strokes.",
                        },
                        {
                            "type": "rest",
                            "duration": {
                                "type": rest,
                                "seconds": 120 if rest == "send_off" else 20,
                            },
                        },
                    ],
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "unit,garmin_unit,meters", [("yards", "yard", 274.32), ("meters", "meter", 300)]
)
def test_distance_uses_display_unit_while_total_uses_meters(unit, garmin_unit, meters):
    payload = render_garmin_workout(definition(unit))
    assert payload["sportType"]["sportTypeId"] == 4
    assert payload["poolLength"] == 25
    assert payload["poolLengthUnit"]["unitKey"] == garmin_unit
    assert payload["estimatedDistanceInMeters"] == meters
    repeat = payload["workoutSegments"][0]["workoutSteps"][0]
    assert repeat["numberOfIterations"] == 3
    assert repeat["skipLastRestStep"] is False
    swim, rest = repeat["workoutSteps"]
    assert swim["endConditionValue"] == 100
    assert swim["preferredEndConditionUnit"]["unitKey"] == garmin_unit
    assert swim["strokeType"]["strokeTypeKey"] == "free"
    assert swim["drillType"]["drillTypeKey"] == "pull"
    assert swim["equipmentType"]["equipmentTypeKey"] == "paddles"
    assert swim["description"] == "Long strokes."
    assert rest["endCondition"]["conditionTypeKey"] == "time"
    assert "strokeType" not in rest
    assert "ownerId" not in payload and "workoutId" not in payload


def test_send_off_and_fixed_rest_are_different_provider_conditions():
    value = definition(rest="send_off")
    payload = render_garmin_workout(value)
    rest = payload["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][1]
    assert rest["endCondition"]["conditionTypeId"] == 9
    assert rest["endCondition"]["conditionTypeKey"] == "fixed.repetition"
    assert rest["endConditionValue"] == 120
    summary = summarize_pool_definition(validate_definition(value))
    assert summary["fixed_rest_seconds"] == 0
    assert summary["timed_seconds"] == 0
    assert summary["send_off_cycle_seconds"] == 360


def test_preview_uses_pool_summary_without_writes(tmp_path):
    state = tmp_path / "no-state.json"
    result = preview_workout(definition(), "2026-10-10", str(state), account_id="123")
    assert result["summary"]["distance_display"]["unit"] == "yards"
    assert not state.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w["workout"]["steps"][0]["steps"][0]["duration"].update(value=101),
        lambda w: w["workout"]["steps"][0]["steps"][1].update(
            swim={"stroke": "freestyle"}
        ),
        lambda w: w["workout"]["steps"][0].update(count=99),
        lambda w: w["workout"]["pool"].update(length=float("inf")),
        lambda w: w["workout"].update(sport="open_water"),
        lambda w: w["workout"]["steps"][0]["steps"][0].update(
            target={"type": "heart_rate_zone", "zone": 2}
        ),
    ],
)
def test_rejects_unrepresentable_or_ambiguous_input(mutate):
    value = deepcopy(definition())
    mutate(value)
    with pytest.raises(WorkoutDefinitionError):
        render_garmin_workout(value)
