"""Idempotent create/update/schedule orchestration for coaching workouts."""

from __future__ import annotations

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
    created = False
    updated = False

    if receipt:
        workout_id = int(receipt["garmin_workout_id"])
        if receipt.get("definition_hash") != digest:
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
