import json
from types import SimpleNamespace

import pytest

from garmin_health_data.weather import capture_activity_weather


def test_opt_in_binding_archive_and_offline_response():
    calls, saved = [], []
    client = SimpleNamespace(
        get_user_profile=lambda: {"id": 123},
        _connectapi=lambda path: calls.append(path)
        or {"temp": 68, "issueDate": "2026-09-18T12:00:00Z"},
    )
    args = dict(
        client=client,
        expected_user_id="123",
        activity_id="456",
        before_request=lambda: None,
        archive_response=saved.append,
    )
    assert capture_activity_weather(**args) == {"status": "disabled"}
    assert calls == saved == []
    result = capture_activity_weather(**args, enabled=True)
    assert calls == ["/activity-service/activity/456/weather"]
    assert result["body"] == saved[0]
    assert json.loads(saved[0])["temp"] == 68


def test_binding_or_revocation_prevents_weather_request():
    def forbidden(*args):
        raise AssertionError("must not request weather")

    client = SimpleNamespace(
        get_user_profile=lambda: {"id": 999}, _connectapi=forbidden
    )
    args = dict(
        client=client,
        expected_user_id="123",
        activity_id="456",
        before_request=lambda: None,
        archive_response=forbidden,
        enabled=True,
    )
    with pytest.raises(PermissionError):
        capture_activity_weather(**args)
    args["before_request"] = lambda: (_ for _ in ()).throw(PermissionError("revoked"))
    with pytest.raises(PermissionError):
        capture_activity_weather(**args)


def test_provider_failure_is_sanitized_and_archive_failure_propagates():
    def fail(*args):
        raise RuntimeError("PRIVATE")

    client = SimpleNamespace(get_user_profile=lambda: {"id": 123}, _connectapi=fail)
    args = dict(
        client=client,
        expected_user_id="123",
        activity_id="456",
        before_request=lambda: None,
        archive_response=fail,
        enabled=True,
    )
    result = capture_activity_weather(**args)
    assert result == {"status": "failed", "error_code": "weather_provider_error"}
    client._connectapi = lambda path: {"temp": 68}
    with pytest.raises(RuntimeError):
        capture_activity_weather(**args)


@pytest.mark.parametrize("activity_id", ["../123", "1/2", "", 123])
def test_invalid_activity_identifiers_never_reach_provider(activity_id):
    with pytest.raises(ValueError):
        capture_activity_weather(
            client=None,
            expected_user_id="123",
            activity_id=activity_id,
            before_request=lambda: pytest.fail("invalid request"),
            archive_response=None,
            enabled=True,
        )


def test_http_404_and_oversize_responses_do_not_get_archived():
    class NotFound(Exception):
        response = SimpleNamespace(status_code=404)

    def unavailable(path):
        raise NotFound("PRIVATE")

    client = SimpleNamespace(
        get_user_profile=lambda: {"id": 123}, _connectapi=unavailable
    )
    args = dict(
        client=client,
        expected_user_id="123",
        activity_id="456",
        enabled=True,
        before_request=lambda: None,
        archive_response=lambda body: pytest.fail("invalid archive"),
    )
    assert capture_activity_weather(**args)["status"] == "unavailable"
    client._connectapi = lambda path: {"private": "x" * (256 * 1024)}
    assert (
        capture_activity_weather(**args)["error_code"] == "weather_response_too_large"
    )
