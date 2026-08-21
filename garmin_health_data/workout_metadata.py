"""Schema migration and preserved-file backfill for structured workout evidence."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import fitdecode


def _decoded_fields(frame) -> dict[str, Any]:
    result = {}
    for field in frame.fields:
        if field.name and "unknown" not in field.name.lower() and field.value is not None:
            value = field.value
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            result[field.name] = value
    return result


def read_fit_workout(path: Path) -> dict[str, Any] | None:
    workout_id = None
    workout = None
    steps = []
    with fitdecode.FitReader(path) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "training_file":
                fields = _decoded_fields(frame)
                if fields.get("type") == "workout" and fields.get("serial_number") is not None:
                    workout_id = int(fields["serial_number"])
            elif frame.name == "workout":
                workout = _decoded_fields(frame)
            elif frame.name == "workout_step":
                steps.append(_decoded_fields(frame))
    if workout_id is None and workout is None:
        return None
    workout = workout or {}
    return {
        "fit_workout_id": workout_id,
        "workout_name": workout.get("wkt_name"),
        "workout_description": workout.get("wkt_description"),
        "sport": workout.get("sport"),
        "sub_sport": workout.get("sub_sport"),
        "step_count": workout.get("num_valid_steps", len(steps)),
        "definition_json": json.dumps(
            {"workout": workout, "steps": steps},
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def _ensure_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(activity)")}
    if "garmin_workout_id" not in columns:
        connection.execute("ALTER TABLE activity ADD COLUMN garmin_workout_id BIGINT")
    connection.executescript("""
        CREATE INDEX IF NOT EXISTS activity_garmin_workout_id_idx
        ON activity (garmin_workout_id) WHERE garmin_workout_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS activity_workout_metadata (
            activity_id BIGINT PRIMARY KEY,
            fit_workout_id BIGINT,
            workout_name TEXT,
            workout_description TEXT,
            sport TEXT,
            sub_sport TEXT,
            step_count INTEGER,
            definition_json JSON,
            create_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            update_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (activity_id) REFERENCES activity (activity_id) ON DELETE CASCADE,
            CHECK (definition_json IS NULL OR JSON_VALID(definition_json))
        );
        CREATE INDEX IF NOT EXISTS activity_workout_metadata_fit_workout_id_idx
        ON activity_workout_metadata (fit_workout_id) WHERE fit_workout_id IS NOT NULL;
    """)


def backfill_workout_metadata(
    database: Path | str,
    files_root: Path | str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill API workout IDs and FIT evidence from preserved source files."""
    database = Path(database)
    files_root = Path(files_root)
    activity_workouts: dict[int, int] = {}
    list_files = list(files_root.rglob("*_ACTIVITIES_LIST_*.json"))
    for path in list_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            continue
        for item in payload:
            activity_id = item.get("activityId")
            workout_id = item.get("workoutId")
            if activity_id is not None and workout_id is not None:
                activity_workouts[int(activity_id)] = int(workout_id)

    fit_paths: dict[int, Path] = {}
    wanted = set(activity_workouts)
    pattern = re.compile(r"_ACTIVITY_(\d+)_.*\.fit$", re.IGNORECASE)
    for path in files_root.rglob("*_ACTIVITY_*.fit"):
        match = pattern.search(path.name)
        if match and int(match.group(1)) in wanted:
            fit_paths[int(match.group(1))] = path

    result = {
        "activity_list_files": len(list_files),
        "api_workout_ids_found": len(activity_workouts),
        "fit_files_found": len(fit_paths),
        "activities_updated": 0,
        "fit_metadata_upserted": 0,
        "identifier_disagreements": [],
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _ensure_schema(connection)
        for activity_id, workout_id in activity_workouts.items():
            cursor = connection.execute(
                "UPDATE activity SET garmin_workout_id=?, update_ts=CURRENT_TIMESTAMP WHERE activity_id=?",
                (workout_id, activity_id),
            )
            result["activities_updated"] += cursor.rowcount
        for activity_id, path in fit_paths.items():
            metadata = read_fit_workout(path)
            if metadata is None:
                continue
            connection.execute(
                "INSERT INTO activity_workout_metadata(activity_id, fit_workout_id, workout_name, "
                "workout_description, sport, sub_sport, step_count, definition_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(activity_id) DO UPDATE SET "
                "fit_workout_id=excluded.fit_workout_id, workout_name=excluded.workout_name, "
                "workout_description=excluded.workout_description, sport=excluded.sport, "
                "sub_sport=excluded.sub_sport, step_count=excluded.step_count, "
                "definition_json=excluded.definition_json, update_ts=CURRENT_TIMESTAMP",
                (activity_id, metadata["fit_workout_id"], metadata["workout_name"],
                 metadata["workout_description"], metadata["sport"], metadata["sub_sport"],
                 metadata["step_count"], metadata["definition_json"]),
            )
            result["fit_metadata_upserted"] += 1
        result["identifier_disagreements"] = [dict(zip(("activity_id", "api_workout_id", "fit_workout_id"), row))
            for row in connection.execute("""SELECT a.activity_id, a.garmin_workout_id, m.fit_workout_id
                FROM activity a JOIN activity_workout_metadata m USING (activity_id)
                WHERE a.garmin_workout_id IS NOT NULL AND m.fit_workout_id IS NOT NULL
                AND a.garmin_workout_id != m.fit_workout_id""")]
        connection.commit()
    return result
