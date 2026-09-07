"""Synthetic HTTP failures and inspection records; no accounts or live requests."""

import io
import json
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from google_docs_authority import mirror_status, oauth


def http_error(code, reason="private-response-detail"):
    return HTTPError(
        "https://example.invalid/private?token=synthetic-secret",
        code,
        "synthetic-secret",
        {},
        io.BytesIO(json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()),
    )


@pytest.mark.parametrize(
    "failure",
    [
        lambda: http_error(429),
        lambda: http_error(500),
        lambda: http_error(503),
        lambda: http_error(403, "rateLimitExceeded"),
        lambda: http_error(403, "userRateLimitExceeded"),
        lambda: TimeoutError("synthetic-secret"),
        lambda: URLError("synthetic-secret"),
        lambda: ConnectionResetError("synthetic-secret"),
        lambda: IncompleteRead(b"synthetic-secret"),
    ],
    ids=[
        "429",
        "500",
        "503",
        "rate-limit",
        "user-rate-limit",
        "timeout",
        "network",
        "reset",
        "truncated",
    ],
)
def test_transient_get_retries_then_succeeds_without_leaking(
    failure, monkeypatch, capsys
):
    attempts = []
    sleeps = []

    def opener(request, **kwargs):
        attempts.append(request)
        if len(attempts) < 3:
            raise failure()
        return io.BytesIO(b"complete body")

    monkeypatch.setattr(mirror_status.time, "sleep", sleeps.append)
    assert (
        mirror_status.read_get(
            Request("https://example.invalid"), opener=opener, timeout=1
        )
        == b"complete body"
    )
    assert len(attempts) == 3
    assert sleeps == [1, 2]
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err


def test_transient_get_exhaustion_is_bounded(monkeypatch):
    attempts = []
    monkeypatch.setattr(mirror_status.time, "sleep", lambda _: None)

    def opener(*args, **kwargs):
        attempts.append(True)
        raise http_error(503)

    with pytest.raises(HTTPError) as caught:
        mirror_status.read_get(
            Request("https://example.invalid"), opener=opener, timeout=1
        )
    assert len(attempts) == 3
    assert mirror_status.failure_details(caught.value) == {
        "category": "server",
        "http_status": 503,
    }


def test_retry_includes_response_read_failure(monkeypatch):
    attempts = []
    closed = []
    monkeypatch.setattr(mirror_status.time, "sleep", lambda _: None)

    class Response(io.BytesIO):
        def read(self):
            if len(attempts) == 1:
                raise TimeoutError("private response detail")
            return b"complete body"

        def close(self):
            closed.append(True)
            super().close()

    def opener(*args, **kwargs):
        attempts.append(True)
        return Response()

    assert (
        mirror_status.read_get(
            Request("https://example.invalid"), opener=opener, timeout=1
        )
        == b"complete body"
    )
    assert len(attempts) == 2 and len(closed) == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
def test_permanent_or_access_errors_are_not_retried(code, monkeypatch):
    attempts = []
    monkeypatch.setattr(
        mirror_status.time, "sleep", lambda _: pytest.fail("unexpected retry")
    )

    def opener(*args, **kwargs):
        attempts.append(True)
        raise http_error(code)

    with pytest.raises(HTTPError):
        mirror_status.read_get(
            Request("https://example.invalid"), opener=opener, timeout=1
        )
    assert attempts == [True]


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_mutating_requests_are_never_retried(method, monkeypatch):
    attempts = []
    monkeypatch.setattr(
        mirror_status.time, "sleep", lambda _: pytest.fail("unexpected retry")
    )

    def opener(*args, **kwargs):
        attempts.append(True)
        raise http_error(503)

    with pytest.raises(HTTPError):
        mirror_status.read_get(
            Request("https://example.invalid", method=method), opener=opener, timeout=1
        )
    assert attempts == [True]


def test_safe_classification_retains_wrapped_http_failure():
    try:
        try:
            raise http_error(404)
        except OSError:
            raise ValueError("mirror-tab-tree-fetch-failed") from None
    except ValueError as error:
        assert mirror_status.failure_details(error) == {
            "category": "inaccessible",
            "http_status": 404,
        }
    assert mirror_status.failure_details(
        oauth.OAuthError("token-exchange-http-503")
    ) == {"category": "server", "http_status": 503}


@pytest.mark.parametrize(
    "failure, category, code",
    [
        (lambda: http_error(429), "rate-limit", 429),
        (lambda: http_error(503), "server", 503),
        (lambda: http_error(401), "authentication", 401),
        (lambda: TimeoutError("synthetic-secret"), "timeout", None),
        (lambda: URLError("synthetic-secret"), "network", None),
    ],
    ids=["rate-limit", "server", "authentication", "timeout", "network"],
)
def test_oauth_service_errors_keep_their_category_without_post_retry(
    monkeypatch, failure, category, code
):
    attempts = []

    def opener(*args, **kwargs):
        attempts.append(True)
        raise failure()

    monkeypatch.setattr(oauth, "open_request", opener)
    monkeypatch.setattr(
        mirror_status.time, "sleep", lambda _: pytest.fail("OAuth POST retry")
    )
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.exchange({"refresh_token": "synthetic-token"})
    assert attempts == [True]
    assert mirror_status.failure_details(caught.value) == {
        "category": category,
        "http_status": code,
    }


def test_durable_status_failure_streak_and_recovery(tmp_path):
    path = tmp_path / "status.json"
    status = mirror_status.load_status(path)
    mirror_status.record_result(path, status, "syntheticDocument", None)
    success_at = status["documents"]["syntheticDocument"]["last_success_at"]
    failure = {"category": "inaccessible", "http_status": 404}
    first = mirror_status.record_result(path, status, "syntheticDocument", failure)
    status = mirror_status.load_status(path)  # Separate scheduled invocation.
    second = mirror_status.record_result(path, status, "syntheticDocument", failure)
    assert second["consecutive_failures"] == 2
    assert second["first_failure_at"] == first["first_failure_at"]
    assert second["last_success_at"] == success_at
    assert second["access_state"] == "inaccessible"  # Never inferred deletion.
    recovered = mirror_status.record_result(path, status, "syntheticDocument")
    assert recovered["consecutive_failures"] == 0
    assert recovered["first_failure_at"] is None
    assert recovered["last_failure_at"] == second["last_failure_at"]
    assert recovered["category"] is None
    assert recovered["access_state"] == "available"
    assert path.stat().st_mode & 0o777 == 0o600


def test_malformed_status_is_not_silently_reset(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"schema": "unexpected", "documents": {}}')
    with pytest.raises(ValueError, match="mirror-status-invalid"):
        mirror_status.load_status(path)
