"""Idempotent create/update/schedule orchestration for coaching workouts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict

from garmin_health_data.workout_state import (
    get_schedule_receipt,
    get_workout_receipt,
    reconciliation_candidates,
    load_state,
    record_schedule,
    record_workout,
    save_state,
)
from garmin_health_data.workouts import (
    definition_hash,
    render_garmin_workout,
    validate_definition,
)


class WorkoutPublishError(RuntimeError):
    """Raised when safe idempotent publishing cannot continue."""


def _preview_account(state: Dict[str, Any], requested: str | None) -> str:
    if requested:
        return str(requested)
    if state.get("_sqlite_path"):
        path = Path(state["_sqlite_path"])
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            accounts = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT account_id FROM workout_publication ORDER BY account_id"
            )]
    else:
        accounts = sorted(str(key) for key in state.get("accounts", {}))
    if len(accounts) != 1:
        raise WorkoutPublishError(
            "Preview requires --account unless training state contains exactly one account"
        )
    return accounts[0]


def _same_day_conflicts(state: Dict[str, Any], account_id: str, key: str,
                        date_str: str) -> list[Dict[str, Any]]:
    if state.get("_sqlite_path"):
        path = Path(state["_sqlite_path"])
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("""
                SELECT s.schedule_id,COALESCE(d.definition_key,p.source_key) AS key,
                       s.status FROM workout_schedule s
                JOIN workout_publication p USING(publication_id)
                LEFT JOIN workout_definition d USING(definition_id)
                WHERE p.account_id=? AND s.calendar_date=?
                  AND s.status!='unscheduled'
                  AND COALESCE(d.definition_key,p.source_key)!=?
                ORDER BY s.schedule_id
            """, (account_id, date_str, key))]
    account = state.get("accounts", {}).get(account_id, {})
    return [{"schedule_id": item.get("garmin_schedule_id"),
             "key": receipt_key.rsplit("@", 1)[0], "status": "verified"}
            for receipt_key, item in account.get("schedules", {}).items()
            if item.get("calendar_date") == date_str
            and receipt_key.rsplit("@", 1)[0] != key]


def _catalog_definition_hash(state: Dict[str, Any], key: str) -> str | None:
    """Return the authoritative normalized catalog hash when the SQLite schema has it."""
    if not state.get("_sqlite_path"):
        return None
    path = Path(state["_sqlite_path"])
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(workout_definition)"
        )}
        if "normalized_json" not in columns:
            return None
        row = connection.execute(
            "SELECT normalized_json FROM workout_definition WHERE definition_key=?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return definition_hash(validate_definition(json.loads(row[0])))


def _step_summary(steps: list[Dict[str, Any]], multiplier: int = 1) -> Dict[str, Any]:
    result = {"structural_step_count": 0, "expanded_step_count": 0,
              "timed_seconds": 0.0, "distance_meters": 0.0,
              "lap_button_step_count": 0}
    for step in steps:
        result["structural_step_count"] += 1
        if step["type"] == "repeat":
            child = _step_summary(step["steps"], multiplier * int(step["count"]))
            for field in result:
                result[field] += child[field]
            continue
        result["expanded_step_count"] += multiplier
        duration = step["duration"]
        if duration["type"] == "time":
            result["timed_seconds"] += multiplier * float(duration["seconds"])
        elif duration["type"] == "distance":
            result["distance_meters"] += multiplier * float(duration["meters"])
        else:
            result["lap_button_step_count"] += multiplier
    result["timed_seconds"] = round(result["timed_seconds"], 3)
    result["distance_meters"] = round(result["distance_meters"], 3)
    return result


def preview_workout(
    definition: Dict[str, Any], date_str: str, state_path: str,
    *, account_id: str | None = None, allow_update: bool = False,
    allow_same_day: bool = False,
) -> Dict[str, Any]:
    """Resolve a domain-level publish/schedule plan without Garmin calls or writes."""
    try:
        calendar_date = date.fromisoformat(date_str).isoformat()
    except (TypeError, ValueError) as error:
        raise WorkoutPublishError("date must be YYYY-MM-DD") from error
    normalized = validate_definition(definition)
    workout = normalized["workout"]
    key = workout["key"]
    digest = definition_hash(normalized)
    state = load_state(state_path)
    resolved_account = _preview_account(state, account_id)
    receipt = get_workout_receipt(state, resolved_account, key)
    catalog_digest = _catalog_definition_hash(state, key)
    schedule = get_schedule_receipt(state, resolved_account, key, calendar_date)
    conflicts = _same_day_conflicts(state, resolved_account, key, calendar_date)
    reasons = []
    if receipt is None:
        publication_action = "create"
    elif receipt.get("definition_hash") == digest or catalog_digest == digest:
        publication_action = "reuse"
    elif allow_update:
        publication_action = "update"
    else:
        publication_action = "blocked_definition_change"
        reasons.append("existing workout key has a different definition")
    if schedule:
        schedule_action = "reuse"
    elif publication_action == "blocked_definition_change":
        schedule_action = "blocked"
    else:
        schedule_action = "create"
    if conflicts and not allow_same_day and schedule_action == "create":
        reasons.append("another workout is already scheduled on this date")
    fingerprint_source = {
        "account_id": resolved_account, "definition_hash": digest, "key": key,
        "calendar_date": calendar_date, "allow_update": allow_update,
        "allow_same_day": allow_same_day,
    }
    fingerprint = "sha256:" + hashlib.sha256(json.dumps(
        fingerprint_source, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "schema_version": "1.0", "valid": True,
        "normalized_definition": normalized,
        "summary": {
            "key": key, "name": workout["name"], "sport": workout["sport"],
            "estimated_duration_seconds": workout["estimated_duration_seconds"],
            **_step_summary(workout["steps"]),
        },
        "plan": {
            "calendar_date": calendar_date,
            "publication_action": publication_action,
            "schedule_action": schedule_action,
            "existing_garmin_workout_id": int(receipt["garmin_workout_id"]) if receipt else None,
            "existing_garmin_schedule_id": int(schedule["garmin_schedule_id"]) if schedule else None,
            "same_day_conflicts": conflicts,
            "ready_to_execute": not reasons,
            "blocking_reasons": reasons,
            "operation_fingerprint": fingerprint,
        },
        "provenance": {
            "definition_contract": "garmin-health-data/workouts schema v1",
            "state_source": "training_state", "read_only": True,
            "garmin_network_access": False, "garmin_payload_exposed": False,
        },
    }


def reconcile_workout_state(client: Any, state_path: str) -> Dict[str, Any]:
    """Read back pending Garmin objects and transactionally mark verified ones."""
    account_id = getattr(client, "account_id", None)
    if not account_id:
        raise WorkoutPublishError("Authenticated client has no account identity")
    state = load_state(state_path)
    pending = reconciliation_candidates(state, account_id)
    results = {"publications": [], "schedules": []}
    for item in pending["publications"]:
        error = None
        try:
            remote = client.get_workout_by_id(int(item["garmin_workout_id"]))
            verified = int(remote.get("workoutId", 0)) == int(item["garmin_workout_id"])
            if not verified:
                error = "Garmin workout read-back returned a different ID"
        except Exception as err:
            verified = False
            error = f"{type(err).__name__}: {err}"
        status = "verified" if verified else "pending_verification"
        record_workout(state, account_id, item["key"], item["garmin_workout_id"],
                       item["definition_hash"], status, error)
        results["publications"].append({**item, "status": status, "error": error})
    for item in pending["schedules"]:
        error = None
        try:
            remote = client.get_scheduled_workout_by_id(int(item["garmin_schedule_id"]))
            remote_workout = remote.get("workout") or {}
            verified = (remote.get("calendarDate") == item["calendar_date"] and
                        int(remote_workout.get("workoutId", 0)) == int(item["garmin_workout_id"]))
            if not verified:
                error = "Garmin schedule read-back did not match date and workout"
        except Exception as err:
            verified = False
            error = f"{type(err).__name__}: {err}"
        status = "verified" if verified else "pending_verification"
        record_schedule(state, account_id, item["key"], item["calendar_date"],
                        item["garmin_workout_id"], item["garmin_schedule_id"], status, error)
        results["schedules"].append({**item, "status": status, "error": error})
    return results


def execute_additive_workout(
    client: Any,
    definition: Dict[str, Any],
    date_str: str,
    state_path: str,
    operation_fingerprint: str,
) -> Dict[str, Any]:
    """Execute one exact create/reuse preview without replacement or date conflicts."""
    account_id = getattr(client, "account_id", None)
    if not account_id:
        raise WorkoutPublishError("Authenticated client has no account identity")
    preview = preview_workout(
        definition, date_str, state_path, account_id=str(account_id)
    )
    plan = preview["plan"]
    if operation_fingerprint != plan["operation_fingerprint"]:
        raise WorkoutPublishError(
            "Operation fingerprint does not match the current preview; preview again"
        )
    if not plan["ready_to_execute"]:
        detail = "; ".join(plan["blocking_reasons"])
        raise WorkoutPublishError(f"Workout operation is blocked: {detail}")
    if plan["publication_action"] not in {"create", "reuse"}:
        raise WorkoutPublishError(
            "MVP execution permits only additive create/reuse publication actions"
        )
    if plan["schedule_action"] not in {"create", "reuse"}:
        raise WorkoutPublishError(
            "MVP execution permits only additive create/reuse schedule actions"
        )
    result = publish_workout(client, definition, date_str, state_path)
    return {
        **result,
        "operation_fingerprint": plan["operation_fingerprint"],
        "publication_action": plan["publication_action"],
        "schedule_action": plan["schedule_action"],
    }


def publish_workout(
    client: Any,
    definition: Dict[str, Any],
    date_str: str,
    state_path: str,
    allow_update: bool = False,
) -> Dict[str, Any]:
    """Create or reuse a workout, schedule it once, and verify both objects."""
    definition = validate_definition(definition)
    workout = definition["workout"]
    key = workout["key"]
    digest = definition_hash(definition)
    payload = render_garmin_workout(definition)
    account_id = getattr(client, "account_id", None)
    if not account_id:
        raise WorkoutPublishError("Authenticated client has no account identity")

    state = load_state(state_path)
    receipt = get_workout_receipt(state, account_id, key)
    catalog_digest = _catalog_definition_hash(state, key)
    created = False
    updated = False

    if receipt:
        workout_id = int(receipt["garmin_workout_id"])
        if receipt.get("definition_hash") != digest and catalog_digest != digest:
            if not allow_update:
                raise WorkoutPublishError(
                    f"Workout key {key!r} already maps to Garmin workout {workout_id} "
                    "with a different definition; pass --update to replace it in place"
                )
            changed = client.update_workout(workout_id, payload)
            workout_id = int(changed.get("workoutId", workout_id))
            updated = True
            record_workout(state, account_id, key, workout_id, digest, "pending_verification")
            save_state(state, state_path)
    else:
        uploaded = client.upload_workout(payload)
        workout_id = uploaded.get("workoutId")
        if not workout_id:
            raise WorkoutPublishError("Garmin upload response contained no workoutId")
        workout_id = int(workout_id)
        created = True
        # Persist the assigned ID before read-back so a transient verification error
        # does not cause a retry to create a duplicate template.
        record_workout(state, account_id, key, workout_id, digest, "pending_verification")
        save_state(state, state_path)

    try:
        verified_workout = client.get_workout_by_id(workout_id)
    except Exception as err:
        record_workout(state, account_id, key, workout_id, digest,
                       "pending_verification", f"{type(err).__name__}: {err}")
        raise WorkoutPublishError(
            f"Garmin workout {workout_id} exists in local receipts but read-back failed; "
            "resolve the account/network state before retrying"
        ) from err
    if int(verified_workout.get("workoutId", 0)) != workout_id:
        record_workout(state, account_id, key, workout_id, digest,
                       "pending_verification", "Garmin workout read-back returned a different ID")
        raise WorkoutPublishError(
            f"Garmin workout read-back did not verify ID {workout_id}"
        )
    record_workout(state, account_id, key, workout_id, digest)
    save_state(state, state_path)

    schedule_key = f"{key}@{date_str}"
    schedule_receipt = get_schedule_receipt(state, account_id, key, date_str)
    scheduled = False
    if schedule_receipt:
        schedule_id = int(schedule_receipt["garmin_schedule_id"])
        try:
            verified_schedule = client.get_scheduled_workout_by_id(schedule_id)
        except Exception as err:
            record_schedule(state, account_id, key, date_str, workout_id, schedule_id,
                            "pending_verification", f"{type(err).__name__}: {err}")
            raise WorkoutPublishError(
                f"Schedule receipt {schedule_key!r} points to Garmin schedule "
                f"{schedule_id}, but read-back failed; refusing to create a duplicate"
            ) from err
    else:
        schedule_response = client.schedule_workout(workout_id, date_str)
        schedule_id = schedule_response.get("workoutScheduleId")
        if not schedule_id:
            raise WorkoutPublishError(
                "Garmin schedule response contained no workoutScheduleId"
            )
        schedule_id = int(schedule_id)
        scheduled = True
        record_schedule(state, account_id, key, date_str, workout_id, schedule_id, "pending_verification")
        save_state(state, state_path)
        try:
            verified_schedule = client.get_scheduled_workout_by_id(schedule_id)
        except Exception as err:
            raise WorkoutPublishError(
                f"Garmin schedule {schedule_id} was created and recorded, but "
                "read-back failed"
            ) from err

    actual_date = verified_schedule.get("calendarDate")
    if actual_date != date_str:
        record_schedule(state, account_id, key, date_str, workout_id, schedule_id,
                        "pending_verification", "Garmin schedule read-back returned a different date")
        raise WorkoutPublishError(
            f"Garmin schedule {schedule_id} read back with date {actual_date!r}, "
            f"expected {date_str!r}"
        )
    scheduled_workout = verified_schedule.get("workout") or {}
    actual_workout_id = scheduled_workout.get("workoutId")
    if actual_workout_id is not None and int(actual_workout_id) != workout_id:
        record_schedule(state, account_id, key, date_str, workout_id, schedule_id,
                        "pending_verification", "Garmin schedule read-back returned a different workout")
        raise WorkoutPublishError(
            f"Garmin schedule {schedule_id} points to workout {actual_workout_id}, "
            f"expected {workout_id}"
        )
    record_schedule(state, account_id, key, date_str, workout_id, schedule_id)
    save_state(state, state_path)

    return {
        "key": key,
        "garmin_workout_id": workout_id,
        "garmin_schedule_id": schedule_id,
        "calendar_date": date_str,
        "created": created,
        "updated": updated,
        "scheduled": scheduled,
        "verified": True,
        "definition_hash": digest,
    }
