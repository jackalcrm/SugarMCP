"""Read tools — every one annotated ``read_only_hint``, so the whole set can be safely
"always allow"ed in a client without that decision ever granting a write.

That split is the point of the design: the approval boundary is drawn between this module
and :mod:`tools.write`, not inside a tool's arguments.

Tools return ``{"error": ...}`` as data rather than raising, following the starter's
convention, so the model can read the guidance and correct itself rather than seeing an
opaque transport failure.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sugar.context import SugarContext, get_context
from sugar.errors import SugarError
from sugar.metadata import COMPACT_LEGEND
from sugar.validation import check_filter
from sugar.shaping import (
    clamp,
    describe_truncation,
    encode_filter_params,
    record_web_url,
    resolve_fields,
    shape_list,
    trim_record,
)

log = logging.getLogger("sugarmcp.tools.read")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

# Built here rather than written as a docstring literal because it interpolates the compact
# encoding's legend. A docstring is captured by @mcp.tool at decoration time, so assigning
# __doc__ afterwards changes nothing the model ever sees — the description must be passed in.
DESCRIBE_MODULE_DOC = f"""\
Describe a module's fields: types, lengths, required and read-only status.

This is how you learn what a module contains. Never assume a field exists — Sugar instances
are heavily customized, and this instance's Accounts module has 80 custom fields that exist
nowhere else.

Two levels of detail, because a module can have hundreds of fields:

* No `fields` argument returns *every* field as one compact line each.
  {COMPACT_LEGEND}
* Passing `fields` returns full detail for just those, which is what you want once you know
  which fields matter.

Fields this user cannot read are omitted entirely. Fields they cannot write are marked `ro`,
so do not attempt to write those.

Enum fields show `opts=<dropdown_name>`, not their values — call sugar_get_enum to resolve
the valid values before filtering or writing on one.

Args:
    module: exact module name from sugar_list_modules, e.g. "Accounts".
    fields: restrict to these field names and return full detail for them.
    include_links: include relationship links. Off by default; they are numerous and only
        needed when navigating to related records.
    refresh: bypass the metadata cache. Use after a Studio change.

Returns:
    Field definitions, counts, and any ACL restrictions on the module.
"""


def tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Convert Sugar failures into result data instead of exceptions.

    A raised exception reaches the model as a transport error with no guidance attached; a
    returned dict carries the label, the failing request, and what to do about it.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except SugarError as exc:
            log.info("%s -> %s", fn.__name__, exc.label)
            return exc.as_tool_error()
        except Exception as exc:  # noqa: BLE001 - never kill the server on one bad call
            # Any exception that can describe itself to a model gets to do so. Keeps this
            # decorator from having to know about validation, or whatever comes next.
            describe = getattr(exc, "as_tool_error", None)
            if callable(describe):
                log.info("%s -> %s", fn.__name__, type(exc).__name__)
                return describe()
            log.exception("%s failed", fn.__name__)
            return {"error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def register(mcp: MCPServer, context_provider: Callable[[], SugarContext] = get_context) -> None:
    """Register every read tool on the server."""

    ctx = context_provider

    # -- orientation --------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_whoami() -> dict[str, Any]:
        """Identify the Sugar user this server is authenticated as, and what they may do.

        Call this first when you need to know whether an action is possible at all. The
        server acts as one specific Sugar user and inherits their permissions — Sugar
        enforces them, so a denial here is a real denial, not a configuration problem.

        Returns:
            User name, id, admin status, licenses, and any modules denied to this user.
        """
        context = ctx()
        me = context.metadata.me().get("current_user", {})
        acl = context.metadata.acl()
        denied = acl.denied_modules()
        return {
            "user_name": me.get("user_name"),
            "full_name": me.get("full_name"),
            "id": me.get("id"),
            "is_admin": acl.is_admin,
            "email": me.get("email"),
            "licenses": me.get("licenses"),
            "denied_modules": denied,
            "denied_module_count": len(denied),
            "instance": context.config.url,
        }

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_server_info() -> dict[str, Any]:
        """Report the Sugar instance version, edition, and which optional features are present.

        Useful when behaviour differs by version, or to confirm which platform and API
        version the connection negotiated.

        Returns:
            Version, flavor, build, host environment, and the server's capability flags.
        """
        context = ctx()
        info = context.metadata.server_info
        return {
            "version": info.get("version"),
            "flavor": info.get("flavor"),
            "build": info.get("build"),
            "product": info.get("product_name"),
            "host_environment": info.get("host_environment"),
            "full_text_search": info.get("fts", {}).get("enabled"),
            **context.capabilities(),
        }

    # -- discovery ----------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_list_modules(include_inaccessible: bool = False) -> dict[str, Any]:
        """List the Sugar modules this user can access, with their display labels.

        Nothing about modules is hard-coded: this reflects the instance's actual
        configuration including custom modules, which are flagged with ``custom: true``.
        Modules the user has no access to are omitted by default.

        Use this before sugar_describe_module to get exact module names — they are
        case-sensitive and often differ from the label shown in the UI.

        Args:
            include_inaccessible: also list modules the user cannot access, marked
                ``accessible: false``. Off by default.

        Returns:
            The module list, and counts of total versus custom modules.
        """
        context = ctx()
        modules = context.metadata.list_modules(include_inaccessible=include_inaccessible)
        return {
            "modules": modules,
            "count": len(modules),
            "custom_count": sum(1 for m in modules if m.get("custom")),
        }

    @mcp.tool(annotations=READ_ONLY, description=DESCRIBE_MODULE_DOC)
    @tool_errors
    def sugar_describe_module(
        module: str,
        fields: list[str] | None = None,
        include_links: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        return ctx().metadata.describe(
            module, fields=fields, include_links=include_links, refresh=refresh
        )

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_get_enum(module: str, field: str, refresh: bool = False) -> dict[str, Any]:
        """Resolve the valid values of a dropdown (enum or multienum) field.

        Returns the values *as customized on this instance*, which are usually not the
        stock Sugar ones — this instance's Accounts.industry is a bespoke list of chemical
        industry codes, for example.

        Call this before filtering or writing on an enum field. Writing an unlisted value
        is rejected by Sugar.

        Args:
            module: module name, e.g. "Accounts".
            field: field name, e.g. "industry".
            refresh: bypass the cache.

        Returns:
            A ``{key: label}`` map. Write the *key*, not the label.
        """
        values = ctx().metadata.enum(module, field, refresh=refresh)
        return {"module": module, "field": field, "count": len(values), "values": values}

    # -- records ------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_query_records(
        module: str,
        filter: list[dict[str, Any]] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
        max_num: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Query records in a module using Sugar's filter DSL.

        The primary way to find records. Always prefer this over fetching everything and
        filtering yourself — a core module here can hold six-figure record counts.

        **Filter syntax.** ``filter`` is a list of conditions, ANDed together. Each
        condition maps a field name to either a bare value (meaning equals) or an operator
        object::

            [{"billing_address_country": "USA"}]
            [{"name": {"$starts": "Acme"}}, {"annual_revenue": {"$gt": 1000000}}]
            [{"$or": [{"status": "New"}, {"status": "Open"}]}]

        Operators: ``$equals`` ``$not_equals`` ``$starts`` ``$ends`` ``$contains``
        ``$not_contains`` ``$in`` ``$not_in`` ``$between`` ``$dateBetween`` ``$is_null``
        ``$not_null`` ``$empty`` ``$not_empty`` ``$lt`` ``$lte`` ``$gt`` ``$gte``
        ``$dateRange`` ``$more_than_x_days_ago`` ``$less_than_x_days_ago``
        ``$more_than_x_days_from_now`` ``$less_than_x_days_from_now``.

        Macros: ``$and`` ``$or`` ``$favorite`` ``$owner`` ``$creator`` ``$tracker``
        ``$following``. ``[{"$owner": ""}]`` means records assigned to the current user.

        Related fields use ``link_name.remote_field``, e.g.
        ``[{"accounts.name": {"$starts": "Acme"}}]`` when querying Contacts.

        Verify field names with sugar_describe_module first — a wrong name returns an
        error, not an empty result.

        Args:
            module: exact module name.
            filter: list of filter conditions, ANDed. Omit to match all records.
            fields: fields to return. Defaults to id, name and date_modified. Ask only for
                what you need — requesting all fields on 20 records costs ~24KB.
            order_by: sort spec, e.g. "date_modified:desc".
            max_num: page size. Defaults to 20, capped at the server's ceiling.
            offset: starting row; pass back the ``next_offset`` from a previous call.

        Returns:
            Matching records, ``next_offset`` for pagination (``-1`` = no more), and
            ``instance_url`` (the Sugar web root). Build a record's clickable link as
            ``instance_url + "/#" + module + "/" + <id>`` and present rows as
            ``[<name>](<url>)``; opening one requires the user be logged into Sugar there.
        """
        context = ctx()
        config = context.config
        limit = clamp(max_num, config.max_records, config.max_records_ceiling)

        # Validate the projection against real metadata so a typo produces a corrective
        # message naming the module, rather than a confusingly empty result.
        available = None
        try:
            described = context.metadata.describe(module)
            available = described.get("fields") or {}
        except SugarError:
            pass  # a describe failure should not block a query that might still work

        projection = resolve_fields(fields, available=available)
        unknown = [f for f in (fields or []) if available and f not in available]

        # Check the filter before spending a round trip. Sugar rejects an unknown filter
        # field with a bare "Unknown field x"; catching it here lets us name the likely
        # intended field instead.
        filter_errors: list[str] = []
        filter_warnings: list[str] = []
        if filter:
            filter_errors, filter_warnings = check_filter(context.metadata, module, filter)
        if filter_errors:
            return {
                "error": f"Invalid filter for {module}: " + " ".join(filter_errors),
                "error_label": "invalid_parameter",
                "module": module,
            }

        body: dict[str, Any] = {
            "fields": ",".join(projection),
            "max_num": limit,
            "offset": max(0, offset),
        }
        if filter:
            body["filter"] = filter
        if order_by:
            body["order_by"] = order_by

        # POST rather than GET: avoids URL-length limits and JSON-in-querystring escaping.
        payload = context.client.post(f"{module}/filter", body)
        result = shape_list(payload)
        result["module"] = module
        result["fields_returned"] = projection
        # The base URL once, not per row — the model builds each record's link from it and
        # the row's id, keeping the list projection lean. See the tool's Returns note.
        result["instance_url"] = context.config.url

        warnings: list[str] = []
        if unknown:
            warnings.append(
                f"These requested fields do not exist on {module} and were dropped: "
                f"{', '.join(unknown)}. Check sugar_describe_module."
            )
        # A filter condition Sugar drops does not fail — it widens the result set silently,
        # which reads as a successful match.
        warnings.extend(filter_warnings)
        if warnings:
            result["warning"] = warnings if len(warnings) > 1 else warnings[0]

        note = describe_truncation(result)
        if note:
            result["note"] = note
        return result

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_count_records(
        module: str, filter: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Count records matching a filter, without fetching them.

        Use this for "how many" questions instead of querying records and counting — it is
        far cheaper and has no page limit.

        Args:
            module: exact module name.
            filter: same syntax as sugar_query_records. Omit to count all records.

        Returns:
            The matching record count.
        """
        context = ctx()
        warnings: list[str] = []
        if filter:
            errors, warnings = check_filter(context.metadata, module, filter)
            if errors:
                return {
                    "error": f"Invalid filter for {module}: " + " ".join(errors),
                    "error_label": "invalid_parameter",
                    "module": module,
                }

        # Unfiltered and filtered counts are different routes: `/count` takes no body,
        # while a filter must go to `/filter/count`.
        if filter:
            payload = context.client.post(f"{module}/filter/count", {"filter": filter})
        else:
            payload = context.client.get(f"{module}/count")

        result: dict[str, Any] = {"module": module, "count": payload.get("record_count")}
        if warnings:
            # Especially important on a count: a dropped condition turns "how many match"
            # into "how many exist", and the number looks perfectly plausible.
            result["warning"] = warnings if len(warnings) > 1 else warnings[0]
        return result

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_get_record(
        module: str, record_id: str, fields: list[str] | None = None
    ) -> dict[str, Any]:
        """Fetch a single record by id.

        Args:
            module: exact module name.
            record_id: the record's Sugar id (a UUID), from a previous query.
            fields: fields to return. Omit for the module's default set — on a
                heavily-customized module that can be large, so naming fields is better.

        Returns:
            The record (empty scaffolding stripped, long text truncated) and ``url`` — its
            page in the Sugar web UI. Offer it as a clickable link, e.g. ``[<name>](<url>)``.
            Opening the link requires the user to be logged into Sugar in that browser.
        """
        context = ctx()
        params = {"fields": ",".join(resolve_fields(fields))} if fields else None
        payload = context.client.get(f"{module}/{record_id}", params)
        record = trim_record(payload) if isinstance(payload, dict) else payload
        return {
            "module": module,
            "record": record,
            "url": record_web_url(context.config.url, module, record_id),
        }

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_get_related(
        module: str,
        record_id: str,
        link_name: str,
        filter: list[dict[str, Any]] | None = None,
        fields: list[str] | None = None,
        max_num: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List records related to a given record through a named relationship.

        Find the available ``link_name`` values with
        ``sugar_describe_module(module, include_links=True)`` — e.g. Accounts has
        ``contacts``, ``opportunities``, ``cases``.

        Args:
            module: the *source* module, e.g. "Accounts".
            record_id: the source record's id.
            link_name: the relationship link name, e.g. "contacts".
            filter: optional filter on the related records, same syntax as
                sugar_query_records.
            fields: fields to return from the related records.
            max_num: page size, capped at the server's ceiling.
            offset: starting row for pagination.

        Returns:
            The related records, plus ``next_offset`` for pagination.
        """
        context = ctx()
        config = context.config
        limit = clamp(max_num, config.max_records, config.max_records_ceiling)

        # GET, not POST. This route has no POST variant, and a POST to the same path
        # matches the relationship-*creating* endpoint instead — see encode_filter_params.
        params: dict[str, Any] = {
            "fields": ",".join(resolve_fields(fields)),
            "max_num": limit,
            "offset": max(0, offset),
        }
        if filter:
            params.update(encode_filter_params(filter))

        payload = context.client.get(
            f"{module}/{record_id}/link/{link_name}/filter", params
        )
        result = shape_list(payload)
        result.update({"module": module, "record_id": record_id, "link_name": link_name})
        return result

    # -- saved reports ------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_list_reports(query: str = "", max_num: int | None = None) -> dict[str, Any]:
        """List saved reports defined in this Sugar instance.

        Reports are records in the Reports module; use this to find one, then run it with
        sugar_run_report.

        Args:
            query: match against the report name.
            max_num: page size, capped at the server's ceiling.

        Returns:
            Saved reports with id, name, type and the module each reports on.
        """
        context = ctx()
        config = context.config
        limit = clamp(max_num, config.max_records, config.max_records_ceiling)

        body: dict[str, Any] = {
            "fields": "id,name,report_type,report_module,date_modified,assigned_user_name",
            "max_num": limit,
            "order_by": "date_modified:desc",
        }
        if query:
            body["filter"] = [{"name": {"$contains": query}}]

        payload = context.client.post("Reports/filter", body)
        result = shape_list(payload)
        result["count"] = len(result.get("records") or [])
        return result

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_run_report(
        report_id: str,
        max_rows: int | None = None,
        offset: int = 0,
        count_only: bool = False,
    ) -> dict[str, Any]:
        """Run a saved report and return its rows.

        Find the report id with sugar_list_reports.

        Rows are limited and projected down to the report's own display columns. That is not
        a convenience: Sugar's report endpoint ignores every pagination parameter and returns
        the complete result set with full record data — one ordinary report on this instance
        returns 940 KB across 109 rows, at 220 fields each, when the report itself displays
        about eight columns. The trimming happens here because it cannot happen anywhere else.

        Use ``count_only`` when you just need the size of the result set; it is far cheaper.

        Args:
            report_id: the saved report's id, from sugar_list_reports.
            max_rows: rows to return, capped at the server's ceiling. Rows beyond this are
                dropped, and the response says how many.
            offset: first row to return, for paging through a large report.
            count_only: return just the row count without fetching any rows.

        Returns:
            The report's rows, its display columns, and the total row count.
        """
        context = ctx()
        config = context.config
        limit = clamp(max_rows, config.max_records, config.max_records_ceiling)

        total = None
        try:
            counted = context.client.get(f"Reports/{report_id}/record_count")
            if isinstance(counted, dict):
                total = counted.get("record_count")
        except SugarError:
            pass  # a missing count must not stop the report from running

        if count_only:
            return {"report_id": report_id, "total_rows": total}

        # The report definition names the columns the report actually displays. Without it
        # every row carries the full bean.
        columns: list[str] = []
        labels: dict[str, str] = {}
        try:
            definition = context.client.get(f"Reports/{report_id}/filter")
            for column in (definition.get("reportDef") or {}).get("display_columns") or []:
                name = column.get("name")
                if name:
                    columns.append(name)
                    if column.get("label"):
                        labels[name] = column["label"]
        except SugarError:
            pass  # fall back to returning whole records, still clamped

        payload = context.client.get(f"Reports/{report_id}/records")
        rows = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []

        window = rows[max(0, offset):max(0, offset) + limit]
        shaped = []
        for row in window:
            if not isinstance(row, dict):
                continue
            projected = {k: row[k] for k in columns if k in row} if columns else row
            shaped.append(trim_record(projected))

        result: dict[str, Any] = {
            "report_id": report_id,
            "rows": shaped,
            "rows_returned": len(shaped),
            "total_rows": total if total is not None else len(rows),
        }
        if columns:
            result["columns"] = columns
            if labels:
                result["column_labels"] = labels
        else:
            result["note"] = (
                "The report definition could not be read, so rows carry every field rather "
                "than the report's display columns."
            )

        remaining = len(rows) - (max(0, offset) + len(shaped))
        if remaining > 0:
            result["next_offset"] = max(0, offset) + len(shaped)
            result["more_available"] = True
            result["truncation_note"] = (
                f"{remaining} further row(s) not shown. Call again with offset="
                f"{result['next_offset']}, or use count_only for totals."
            )
        return result

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    def sugar_search(
        query: str, modules: list[str] | None = None, max_num: int | None = None
    ) -> dict[str, Any]:
        """Search across modules by free text, using Sugar's full-text index.

        Best for "find anything called X" when you do not know which module holds it. For
        precise field-level conditions use sugar_query_records instead.

        Note this depends on the instance's Elasticsearch index being populated. If it
        returns nothing for a term you expect to match, fall back to sugar_query_records
        with a ``$contains`` filter.

        Args:
            query: the text to search for.
            modules: restrict to these modules. Omit to search all indexed modules.
            max_num: maximum results, capped at the server's ceiling.

        Returns:
            Matching records with their module and id.
        """
        context = ctx()
        config = context.config
        limit = clamp(max_num, config.max_records, config.max_records_ceiling)

        params: dict[str, Any] = {"q": query, "max_num": limit}
        if modules:
            params["module_list"] = ",".join(modules)

        payload = context.client.get("globalsearch", params)
        records = [
            trim_record(r) for r in (payload.get("records") or []) if isinstance(r, dict)
        ]
        result: dict[str, Any] = {
            "query": query,
            "records": records,
            "count": len(records),
            "total": payload.get("total"),
        }
        if not records:
            result["note"] = (
                "No full-text matches. The instance's search index may not be populated. "
                "Try sugar_query_records with a $contains filter on a specific module."
            )
        return result
