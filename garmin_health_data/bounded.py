"""
Bounded Garmin source capture and offline replay.

This module is the public integration boundary for hosted extractors. It records every
successful data response before the regular extractor can split, filter, or unpack it,
then replays those responses through the existing extractor and SQLite processor with
Garmin access disabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
)
from uuid import uuid4

from garmin_health_data.cli import (
    _classify_files_by_type,
    _group_files_by_user_and_timestamp,
    _partition_processable_and_backup,
)
from garmin_health_data.db import create_tables, get_session
from garmin_health_data.extractor import ExtractionFailure, GarminExtractor
from garmin_health_data.garmin_client import GarminClient
from garmin_health_data.lifecycle import (
    move_files_to_quarantine,
    move_files_to_storage,
    move_ingest_to_process,
    recover_stale_process,
    setup_lifecycle_dirs,
)
from garmin_health_data.processor import GarminProcessor
from garmin_health_data.processor_helpers import FileSet


ArchiveSink = Callable[[str, bytes], None]

AGREED_BOUNDED_CATEGORIES: Tuple[str, ...] = (
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

_CURRENT_STATE_CATEGORIES = {"USER_PROFILE", "RACE_PREDICTIONS"}
_OPTIONAL_CATEGORIES = {
    "BODY_COMPOSITION",
    "RACE_PREDICTIONS",
    "RUNNING_TOLERANCE",
}
_PER_ACTIVITY_CATEGORIES = {
    "ACTIVITY",
    "ACTIVITY_HR_ZONES",
    "ACTIVITY_POWER_ZONES",
    "EXERCISE_SETS",
    "MULTISPORT_CHILDREN",
}
_PUBLIC_METHOD_CATEGORIES = {
    "get_activities_by_date": "ACTIVITIES_LIST",
    "download_activity": "ACTIVITY",
    "get_activity_hr_zones": "ACTIVITY_HR_ZONES",
    "get_activity_power_zones": "ACTIVITY_POWER_ZONES",
    "get_activity_exercise_sets": "EXERCISE_SETS",
    "get_activity_details": "MULTISPORT_CHILDREN",
    "get_sleep_data": "SLEEP",
    "get_stress_data": "STRESS",
    "get_steps_data": "STEPS",
    "get_training_readiness": "TRAINING_READINESS",
    "get_training_status": "TRAINING_STATUS",
    "get_user_profile": "USER_PROFILE",
    "get_body_composition": "BODY_COMPOSITION",
    "get_race_predictions": "RACE_PREDICTIONS",
    "get_running_tolerance": "RUNNING_TOLERANCE",
}
_AUTH_PATH_MARKERS = (
    "/oauth",
    "/token",
    "/sso",
    "/signin",
    "/login",
)


class BoundedArchiveError(RuntimeError):
    """Raised when a bounded archive is unsafe, incomplete, or inconsistent."""


class SourceBindingError(BoundedArchiveError):
    """Raised when the authenticated Garmin account does not match the binding."""


class ArchivedResponseMissing(BoundedArchiveError):
    """Raised when replay asks for a successful response absent from the archive."""


@dataclass(frozen=True)
class CaptureResult:
    """Result of extraction-only source capture."""

    archive_dir: Path
    manifest_path: Path
    manifest: Dict[str, Any]
    normalized_ingest_dir: Path


@dataclass(frozen=True)
class ReplayResult:
    """Result of replay through the existing extractor and processor."""

    archive_dir: Path
    staging_db_path: Path
    files_root: Path
    categories: Dict[str, Dict[str, Any]]
    processed_files: int
    backup_only_files: int
    quarantined_files: int
    recovered_files: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with open(temporary, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _safe_failure(failure: ExtractionFailure) -> Dict[str, str]:
    first_line = failure.error.splitlines()[0]
    error_type = first_line.split(":", 1)[0][:100]
    http_status = re.search(
        r"\b(?:HTTP|API Error)\s*(\d{3})\b", first_line, re.IGNORECASE
    )
    result = {
        "date": failure.date,
        "activity_id": failure.activity_id,
        "error_type": error_type,
    }
    if http_status:
        result["http_status"] = http_status.group(1)
    return result


class _SourceRecorder:
    """Temporarily instrument an authenticated Garmin client at its data boundary."""

    def __init__(
        self,
        client: GarminClient,
        archive_dir: Path,
        manifest: Dict[str, Any],
        archive_sink: Optional[ArchiveSink],
        clock: Callable[[], datetime],
        no_date_file_dates: MutableMapping[str, date],
    ) -> None:
        self.client = client
        self.archive_dir = archive_dir
        self.manifest = manifest
        self.archive_sink = archive_sink
        self.clock = clock
        self.no_date_file_dates = no_date_file_dates
        self.current_category: Optional[str] = None
        self._original_methods: Dict[str, Any] = {}
        self._emitted_objects: set[str] = set()

    def __enter__(self) -> "_SourceRecorder":
        self._original_methods["_connectapi"] = self.client._connectapi
        self._original_methods["_download"] = self.client._download
        self.client._connectapi = self._capture_json  # type: ignore[method-assign]
        self.client._download = self._capture_binary  # type: ignore[method-assign]

        for method_name, category in _PUBLIC_METHOD_CATEGORIES.items():
            if not hasattr(self.client, method_name):
                continue
            original = getattr(self.client, method_name)
            self._original_methods[method_name] = original

            def categorized(
                *args: Any, _fn=original, _category=category, **kwargs: Any
            ):
                previous = self.current_category
                self.current_category = _category
                try:
                    return _fn(*args, **kwargs)
                finally:
                    self.current_category = previous

            setattr(self.client, method_name, categorized)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        for method_name, original in self._original_methods.items():
            setattr(self.client, method_name, original)

    def _capture_json(self, path: str, **kwargs: Any) -> Any:
        category = self._require_category(path)
        result = self._original_methods["_connectapi"](path, **kwargs)
        self._record(category, "GET", path, kwargs.get("params") or {}, "json", result)
        return result

    def _capture_binary(self, path: str, **kwargs: Any) -> bytes:
        category = self._require_category(path)
        result = self._original_methods["_download"](path, **kwargs)
        self._record(
            category, "GET", path, kwargs.get("params") or {}, "binary", result
        )
        return result

    def _require_category(self, path: str) -> str:
        normalized_path = f"/{path.lstrip('/').lower()}"
        if any(marker in normalized_path for marker in _AUTH_PATH_MARKERS):
            raise BoundedArchiveError(
                "Refusing to archive an authentication or token endpoint."
            )
        if self.current_category is None:
            raise BoundedArchiveError(
                f"Refusing to archive uncategorized endpoint {path!r}; authentication "
                "and token responses are outside the source archive."
            )
        return self.current_category

    def _record(
        self,
        category: str,
        method: str,
        path: str,
        params: Mapping[str, Any],
        response_kind: str,
        value: Any,
    ) -> None:
        body = value if isinstance(value, bytes) else _json_bytes(value)
        digest = hashlib.sha256(body).hexdigest()
        suffix = "bin" if isinstance(value, bytes) else "json"
        object_path = f"objects/{digest}.{suffix}"
        local_object = self.archive_dir / object_path
        if local_object.exists():
            if hashlib.sha256(local_object.read_bytes()).hexdigest() != digest:
                raise BoundedArchiveError(
                    f"Archive object hash mismatch: {object_path}."
                )
        else:
            _atomic_write(local_object, body)

        fetched_at = _iso_utc(self.clock())
        request = {
            "sequence": len(self.manifest["requests"]) + 1,
            "category": category,
            "method": method,
            "path": path,
            "params": dict(params),
            "response_kind": response_kind,
            "content_type": (
                "application/zip" if isinstance(value, bytes) else "application/json"
            ),
            "object_path": object_path,
            "sha256": digest,
            "byte_length": len(body),
            "fetched_at": fetched_at,
        }
        self.manifest["requests"].append(request)
        if category in _CURRENT_STATE_CATEGORIES:
            self.no_date_file_dates[category] = date.fromisoformat(fetched_at[:10])
        if self.archive_sink is not None and object_path not in self._emitted_objects:
            self.archive_sink(object_path, body)
            self._emitted_objects.add(object_path)
        self._checkpoint(int(request["sequence"]))

    def _checkpoint(self, sequence: int) -> None:
        body = _json_bytes(self.manifest)
        _atomic_write(self.archive_dir / "manifest.incomplete.json", body)
        if self.archive_sink is not None:
            self.archive_sink(f"checkpoints/{sequence:06d}.json", body)


def _coverage(
    category: str,
    start_date: date,
    end_date_exclusive: date,
    fetched_at: str,
) -> Dict[str, str]:
    if category in _CURRENT_STATE_CATEGORIES:
        return {"kind": "current_state", "fetched_at": fetched_at}
    return {
        "kind": "interval",
        "start_inclusive": start_date.isoformat(),
        "end_exclusive": end_date_exclusive.isoformat(),
    }


def _category_results(
    requested: Iterable[str],
    requests_: List[Dict[str, Any]],
    failures: List[ExtractionFailure],
    start_date: date,
    end_date_exclusive: date,
    fetched_at: str,
) -> Dict[str, Dict[str, Any]]:
    request_counts: Dict[str, int] = defaultdict(int)
    last_fetch_by_category: Dict[str, str] = {}
    for request in requests_:
        request_counts[request["category"]] += 1
        last_fetch_by_category[request["category"]] = request["fetched_at"]
    failure_groups: Dict[str, List[ExtractionFailure]] = defaultdict(list)
    for failure in failures:
        failure_groups[failure.data_type].append(failure)

    categories = list(dict.fromkeys(requested))
    categories.extend(
        sorted((set(request_counts) | set(failure_groups)) - set(categories))
    )
    results: Dict[str, Dict[str, Any]] = {}
    for category in categories:
        category_failures = failure_groups.get(category, [])
        safe_failures = [_safe_failure(item) for item in category_failures]
        if category_failures:
            unavailable = category in _OPTIONAL_CATEGORIES and all(
                "API Error 404" in item.error for item in category_failures
            )
            status = "unavailable" if unavailable else "failed"
            complete = False
        else:
            status = "success"
            complete = True

        if (
            status == "success"
            and request_counts[category] == 0
            and category not in _PER_ACTIVITY_CATEGORIES
        ):
            status = "failed"
            complete = False
            safe_failures.append(
                {
                    "date": "",
                    "activity_id": "",
                    "error_type": "NoSourceResponse",
                }
            )

        results[category] = {
            "status": status,
            "complete": complete,
            "coverage": _coverage(
                category,
                start_date,
                end_date_exclusive,
                last_fetch_by_category.get(category, fetched_at),
            ),
            "request_count": request_counts[category],
            "failures": safe_failures,
        }

    activity_list_failed = bool(failure_groups.get("ACTIVITIES_LIST"))
    activity_list_captured = request_counts["ACTIVITIES_LIST"] > 0
    for category in set(requested) & _PER_ACTIVITY_CATEGORIES:
        if activity_list_failed or not activity_list_captured:
            outcome = results[category]
            outcome["status"] = "failed"
            outcome["complete"] = False
            outcome["failures"].append(
                {
                    "date": "",
                    "activity_id": "",
                    "error_type": "ActivityListDependencyFailure",
                }
            )
    return results


def capture_bounded_source(
    *,
    client: GarminClient,
    user_id: str,
    source_binding: str,
    start_date: date,
    end_date_exclusive: date,
    archive_dir: Path,
    categories: Iterable[str] = AGREED_BOUNDED_CATEGORIES,
    run_id: Optional[str] = None,
    archive_sink: Optional[ArchiveSink] = None,
    clock: Callable[[], datetime] = _utc_now,
) -> CaptureResult:
    """
    Capture one explicit Garmin interval without loading SQLite.

    ``client`` must already be authenticated. The function verifies its authoritative
    profile ID before extracting any other category. The exclusive end is converted to
    the existing extractor's inclusive internal end without weakening the public date
    contract.

    :param client: Authenticated Garmin client owned by the caller.
    :param user_id: Expected authoritative Garmin profile ID.
    :param source_binding: Stable opaque source-owner binding.
    :param start_date: First calendar date to fetch, inclusive.
    :param end_date_exclusive: Calendar date immediately after the fetch window.
    :param archive_dir: Empty directory for this immutable run archive.
    :param categories: Explicit data categories to fetch.
    :param run_id: Optional caller-assigned run ID.
    :param archive_sink: Optional synchronous durable-object callback.
    :param clock: UTC clock override for deterministic tests.
    :return: Capture metadata and completed manifest.
    """
    archive_dir = Path(archive_dir)
    requested = tuple(dict.fromkeys(categories))
    if not requested:
        raise ValueError("At least one explicit category is required.")
    if end_date_exclusive <= start_date:
        raise ValueError("end_date_exclusive must be after start_date.")
    if not str(user_id) or not source_binding:
        raise ValueError("user_id and source_binding are required.")
    if archive_dir.exists() and any(archive_dir.iterdir()):
        raise BoundedArchiveError(
            f"Archive directory must be empty for a new run: {archive_dir}."
        )
    archive_dir.mkdir(parents=True, exist_ok=True)
    normalized_ingest = archive_dir / "work" / "ingest"
    normalized_ingest.mkdir(parents=True, exist_ok=True)

    started = _iso_utc(clock())
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "state": "capturing",
        "provider": "garmin",
        "source_binding": source_binding,
        "source_account_id": str(user_id),
        "source_binding_verified": False,
        "source_routing": {
            "display_name": client.display_name,
            "full_name": client.full_name,
        },
        "run_id": run_id or uuid4().hex,
        "requested_interval": {
            "start_inclusive": start_date.isoformat(),
            "end_exclusive": end_date_exclusive.isoformat(),
        },
        "fetched_at_start": started,
        "fetched_at_end": None,
        "requested_categories": list(requested),
        "categories": {},
        "requests": [],
    }
    initial = _json_bytes(manifest)
    _atomic_write(archive_dir / "manifest.incomplete.json", initial)
    if archive_sink is not None:
        archive_sink("checkpoints/000000.json", initial)

    no_date_file_dates: Dict[str, date] = {}
    extractor = GarminExtractor(
        start_date=start_date,
        end_date=end_date_exclusive - timedelta(days=1),
        ingest_dir=normalized_ingest,
        data_types=list(requested),
        no_date_file_dates=no_date_file_dates,
    )
    extractor.garmin_client = client  # type: ignore[assignment]

    try:
        with _SourceRecorder(
            client,
            archive_dir,
            manifest,
            archive_sink,
            clock,
            no_date_file_dates,
        ):
            profile = client.get_user_profile()
            actual_user_id = profile.get("id") if isinstance(profile, dict) else None
            if str(actual_user_id) != str(user_id):
                raise SourceBindingError(
                    "Authenticated Garmin profile does not match the expected source "
                    "account."
                )
            manifest["source_binding_verified"] = True
            extractor.user_id = str(actual_user_id)  # type: ignore[assignment]
            extractor.extract_garmin_data()
            if set(requested) & _PER_ACTIVITY_CATEGORIES:
                extractor.extract_fit_activities()
    except BaseException:
        manifest["state"] = "interrupted"
        manifest["fetched_at_end"] = _iso_utc(clock())
        _atomic_write(archive_dir / "manifest.incomplete.json", _json_bytes(manifest))
        raise

    finished = _iso_utc(clock())
    manifest["fetched_at_end"] = finished
    manifest["categories"] = _category_results(
        requested,
        manifest["requests"],
        extractor.failures,
        start_date,
        end_date_exclusive,
        finished,
    )
    manifest["state"] = "complete"
    final_body = _json_bytes(manifest)
    if archive_sink is not None:
        archive_sink("manifest.json", final_body)
    incomplete = archive_dir / "manifest.incomplete.json"
    final_path = archive_dir / "manifest.json"
    _atomic_write(final_path, final_body)
    if incomplete.exists():
        incomplete.unlink()
    return CaptureResult(archive_dir, final_path, manifest, normalized_ingest)


def _request_key(
    method: str, path: str, params: Mapping[str, Any], response_kind: str
) -> str:
    return hashlib.sha256(
        _json_bytes(
            {
                "method": method,
                "path": path,
                "params": dict(params),
                "response_kind": response_kind,
            }
        )
    ).hexdigest()


class _ArchivedGarminClient(GarminClient):
    """Garmin client whose transport reads verified archive objects only."""

    def __init__(self, archive_dir: Path, manifest: Dict[str, Any]) -> None:
        super().__init__()
        self.archive_dir = archive_dir
        source_routing = manifest.get("source_routing") or {}
        self.display_name = source_routing.get("display_name")
        self.full_name = source_routing.get("full_name")
        self._responses: Dict[str, Deque[Dict[str, Any]]] = defaultdict(deque)
        for request in manifest["requests"]:
            key = _request_key(
                request["method"],
                request["path"],
                request.get("params") or {},
                request["response_kind"],
            )
            self._responses[key].append(request)

    def _read(self, request: Dict[str, Any]) -> Any:
        relative = Path(request["object_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise BoundedArchiveError(
                "Archive object path is not a safe relative path."
            )
        path = (self.archive_dir / relative).resolve()
        archive_root = self.archive_dir.resolve()
        if path.parent != archive_root and archive_root not in path.parents:
            raise BoundedArchiveError("Archive object path escapes the archive root.")
        body = path.read_bytes()
        actual = hashlib.sha256(body).hexdigest()
        if actual != request["sha256"] or len(body) != request["byte_length"]:
            raise BoundedArchiveError(
                f"Archived response verification failed: {request['object_path']}."
            )
        if request["response_kind"] == "binary":
            return body
        return json.loads(body.decode("utf-8"))

    def _pop(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any],
        response_kind: str,
    ) -> Any:
        key = _request_key(method, path, params, response_kind)
        if not self._responses[key]:
            raise ArchivedResponseMissing(
                f"No archived {response_kind} response for {method} {path}."
            )
        return self._read(self._responses[key].popleft())

    def _connectapi(self, path: str, **kwargs: Any) -> Any:
        return self._pop("GET", path, kwargs.get("params") or {}, "json")

    def _download(self, path: str, **kwargs: Any) -> bytes:
        return self._pop("GET", path, kwargs.get("params") or {}, "binary")


def _load_manifest(archive_dir: Path) -> Dict[str, Any]:
    manifest_path = archive_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BoundedArchiveError(
            f"Completed manifest is missing from archive: {manifest_path}."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("state") != "complete":
        raise BoundedArchiveError("Archive manifest is not a completed version 1 run.")
    if not manifest.get("source_binding_verified"):
        raise BoundedArchiveError("Archive source binding was not verified.")
    return manifest


def replay_bounded_source(
    *,
    archive_dir: Path,
    staging_db_path: Path,
    files_root: Optional[Path] = None,
    allow_incomplete: bool = False,
) -> ReplayResult:
    """
    Replay a completed source archive into SQLite without Garmin access.

    :param archive_dir: Directory containing a completed capture manifest and objects.
    :param staging_db_path: SQLite staging database to create or update.
    :param files_root: Optional existing extractor lifecycle root.
    :param allow_incomplete: Process successful material from failed/unavailable categories.
    :return: Replay and processor outcomes.
    """
    archive_dir = Path(archive_dir)
    staging_db_path = Path(staging_db_path)
    manifest = _load_manifest(archive_dir)
    incomplete = [
        name
        for name, outcome in manifest["categories"].items()
        if not outcome.get("complete")
    ]
    if incomplete and not allow_incomplete:
        raise BoundedArchiveError(
            f"Archive has incomplete categories: {', '.join(sorted(incomplete))}."
        )

    interval = manifest["requested_interval"]
    start_date = date.fromisoformat(interval["start_inclusive"])
    end_date_exclusive = date.fromisoformat(interval["end_exclusive"])
    files_root = (
        Path(files_root)
        if files_root is not None
        else staging_db_path.parent / f"{staging_db_path.stem}_files"
    )
    setup_lifecycle_dirs(files_root)
    recovered = recover_stale_process(files_root)
    ingest_dir = files_root / "ingest"
    process_dir = files_root / "process"

    client = _ArchivedGarminClient(archive_dir, manifest)
    binding_profile = client.get_user_profile()
    replay_user_id = (
        binding_profile.get("id") if isinstance(binding_profile, dict) else None
    )
    if str(replay_user_id) != str(manifest["source_account_id"]):
        raise SourceBindingError(
            "Archived Garmin profile does not match the manifest source account."
        )
    no_date_file_dates = {
        request["category"]: date.fromisoformat(request["fetched_at"][:10])
        for request in manifest["requests"]
        if request["category"] in _CURRENT_STATE_CATEGORIES
    }
    extractor = GarminExtractor(
        start_date=start_date,
        end_date=end_date_exclusive - timedelta(days=1),
        ingest_dir=ingest_dir,
        data_types=list(manifest["requested_categories"]),
        no_date_file_dates=no_date_file_dates,
    )
    extractor.garmin_client = client  # type: ignore[assignment]
    extractor.user_id = manifest["source_account_id"]
    extractor.extract_garmin_data()
    if set(manifest["requested_categories"]) & _PER_ACTIVITY_CATEGORIES:
        extractor.extract_fit_activities()

    staging_db_path.parent.mkdir(parents=True, exist_ok=True)
    create_tables(str(staging_db_path))
    move_ingest_to_process(files_root)
    all_in_process = [path for path in process_dir.iterdir() if path.is_file()]
    processable, backup_only = _partition_processable_and_backup(all_in_process)
    replay_categories = json.loads(json.dumps(manifest["categories"]))
    for outcome in replay_categories.values():
        outcome["replay_status"] = "no_files"
        outcome["normalized_files_processed"] = 0
        outcome["normalized_files_quarantined"] = 0
    backup_count = 0
    if backup_only:
        move_files_to_storage(backup_only, files_root)
        backup_count = len(backup_only)

    processed = 0
    quarantined = 0
    files_by_key = _group_files_by_user_and_timestamp(processable)
    for timestamp_files in files_by_key.values():
        files_by_type = _classify_files_by_type(timestamp_files)
        matched = [path for paths in files_by_type.values() for path in paths]
        file_set = FileSet(file_paths=matched, files=files_by_type)
        engine = None
        try:
            with get_session(str(staging_db_path)) as session:
                engine = session.get_bind()
                processor = GarminProcessor(file_set, session)
                processor.process_file_set(file_set, session)
        except Exception as error:
            move_files_to_quarantine(matched, files_root)
            quarantined += len(matched)
            for key, category_files in files_by_type.items():
                outcome = replay_categories.setdefault(key.name, {})
                outcome["replay_status"] = "failed"
                outcome["normalized_files_quarantined"] = outcome.get(
                    "normalized_files_quarantined", 0
                ) + len(category_files)
                outcome.setdefault("replay_failures", []).append(
                    {"error_type": type(error).__name__}
                )
            continue
        finally:
            if engine is not None:
                engine.dispose()
        move_files_to_storage(matched, files_root)
        processed += len(matched)
        for key, category_files in files_by_type.items():
            outcome = replay_categories.setdefault(key.name, {})
            if outcome.get("replay_status") != "failed":
                outcome["replay_status"] = "success"
            outcome["normalized_files_processed"] = outcome.get(
                "normalized_files_processed", 0
            ) + len(category_files)
    for failure in extractor.failures:
        outcome = replay_categories.setdefault(
            failure.data_type,
            {"status": "failed", "complete": False, "failures": []},
        )
        outcome["status"] = "failed"
        outcome["complete"] = False
        outcome["replay_status"] = "failed"
        outcome.setdefault("normalized_files_processed", 0)
        outcome.setdefault("normalized_files_quarantined", 0)
        outcome.setdefault("failures", []).append(_safe_failure(failure))
    return ReplayResult(
        archive_dir=archive_dir,
        staging_db_path=staging_db_path,
        files_root=files_root,
        categories=replay_categories,
        processed_files=processed,
        backup_only_files=backup_count,
        quarantined_files=quarantined,
        recovered_files=recovered,
    )


__all__ = [
    "AGREED_BOUNDED_CATEGORIES",
    "ArchivedResponseMissing",
    "BoundedArchiveError",
    "CaptureResult",
    "ReplayResult",
    "SourceBindingError",
    "capture_bounded_source",
    "replay_bounded_source",
]
