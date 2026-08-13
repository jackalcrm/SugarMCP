"""Result shaping — the difference between a usable tool and one that eats the context.

A default ``POST /<module>/filter`` returns every list-view field on 20 records. On a
heavily customized instance a module like Accounts can carry 175+ readable fields over
six-figure record counts, so letting Sugar choose the projection is the single most
expensive mistake available.

Rules, all enforced here rather than trusted to the caller:

* **Always send ``fields``.** Never let Sugar pick.
* **Clamp ``max_num``** to a configured ceiling.
* **Truncate long text** with an explicit marker, so the model knows it was cut.
* **Strip empty scaffolding** — ``_acl: {"fields": {}}``, ``_module``, null-valued keys.
* **Propagate ``next_offset``** so pagination is possible; ``-1`` means exhausted.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# Enough to identify a record in a list without knowing anything about the module.
DEFAULT_FIELDS: tuple[str, ...] = ("id", "name", "date_modified")

# Characters, not tokens — a rough budget that keeps one long description from dominating.
DEFAULT_TEXT_LIMIT = 500

# Keys Sugar attaches to every record that carry no information for the model.
_SCAFFOLDING = frozenset({"_module", "_erased_fields", "_hash"})


def record_web_url(base_url: str, module: str, record_id: str) -> str:
    """Build a record's Sugar web-UI URL: ``<base>/#<Module>/<id>``.

    Sidecar routes records behind the hash fragment, so this is the link a user clicks to
    open the record in the browser (they must be logged into Sugar there). ``base_url`` is the
    instance root as configured — already stored without a trailing slash — so a subpath
    install like ``http://host/sugar`` yields ``http://host/sugar/#Accounts/<id>``.
    """
    return f"{base_url}/#{module}/{record_id}"


def resolve_fields(
    requested: Iterable[str] | None,
    *,
    available: Mapping[str, Any] | None = None,
    always: Sequence[str] = ("id",),
) -> list[str]:
    """Decide the field projection for a list call.

    ``id`` is always included: without it nothing can be fetched or updated afterwards, and
    a model that omitted it would have to re-query.
    """
    fields = list(requested) if requested else list(DEFAULT_FIELDS)
    for name in always:
        if name not in fields:
            fields.insert(0, name)
    if available:
        # Silently dropping unknown fields would hide a typo; the caller checks this list
        # against what it asked for and reports the difference.
        fields = [f for f in fields if f in available or f in always]
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [f for f in fields if not (f in seen or seen.add(f))]


def encode_filter_params(filter_spec: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten a filter list into PHP bracket-notation query parameters.

    Needed because some filter endpoints are **GET only**. The related-records route
    ``/<module>/:record/link/:link_name/filter`` has no POST variant — and a POST to that
    path instead matches ``POST /<module>/:record/link/:link_name/:remote_id``, which
    *creates a relationship*. Sending a read as a POST there is not a failed read, it is an
    accidental write, so link filters are encoded here and sent by GET.

    ``[{"name": {"$starts": "A"}}]`` becomes ``{"filter[0][name][$starts]": "A"}``.
    """
    params: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                walk(f"{prefix}[{key}]", item)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(f"{prefix}[{index}]", item)
        elif isinstance(value, bool):
            params[prefix] = "true" if value else "false"
        elif value is None:
            params[prefix] = ""
        else:
            params[prefix] = value

    walk("filter", list(filter_spec))
    return params


def clamp(value: int | None, default: int, ceiling: int) -> int:
    """Bound a caller-supplied page size."""
    if value is None:
        return min(default, ceiling)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return min(default, ceiling)
    return max(1, min(value, ceiling))


def trim_record(
    record: Mapping[str, Any],
    *,
    text_limit: int = DEFAULT_TEXT_LIMIT,
    drop_empty: bool = True,
) -> dict[str, Any]:
    """Strip scaffolding and truncate long strings in one record."""
    out: dict[str, Any] = {}
    truncated: list[str] = []

    for key, value in record.items():
        if key in _SCAFFOLDING:
            continue
        if key == "_acl":
            # Record `_acl` is only the diff from the module ACL (array_diff_assoc), so an
            # empty fields map means "same as the module" and is not worth reporting.
            if isinstance(value, Mapping) and value.get("fields") in ({}, [], None):
                continue
            out[key] = value
            continue
        if drop_empty and (value is None or value == "" or value == []):
            continue
        if isinstance(value, str) and len(value) > text_limit:
            out[key] = value[:text_limit]
            truncated.append(key)
            continue
        out[key] = value

    if truncated:
        out["_truncated"] = truncated
    return out


def shape_list(
    payload: Mapping[str, Any],
    *,
    text_limit: int = DEFAULT_TEXT_LIMIT,
) -> dict[str, Any]:
    """Shape a ``/filter`` or link-list response into a compact tool result.

    Returns ``records`` plus pagination state. ``next_offset`` of ``-1`` means the result
    set is exhausted; anything else can be passed back as ``offset``.
    """
    records = payload.get("records")
    if not isinstance(records, list):
        records = []

    shaped = [trim_record(r, text_limit=text_limit) for r in records if isinstance(r, Mapping)]

    out: dict[str, Any] = {"records": shaped, "count": len(shaped)}

    next_offset = payload.get("next_offset")
    if next_offset is not None:
        out["next_offset"] = next_offset
        out["more_available"] = next_offset != -1
    return out


def describe_truncation(shaped: Mapping[str, Any]) -> str | None:
    """A one-line note when any record had a field truncated, else None."""
    hits = sum(1 for r in shaped.get("records", []) if r.get("_truncated"))
    if not hits:
        return None
    return (
        f"{hits} record(s) had long text fields truncated; the affected field names are "
        "listed in each record's _truncated key. Fetch a single record with "
        "sugar_get_record for the full value."
    )
