"""Tests for receipt-backed idempotent workout publishing."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from garmin_health_data.workout_publish import (
    WorkoutPublishError,
    preview_workout,
    publish_workout,
    reconcile_workout_state,
)
from garmin_health_data.workout_state import load_state
from garmin_health_data.workouts import definition_hash, validate_definition


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


def test_preview_is_domain_level_and_read_only(tmp_path):
    state_path = sqlite_state(tmp_path)
    before = Path(state_path).read_bytes()
    result = preview_workout(definition(), "2026-08-22", state_path,
                             account_id="95016052")
    assert result["summary"]["timed_seconds"] == 300
    assert result["summary"]["expanded_step_count"] == 1
    assert result["plan"]["publication_action"] == "create"
    assert result["plan"]["schedule_action"] == "create"
    assert result["plan"]["ready_to_execute"] is True
    assert result["provenance"]["garmin_payload_exposed"] is False
    assert Path(state_path).read_bytes() == before


def test_preview_reuses_matching_receipts_and_fingerprint_is_stable(tmp_path):
    state_path = sqlite_state(tmp_path)
    digest = definition_hash(validate_definition(definition()))
    with sqlite3.connect(state_path) as db:
        db.execute("INSERT INTO workout_publication VALUES(1,1,'95016052','idempotent-poc',42,?,'verified',NULL,'now',NULL,NULL)", (digest,))
        db.execute("INSERT INTO workout_schedule VALUES(1,1,'2026-08-22',99,'verified',NULL,'now',NULL,NULL)")
        db.commit()
    first = preview_workout(definition(), "2026-08-22", state_path)
    second = preview_workout(definition(), "2026-08-22", state_path)
    assert first["plan"]["publication_action"] == "reuse"
    assert first["plan"]["schedule_action"] == "reuse"
    assert first["plan"]["operation_fingerprint"] == second["plan"]["operation_fingerprint"]


def test_preview_blocks_changed_definition_and_same_day_conflict(tmp_path):
    state_path = sqlite_state(tmp_path)
    with sqlite3.connect(state_path) as db:
        db.execute("INSERT INTO workout_definition VALUES(2,'other')")
        db.execute("INSERT INTO workout_publication VALUES(1,1,'95016052','idempotent-poc',42,'old','verified',NULL,'now',NULL,NULL)")
        db.execute("INSERT INTO workout_publication VALUES(2,2,'95016052','other',43,'other','verified',NULL,'now',NULL,NULL)")
        db.execute("INSERT INTO workout_schedule VALUES(1,2,'2026-08-22',100,'verified',NULL,'now',NULL,NULL)")
        db.commit()
    result = preview_workout(definition(), "2026-08-22", state_path)
    assert result["plan"]["publication_action"] == "blocked_definition_change"
    assert result["plan"]["ready_to_execute"] is False
    assert len(result["plan"]["same_day_conflicts"]) == 1


def sqlite_state(tmp_path) -> str:
    path = tmp_path / "training_state.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE workout_definition (
                definition_id INTEGER PRIMARY KEY,
                definition_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE workout_publication (
                publication_id INTEGER PRIMARY KEY,
                definition_id INTEGER REFERENCES workout_definition(definition_id),
                account_id TEXT NOT NULL,
                source_key TEXT,
                garmin_workout_id INTEGER NOT NULL,
                definition_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                last_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                last_error TEXT,
                UNIQUE(account_id, garmin_workout_id),
                UNIQUE(account_id, source_key),
                UNIQUE(account_id, definition_id)
            );
            CREATE TABLE workout_schedule (
                schedule_id INTEGER PRIMARY KEY,
                publication_id INTEGER NOT NULL REFERENCES workout_publication(publication_id),
                calendar_date TEXT NOT NULL,
                garmin_schedule_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL,
                last_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                last_error TEXT,
                UNIQUE(publication_id, calendar_date)
            );
            CREATE TABLE workout_reconciliation_event (
                event_id INTEGER PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id INTEGER NOT NULL,
                previous_status TEXT,
                new_status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO workout_definition(definition_id, definition_key)
            VALUES (1, 'idempotent-poc');
        """)
    return str(path)


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


def test_sqlite_publish_is_transactional_and_idempotent(tmp_path) -> None:
    state_path = sqlite_state(tmp_path)
    garmin = client()

    first = publish_workout(garmin, definition(), "2026-08-22", state_path)
    second = publish_workout(garmin, definition(), "2026-08-22", state_path)

    assert first["created"] is True
    assert second["created"] is False
    garmin.upload_workout.assert_called_once()
    garmin.schedule_workout.assert_called_once()
    with sqlite3.connect(state_path) as db:
        assert db.execute("SELECT status FROM workout_publication").fetchone()[0] == "verified"
        assert db.execute("SELECT status FROM workout_schedule").fetchone()[0] == "verified"
        assert db.execute("SELECT COUNT(*) FROM workout_reconciliation_event").fetchone()[0] == 4


def test_sqlite_failed_readback_stays_pending_and_reconciles(tmp_path) -> None:
    state_path = sqlite_state(tmp_path)
    garmin = client()
    garmin.get_workout_by_id.side_effect = [RuntimeError("temporary"), {"workoutId": 42}]

    with pytest.raises(WorkoutPublishError, match="exists in local receipts"):
        publish_workout(garmin, definition(), "2026-08-22", state_path)
    with sqlite3.connect(state_path) as db:
        assert db.execute("SELECT status FROM workout_publication").fetchone()[0] == "pending_verification"
        assert "RuntimeError: temporary" in db.execute(
            "SELECT last_error FROM workout_publication"
        ).fetchone()[0]

    result = reconcile_workout_state(garmin, state_path)

    assert result["publications"][0]["status"] == "verified"
    assert result["schedules"] == []
    with sqlite3.connect(state_path) as db:
        assert db.execute("SELECT status FROM workout_publication").fetchone()[0] == "verified"
