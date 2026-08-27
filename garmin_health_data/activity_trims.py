"""User-authored activity trim windows applied during FIT processing."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ActivityTrim:
    """Inclusive start and exclusive end bounds for one activity."""

    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None

    def contains(self, timestamp: datetime) -> bool:
        """Return whether a UTC timestamp is inside this trim window."""
        value = timestamp.astimezone(timezone.utc)
        if self.start_utc is not None and value < self.start_utc:
            return False
        if self.end_utc is not None and value >= self.end_utc:
            return False
        return True


def _parse_utc(value: Any, field: str, activity_id: int) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Activity {activity_id} trim {field} must be an ISO string.")
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"Activity {activity_id} trim {field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def load_activity_trims(path: Optional[str]) -> Dict[int, ActivityTrim]:
    """Load a JSON mapping of activity IDs to trim windows."""
    if not path:
        return {}
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"Activity trim config does not exist: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Activity trim config must be a JSON object keyed by activity ID.")

    trims: Dict[int, ActivityTrim] = {}
    for raw_id, raw_trim in payload.items():
        try:
            activity_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid activity trim ID: {raw_id!r}") from exc
        if not isinstance(raw_trim, dict):
            raise ValueError(f"Activity {activity_id} trim must be an object.")
        trim = ActivityTrim(
            start_utc=_parse_utc(raw_trim.get("start_utc"), "start_utc", activity_id),
            end_utc=_parse_utc(raw_trim.get("end_utc"), "end_utc", activity_id),
        )
        if trim.start_utc is None and trim.end_utc is None:
            raise ValueError(f"Activity {activity_id} trim needs start_utc or end_utc.")
        if trim.start_utc and trim.end_utc and trim.start_utc >= trim.end_utc:
            raise ValueError(f"Activity {activity_id} trim start must precede end.")
        trims[activity_id] = trim
    return trims
