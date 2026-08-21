import json
import sqlite3

from garmin_health_data.workout_metadata import backfill_workout_metadata


def database(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE activity (
                activity_id BIGINT PRIMARY KEY,
                update_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO activity(activity_id) VALUES (24048429539);
        """)


def test_backfill_migrates_and_loads_api_and_fit_evidence(tmp_path, monkeypatch):
    db_path = tmp_path / "garmin.db"
    database(db_path)
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "u_ACTIVITIES_LIST_2026-08-20T12-00-00Z.json").write_text(
        json.dumps([{"activityId": 24048429539, "workoutId": 1669685793}]),
        encoding="utf-8",
    )
    fit_path = storage / "u_ACTIVITY_24048429539_2026-08-20T12-00-00Z.fit"
    fit_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "garmin_health_data.workout_metadata.read_fit_workout",
        lambda path: {
            "fit_workout_id": 1669685793,
            "workout_name": "Thursday Easy Run",
            "workout_description": "Easy",
            "sport": "running",
            "sub_sport": "generic",
            "step_count": 3,
            "definition_json": json.dumps({"steps": [1, 2, 3]}),
        },
    )

    result = backfill_workout_metadata(db_path, tmp_path)

    assert result["activities_updated"] == 1
    assert result["fit_metadata_upserted"] == 1
    assert result["identifier_disagreements"] == []
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT garmin_workout_id FROM activity"
        ).fetchone()[0] == 1669685793
        assert db.execute(
            "SELECT fit_workout_id, step_count FROM activity_workout_metadata"
        ).fetchone() == (1669685793, 3)


def test_dry_run_does_not_change_schema(tmp_path):
    db_path = tmp_path / "garmin.db"
    database(db_path)

    result = backfill_workout_metadata(db_path, tmp_path, dry_run=True)

    assert result["dry_run"] is True
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(activity)")}
    assert "garmin_workout_id" not in columns
