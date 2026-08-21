"""Local idempotency receipts for Garmin workout publishing."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


STATE_SCHEMA_VERSION = 1
LEGACY_STATE_PATH = "~/.garminconnect/workout_receipts.json"
DEFAULT_STATE_PATH = os.environ.get(
    "GARMIN_TRAINING_STATE_DB", "~/.garminconnect/training_state.db"
)


def _sqlite_path(path: str) -> Path | None:
    candidate = Path(path).expanduser()
    return candidate if candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"} else None


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    required = {"workout_definition", "workout_publication", "workout_schedule"}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        connection.close()
        raise ValueError(f"Training state database {path} is not initialized")
    return connection


def empty_state() -> Dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "accounts": {}}


def load_state(path: str = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    state_path = Path(path).expanduser()
    if _sqlite_path(path):
        if not state_path.exists():
            raise ValueError(f"Training state database {state_path} does not exist")
        with _connect(state_path):
            pass
        return {"_sqlite_path": str(state_path)}
    if not state_path.exists():
        return empty_state()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(
            f"Could not load workout receipt state {state_path}: {err}"
        ) from err
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != STATE_SCHEMA_VERSION
    ):
        raise ValueError(
            f"Unsupported workout receipt state schema in {state_path}; expected "
            f"version {STATE_SCHEMA_VERSION}"
        )
    if not isinstance(state.get("accounts"), dict):
        raise ValueError(
            f"Invalid workout receipt state in {state_path}: accounts must be an object"
        )
    return state


def save_state(state: Dict[str, Any], path: str = DEFAULT_STATE_PATH) -> None:
    """Atomically persist receipt state after a verified Garmin mutation."""
    if state.get("_sqlite_path"):
        return
    state_path = Path(path).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=state_path.name + ".", suffix=".tmp", dir=state_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, state_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def account_state(state: Dict[str, Any], account_id: str) -> Dict[str, Any]:
    return state["accounts"].setdefault(
        str(account_id), {"workouts": {}, "schedules": {}}
    )


def get_workout_receipt(state: Dict[str, Any], account_id: str, key: str):
    if not state.get("_sqlite_path"):
        return account_state(state, account_id)["workouts"].get(key)
    with _connect(Path(state["_sqlite_path"])) as connection:
        row = connection.execute(
            "SELECT p.garmin_workout_id, p.definition_hash, p.status "
            "FROM workout_publication p LEFT JOIN workout_definition d USING (definition_id) "
            "WHERE p.account_id=? AND (p.source_key=? OR d.definition_key=?) "
            "AND p.status != 'deleted'",
            (str(account_id), key, key),
        ).fetchone()
        return dict(row) if row else None


def get_schedule_receipt(state: Dict[str, Any], account_id: str, key: str, date_str: str):
    if not state.get("_sqlite_path"):
        return account_state(state, account_id)["schedules"].get(f"{key}@{date_str}")
    with _connect(Path(state["_sqlite_path"])) as connection:
        row = connection.execute(
            "SELECT s.garmin_schedule_id, p.garmin_workout_id, s.calendar_date, s.status "
            "FROM workout_schedule s JOIN workout_publication p USING (publication_id) "
            "LEFT JOIN workout_definition d USING (definition_id) "
            "WHERE p.account_id=? AND (p.source_key=? OR d.definition_key=?) "
            "AND s.calendar_date=? AND s.status != 'unscheduled'",
            (str(account_id), key, key, date_str),
        ).fetchone()
        return dict(row) if row else None


def reconciliation_candidates(state: Dict[str, Any], account_id: str) -> Dict[str, list]:
    """Return only unresolved SQLite objects for bounded remote read-back."""
    if not state.get("_sqlite_path"):
        raise ValueError("Reconciliation requires SQLite training state")
    with _connect(Path(state["_sqlite_path"])) as connection:
        publications = [dict(row) for row in connection.execute(
            "SELECT COALESCE(d.definition_key, p.source_key) AS key, p.garmin_workout_id, "
            "p.definition_hash FROM workout_publication p LEFT JOIN workout_definition d USING (definition_id) "
            "WHERE p.account_id=? AND p.status='pending_verification' ORDER BY p.publication_id",
            (str(account_id),),
        )]
        schedules = [dict(row) for row in connection.execute(
            "SELECT COALESCE(d.definition_key, p.source_key) AS key, p.garmin_workout_id, "
            "s.garmin_schedule_id, s.calendar_date FROM workout_schedule s "
            "JOIN workout_publication p USING (publication_id) LEFT JOIN workout_definition d USING (definition_id) "
            "WHERE p.account_id=? AND s.status='pending_verification' ORDER BY s.schedule_id",
            (str(account_id),),
        )]
    return {"publications": publications, "schedules": schedules}


def record_workout(
    state: Dict[str, Any],
    account_id: str,
    key: str,
    workout_id: int,
    definition_hash: str,
    status: str = "verified",
    error: str | None = None,
) -> None:
    if state.get("_sqlite_path"):
        now = datetime.now(timezone.utc).isoformat()
        with _connect(Path(state["_sqlite_path"])) as connection:
            existing = connection.execute(
                "SELECT p.publication_id, p.status FROM workout_publication p "
                "LEFT JOIN workout_definition d USING (definition_id) "
                "WHERE p.account_id=? AND (p.source_key=? OR d.definition_key=?)",
                (str(account_id), key, key),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE workout_publication SET garmin_workout_id=?, definition_hash=?, status=?, "
                    "last_verified_at=CASE WHEN ?='verified' THEN ? ELSE last_verified_at END, "
                    "updated_at=?, last_error=? WHERE publication_id=?",
                    (int(workout_id), definition_hash, status, status, now, now, error, int(existing[0])),
                )
                object_id, previous = int(existing[0]), existing[1]
            else:
                definition = connection.execute(
                    "SELECT definition_id FROM workout_definition WHERE definition_key=?", (key,)
                ).fetchone()
                if definition is None:
                    raise ValueError(f"Workout {key!r} must be registered in the catalog before publishing")
                cursor = connection.execute(
                    "INSERT INTO workout_publication(definition_id, account_id, source_key, garmin_workout_id, "
                    "definition_hash, status, last_verified_at, created_at, updated_at, last_error) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(definition[0]), str(account_id), key, int(workout_id), definition_hash, status,
                     now if status == "verified" else None, now, now, error),
                )
                object_id, previous = int(cursor.lastrowid), None
            if previous != status or error:
                connection.execute(
                    "INSERT INTO workout_reconciliation_event(object_type, object_id, previous_status, new_status, detail, created_at) "
                    "VALUES ('publication', ?, ?, ?, ?, ?)", (object_id, previous, status, error, now)
                )
            connection.commit()
        return
    account_state(state, account_id)["workouts"][key] = {
        "garmin_workout_id": int(workout_id),
        "definition_hash": definition_hash,
        "last_verified_at": datetime.now(timezone.utc).isoformat(),
    }


def record_schedule(
    state: Dict[str, Any],
    account_id: str,
    key: str,
    date_str: str,
    workout_id: int,
    schedule_id: int,
    status: str = "verified",
    error: str | None = None,
) -> None:
    if state.get("_sqlite_path"):
        now = datetime.now(timezone.utc).isoformat()
        with _connect(Path(state["_sqlite_path"])) as connection:
            publication = connection.execute(
                "SELECT p.publication_id FROM workout_publication p LEFT JOIN workout_definition d USING (definition_id) "
                "WHERE p.account_id=? AND (p.source_key=? OR d.definition_key=?)",
                (str(account_id), key, key),
            ).fetchone()
            if publication is None:
                raise ValueError(f"No publication exists for workout {key!r}")
            existing = connection.execute(
                "SELECT schedule_id, status FROM workout_schedule WHERE publication_id=? AND calendar_date=?",
                (int(publication[0]), date_str),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE workout_schedule SET garmin_schedule_id=?, status=?, "
                    "last_verified_at=CASE WHEN ?='verified' THEN ? ELSE last_verified_at END, "
                    "updated_at=?, last_error=? WHERE schedule_id=?",
                    (int(schedule_id), status, status, now, now, error, int(existing[0])),
                )
                object_id, previous = int(existing[0]), existing[1]
            else:
                cursor = connection.execute(
                    "INSERT INTO workout_schedule(publication_id, calendar_date, garmin_schedule_id, status, "
                    "last_verified_at, created_at, updated_at, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(publication[0]), date_str, int(schedule_id), status,
                     now if status == "verified" else None, now, now, error),
                )
                object_id, previous = int(cursor.lastrowid), None
            if previous != status or error:
                connection.execute(
                    "INSERT INTO workout_reconciliation_event(object_type, object_id, previous_status, new_status, detail, created_at) "
                    "VALUES ('schedule', ?, ?, ?, ?, ?)", (object_id, previous, status, error, now)
                )
            connection.commit()
        return
    account_state(state, account_id)["schedules"][f"{key}@{date_str}"] = {
        "garmin_schedule_id": int(schedule_id),
        "garmin_workout_id": int(workout_id),
        "calendar_date": date_str,
        "last_verified_at": datetime.now(timezone.utc).isoformat(),
    }


def remove_workout_receipt(
    state: Dict[str, Any], account_id: str, workout_id: int
) -> None:
    if state.get("_sqlite_path"):
        now = datetime.now(timezone.utc).isoformat()
        with _connect(Path(state["_sqlite_path"])) as connection:
            rows = connection.execute(
                "SELECT publication_id, status FROM workout_publication WHERE account_id=? AND garmin_workout_id=?",
                (str(account_id), int(workout_id)),
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE workout_publication SET status='deleted', updated_at=?, last_error=NULL WHERE publication_id=?",
                                   (now, int(row[0])))
                connection.execute("INSERT INTO workout_reconciliation_event(object_type, object_id, previous_status, new_status, created_at) "
                                   "VALUES ('publication', ?, ?, 'deleted', ?)", (int(row[0]), row[1], now))
            connection.commit()
        return
    account = account_state(state, account_id)
    account["workouts"] = {
        key: value
        for key, value in account["workouts"].items()
        if value.get("garmin_workout_id") != workout_id
    }


def remove_schedule_receipt(
    state: Dict[str, Any], account_id: str, schedule_id: int
) -> None:
    if state.get("_sqlite_path"):
        now = datetime.now(timezone.utc).isoformat()
        with _connect(Path(state["_sqlite_path"])) as connection:
            rows = connection.execute(
                "SELECT s.schedule_id, s.status FROM workout_schedule s JOIN workout_publication p USING (publication_id) "
                "WHERE p.account_id=? AND s.garmin_schedule_id=?", (str(account_id), int(schedule_id))
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE workout_schedule SET status='unscheduled', updated_at=?, last_error=NULL WHERE schedule_id=?",
                                   (now, int(row[0])))
                connection.execute("INSERT INTO workout_reconciliation_event(object_type, object_id, previous_status, new_status, created_at) "
                                   "VALUES ('schedule', ?, ?, 'unscheduled', ?)", (int(row[0]), row[1], now))
            connection.commit()
        return
    account = account_state(state, account_id)
    account["schedules"] = {
        key: value
        for key, value in account["schedules"].items()
        if value.get("garmin_schedule_id") != schedule_id
    }
