"""
Version-two pool prescriptions, with explicit units and rest semantics.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from .workouts import WorkoutDefinitionError


STROKES = frozenset(
    {
        "freestyle",
        "backstroke",
        "breaststroke",
        "butterfly",
        "choice",
        "mixed",
        "individual_medley",
    }
)
DRILLS = frozenset({"kick", "pull", "drill"})
EQUIPMENT = frozenset({"fins", "kickboard", "paddles", "pull_buoy"})
STEP_TYPES = frozenset({"warmup", "interval", "recovery", "cooldown", "rest", "other"})
UNITS = frozenset({"meters", "yards"})


def _fail(path: str, message: str) -> None:
    raise WorkoutDefinitionError(f"{path}: {message}")


def _obj(value: Any, keys: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    if set(value) - keys:
        _fail(path, "unknown fields: " + ", ".join(sorted(set(value) - keys)))
    return value


def _number(value: Any, path: str, *, zero: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        _fail(path, "must be a finite number")
    if value > 1000000:
        _fail(path, "must not exceed 1000000")
    if value < 0 or (not zero and value == 0):
        _fail(path, "must be positive" if not zero else "must be non-negative")
    return float(value)


def _integer(value: Any, path: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(path, f"must be an integer from {low} to {high}")
    return value


def _text(value: Any, path: str, limit: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (required and not value.strip())
    ):
        _fail(
            path,
            f"must be {'a nonempty' if required else 'a'} string of at most {limit} characters",
        )
    return value.strip() if required else value


def _enum(value: Any, values: frozenset[str], path: str) -> str:
    if not isinstance(value, str) or value not in values:
        _fail(path, "must be one of: " + ", ".join(sorted(values)))
    return value


def distance_meters(duration: dict) -> float:
    """
    Convert an explicit swim distance without changing its display unit.
    """
    return duration["value"] * (0.9144 if duration["unit"] == "yards" else 1.0)


def _steps(
    raw: Any, pool: dict, path: str, depth: int = 0, budget: list[int] | None = None
) -> list[dict]:
    if budget is None:
        budget = [100]
    if depth > 4 or not isinstance(raw, list) or not raw or len(raw) > 100:
        _fail(path, "requires 1..100 steps and at most four nested repeats")
    result = []
    for index, value in enumerate(raw):
        budget[0] -= 1
        if budget[0] < 0:
            _fail(path, "maximum is 100 structural steps")
        here = f"{path}[{index}]"
        step = _obj(
            value,
            {"type", "count", "steps", "duration", "target", "swim", "name", "notes"},
            here,
        )
        kind = step.get("type")
        text = {
            key: _text(step[key], here + "." + key, limit)
            for key, limit in (("name", 80), ("notes", 200))
            if step.get(key) is not None
        }
        if kind == "repeat":
            _obj(step, {"type", "count", "steps", "name", "notes"}, here)
            result.append(
                {
                    "type": kind,
                    "count": _integer(step.get("count"), here + ".count", 2, 99),
                    "steps": _steps(
                        step.get("steps"), pool, here + ".steps", depth + 1, budget
                    ),
                    **text,
                }
            )
            continue
        _enum(kind, STEP_TYPES, here + ".type")
        _obj(step, {"type", "duration", "target", "swim", "name", "notes"}, here)
        duration = _obj(
            step.get("duration"),
            {"type", "seconds", "value", "unit"},
            here + ".duration",
        )
        duration_kind = _enum(
            duration.get("type"),
            frozenset({"time", "distance", "lap_button", "send_off"}),
            here + ".duration.type",
        )
        if duration_kind in {"time", "send_off"}:
            _obj(duration, {"type", "seconds"}, here + ".duration")
            duration = {
                "type": duration_kind,
                "seconds": _number(duration.get("seconds"), here + ".duration.seconds"),
            }
        elif duration_kind == "distance":
            _obj(duration, {"type", "value", "unit"}, here + ".duration")
            duration = {
                "type": "distance",
                "value": _number(duration.get("value"), here + ".duration.value"),
                "unit": _enum(duration.get("unit"), UNITS, here + ".duration.unit"),
            }
            lengths = distance_meters(duration) / (
                pool["length"] * (0.9144 if pool["unit"] == "yards" else 1)
            )
            if lengths < 1 or not math.isclose(
                lengths, round(lengths), rel_tol=0, abs_tol=1e-7
            ):
                _fail(
                    here + ".duration",
                    "distance must be a whole number of pool lengths",
                )
        else:
            _obj(duration, {"type"}, here + ".duration")
            duration = {"type": "lap_button"}
        target = _obj(step.get("target", {"type": "open"}), {"type"}, here + ".target")
        if target.get("type") != "open":
            _fail(
                here + ".target",
                "pool workouts currently support open targets; preserve effort and pace instructions in notes",
            )
        if duration_kind == "send_off":
            if (
                depth == 0
                or kind != "rest"
                or not result
                or result[-1]["type"] in {"repeat", "rest"}
                or result[-1]["duration"]["type"] != "distance"
            ):
                _fail(
                    here,
                    "send-off rest must immediately follow a distance swim inside a repeat",
                )
        extra = {}
        if kind == "rest":
            if duration_kind == "distance" or "swim" in step:
                _fail(here, "rest cannot have swimming attributes or distance")
        else:
            swim = _obj(
                step.get("swim", {}), {"stroke", "drill", "equipment"}, here + ".swim"
            )
            normalized = {
                "stroke": _enum(
                    swim.get("stroke", "choice"), STROKES, here + ".swim.stroke"
                )
            }
            for key, values in (("drill", DRILLS), ("equipment", EQUIPMENT)):
                if key in swim:
                    normalized[key] = _enum(swim[key], values, here + ".swim." + key)
            extra["swim"] = normalized
        result.append(
            {
                "type": kind,
                "duration": duration,
                "target": {"type": "open"},
                **extra,
                **text,
            }
        )
    return result


def expanded_steps(steps: list[dict]):
    """
    Yield executable steps, including the final rest of each repeat.
    """
    for step in steps:
        if step["type"] == "repeat":
            for _ in range(step["count"]):
                yield from expanded_steps(step["steps"])
        else:
            yield step


def validate_pool_definition(raw: Any) -> dict:
    """
    Validate the bounded provider-neutral pool-workout contract.
    """
    root = _obj(raw, {"schema_version", "workout"}, "$")
    if type(root.get("schema_version")) is not int or root["schema_version"] != 2:
        _fail("$.schema_version", "must equal 2 for pool swimming")
    w = _obj(
        root.get("workout"),
        {
            "key",
            "family",
            "version",
            "name",
            "description",
            "sport",
            "pool",
            "estimated_duration_seconds",
            "steps",
        },
        "$.workout",
    )
    if w.get("sport") != "swimming":
        _fail(
            "$.workout.sport",
            "schema 2 requires swimming; running and cycling retain schema 1",
        )
    family = w.get("family")
    if not isinstance(family, str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", family
    ):
        _fail("$.workout.family", "must be a lowercase hyphenated identifier")
    version = _integer(w.get("version"), "$.workout.version", 1, 2147483647)
    key = _text(w.get("key"), "$.workout.key", 100, required=True)
    if key != f"{family}-v{version}":
        _fail("$.workout.key", "must equal '<family>-v<version>'")
    pool = _obj(w.get("pool"), {"length", "unit"}, "$.workout.pool")
    pool = {
        "length": _number(pool.get("length"), "$.workout.pool.length"),
        "unit": _enum(pool.get("unit"), UNITS, "$.workout.pool.unit"),
    }
    steps = _steps(w.get("steps"), pool, "$.workout.steps")
    count, seconds, all_time = 0, 0.0, True
    for step in expanded_steps(steps):
        count += 1
        if count > 100:
            _fail("$.workout.steps", "maximum is 100 expanded steps")
        duration = step["duration"]
        if duration["type"] == "time":
            seconds += duration["seconds"]
        else:
            all_time = False
    estimate = w.get("estimated_duration_seconds")
    estimate = (
        _number(estimate, "$.workout.estimated_duration_seconds", zero=True)
        if estimate is not None
        else seconds if all_time else 0
    )
    return {
        "schema_version": 2,
        "workout": {
            "key": key,
            "family": family,
            "version": version,
            "name": _text(w.get("name"), "$.workout.name", 80, required=True),
            "description": _text(w.get("description"), "$.workout.description", 512),
            "sport": "swimming",
            "pool": pool,
            "steps": steps,
            "estimated_duration_seconds": int(estimate),
        },
    }


def summarize_pool_definition(definition: dict) -> dict:
    """
    Keep fixed rest separate from send-off cycles and preserve coaching notes.
    """
    w = definition["workout"]
    steps = list(expanded_steps(w["steps"]))
    distance = sum(
        distance_meters(s["duration"])
        for s in steps
        if s["duration"]["type"] == "distance"
    )
    fixed_rest = sum(
        s["duration"]["seconds"]
        for s in steps
        if s["type"] == "rest" and s["duration"]["type"] == "time"
    )
    send_off = [
        s["duration"]["seconds"] for s in steps if s["duration"]["type"] == "send_off"
    ]
    outline = []

    def visit(items: list[dict], parent: str = "", multiplier: int = 1) -> None:
        for index, step in enumerate(items, 1):
            path = f"{parent}.{index}" if parent else str(index)
            outline.append(
                {
                    "path": path,
                    "repeat_multiplier": multiplier,
                    **copy.deepcopy({k: v for k, v in step.items() if k != "steps"}),
                }
            )
            if step["type"] == "repeat":
                visit(step["steps"], path, multiplier * step["count"])

    visit(w["steps"])
    return {
        **{
            k: copy.deepcopy(w[k])
            for k in (
                "key",
                "family",
                "version",
                "name",
                "description",
                "sport",
                "pool",
                "estimated_duration_seconds",
            )
        },
        "structural_step_count": len(outline),
        "expanded_step_count": len(steps),
        "timed_seconds": round(
            sum(
                s["duration"]["seconds"]
                for s in steps
                if s["duration"]["type"] == "time"
            ),
            3,
        ),
        "distance_meters": round(distance, 3),
        "distance_display": {
            "value": round(
                distance / (0.9144 if w["pool"]["unit"] == "yards" else 1), 3
            ),
            "unit": w["pool"]["unit"],
        },
        "fixed_rest_seconds": round(fixed_rest, 3),
        "send_off_step_count": len(send_off),
        "send_off_cycle_seconds": round(sum(send_off), 3),
        "lap_button_step_count": sum(
            s["duration"]["type"] == "lap_button" for s in steps
        ),
        "target_type_counts": {"open": len(steps)},
        "step_outline": outline,
    }
