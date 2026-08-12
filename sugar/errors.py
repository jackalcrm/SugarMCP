"""Sugar REST error envelopes mapped to structured, actionable tool errors.

Sugar answers failures with ``{"error": "<label>", "error_message": "..."}``. The label
is the stable part; ``error_message`` is localized and sometimes empty. Everything here
keys off the label.

Two conventions from the design doc:

* Tools return ``{"error": ...}`` as *data* rather than raising, so the model can read the
  guidance and correct itself on the next call.
* Retry policy lives here, not scattered through the client, so there is exactly one place
  that decides whether a failure is replayable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Retry(str, Enum):
    """What the client should do with a failed request."""

    NONE = "none"
    REFRESH_TOKEN = "refresh_token"  # refresh the OAuth token, replay once
    RELOGIN = "relogin"  # full password grant, replay once
    INVALIDATE_METADATA = "invalidate_metadata"  # drop metadata cache, replay once
    NEGOTIATE_VERSION = "negotiate_version"  # step the API version down, replay once


@dataclass(frozen=True)
class ErrorSpec:
    retry: Retry = Retry.NONE
    guidance: str = ""


# Keyed by Sugar's error label. Anything absent falls through to UNKNOWN_ERROR.
ERROR_TABLE: dict[str, ErrorSpec] = {
    "need_login": ErrorSpec(
        Retry.REFRESH_TOKEN,
        "The access token expired. Refreshing and replaying automatically.",
    ),
    "invalid_grant": ErrorSpec(
        Retry.NONE,
        "Sugar rejected the credentials. Check SUGAR_USERNAME and SUGAR_PASSWORD, and "
        "confirm the user is active and not locked out. Do not retry with the same values.",
    ),
    "invalid_client": ErrorSpec(
        Retry.NONE,
        "The OAuth client key was rejected. Check SUGAR_CLIENT_ID / SUGAR_CLIENT_SECRET. "
        "A custom platform's key must have client_type='user'.",
    ),
    "not_authorized": ErrorSpec(
        Retry.NONE,
        "Sugar's ACLs deny this operation for the current user. This is a permission "
        "result, not a bug — do not retry. Report which module/field was refused.",
    ),
    "client_not_allowed": ErrorSpec(
        Retry.NONE,
        "The instance restricts API clients via $sugar_config['api']['allowedClients'] and "
        "our User-Agent is not on the list. An administrator must allow it.",
    ),
    "metadata_out_of_date": ErrorSpec(
        Retry.INVALIDATE_METADATA,
        "Cached metadata is stale. Dropping the cache and replaying.",
    ),
    "edit_conflict": ErrorSpec(
        Retry.NONE,
        "The record changed after it was read. Re-read the record, re-apply the intended "
        "change, and ask the user before overwriting — do not auto-retry.",
    ),
    "invalid_parameter": ErrorSpec(
        Retry.NONE,
        "A parameter was rejected. Check the field names, types and enum values against "
        "sugar_describe_module for this module.",
    ),
    "missing_parameter": ErrorSpec(
        Retry.NONE,
        "A required parameter was omitted. Check sugar_describe_module for required fields.",
    ),
    "incorrect_version": ErrorSpec(
        Retry.NEGOTIATE_VERSION,
        "This instance does not support the requested REST API version. Stepping down.",
    ),
    "not_found": ErrorSpec(
        Retry.NONE,
        "No such record, module or endpoint. Confirm the id with a query, and the module "
        "name with sugar_list_modules — names are case-sensitive.",
    ),
    "no_method": ErrorSpec(
        Retry.NONE,
        "No route matches that path. Check sugar_list_endpoints for the real surface.",
    ),
    "request_too_large": ErrorSpec(
        Retry.NONE, "The request body exceeded the instance limit. Send fewer records."
    ),
    "search_unavailable": ErrorSpec(
        Retry.NONE,
        "Full-text search (Elasticsearch) is unavailable on this instance. Use "
        "sugar_query_records with a $contains filter instead of sugar_search.",
    ),
    "search_runtime": ErrorSpec(
        Retry.NONE,
        "The search backend errored on this query. Simplify the query terms.",
    ),
    "inactive_portal_user": ErrorSpec(Retry.NONE, "The portal user is inactive."),
    "invalid_header": ErrorSpec(Retry.NONE, "A required request header was malformed."),
    "connection_failed": ErrorSpec(
        Retry.NONE,
        "The Sugar instance could not be reached at all. This is a network or permission "
        "problem on the machine running this server, not something the caller can fix by "
        "retrying or changing arguments.",
    ),
}

# Sugar reports an unregistered platform as a generic invalid_parameter with this string
# buried in the message. It needs distinct handling (fall back to the `base` platform), so
# it is detected separately rather than given a table row.
INVALID_PLATFORM_MARKER = "EXCEPTION_INVALID_PLATFORM"


@dataclass
class SugarError(Exception):
    """A Sugar API failure, carrying enough context for both retry logic and the model."""

    label: str
    message: str = ""
    status_code: int | None = None
    method: str | None = None
    path: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message or self.label)

    @property
    def spec(self) -> ErrorSpec:
        return ERROR_TABLE.get(self.label, ErrorSpec())

    @property
    def retry(self) -> Retry:
        return self.spec.retry

    @property
    def is_invalid_platform(self) -> bool:
        return INVALID_PLATFORM_MARKER in f"{self.message} {self.payload}"

    def as_tool_error(self, **extra: Any) -> dict[str, Any]:
        """Render as the ``{"error": ...}`` dict tools return instead of raising."""
        out: dict[str, Any] = {"error": self.message or self.label, "error_label": self.label}
        if self.status_code is not None:
            out["status"] = self.status_code
        if self.spec.guidance:
            out["guidance"] = self.spec.guidance
        if self.method and self.path:
            out["request"] = f"{self.method} {self.path}"
        out.update(extra)
        return out


def classify(
    status_code: int,
    payload: Any,
    *,
    method: str | None = None,
    path: str | None = None,
) -> SugarError:
    """Build a SugarError from an HTTP status and a decoded response body."""
    if isinstance(payload, dict):
        label = str(payload.get("error") or "") or _label_from_status(status_code)
        message = str(payload.get("error_message") or "")
        body = payload
    else:
        label = _label_from_status(status_code)
        message = str(payload)[:500] if payload else ""
        body = {"raw": str(payload)[:2000]} if payload else {}

    return SugarError(
        label=label,
        message=message,
        status_code=status_code,
        method=method,
        path=path,
        payload=body,
    )


def _label_from_status(status_code: int) -> str:
    """Fallback label for a response with no parseable Sugar error envelope."""
    return {
        301: "incorrect_version",
        400: "invalid_parameter",
        401: "need_login",
        403: "not_authorized",
        404: "not_found",
        409: "edit_conflict",
        412: "metadata_out_of_date",
        413: "request_too_large",
        422: "invalid_parameter",
    }.get(status_code, f"http_{status_code}")
