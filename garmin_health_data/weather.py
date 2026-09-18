"""
Explicit activity-weather capture for an already owned, serialized session.

No authentication, SQLite schema, default category or refresh behavior is changed. The
caller archives bytes before normalization and owns session deadlines/leases.
"""

from __future__ import annotations

import json

MAX_WEATHER_BYTES = 256 * 1024
WEATHER_CAPTURE_VERSION = 1


def capture_activity_weather(
    *,
    client,
    expected_user_id,
    activity_id,
    before_request,
    archive_response,
    enabled=False,
):
    if type(enabled) is not bool:
        raise ValueError("weather enabled must be boolean")
    if not enabled:
        return {"status": "disabled"}
    if (
        not isinstance(activity_id, str)
        or not activity_id.isdecimal()
        or len(activity_id) > 32
        or not str(expected_user_id).isdecimal()
    ):
        raise ValueError("invalid bound weather request")
    before_request()
    # Reconfirm account identity on the caller's existing session. Never log profiles.
    profile = client.get_user_profile()
    if not isinstance(profile, dict) or str(profile.get("id")) != str(expected_user_id):
        raise PermissionError("weather provider binding mismatch")
    before_request()
    try:
        payload = client._connectapi(
            f"/activity-service/activity/{activity_id}/weather"
        )
    except Exception as exc:
        code = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        return {
            "status": "unavailable" if code in {204, 404} else "failed",
            "error_code": (
                "weather_unavailable"
                if code in {204, 404}
                else "weather_provider_error"
            ),
        }
    try:
        body = json.dumps(
            payload, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError):
        return {"status": "failed", "error_code": "invalid_weather_response"}
    if len(body) > MAX_WEATHER_BYTES:
        return {"status": "failed", "error_code": "weather_response_too_large"}
    before_request()
    archive_response(body)
    return {"status": "captured", "body": body}
