"""Tests for structured workout API wrappers and CLI commands."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from garmin_health_data.auth import load_authenticated_client
from garmin_health_data.cli import cli
from garmin_health_data.garmin_client import api


def _payload() -> dict:
    return {
        "workoutName": "POC easy run",
        "workoutSegments": [{"segmentOrder": 1, "workoutSteps": [{}]}],
    }


def _definition_payload() -> dict:
    return {
        "schema_version": 1,
        "workout": {
            "key": "poc-easy-run",
            "name": "POC easy run",
            "sport": "running",
            "steps": [
                {
                    "type": "interval",
                    "duration": {"type": "time", "seconds": 300},
                    "target": {"type": "open"},
                }
            ],
        },
    }


def test_workout_api_paths_and_payloads() -> None:
    client = MagicMock()
    client._connectapi.side_effect = [[{"workoutId": 12}], {"workoutId": 12}, {}]

    assert api.get_workouts(client, 5, 10) == [{"workoutId": 12}]
    assert api.get_workout_by_id(client, "12") == {"workoutId": 12}
    assert api.get_scheduled_workouts(client, 2026, 8) == {}

    assert client._connectapi.call_args_list[0].args == (
        "/workout-service/workouts",
    )
    assert client._connectapi.call_args_list[0].kwargs == {
        "params": {"start": 5, "limit": 10}
    }
    assert client._connectapi.call_args_list[1].args == (
        "/workout-service/workout/12",
    )
    assert client._connectapi.call_args_list[2].args == (
        "/calendar-service/year/2026/month/7",
    )


def test_workout_mutations_use_post() -> None:
    client = MagicMock()
    response = MagicMock(status_code=200)
    response.json.side_effect = [{"workoutId": 42}, {"workoutScheduleId": 99}]
    client._request.return_value = response

    assert api.upload_workout(client, _payload()) == {"workoutId": 42}
    assert api.schedule_workout(client, 42, "2026-08-21") == {
        "workoutScheduleId": 99
    }
    assert client._request.call_args_list[0].args == (
        "POST",
        "/workout-service/workout",
    )
    assert client._request.call_args_list[0].kwargs == {"json": _payload()}
    assert client._request.call_args_list[1].args == (
        "POST",
        "/workout-service/schedule/42",
    )
    assert client._request.call_args_list[1].kwargs == {
        "json": {"date": "2026-08-21"}
    }


def test_update_delete_and_unschedule_methods() -> None:
    client = MagicMock()
    response = MagicMock(status_code=204)
    client._request.return_value = response

    assert api.update_workout(client, 42, _payload()) == {}
    assert api.delete_workout(client, 42) == {}
    assert api.unschedule_workout(client, 99) == {}

    assert client._request.call_args_list[0].args == (
        "PUT",
        "/workout-service/workout/42",
    )
    assert client._request.call_args_list[1].args == (
        "DELETE",
        "/workout-service/workout/42",
    )
    assert client._request.call_args_list[2].args == (
        "DELETE",
        "/workout-service/schedule/99",
    )


def test_load_authenticated_client_requires_account_when_multiple(tmp_path) -> None:
    for account in ("111", "222"):
        directory = tmp_path / account
        directory.mkdir()
        (directory / "garmin_tokens.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Multiple Garmin accounts"):
        load_authenticated_client(base_token_dir=str(tmp_path))

    with patch(
        "garmin_health_data.auth.GarminClient.from_tokens"
    ) as from_tokens:
        load_authenticated_client(account="222", base_token_dir=str(tmp_path))
    from_tokens.assert_called_once_with(tmp_path / "222")


def test_validate_command_is_offline(tmp_path) -> None:
    workout_file = tmp_path / "workout.json"
    workout_file.write_text(json.dumps(_definition_payload()), encoding="utf-8")

    result = CliRunner().invoke(
        cli, ["workout", "validate", "--file", str(workout_file)]
    )

    assert result.exit_code == 0
    assert "valid: True" in result.output
    assert "POC easy run" in result.output


def test_upload_and_schedule_read_back(tmp_path) -> None:
    workout_file = tmp_path / "workout.json"
    workout_file.write_text(json.dumps(_payload()), encoding="utf-8")
    client = MagicMock()
    client.upload_workout.return_value = {"workoutId": 42}
    client.get_workout_by_id.return_value = {"workoutId": 42, "verified": True}
    client.schedule_workout.return_value = {"workoutScheduleId": 99}
    client.get_scheduled_workouts.return_value = {"calendarItems": []}

    with patch(
        "garmin_health_data.cli.load_authenticated_client", return_value=client
    ):
        result = CliRunner().invoke(
            cli,
            [
                "workout",
                "upload",
                "--file",
                str(workout_file),
                "--schedule-date",
                "2026-08-21",
            ],
        )

    assert result.exit_code == 0, result.output
    client.upload_workout.assert_called_once_with(_payload())
    client.get_workout_by_id.assert_called_once_with(42)
    client.schedule_workout.assert_called_once_with(42, "2026-08-21")
    client.get_scheduled_workouts.assert_called_once_with(2026, 8)
    assert "'workoutScheduleId': 99" in result.output
