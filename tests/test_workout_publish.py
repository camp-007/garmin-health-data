"""Tests for receipt-backed idempotent workout publishing."""

from unittest.mock import MagicMock

import pytest

from garmin_health_data.workout_publish import WorkoutPublishError, publish_workout
from garmin_health_data.workout_state import load_state


def definition(seconds: int = 300) -> dict:
    return {
        "schema_version": 1,
        "workout": {
            "key": "idempotent-poc",
            "name": "Idempotent POC",
            "sport": "running",
            "steps": [
                {
                    "type": "interval",
                    "duration": {"type": "time", "seconds": seconds},
                    "target": {"type": "open"},
                }
            ],
        },
    }


def client() -> MagicMock:
    result = MagicMock()
    result.account_id = "95016052"
    result.upload_workout.return_value = {"workoutId": 42}
    result.get_workout_by_id.return_value = {"workoutId": 42}
    result.schedule_workout.return_value = {"workoutScheduleId": 99}
    result.get_scheduled_workout_by_id.return_value = {
        "workoutScheduleId": 99,
        "calendarDate": "2026-08-22",
        "workout": {"workoutId": 42},
    }
    return result


def test_publish_creates_then_reuses_without_duplicates(tmp_path) -> None:
    state_path = str(tmp_path / "receipts.json")
    garmin = client()

    first = publish_workout(garmin, definition(), "2026-08-22", state_path)
    second = publish_workout(garmin, definition(), "2026-08-22", state_path)

    assert first["created"] is True
    assert first["scheduled"] is True
    assert second["created"] is False
    assert second["scheduled"] is False
    garmin.upload_workout.assert_called_once()
    garmin.schedule_workout.assert_called_once_with(42, "2026-08-22")
    state = load_state(state_path)
    account = state["accounts"]["95016052"]
    assert account["workouts"]["idempotent-poc"]["garmin_workout_id"] == 42
    assert account["schedules"]["idempotent-poc@2026-08-22"]["garmin_schedule_id"] == 99


def test_changed_definition_requires_explicit_update(tmp_path) -> None:
    state_path = str(tmp_path / "receipts.json")
    garmin = client()
    publish_workout(garmin, definition(), "2026-08-22", state_path)

    with pytest.raises(WorkoutPublishError, match="pass --update"):
        publish_workout(garmin, definition(600), "2026-08-23", state_path)


def test_changed_definition_updates_in_place_when_allowed(tmp_path) -> None:
    state_path = str(tmp_path / "receipts.json")
    garmin = client()
    publish_workout(garmin, definition(), "2026-08-22", state_path)
    garmin.update_workout.return_value = {"workoutId": 42}
    garmin.schedule_workout.return_value = {"workoutScheduleId": 100}
    garmin.get_scheduled_workout_by_id.return_value = {
        "workoutScheduleId": 100,
        "calendarDate": "2026-08-23",
        "workout": {"workoutId": 42},
    }

    result = publish_workout(
        garmin, definition(600), "2026-08-23", state_path, allow_update=True
    )

    assert result["updated"] is True
    garmin.update_workout.assert_called_once()
    garmin.schedule_workout.assert_called_with(42, "2026-08-23")
