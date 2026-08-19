"""Versioned coaching workout definitions and Garmin payload rendering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCHEMA_VERSION = 1
SUPPORTED_SPORTS = {"running": (1, 1), "cycling": (2, 2)}
STEP_TYPES = {
    "warmup": (1, 1),
    "cooldown": (2, 2),
    "interval": (3, 3),
    "recovery": (4, 4),
    "rest": (5, 5),
    "other": (7, 7),
}
CONDITION_TYPES = {
    "lap_button": (1, 1, True),
    "time": (2, 2, True),
    "distance": (3, 3, True),
    "iterations": (7, 7, False),
}
TARGET_TYPES = {
    "open": (1, "no.target", 1),
    "power": (2, "power.zone", 2),
    "power_zone": (2, "power.zone", 2),
    "heart_rate": (4, "heart.rate.zone", 4),
    "heart_rate_zone": (4, "heart.rate.zone", 4),
    "pace": (5, "speed.zone", 5),
}
MAX_EXPANDED_STEPS = 100


class WorkoutDefinitionError(ValueError):
    """Raised when a coaching workout definition violates the contract."""


def _error(path: str, message: str) -> None:
    raise WorkoutDefinitionError(f"{path}: {message}")


def _object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _error(path, "must be an object")
    return value


def _strict_keys(value: Dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _error(path, f"unknown field(s): {', '.join(unknown)}")


def _positive_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        _error(path, "must be a positive number")
    return float(value)


def _nonnegative_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        _error(path, "must be a non-negative number")
    return float(value)


def _integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _error(path, "must be an integer")
    if not minimum <= value <= maximum:
        _error(path, f"must be between {minimum} and {maximum}")
    return value


def load_definition(path: str | Path) -> Dict[str, Any]:
    """Load and validate a versioned workout definition from JSON."""
    file_path = Path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except OSError as err:
        raise WorkoutDefinitionError(f"could not read {file_path}: {err}") from err
    except json.JSONDecodeError as err:
        raise WorkoutDefinitionError(
            f"invalid JSON at line {err.lineno}, column {err.colno}: {err.msg}"
        ) from err
    return validate_definition(raw)


def validate_definition(raw: Any) -> Dict[str, Any]:
    """Validate and normalize the public schema-version-1 contract."""
    root = _object(raw, "$ ".strip())
    _strict_keys(root, {"schema_version", "workout"}, "$")
    if root.get("schema_version") != SCHEMA_VERSION:
        _error("$.schema_version", f"must equal {SCHEMA_VERSION}")

    workout = _object(root.get("workout"), "$.workout")
    _strict_keys(
        workout,
        {
            "key",
            "name",
            "description",
            "sport",
            "estimated_duration_seconds",
            "steps",
        },
        "$.workout",
    )
    key = workout.get("key")
    if not isinstance(key, str) or not key.strip() or len(key) > 100:
        _error("$.workout.key", "must be a non-empty string of at most 100 characters")
    name = workout.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        _error("$.workout.name", "must be a non-empty string of at most 80 characters")
    description = workout.get("description")
    if description is not None and (
        not isinstance(description, str) or len(description) > 1024
    ):
        _error("$.workout.description", "must be a string of at most 1024 characters")
    sport = workout.get("sport")
    if sport not in SUPPORTED_SPORTS:
        _error("$.workout.sport", "must be running or cycling")
    steps = workout.get("steps")
    if not isinstance(steps, list) or not steps:
        _error("$.workout.steps", "must be a non-empty array")

    normalized_steps = [
        _validate_step(step, f"$.workout.steps[{index}]", sport, 0)
        for index, step in enumerate(steps)
    ]
    calculated = calculate_duration(normalized_steps)
    estimate = workout.get("estimated_duration_seconds")
    if estimate is not None:
        estimate = int(
            _nonnegative_number(estimate, "$.workout.estimated_duration_seconds")
        )
    elif calculated is not None:
        estimate = int(calculated)
    else:
        estimate = 0

    expanded = expanded_step_count(normalized_steps)
    if expanded > MAX_EXPANDED_STEPS:
        _error(
            "$.workout.steps",
            f"expands to {expanded} executable steps; maximum is {MAX_EXPANDED_STEPS}",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workout": {
            "key": key.strip(),
            "name": name.strip(),
            "description": description,
            "sport": sport,
            "estimated_duration_seconds": estimate,
            "steps": normalized_steps,
        },
    }


def _validate_step(raw: Any, path: str, sport: str, depth: int) -> Dict[str, Any]:
    if depth > 4:
        _error(path, "repeat nesting cannot exceed four levels")
    step = _object(raw, path)
    step_type = step.get("type")
    if step_type == "repeat":
        _strict_keys(step, {"type", "count", "steps", "name", "notes"}, path)
        count = _integer(step.get("count"), f"{path}.count", 2, 99)
        children = step.get("steps")
        if not isinstance(children, list) or not children:
            _error(f"{path}.steps", "must be a non-empty array")
        return {
            "type": "repeat",
            "count": count,
            "steps": [
                _validate_step(child, f"{path}.steps[{index}]", sport, depth + 1)
                for index, child in enumerate(children)
            ],
            **_optional_text(step, path),
        }

    if step_type not in STEP_TYPES:
        _error(f"{path}.type", f"must be one of: {', '.join(STEP_TYPES)} or repeat")
    _strict_keys(step, {"type", "name", "notes", "duration", "target"}, path)
    duration = _validate_duration(step.get("duration"), f"{path}.duration")
    target = _validate_target(
        step.get("target", {"type": "open"}), f"{path}.target", sport
    )
    return {
        "type": step_type,
        "duration": duration,
        "target": target,
        **_optional_text(step, path),
    }


def _optional_text(step: Dict[str, Any], path: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for field, limit in (("name", 80), ("notes", 512)):
        value = step.get(field)
        if value is not None:
            if not isinstance(value, str) or len(value) > limit:
                _error(
                    f"{path}.{field}",
                    f"must be a string of at most {limit} characters",
                )
            result[field] = value
    return result


def _validate_duration(raw: Any, path: str) -> Dict[str, Any]:
    duration = _object(raw, path)
    kind = duration.get("type")
    if kind == "time":
        _strict_keys(duration, {"type", "seconds"}, path)
        return {
            "type": kind,
            "seconds": _positive_number(duration.get("seconds"), f"{path}.seconds"),
        }
    if kind == "distance":
        _strict_keys(duration, {"type", "meters"}, path)
        return {
            "type": kind,
            "meters": _positive_number(duration.get("meters"), f"{path}.meters"),
        }
    if kind == "lap_button":
        _strict_keys(duration, {"type"}, path)
        return {"type": kind}
    _error(f"{path}.type", "must be time, distance, or lap_button")


def _validate_target(raw: Any, path: str, sport: str) -> Dict[str, Any]:
    target = _object(raw, path)
    kind = target.get("type")
    if kind == "open":
        _strict_keys(target, {"type"}, path)
        return {"type": kind}
    if kind == "heart_rate":
        _strict_keys(target, {"type", "min_bpm", "max_bpm"}, path)
        low = _integer(target.get("min_bpm"), f"{path}.min_bpm", 30, 250)
        high = _integer(target.get("max_bpm"), f"{path}.max_bpm", 30, 250)
        if low >= high:
            _error(path, "min_bpm must be less than max_bpm")
        return {"type": kind, "min_bpm": low, "max_bpm": high}
    if kind == "heart_rate_zone":
        _strict_keys(target, {"type", "zone"}, path)
        return {
            "type": kind,
            "zone": _integer(target.get("zone"), f"{path}.zone", 1, 5),
        }
    if kind == "pace":
        if sport != "running":
            _error(path, "pace targets are supported only for running")
        _strict_keys(
            target, {"type", "min_seconds_per_km", "max_seconds_per_km"}, path
        )
        fastest = _positive_number(
            target.get("min_seconds_per_km"), f"{path}.min_seconds_per_km"
        )
        slowest = _positive_number(
            target.get("max_seconds_per_km"), f"{path}.max_seconds_per_km"
        )
        if fastest >= slowest:
            _error(path, "min_seconds_per_km must be less than max_seconds_per_km")
        return {
            "type": kind,
            "min_seconds_per_km": fastest,
            "max_seconds_per_km": slowest,
        }
    if kind == "power":
        _strict_keys(target, {"type", "min_watts", "max_watts"}, path)
        low = _integer(target.get("min_watts"), f"{path}.min_watts", 1, 3000)
        high = _integer(target.get("max_watts"), f"{path}.max_watts", 1, 3000)
        if low >= high:
            _error(path, "min_watts must be less than max_watts")
        return {"type": kind, "min_watts": low, "max_watts": high}
    if kind == "power_zone":
        _strict_keys(target, {"type", "zone"}, path)
        return {
            "type": kind,
            "zone": _integer(target.get("zone"), f"{path}.zone", 1, 7),
        }
    _error(f"{path}.type", f"must be one of: {', '.join(TARGET_TYPES)}")


def calculate_duration(steps: List[Dict[str, Any]]) -> float | None:
    total = 0.0
    for step in steps:
        if step["type"] == "repeat":
            child = calculate_duration(step["steps"])
            if child is None:
                return None
            total += step["count"] * child
        elif step["duration"]["type"] == "time":
            total += step["duration"]["seconds"]
        else:
            return None
    return total


def expanded_step_count(steps: List[Dict[str, Any]]) -> int:
    total = 0
    for step in steps:
        if step["type"] == "repeat":
            total += step["count"] * expanded_step_count(step["steps"])
        else:
            total += 1
    return total


def definition_hash(definition: Dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_garmin_workout(definition: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a validated public definition into Garmin workout JSON."""
    definition = validate_definition(definition)
    workout = definition["workout"]
    sport_id, display_order = SUPPORTED_SPORTS[workout["sport"]]
    sport = {
        "sportTypeId": sport_id,
        "sportTypeKey": workout["sport"],
        "displayOrder": display_order,
    }
    rendered_steps, _ = _render_steps(workout["steps"], 1)
    payload = {
        "workoutName": workout["name"],
        "sportType": sport,
        "estimatedDurationInSecs": workout["estimated_duration_seconds"],
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": rendered_steps}
        ],
    }
    if workout["description"] is not None:
        payload["description"] = workout["description"]
    return payload


def _render_steps(
    steps: List[Dict[str, Any]], next_order: int
) -> Tuple[List[Dict[str, Any]], int]:
    rendered: List[Dict[str, Any]] = []
    for step in steps:
        order = next_order
        next_order += 1
        if step["type"] == "repeat":
            children, next_order = _render_steps(step["steps"], next_order)
            rendered.append(
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": order,
                    "stepType": _step_type("repeat"),
                    "numberOfIterations": step["count"],
                    "workoutSteps": children,
                    "endCondition": _condition("iterations"),
                    "endConditionValue": float(step["count"]),
                    "smartRepeat": False,
                }
            )
            continue
        item = {
            "type": "ExecutableStepDTO",
            "stepOrder": order,
            "stepType": _step_type(step["type"]),
            **_render_duration(step["duration"]),
            **_render_target(step["target"]),
        }
        if step.get("notes"):
            item["description"] = step["notes"]
        rendered.append(item)
    return rendered, next_order


def _step_type(kind: str) -> Dict[str, Any]:
    if kind == "repeat":
        return {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}
    type_id, display = STEP_TYPES[kind]
    return {"stepTypeId": type_id, "stepTypeKey": kind, "displayOrder": display}


def _condition(kind: str) -> Dict[str, Any]:
    type_id, display, displayable = CONDITION_TYPES[kind]
    return {
        "conditionTypeId": type_id,
        "conditionTypeKey": kind.replace("_", ".") if kind == "lap_button" else kind,
        "displayOrder": display,
        "displayable": displayable,
    }


def _render_duration(duration: Dict[str, Any]) -> Dict[str, Any]:
    kind = duration["type"]
    result = {"endCondition": _condition(kind)}
    if kind == "time":
        result["endConditionValue"] = duration["seconds"]
    elif kind == "distance":
        result["endConditionValue"] = duration["meters"]
    return result


def _render_target(target: Dict[str, Any]) -> Dict[str, Any]:
    kind = target["type"]
    type_id, key, display = TARGET_TYPES[kind]
    result: Dict[str, Any] = {
        "targetType": {
            "workoutTargetTypeId": type_id,
            "workoutTargetTypeKey": key,
            "displayOrder": display,
        }
    }
    if kind == "heart_rate":
        result.update(
            targetValueOne=target["min_bpm"], targetValueTwo=target["max_bpm"]
        )
    elif kind == "heart_rate_zone":
        result["zoneNumber"] = target["zone"]
    elif kind == "pace":
        speeds = sorted(
            [
                1000.0 / target["min_seconds_per_km"],
                1000.0 / target["max_seconds_per_km"],
            ]
        )
        result.update(targetValueOne=speeds[0], targetValueTwo=speeds[1])
    elif kind == "power":
        result.update(
            targetValueOne=target["min_watts"], targetValueTwo=target["max_watts"]
        )
    elif kind == "power_zone":
        result["zoneNumber"] = target["zone"]
    return result
