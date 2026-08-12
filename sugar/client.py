"""REST transport: auth headers, retry policy, version negotiation, bulk batching.

This is the only layer that talks HTTP to Sugar. It owns retries so that no caller has to
think about token expiry, and it raises :class:`SugarError` for everything else — tools
convert those to ``{"error": ...}`` data at the boundary.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .errors import Retry, SugarError, classify
from .session import SugarSession

log = logging.getLogger("sugarmcp.client")

# Descending ladder walked on a 301 incorrect_version. Sugar's own version list is dense
# and mostly unused; these are the ones that exist as route roots across 10.x–26.x.
VERSION_LADDER: tuple[str, ...] = (
    "v11_27", "v11_26", "v11_25", "v11_24", "v11_23", "v11_22", "v11_21", "v11_20",
    "v11_19", "v11_18", "v11_17", "v11_16", "v11_15", "v11_14", "v11_13", "v11_12",
    "v11_11", "v11_10", "v11_9", "v11_8", "v11_7", "v11_6", "v11_5", "v11_4", "v11_3",
    "v11_2", "v11_1", "v11", "v10",
)

# Keys Sugar returns as empty scaffolding on every record. Stripped before the payload
# reaches the model — see shaping.py for the record-level trimming.
_EMPTY_ACL = {"fields": {}}


class SugarClient:
    """Thin, retrying wrapper over Sugar's REST API.

    Retry rules, each applied at most once per request so a failure can never loop:

    * ``need_login`` (401) — refresh the token, replay.
    * ``incorrect_version`` (301) — step the API version down, replay.
    * ``metadata_out_of_date`` (412) — notify the cache invalidator, replay.
    """

    def __init__(
        self,
        session: SugarSession,
        *,
        on_metadata_stale: Callable[[], None] | None = None,
    ):
        self.session = session
        self._on_metadata_stale = on_metadata_stale

    # -- verbs --------------------------------------------------------------

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> Any:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, body=body, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, body=body, **kw)

    def delete(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("DELETE", path, body=body, **kw)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        api_version: str | None = None,
        _attempts: frozenset[Retry] = frozenset(),
    ) -> Any:
        """Issue one Sugar REST call, applying each retry rule at most once."""
        version = api_version or self.session.api_version
        url = f"{self.session.config.rest_base(version)}/{path.lstrip('/')}"

        headers = {"OAuth-Token": self.session.access_token()}
        try:
            response = self.session.http.request(
                method,
                url,
                params=_clean_params(params),
                json=body if body is not None else None,
                headers=headers,
            )
        except httpx.TransportError as exc:
            # A transport failure never reaches Sugar, so it has no error envelope and none
            # of the retry rules apply. Left raw it surfaces to the model as an opaque
            # "ConnectError", which invites pointless retries; diagnose it instead.
            raise _connection_error(exc, self.session.config.url, method, path) from exc

        if response.status_code < 300:
            return _decode(response)

        error = classify(
            response.status_code,
            _decode(response, quiet=True),
            method=method,
            path=path,
        )

        action = error.retry
        if response.status_code == 401 and error.label == "invalid_grant":
            # `invalid_grant` means two different things depending on where it lands. From
            # the token endpoint it means the credentials are wrong and retrying is futile.
            # From an ordinary API call it means this token is no longer valid — which
            # happens when the session is evicted out from under us, e.g. another login on
            # the same platform slot (SugarOAuth2StorageBase allows one). That is
            # recoverable, and only by a full re-login: the refresh token died with it.
            action = Retry.RELOGIN
        if action is not Retry.NONE and action not in _attempts:
            attempts = _attempts | {action}

            if action is Retry.REFRESH_TOKEN:
                self.session.force_refresh()
                return self.request(
                    method, path, params=params, body=body,
                    api_version=version, _attempts=attempts,
                )

            if action is Retry.RELOGIN:
                log.info("Token was invalidated; re-authenticating")
                self.session.invalidate()
                return self.request(
                    method, path, params=params, body=body,
                    api_version=version, _attempts=attempts,
                )

            if action is Retry.NEGOTIATE_VERSION:
                lower = _next_version(version)
                if lower:
                    log.warning(
                        "Instance rejected API %s; negotiating down to %s", version, lower
                    )
                    self.session.set_api_version(lower)
                    return self.request(
                        method, path, params=params, body=body,
                        api_version=lower, _attempts=attempts,
                    )

            if action is Retry.INVALIDATE_METADATA:
                if self._on_metadata_stale:
                    self._on_metadata_stale()
                return self.request(
                    method, path, params=params, body=body,
                    api_version=version, _attempts=attempts,
                )

        raise error

    # -- bulk ---------------------------------------------------------------

    def bulk(self, calls: Sequence[Mapping[str, Any]]) -> list[Any]:
        """Batch several calls into one round trip via ``POST /bulk``.

        Two quirks the endpoint does not document well and that break naive callers:
        ``url`` must carry the version prefix (``/v11_27/Accounts``), and ``data`` must be a
        JSON **string**, not an object. Both are handled here, so callers pass ordinary
        paths and dicts.

        Args:
            calls: mappings of ``{"method", "url", "data"}``; ``url`` is a plain path.

        Returns:
            One entry per call, in order, each either the decoded body or an error dict.
        """
        version = self.session.api_version
        requests: list[dict[str, Any]] = []
        for call in calls:
            entry: dict[str, Any] = {
                "method": str(call.get("method", "GET")).upper(),
                "url": f"/{version}/{str(call['url']).lstrip('/')}",
            }
            data = call.get("data")
            if data is not None:
                entry["data"] = data if isinstance(data, str) else json.dumps(data)
            requests.append(entry)

        raw = self.request("POST", "bulk", body={"requests": requests})

        results: list[Any] = []
        for item in raw if isinstance(raw, list) else []:
            status = int(item.get("status", 200)) if isinstance(item, dict) else 200
            contents = item.get("contents") if isinstance(item, dict) else item
            if status >= 300:
                results.append(
                    classify(status, contents, method="BULK", path="").as_tool_error()
                )
            else:
                results.append(contents)
        return results

    # -- convenience --------------------------------------------------------

    def probe(self, path: str, params: Mapping[str, Any] | None = None) -> Any | None:
        """GET a path that may not exist, returning None on 404/403 instead of raising.

        Used for the ``/mcp/*`` capability probes, where absence is the expected case on a
        stock instance.
        """
        try:
            return self.get(path, params)
        except SugarError as exc:
            if exc.status_code in (403, 404) or exc.label in ("not_found", "no_method"):
                return None
            raise


def _connection_error(
    exc: httpx.TransportError, url: str, method: str, path: str
) -> SugarError:
    """Turn a transport failure into something actionable.

    The distinction that matters when diagnosing these:

    * ``ConnectTimeout`` / ``ConnectError`` with ``EHOSTUNREACH`` — the name resolved and the
      connection was then refused a route. On macOS 15+ this is the signature of the **Local
      Network privacy permission** being denied to the *calling application*, because the
      OS reports a denial as unreachability rather than as a permission error. It bites
      MCP servers specifically: the server inherits the permissions of the client that
      spawned it, so the same code works from a terminal and fails under a desktop app.
    * A name-resolution failure looks different — ``gaierror`` / "nodename nor servname
      provided" — and means DNS, not permissions.
    """
    detail = str(exc) or type(exc).__name__
    hints = [
        f"Confirm the instance is reachable from this machine: curl -sI {url}",
    ]

    if "No route to host" in detail or "Errno 65" in detail:
        hints.insert(0, (
            "The hostname resolved but the connection was refused a route. If this server "
            "is running under a desktop application on macOS 15 or later, and the instance "
            "is on a private address, grant that application Local Network access in "
            "System Settings > Privacy & Security > Local Network, then fully quit and "
            "reopen it. macOS reports a denied local-network connection as unreachable "
            "rather than as a permission error."
        ))
    elif "Name or service not known" in detail or "nodename nor servname" in detail:
        hints.insert(0, (
            "The hostname could not be resolved. Check SUGAR_URL for a typo, and that any "
            "custom DNS the hostname depends on is available to this process."
        ))
    elif "Connection refused" in detail:
        hints.insert(0, "The host is reachable but nothing is listening on that port.")
    elif "timed out" in detail.lower():
        hints.insert(0, "The connection timed out — the host may be behind a VPN or firewall.")

    return SugarError(
        label="connection_failed",
        message=f"Could not reach the Sugar instance at {url}: {detail}",
        status_code=None,
        method=method,
        path=path,
        payload={"hints": hints},
    )


def _decode(response: httpx.Response, *, quiet: bool = False) -> Any:
    try:
        return response.json()
    except ValueError:
        if quiet:
            return response.text
        return response.text


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop None values and render booleans the way Sugar expects."""
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = value
    return out


def _next_version(current: str) -> str | None:
    """The next version down the ladder, or None if we are already at the bottom."""
    try:
        index = VERSION_LADDER.index(current)
    except ValueError:
        return VERSION_LADDER[0]
    if index + 1 >= len(VERSION_LADDER):
        return None
    return VERSION_LADDER[index + 1]


def strip_empty_acl(record: Any) -> Any:
    """Remove ``_acl: {"fields": {}}`` scaffolding.

    Record-level ``_acl`` is only the *diff* from the module ACL (``array_diff_assoc``), so
    an empty fields map means "same as the module" and carries no information. Only a
    non-empty diff is worth spending context on.
    """
    if isinstance(record, list):
        return [strip_empty_acl(item) for item in record]
    if not isinstance(record, dict):
        return record
    acl = record.get("_acl")
    if acl == _EMPTY_ACL or acl == {}:
        record = {k: v for k, v in record.items() if k != "_acl"}
    return record
