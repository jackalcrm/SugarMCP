"""Endpoint discovery and the raw-API escape hatches.

The escape hatches are split by verb on purpose. A single combined raw-call tool would mean
one "always allow" silently granting DELETE; splitting them keeps ``sugar_api_get`` inside
the same read-only approval boundary as the rest of :mod:`tools.read`, while
``sugar_api_call`` sits with the writes and carries ``destructive_hint``.

Both refuse paths that a typed tool already covers, so the model cannot route around
metadata validation and result shaping by going direct.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from sugar.context import SugarContext, get_context
from sugar.discovery import refuses_path
from sugar.shaping import trim_record

from .progress import report as report_progress
from .read import READ_ONLY, tool_errors

log = logging.getLogger("sugarmcp.tools.discovery")

DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)

# Cap on a raw response before it is summarized rather than returned whole. A custom
# endpoint has no field projection to clamp it, so this is the only guard.
MAX_RAW_BYTES = 20_000


def _shape_raw(payload: Any) -> Any:
    """Trim an arbitrary response, since a custom endpoint has no projection to clamp it."""
    if isinstance(payload, list):
        return [trim_record(item) if isinstance(item, dict) else item for item in payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            trimmed = dict(payload)
            trimmed["records"] = [
                trim_record(r) if isinstance(r, dict) else r for r in payload["records"]
            ]
            return trimmed
        return trim_record(payload)
    return payload


def register(
    mcp: MCPServer,
    context_provider: Callable[[], SugarContext] = get_context,
    *,
    read_only: bool = False,
) -> None:
    """Register discovery and raw-call tools.

    ``read_only`` is passed in rather than read off the context, because registration must
    not touch the context — building it opens a Sugar session, and the server has to start
    cleanly even when the instance is unreachable.
    """

    ctx = context_provider

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def sugar_list_endpoints(
        query: str = "",
        module: str = "",
        method: str = "",
        custom_only: bool = False,
        limit: int = 50,
        mcp_ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Search this instance's REST API endpoints, including custom ones.

        Sugar instances add their own endpoints, and they appear here automatically because
        the catalog comes from the same service dictionary that routes real requests. This
        instance has 20 custom endpoints that exist on no other Sugar install.

        Use this to find functionality the typed tools do not cover — then call it with
        sugar_api_get (reads) or sugar_api_call (writes).

        Args:
            query: substring to match against the path or description.
            module: restrict to endpoints whose first path segment is this module.
            method: restrict to an HTTP verb, e.g. "POST".
            custom_only: only endpoints defined under custom/, i.e. added by this instance.
            limit: maximum results. The full catalog is ~700 endpoints.

        Returns:
            Matching endpoints with method, path, description and source file.
        """
        await report_progress(mcp_ctx, 1, total=2, message="Loading REST endpoint catalog")
        context = ctx()
        catalog = context.catalog
        await report_progress(mcp_ctx, 2, total=2, message="Searching catalog")
        results = catalog.search(
            query=query, module=module, method=method,
            custom_only=custom_only, limit=limit,
        )
        total = len(catalog.load())
        return {
            "endpoints": results,
            "count": len(results),
            "total_available": total,
            "catalog_source": catalog.source,
            **(
                {"note": (
                    "Catalog parsed from Sugar's HTML help page, which omits endpoints "
                    "whose short description is empty. Installing the SugarMCP package "
                    "adds /mcp/help for a complete JSON catalog."
                )}
                if catalog.source == "help-html" else {}
            ),
        }

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an arbitrary Sugar REST **GET** endpoint. Read-only by construction.

        The escape hatch for custom read endpoints that the typed tools do not cover. Find
        candidates with sugar_list_endpoints.

        Paths already served by a typed tool are refused, because those tools apply field
        projection, result clamping and metadata validation that a raw call would bypass.

        Args:
            path: path after the version prefix, e.g. "Accounts/:id/opportunity_stats".
                Do not include /rest/v11_x.
            params: optional query-string parameters.

        Returns:
            The decoded response, trimmed of empty scaffolding.
        """
        refusal = refuses_path(path)
        if refusal:
            return {
                "error": f"Refused: {refusal}",
                "error_label": "use_typed_tool",
                "guidance": (
                    "The dedicated tool validates fields against metadata and clamps the "
                    "result size; a raw call does neither."
                ),
            }

        context = ctx()
        payload = context.client.get(path.lstrip("/"), params)
        shaped = _shape_raw(payload)

        import json

        size = len(json.dumps(shaped, default=str))
        result: dict[str, Any] = {"path": path, "response": shaped}
        if size > MAX_RAW_BYTES:
            result["warning"] = (
                f"Response is {size:,} bytes. A custom endpoint has no field projection to "
                "limit it — narrow the request with params if the endpoint supports it."
            )
        return result

    if read_only:
        log.info("Read-only mode — sugar_api_call not registered")
        return

    @mcp.tool(annotations=DESTRUCTIVE)
    @tool_errors
    def sugar_api_call(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an arbitrary Sugar REST endpoint with POST, PUT, PATCH or DELETE.

        The escape hatch for custom *write* endpoints. Prefer the typed write tools — they
        validate against metadata first, and Sugar itself does not validate writes at all,
        so a raw call can silently corrupt data.

        Only use this for functionality no typed tool covers, and confirm with the user
        first: this can modify or delete CRM records.

        Args:
            method: POST, PUT, PATCH or DELETE.
            path: path after the version prefix. Do not include /rest/v11_x.
            body: JSON request body.
            params: optional query-string parameters.

        Returns:
            The decoded response.
        """
        verb = method.upper()
        if verb == "GET":
            return {
                "error": "Use sugar_api_get for GET requests.",
                "error_label": "wrong_tool",
            }
        if verb not in ("POST", "PUT", "PATCH", "DELETE"):
            return {
                "error": f"Unsupported method {method!r}. Use POST, PUT, PATCH or DELETE.",
                "error_label": "invalid_parameter",
            }

        refusal = refuses_path(path)
        if refusal:
            return {
                "error": f"Refused: {refusal}",
                "error_label": "use_typed_tool",
                "guidance": (
                    "Sugar does not validate writes, so the typed tools' pre-flight checks "
                    "are the only protection against corrupting data."
                ),
            }

        context = ctx()
        payload = context.client.request(verb, path.lstrip("/"), params=params, body=body)
        return {"method": verb, "path": path, "response": _shape_raw(payload)}
