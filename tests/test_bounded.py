"""Tests for bounded source capture and offline replay."""

import hashlib
import io
import json
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from garmin_health_data.bounded import (
    AGREED_BOUNDED_CATEGORIES,
    BoundedArchiveError,
    SourceBindingError,
    capture_bounded_source,
    replay_bounded_source,
)
from garmin_health_data.garmin_client import GarminClient


class FakeTransportClient(GarminClient):
    """Real Garmin API wrappers backed by deterministic in-memory responses."""

    def __init__(self, activities: List[Dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.display_name = "test-display"
        self.full_name = "Test Athlete"
        self.activities = activities or []
        self.download_bytes = b""
        self.fail_paths: Dict[str, Exception] = {}

    def _connectapi(self, path: str, **kwargs: Any) -> Any:
        for fragment, error in self.fail_paths.items():
            if fragment in path:
                raise error
        if path.endswith("/user-settings"):
            return {"id": 42, "weight": 70000, "unknownSourceField": "retained"}
        if "activities/search/activities" in path:
            start = int((kwargs.get("params") or {}).get("start", 0))
            return self.activities[start : start + 20]
        if "weight/range" in path:
            return {
                "dailyWeightSummaries": [],
                "totalAverage": {"sourceOnly": "must survive raw capture"},
            }
        return {}

    def _download(self, path: str, **kwargs: Any) -> bytes:
        return self.download_bytes


def _fixed_clock() -> datetime:
    return datetime(2026, 9, 12, 12, 34, 56, tzinfo=timezone.utc)


def test_agreed_selection_is_explicit() -> None:
    assert AGREED_BOUNDED_CATEGORIES == (
        "ACTIVITIES_LIST",
        "ACTIVITY",
        "ACTIVITY_HR_ZONES",
        "ACTIVITY_POWER_ZONES",
        "SLEEP",
        "STRESS",
        "STEPS",
        "TRAINING_READINESS",
        "TRAINING_STATUS",
        "USER_PROFILE",
        "BODY_COMPOSITION",
        "RACE_PREDICTIONS",
        "RUNNING_TOLERANCE",
    )


def test_capture_reports_category_coverage_and_current_state(tmp_path: Path) -> None:
    result = capture_bounded_source(
        client=FakeTransportClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 3),
        archive_dir=tmp_path / "archive",
        clock=_fixed_clock,
    )

    manifest = result.manifest
    assert manifest["requested_categories"] == list(AGREED_BOUNDED_CATEGORIES)
    assert manifest["requested_interval"] == {
        "start_inclusive": "2026-09-01",
        "end_exclusive": "2026-09-03",
    }
    assert set(manifest["categories"]) >= set(AGREED_BOUNDED_CATEGORIES)
    assert all(
        manifest["categories"][name]["status"] == "success"
        for name in AGREED_BOUNDED_CATEGORIES
    )
    assert manifest["categories"]["USER_PROFILE"]["coverage"] == {
        "kind": "current_state",
        "fetched_at": "2026-09-12T12:34:56Z",
    }
    assert manifest["categories"]["SLEEP"]["coverage"]["end_exclusive"] == (
        "2026-09-03"
    )
    assert not (result.archive_dir / "manifest.incomplete.json").exists()


def test_capture_keeps_each_pagination_page_before_split(tmp_path: Path) -> None:
    activities = [
        {"activityId": value, "startTimeLocal": "2026-09-01T08:00:00.0"}
        for value in range(1, 21)
    ]
    activities.append(
        {"activityId": 21, "startTimeLocal": "not-a-date", "rawOnly": True}
    )
    result = capture_bounded_source(
        client=FakeTransportClient(activities),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("ACTIVITIES_LIST",),
        clock=_fixed_clock,
    )

    pages = [
        request
        for request in result.manifest["requests"]
        if request["category"] == "ACTIVITIES_LIST"
    ]
    assert [page["params"]["start"] for page in pages] == ["0", "20", "40"]
    raw_second_page = json.loads(
        (result.archive_dir / pages[1]["object_path"]).read_text(encoding="utf-8")
    )
    assert raw_second_page == [
        {"activityId": 21, "rawOnly": True, "startTimeLocal": "not-a-date"}
    ]
    normalized = list(result.normalized_ingest_dir.glob("*_ACTIVITIES_LIST_*.json"))
    assert len(normalized) == 1
    assert len(json.loads(normalized[0].read_text(encoding="utf-8"))) == 20


def test_original_zip_is_archived_before_entry_selection(tmp_path: Path) -> None:
    client = FakeTransportClient(
        [{"activityId": 99, "startTimeLocal": "2026-09-01T08:00:00.0"}]
    )
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("activity.tcx", b"<TrainingCenterDatabase />")
        archive.writestr("source-sidecar.txt", b"retained only in original archive")
    client.download_bytes = zipped.getvalue()

    result = capture_bounded_source(
        client=client,
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("ACTIVITIES_LIST", "ACTIVITY"),
        clock=_fixed_clock,
    )

    request = next(
        item for item in result.manifest["requests"] if item["category"] == "ACTIVITY"
    )
    archived = (result.archive_dir / request["object_path"]).read_bytes()
    assert archived == zipped.getvalue()
    assert request["sha256"] == hashlib.sha256(zipped.getvalue()).hexdigest()
    assert request["content_type"] == "application/zip"
    extracted = list(result.normalized_ingest_dir.glob("*_ACTIVITY_*.tcx"))
    assert extracted[0].read_bytes() == b"<TrainingCenterDatabase />"


def test_capture_verifies_profile_before_other_categories(tmp_path: Path) -> None:
    sink_paths: List[str] = []

    with pytest.raises(SourceBindingError):
        capture_bounded_source(
            client=FakeTransportClient(),
            user_id="wrong-account",
            source_binding="owner-hash",
            start_date=date(2026, 9, 1),
            end_date_exclusive=date(2026, 9, 2),
            archive_dir=tmp_path / "archive",
            categories=("SLEEP",),
            archive_sink=lambda path, body: sink_paths.append(path),
            clock=_fixed_clock,
        )

    incomplete = json.loads(
        (tmp_path / "archive" / "manifest.incomplete.json").read_text()
    )
    assert incomplete["state"] == "interrupted"
    assert [item["category"] for item in incomplete["requests"]] == ["USER_PROFILE"]
    assert "objects/" in sink_paths[1]
    assert all("SLEEP" not in path for path in sink_paths)

    checkpoint = (tmp_path / "archive" / "manifest.incomplete.json").read_bytes()
    with pytest.raises(BoundedArchiveError, match="must be empty"):
        capture_bounded_source(
            client=FakeTransportClient(),
            user_id="42",
            source_binding="owner-hash",
            start_date=date(2026, 9, 1),
            end_date_exclusive=date(2026, 9, 2),
            archive_dir=tmp_path / "archive",
            categories=("SLEEP",),
            clock=_fixed_clock,
        )
    assert (
        tmp_path / "archive" / "manifest.incomplete.json"
    ).read_bytes() == checkpoint


def test_optional_404_is_unavailable_and_default_replay_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from garmin_health_data.garmin_client.exceptions import GarminConnectionError

    monkeypatch.setattr("garmin_health_data.extractor._RETRY_BACKOFFS", ())
    client = FakeTransportClient()
    client.fail_paths["runningtolerance"] = GarminConnectionError("API Error 404")
    capture = capture_bounded_source(
        client=client,
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("RUNNING_TOLERANCE",),
        clock=_fixed_clock,
    )

    outcome = capture.manifest["categories"]["RUNNING_TOLERANCE"]
    assert outcome["status"] == "unavailable"
    assert outcome["complete"] is False
    assert outcome["failures"] == [
        {
            "date": "2026-09-01..2026-09-01",
            "activity_id": "",
            "error_type": "GarminConnectionError",
            "http_status": "404",
        }
    ]
    with pytest.raises(BoundedArchiveError, match="incomplete categories"):
        replay_bounded_source(
            archive_dir=capture.archive_dir,
            staging_db_path=tmp_path / "staging.db",
        )
    partial = replay_bounded_source(
        archive_dir=capture.archive_dir,
        staging_db_path=tmp_path / "partial.db",
        allow_incomplete=True,
    )
    assert partial.categories["RUNNING_TOLERANCE"]["replay_status"] == "failed"
    assert partial.categories["RUNNING_TOLERANCE"]["complete"] is False


def test_offline_replay_is_duplicate_safe_and_recovers_process_files(
    tmp_path: Path,
) -> None:
    class BodyCompositionClient(FakeTransportClient):
        def _connectapi(self, path: str, **kwargs: Any) -> Any:
            if path.endswith("/user-settings"):
                return super()._connectapi(path, **kwargs)
            if "weight/range" in path:
                return {
                    "dailyWeightSummaries": [
                        {
                            "summaryDate": "2026-09-01",
                            "allWeightMetrics": [
                                {
                                    "timestampGMT": 1788264000000,
                                    "weight": 70000.0,
                                    "sourceType": "INDEX_SCALE",
                                }
                            ],
                        }
                    ],
                    "totalAverage": {"weight": 70000.0},
                }
            raise AssertionError(f"Unexpected live-style path: {path}")

    capture = capture_bounded_source(
        client=BodyCompositionClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("BODY_COMPOSITION",),
        clock=_fixed_clock,
    )
    staging_db = tmp_path / "staging.db"
    files_root = tmp_path / "files"

    first = replay_bounded_source(
        archive_dir=capture.archive_dir,
        staging_db_path=staging_db,
        files_root=files_root,
    )
    connection = sqlite3.connect(staging_db)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM body_composition").fetchone()[0]
            == 1
        )
    finally:
        connection.close()

    stored = next((files_root / "storage").glob("*_BODY_COMPOSITION_*.json"))
    shutil.copy2(stored, files_root / "process" / stored.name)
    second = replay_bounded_source(
        archive_dir=capture.archive_dir,
        staging_db_path=staging_db,
        files_root=files_root,
    )
    renamed_db = tmp_path / "renamed-staging.db"
    staging_db.rename(renamed_db)
    connection = sqlite3.connect(renamed_db)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM body_composition").fetchone()[0]
            == 1
        )
    finally:
        connection.close()
    renamed_db.unlink()

    assert first.quarantined_files == 0
    assert second.quarantined_files == 0
    assert second.recovered_files == 1


def test_replay_rejects_archive_path_traversal(tmp_path: Path) -> None:
    capture = capture_bounded_source(
        client=FakeTransportClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("BODY_COMPOSITION",),
        clock=_fixed_clock,
    )
    manifest = capture.manifest
    manifest["requests"][0]["object_path"] = "../outside.json"
    capture.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BoundedArchiveError, match="safe relative"):
        replay_bounded_source(
            archive_dir=capture.archive_dir,
            staging_db_path=tmp_path / "staging.db",
        )


def test_sink_persists_raw_response_before_split_and_fails_closed(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    saw_unsplit_response = False

    def sink(path: str, body: bytes) -> None:
        nonlocal saw_unsplit_response
        if path.startswith("objects/") and b"sourceOnly" in body:
            saw_unsplit_response = True
            assert not list((archive_dir / "work" / "ingest").glob("*.json"))

    capture_bounded_source(
        client=FakeTransportClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=archive_dir,
        categories=("BODY_COMPOSITION",),
        archive_sink=sink,
        clock=_fixed_clock,
    )
    assert saw_unsplit_response

    failed_dir = tmp_path / "failed-archive"
    failed_once = False

    def failing_sink(path: str, body: bytes) -> None:
        nonlocal failed_once
        if path.startswith("objects/") and b"sourceOnly" in body and not failed_once:
            failed_once = True
            raise OSError("synthetic durable sink failure")

    failed = capture_bounded_source(
        client=FakeTransportClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=failed_dir,
        categories=("BODY_COMPOSITION",),
        archive_sink=failing_sink,
        clock=_fixed_clock,
    )
    outcome = failed.manifest["categories"]["BODY_COMPOSITION"]
    assert outcome["status"] == "failed"
    assert outcome["complete"] is False


def test_processor_failure_is_reported_by_category(tmp_path: Path) -> None:
    capture = capture_bounded_source(
        client=FakeTransportClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("USER_PROFILE",),
        clock=_fixed_clock,
    )

    replay = replay_bounded_source(
        archive_dir=capture.archive_dir,
        staging_db_path=tmp_path / "staging.db",
    )
    outcome = replay.categories["USER_PROFILE"]
    assert replay.quarantined_files == 1
    assert outcome["status"] == "success"
    assert outcome["replay_status"] == "failed"
    assert outcome["normalized_files_quarantined"] == 1
    assert outcome["replay_failures"] == [{"error_type": "KeyError"}]


def test_auth_endpoint_is_never_archived_even_in_data_context(tmp_path: Path) -> None:
    class UnsafeClient(FakeTransportClient):
        def get_sleep_data(self, cdate: str) -> Dict[str, Any]:
            return self._connectapi("/oauth-service/token")

    capture = capture_bounded_source(
        client=UnsafeClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2026, 9, 1),
        end_date_exclusive=date(2026, 9, 2),
        archive_dir=tmp_path / "archive",
        categories=("SLEEP",),
        clock=_fixed_clock,
    )

    assert not [
        request
        for request in capture.manifest["requests"]
        if request["category"] == "SLEEP"
    ]
    outcome = capture.manifest["categories"]["SLEEP"]
    assert outcome["status"] == "failed"
    assert outcome["failures"][0]["error_type"] == "BoundedArchiveError"


def test_replay_preserves_current_state_fetch_date_in_filename(
    tmp_path: Path,
) -> None:
    class ValidProfileClient(FakeTransportClient):
        def _connectapi(self, path: str, **kwargs: Any) -> Any:
            if path.endswith("/user-settings"):
                return {"id": 42, "userData": {"birthDate": None}}
            return super()._connectapi(path, **kwargs)

    def archived_clock() -> datetime:
        return datetime(2024, 2, 3, 23, 45, tzinfo=timezone.utc)

    capture = capture_bounded_source(
        client=ValidProfileClient(),
        user_id="42",
        source_binding="owner-hash",
        start_date=date(2020, 1, 1),
        end_date_exclusive=date(2020, 1, 2),
        archive_dir=tmp_path / "archive",
        categories=("USER_PROFILE",),
        clock=archived_clock,
    )
    replay = replay_bounded_source(
        archive_dir=capture.archive_dir,
        staging_db_path=tmp_path / "staging.db",
    )

    stored = list((replay.files_root / "storage").glob("*_USER_PROFILE_*.json"))
    assert len(stored) == 1
    assert "USER_PROFILE_2024-02-03T12-00-00Z" in stored[0].name
    assert "2020-01-01" not in stored[0].name
