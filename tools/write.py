"""Write tools — registered only when SUGAR_READ_ONLY is unset.

The approval boundary is the module split: nothing here is annotated ``read_only_hint``, so a
client that has "always allow"ed the read set has granted nothing in this file. The two tools
that destroy data carry ``destructive_hint`` on top of that.

Every write is validated against live metadata *before* it reaches Sugar. That is not a
nicety: Sugar's REST API accepts invalid writes silently — unknown fields are dropped, enum
values outside the dropdown are stored verbatim, and a string written to an int column is
truncated to the column width. See :mod:`sugar.validation`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from sugar.context import SugarContext, get_context
from sugar.shaping import trim_record
from sugar.validation import ValidationError, WriteValidator

from .read import tool_errors

log = logging.getLogger("sugarmcp.tools.write")

# Creates and updates: not read-only, but they do not destroy anything.
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

# Deletes and unlinks: the client should prompt every time.
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def register(mcp: MCPServer, context_provider: Callable[[], SugarContext] = get_context) -> None:
    """Register the write tools."""

    ctx = context_provider

    def validator(context: SugarContext) -> WriteValidator:
        return WriteValidator(context.metadata)

    @mcp.tool(annotations=MUTATING)
    @tool_errors
    def sugar_create_record(module: str, values: dict[str, Any]) -> dict[str, Any]:
        """Create a new record in a module.

        Values are validated against this instance's metadata before anything is sent.
        Sugar itself does not validate writes — it will accept an unknown field, an invalid
        dropdown value, or text too long for the column, and silently discard, store or
        truncate it. So a rejection here is protecting the data, not being pedantic.

        Check the field names and types with sugar_describe_module first, and resolve any
        dropdown values with sugar_get_enum. Write enum *keys*, not their labels.

        Args:
            module: exact module name, e.g. "Accounts".
            values: field name to value. Omit read-only and calculated fields.

        Returns:
            The created record's id and the fields Sugar echoed back.
        """
        context = ctx()
        issues = validator(context).validate(module, values, verb="create")
        if issues:
            raise ValidationError(module, issues)

        created = context.client.post(module, values)
        record = trim_record(created) if isinstance(created, dict) else created
        return {
            "created": True,
            "module": module,
            "id": record.get("id") if isinstance(record, dict) else None,
            "record": record,
        }

    @mcp.tool(annotations=MUTATING)
    @tool_errors
    def sugar_update_record(
        module: str, record_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Update fields on an existing record.

        Only the fields named in ``values`` are changed; everything else is left alone.

        As with creates, values are validated against live metadata first, because Sugar
        accepts invalid writes without complaint.

        Args:
            module: exact module name.
            record_id: the record's Sugar id, from a query.
            values: field name to new value.

        Returns:
            The updated record as Sugar echoed it back.
        """
        context = ctx()
        if not values:
            return {"error": "No values supplied — nothing to update."}

        issues = validator(context).validate(module, values, verb="update")
        if issues:
            raise ValidationError(module, issues)

        updated = context.client.put(f"{module}/{record_id}", values)
        record = trim_record(updated) if isinstance(updated, dict) else updated
        return {
            "updated": True,
            "module": module,
            "id": record_id,
            "fields_changed": sorted(values),
            "record": record,
        }

    @mcp.tool(annotations=MUTATING)
    @tool_errors
    def sugar_link_records(
        module: str, record_id: str, link_name: str, related_ids: list[str]
    ) -> dict[str, Any]:
        """Relate one record to one or more others through a named relationship.

        Find the available ``link_name`` values with
        ``sugar_describe_module(module, include_links=True)``.

        Args:
            module: the source module, e.g. "Accounts".
            record_id: the source record's id.
            link_name: the relationship link, e.g. "contacts".
            related_ids: ids of the records to relate.

        Returns:
            Which links succeeded and which failed.
        """
        context = ctx()
        acl = context.metadata.acl()
        denial = acl.check_write(module, "link", ())
        if denial:
            return {"error": denial, "error_label": "not_authorized"}

        linked: list[str] = []
        failed: list[dict[str, str]] = []
        for related_id in related_ids:
            try:
                context.client.post(
                    f"{module}/{record_id}/link/{link_name}/{related_id}", {}
                )
                linked.append(related_id)
            except Exception as exc:  # noqa: BLE001 - report per-id, keep going
                failed.append({"id": related_id, "error": str(exc)})

        return {
            "module": module,
            "record_id": record_id,
            "link_name": link_name,
            "linked": linked,
            "linked_count": len(linked),
            **({"failed": failed} if failed else {}),
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    @tool_errors
    def sugar_delete_record(module: str, record_id: str) -> dict[str, Any]:
        """Delete a record. This removes it from the CRM.

        Sugar performs a soft delete (the row is marked deleted, not dropped), but the
        record disappears from the application and from every query, and there is no
        undelete through this server. Confirm with the user before calling this.

        Args:
            module: exact module name.
            record_id: the id of the record to delete.

        Returns:
            Confirmation, including the name of what was deleted.
        """
        context = ctx()
        acl = context.metadata.acl()
        denial = acl.check_write(module, "delete", ())
        if denial:
            return {"error": denial, "error_label": "not_authorized"}

        # Read the record first so the confirmation can say what was destroyed rather than
        # echoing back an opaque id.
        label = None
        try:
            existing = context.client.get(f"{module}/{record_id}", {"fields": "id,name"})
            label = existing.get("name") if isinstance(existing, dict) else None
        except Exception:  # noqa: BLE001 - the delete is what matters
            pass

        context.client.delete(f"{module}/{record_id}")
        return {
            "deleted": True,
            "module": module,
            "id": record_id,
            **({"name": label} if label else {}),
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    @tool_errors
    def sugar_unlink_records(
        module: str, record_id: str, link_name: str, related_id: str
    ) -> dict[str, Any]:
        """Remove a relationship between two records.

        This deletes the relationship, not the records themselves. For some relationship
        types Sugar also clears fields on the related record.

        Args:
            module: the source module.
            record_id: the source record's id.
            link_name: the relationship link.
            related_id: the id of the record to unrelate.

        Returns:
            Confirmation of the removal.
        """
        context = ctx()
        acl = context.metadata.acl()
        denial = acl.check_write(module, "unlink", ())
        if denial:
            return {"error": denial, "error_label": "not_authorized"}

        context.client.delete(f"{module}/{record_id}/link/{link_name}/{related_id}")
        return {
            "unlinked": True,
            "module": module,
            "record_id": record_id,
            "link_name": link_name,
            "related_id": related_id,
        }
