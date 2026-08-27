import json

import pytest

from garmin_health_data.activity_trims import load_activity_trims


def test_load_activity_trims_normalizes_utc(tmp_path):
    path = tmp_path / "trims.json"
    path.write_text(
        json.dumps({"123": {"end_utc": "2026-08-27T15:44:57Z"}}),
        encoding="utf-8",
    )

    trim = load_activity_trims(str(path))[123]

    assert trim.end_utc.isoformat() == "2026-08-27T15:44:57+00:00"
    assert trim.contains(trim.end_utc.replace(microsecond=0)) is False


def test_load_activity_trims_requires_timezone(tmp_path):
    path = tmp_path / "trims.json"
    path.write_text(
        json.dumps({"123": {"end_utc": "2026-08-27T15:44:57"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must include a timezone"):
        load_activity_trims(str(path))
