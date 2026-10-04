"""
Translate version-two pool workouts to Garmin's private transport format.
"""

from __future__ import annotations

from typing import Any

from .pool_workouts import summarize_pool_definition


STROKES = {
    "choice": (1, "any_stroke"),
    "backstroke": (2, "backstroke"),
    "breaststroke": (3, "breaststroke"),
    "butterfly": (5, "fly"),
    "freestyle": (6, "free"),
    "individual_medley": (7, "individual_medley"),
    "mixed": (8, "mixed"),
}
EQUIPMENT = {"fins": 1, "kickboard": 2, "paddles": 3, "pull_buoy": 4}
DRILLS = {"kick": 1, "pull": 2, "drill": 3}
STEPS = {
    "warmup": 1,
    "cooldown": 2,
    "interval": 8,
    "recovery": 4,
    "rest": 5,
    "other": 7,
}
CONDITIONS = {
    "time": (2, "time"),
    "distance": (3, "distance"),
    "lap_button": (1, "lap.button"),
    "send_off": (9, "fixed.repetition"),
}


def _unit(unit: str) -> dict[str, Any]:
    """
    Return the reviewed unit descriptor; values retain these units.
    """
    return (
        {"unitId": 230, "unitKey": "yard", "factor": 91.44}
        if unit == "yards"
        else {"unitId": 1, "unitKey": "meter", "factor": 100}
    )


def _enum(prefix: str, identifier: int, key: str) -> dict[str, Any]:
    """
    Build a provider enumeration without exposing it in domain records.
    """
    return {prefix + "Id": identifier, prefix + "Key": key, "displayOrder": identifier}


def render_pool_workout(definition: dict[str, Any]) -> dict[str, Any]:
    """
    Render an already validated pool prescription without provider calls.
    """
    workout = definition["workout"]
    order = 0

    def render(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal order
        result = []
        for step in steps:
            order += 1
            item: dict[str, Any] = {"stepOrder": order}
            if step["type"] == "repeat":
                item.update(
                    type="RepeatGroupDTO",
                    stepType=_enum("stepType", 6, "repeat"),
                    numberOfIterations=step["count"],
                    endConditionValue=float(step["count"]),
                    endCondition={
                        "conditionTypeId": 7,
                        "conditionTypeKey": "iterations",
                        "displayOrder": 7,
                        "displayable": False,
                    },
                    smartRepeat=False,
                    skipLastRestStep=False,
                    workoutSteps=render(step["steps"]),
                )
            else:
                duration = step["duration"]
                identifier, key = CONDITIONS[duration["type"]]
                kind = "main" if step["type"] == "interval" else step["type"]
                item.update(
                    type="ExecutableStepDTO",
                    stepType=_enum("stepType", STEPS[step["type"]], kind),
                    endCondition={
                        "conditionTypeId": identifier,
                        "conditionTypeKey": key,
                        "displayOrder": identifier,
                        "displayable": True,
                    },
                    targetType=_enum("workoutTargetType", 1, "no.target"),
                )
                if duration["type"] == "distance":
                    item.update(
                        endConditionValue=duration["value"],
                        preferredEndConditionUnit=_unit(duration["unit"]),
                    )
                elif duration["type"] != "lap_button":
                    item["endConditionValue"] = duration["seconds"]
                swim = step.get("swim")
                if swim:
                    stroke_id, stroke_key = STROKES[swim["stroke"]]
                    item["strokeType"] = _enum("strokeType", stroke_id, stroke_key)
                    if "equipment" in swim:
                        item["equipmentType"] = _enum(
                            "equipmentType",
                            EQUIPMENT[swim["equipment"]],
                            swim["equipment"],
                        )
                    if "drill" in swim:
                        item["drillType"] = _enum(
                            "drillType", DRILLS[swim["drill"]], swim["drill"]
                        )
            if step.get("notes"):
                item["description"] = step["notes"]
            result.append(item)
        return result

    sport = {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3}
    result = {
        "workoutName": workout["name"],
        "sportType": sport,
        "poolLength": workout["pool"]["length"],
        "poolLengthUnit": _unit(workout["pool"]["unit"]),
        "estimatedDurationInSecs": workout["estimated_duration_seconds"],
        "estimatedDistanceInMeters": summarize_pool_definition(definition)[
            "distance_meters"
        ],
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": sport,
                "workoutSteps": render(workout["steps"]),
            }
        ],
    }
    if workout["description"] is not None:
        result["description"] = workout["description"]
    return result
