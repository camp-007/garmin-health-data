"""Local idempotency receipts for Garmin workout publishing."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


STATE_SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = "~/.garminconnect/workout_receipts.json"


def empty_state() -> Dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "accounts": {}}


def load_state(path: str = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    state_path = Path(path).expanduser()
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


def record_workout(
    state: Dict[str, Any],
    account_id: str,
    key: str,
    workout_id: int,
    definition_hash: str,
) -> None:
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
) -> None:
    account_state(state, account_id)["schedules"][f"{key}@{date_str}"] = {
        "garmin_schedule_id": int(schedule_id),
        "garmin_workout_id": int(workout_id),
        "calendar_date": date_str,
        "last_verified_at": datetime.now(timezone.utc).isoformat(),
    }


def remove_workout_receipt(
    state: Dict[str, Any], account_id: str, workout_id: int
) -> None:
    account = account_state(state, account_id)
    account["workouts"] = {
        key: value
        for key, value in account["workouts"].items()
        if value.get("garmin_workout_id") != workout_id
    }


def remove_schedule_receipt(
    state: Dict[str, Any], account_id: str, schedule_id: int
) -> None:
    account = account_state(state, account_id)
    account["schedules"] = {
        key: value
        for key, value in account["schedules"].items()
        if value.get("garmin_schedule_id") != schedule_id
    }
