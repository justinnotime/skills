"""Bounded read retries and private inspection status, separate from publication state."""

import json
import re
import time
from datetime import datetime, timezone
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

from google_docs_authority import config
from google_docs_authority.oauth import OAuthError

SCHEMA = "google-docs-mirror-status/v1"
RETRY_DELAYS = (1, 2)
RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class TrashedDocument(ValueError):
    """Drive explicitly reports the document in trash; do not remove its mirror."""


def http_body(error):
    """Read a bounded body once for classification and supported export fallback."""
    if not hasattr(error, "_mirror_body"):
        try:
            error._mirror_body = error.read(4096)
        except (OSError, IncompleteRead):
            error._mirror_body = b""
    return error._mirror_body


def rate_limited(error):
    if error.code == 429:
        return True
    if error.code != 403:
        return False
    try:
        record = json.loads(http_body(error)).get("error", {})
        reasons = {item.get("reason") for item in record.get("errors", [])}
        return bool(reasons & RATE_LIMIT_REASONS)
    except (ValueError, TypeError, AttributeError):
        return False


def failure_details(error):
    """Only fixed categories and numeric HTTP codes may escape exception objects."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, TrashedDocument):
            return {"category": "trashed", "http_status": None}
        if isinstance(error, OAuthError):
            code = re.fullmatch(r"token-exchange-http-([0-9]{3})", str(error))
            http_status = int(code[1]) if code else None
            context = error.__cause__ or error.__context__
            if http_status is None and isinstance(
                context,
                (HTTPError, URLError, TimeoutError, ConnectionError, IncompleteRead),
            ):
                error = context
                continue
            return {
                "category": (
                    "rate-limit"
                    if http_status == 429
                    else "server"
                    if http_status is not None and 500 <= http_status <= 599
                    else "authentication"
                ),
                "http_status": http_status,
            }
        if isinstance(error, HTTPError):
            category = (
                "rate-limit"
                if rate_limited(error)
                else "server"
                if 500 <= error.code <= 599
                else "authentication"
                if error.code == 401
                else "inaccessible"
                if error.code in {403, 404, 410}
                else "http"
            )
            return {"category": category, "http_status": error.code}
        if isinstance(error, TimeoutError) or (
            isinstance(error, URLError) and isinstance(error.reason, TimeoutError)
        ):
            return {"category": "timeout", "http_status": None}
        if isinstance(error, (URLError, ConnectionError, IncompleteRead)):
            return {"category": "network", "http_status": None}
        error = error.__cause__ or error.__context__
    return {"category": "local", "http_status": None}


def retryable(error):
    return failure_details(error)["category"] in {
        "rate-limit",
        "server",
        "timeout",
        "network",
    }


def read_get(request, *, opener, timeout):
    """Retry opening and reading GET responses only; no credentials are logged."""
    delays = RETRY_DELAYS if request.get_method() == "GET" else ()
    for attempt in range(len(delays) + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return response.read()
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ConnectionError,
            IncompleteRead,
        ) as error:
            if attempt == len(delays) or not retryable(error):
                raise
            if isinstance(error, HTTPError):
                error.close()
            time.sleep(delays[attempt])


def load_status(path):
    if path is None or not path.exists():
        return {"schema": SCHEMA, "documents": {}}
    record = json.loads(path.read_text())
    if (
        not isinstance(record, dict)
        or record.get("schema") != SCHEMA
        or not isinstance(record.get("documents"), dict)
    ):
        raise ValueError("mirror-status-invalid")
    # Reject malformed input instead of resetting the operator's failure history.
    for doc_id, entry in record["documents"].items():
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id)
            or not isinstance(entry, dict)
            or type(entry.get("consecutive_failures")) is not int
            or entry["consecutive_failures"] < 0
        ):
            raise ValueError("mirror-status-invalid")
    return record


def record_result(path, status, doc_id, failure=None):
    """Persist each attempted live inspection, even when publication must abort."""
    if path is None:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = status["documents"].get(doc_id, {})
    record = {
        "consecutive_failures": 0,
        "first_failure_at": None,
        "last_failure_at": previous.get("last_failure_at"),
        "last_success_at": previous.get("last_success_at"),
        "category": None,
        "http_status": None,
        "access_state": "available",
    }
    if failure:
        record.update(failure)
        record["consecutive_failures"] = previous.get("consecutive_failures", 0) + 1
        record["first_failure_at"] = previous.get("first_failure_at") or now
        record["last_failure_at"] = now
        record["access_state"] = (
            "trashed"
            if failure["category"] == "trashed"
            else "inaccessible"
            if failure["category"] == "inaccessible"
            else "unknown"
        )
    else:
        record["last_success_at"] = now
    status["documents"][doc_id] = record
    config.atomic_write(path, json.dumps(status, indent=2, sort_keys=True) + "\n")
    return record
