"""Pre-flight validation for writes.

**Sugar's REST API does not validate writes.** Verified against a live 25.2 instance: a
create with no `name` (a required field) succeeds; an unknown field is silently dropped; an
enum value outside the dropdown is stored verbatim; a write to a read-only field is accepted;
a 400-character value into a `len 150` column is truncated without complaint; and the string
``"not-a-number"`` written to an int field is stored as ``"not-a-numb"``.

That makes this module the only thing between a model's guess and corrupted CRM data. It is
not here to produce friendlier errors than Sugar's — Sugar does not produce an error at all.

Everything is checked against the instance's own metadata, so nothing is hard-coded and a
custom field added in Studio is validated the moment it appears in metadata.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .acl import AclIndex
from .metadata import MetadataManager

# How many valid enum values to list back before truncating. Enough to correct a mistake,
# not enough to dominate the response — Accounts.industry has 41.
MAX_SUGGESTED_VALUES = 25

# Types whose values are free text, bounded only by `len`.
TEXT_TYPES = frozenset({
    "varchar", "name", "text", "url", "email", "phone", "id", "assigned_user_name",
    "longtext", "html", "encrypt", "file", "image", "password",
})
INT_TYPES = frozenset({"int", "tinyint", "smallint", "bigint", "long", "short"})
FLOAT_TYPES = frozenset({"decimal", "float", "double", "currency", "decimal2"})
BOOL_TYPES = frozenset({"bool", "boolean"})
ENUM_TYPES = frozenset({"enum", "radioenum", "dynamicenum"})
MULTIENUM_TYPES = frozenset({"multienum"})
DATE_TYPES = frozenset({"date"})
DATETIME_TYPES = frozenset({"datetime", "datetimecombo"})

# Fields that are required in metadata but are managed by Sugar rather than supplied by a
# caller. Requiring them on create would make every create fail.
_SYSTEM_REQUIRED = frozenset({
    "id", "date_entered", "date_modified", "modified_user_id", "created_by",
    "team_count", "team_name", "team_set_id", "acl_team_set_id", "deleted",
})

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class Issue:
    """One problem with a proposed write, phrased so the model can correct it."""

    field: str
    problem: str

    def __str__(self) -> str:
        return f"{self.field}: {self.problem}" if self.field else self.problem


class ValidationError(Exception):
    """Raised when a write must not be sent to Sugar."""

    def __init__(self, module: str, issues: list[Issue]):
        self.module = module
        self.issues = issues
        super().__init__(f"{len(issues)} validation problem(s) on {module}")

    def as_tool_error(self) -> dict[str, Any]:
        return {
            "error": (
                f"The write to {self.module} was rejected before sending it to Sugar, "
                f"because it would have corrupted data: {len(self.issues)} problem(s)."
            ),
            "error_label": "validation_failed",
            "problems": [str(issue) for issue in self.issues],
            "guidance": (
                "Sugar does not validate writes and would have accepted this silently. "
                "Fix the values listed above and try again. Use sugar_describe_module for "
                "field types and sugar_get_enum for valid dropdown values."
            ),
        }


def _has_usable_default(default: Any) -> bool:
    """True when a vardef's default would actually populate the field.

    Sugar spells "no default" several ways: absent, ``""``, ``[]``, and — for multienums —
    ``[""]``. Treating any of those as a satisfied requirement would let a required field
    through empty.
    """
    if default is None or default == "" or default == []:
        return False
    if isinstance(default, (list, tuple)):
        return any(str(item).strip() for item in default)
    return bool(str(default).strip())


def _truncate_values(values: Iterable[str]) -> tuple[list[str], int]:
    listed = list(values)
    if len(listed) <= MAX_SUGGESTED_VALUES:
        return listed, 0
    return listed[:MAX_SUGGESTED_VALUES], len(listed) - MAX_SUGGESTED_VALUES


def filter_field_names(filter_spec: Any) -> list[str]:
    """Every field name referenced by a filter, descending through ``$and``/``$or``."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key.startswith("$"):
                    # A macro: $and/$or carry nested conditions, $owner and friends do not.
                    walk(value)
                else:
                    found.append(key)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(filter_spec)
    return found


def check_filter(
    metadata: MetadataManager, module: str, filter_spec: Any
) -> tuple[list[str], list[str]]:
    """Inspect a filter for conditions Sugar mishandles. Returns ``(errors, warnings)``.

    The two cases behave completely differently, which is why they are separated:

    * **Unknown field** — Sugar rejects the query outright with a 422 "Unknown field x".
      Blocking, and worth pre-empting only because we can suggest the intended name;
      Sugar's own message does not.
    * **Non-database field** — the field exists, so there is no error, but it has no column
      to filter on and Sugar *drops the condition*. The query then returns rows that do not
      match, which reads as success. Verified live: filtering ``Contacts.name`` (a computed
      full-name field) with ``$starts`` returned every contact in the module rather than none.

    Never raises; a filter this cannot verify is allowed through.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        vardefs = (metadata._module_metadata(module).get("fields") or {})
    except Exception:  # noqa: BLE001 - a metadata failure must not block the query
        return errors, warnings

    for name in filter_field_names(filter_spec):
        if "." in name:
            # link_name.remote_field — resolving the far side needs the related module's
            # metadata, which is not worth a fetch just to check a name.
            continue
        vardef = vardefs.get(name)
        if not isinstance(vardef, Mapping):
            close = difflib.get_close_matches(name, list(vardefs), n=3, cutoff=0.6)
            hint = (
                f" Did you mean: {', '.join(close)}?" if close
                else " Check sugar_describe_module for the field list."
            )
            errors.append(f"{name!r} is not a field on {module}.{hint}")
        elif vardef.get("source") == "non-db":
            warnings.append(
                f"{name!r} is a computed field with no database column, so Sugar silently "
                f"drops this condition and returns rows that do not match it. Filter on "
                f"the underlying stored field instead."
            )
    return errors, warnings


class WriteValidator:
    """Validates a proposed create or update against live metadata and ACLs."""

    def __init__(self, metadata: MetadataManager):
        self.metadata = metadata

    def validate(
        self,
        module: str,
        values: Mapping[str, Any],
        *,
        verb: str = "update",
    ) -> list[Issue]:
        """Check a write. Returns the problems found; an empty list means send it."""
        issues: list[Issue] = []

        acl: AclIndex = self.metadata.acl()
        # Module reachability and per-action permission, before anything field-level.
        denial = acl.check_write(module, verb, ())
        if denial:
            return [Issue("", denial)]

        raw = self.metadata._module_metadata(module)
        vardefs = raw.get("fields") or {}
        module_acl = acl.module(module)

        for name, value in values.items():
            vardef = vardefs.get(name)
            if not isinstance(vardef, Mapping):
                issues.append(Issue(name, self._unknown_field_message(name, vardefs)))
                continue

            field_type = str(vardef.get("type") or "")

            # A link is not a writable column; relationships go through sugar_link_records.
            if field_type == "link":
                issues.append(Issue(
                    name,
                    "is a relationship link, not a writable field. Use sugar_link_records "
                    "to relate records.",
                ))
                continue

            field_acl = module_acl.field(name)
            permitted = field_acl.creatable if verb == "create" else field_acl.writable
            if not permitted:
                issues.append(Issue(name, f"cannot be written — {field_acl.reason()}."))
                continue

            if vardef.get("readonly") or vardef.get("readonly_formula"):
                issues.append(Issue(
                    name,
                    "is read-only and is maintained by Sugar. Sugar would accept this "
                    "write and then ignore or overwrite it.",
                ))
                continue

            if vardef.get("calculated"):
                issues.append(Issue(
                    name, "is a calculated field; its value is derived, not written."
                ))
                continue

            issues.extend(self._check_value(module, name, vardef, field_type, value))

        if verb == "create":
            issues.extend(self._check_required(module, vardefs, values, module_acl))

        return issues

    # -- individual checks --------------------------------------------------

    def _unknown_field_message(self, name: str, vardefs: Mapping[str, Any]) -> str:
        """Name the near misses — a typo is the likeliest cause, and Sugar would ignore it."""
        candidates = [n for n in vardefs if n != "_hash"]
        close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.6)
        message = (
            "does not exist on this module. Sugar would silently discard it rather than "
            "report an error."
        )
        if close:
            message += f" Did you mean: {', '.join(close)}?"
        else:
            message += " Check sugar_describe_module for the field list."
        return message

    def _check_required(
        self,
        module: str,
        vardefs: Mapping[str, Any],
        values: Mapping[str, Any],
        module_acl: Any,
    ) -> list[Issue]:
        """Required fields Sugar will happily let you omit.

        Only fields a caller can actually supply: system-managed and non-database fields are
        flagged required in metadata but are not the caller's to provide.
        """
        missing = []
        for name, vardef in vardefs.items():
            if not isinstance(vardef, Mapping) or not vardef.get("required"):
                continue
            if name in _SYSTEM_REQUIRED or name in values:
                continue
            if vardef.get("readonly") or vardef.get("readonly_formula"):
                continue
            if vardef.get("source") == "non-db" or vardef.get("type") == "link":
                continue
            if not module_acl.field(name).writable:
                continue
            # A meaningful default satisfies the requirement — Sugar fills it in. Note the
            # empty forms Sugar uses for "no default": "", [], and [""] for multienums.
            if _has_usable_default(vardef.get("default")):
                continue
            missing.append(name)

        if not missing:
            return []
        return [Issue(
            ", ".join(sorted(missing)),
            "required by this module but not supplied. Sugar would create the record "
            "anyway, leaving it incomplete.",
        )]

    def _check_value(
        self,
        module: str,
        name: str,
        vardef: Mapping[str, Any],
        field_type: str,
        value: Any,
    ) -> list[Issue]:
        # None clears a field; that is legitimate for anything not required.
        if value is None:
            if vardef.get("required"):
                return [Issue(name, "is required and cannot be set to null.")]
            return []

        if field_type in ENUM_TYPES:
            return self._check_enum(module, name, value)
        if field_type in MULTIENUM_TYPES:
            return self._check_multienum(module, name, value)
        if field_type in INT_TYPES:
            return self._check_int(name, value)
        if field_type in FLOAT_TYPES:
            return self._check_float(name, value)
        if field_type in BOOL_TYPES:
            return self._check_bool(name, value)
        if field_type in DATE_TYPES:
            return self._check_pattern(
                name, value, _DATE_RE, "a date as YYYY-MM-DD"
            )
        if field_type in DATETIME_TYPES:
            return self._check_pattern(
                name, value, _DATETIME_RE,
                "an ISO-8601 datetime, e.g. 2026-01-31T14:30:00+00:00",
            )
        if field_type in TEXT_TYPES or not field_type:
            return self._check_text(name, vardef, value)
        return []

    def _check_enum(self, module: str, name: str, value: Any) -> list[Issue]:
        if not isinstance(value, str):
            return [Issue(name, f"is a dropdown and needs a string key, got {type(value).__name__}.")]
        try:
            options = self.metadata.enum(module, name)
        except Exception:  # noqa: BLE001 - a failed lookup must not block the write
            return []
        if not options or value in options:
            return []

        shown, extra = _truncate_values(options)
        suffix = f" (+{extra} more; call sugar_get_enum for the full list)" if extra else ""
        close = difflib.get_close_matches(value, list(options), n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        return [Issue(
            name,
            f"{value!r} is not a valid value. Sugar would store it verbatim, corrupting "
            f"the field. Valid keys: {', '.join(repr(v) for v in shown)}{suffix}.{hint}",
        )]

    def _check_multienum(self, module: str, name: str, value: Any) -> list[Issue]:
        # Sugar accepts a list or its own ^value^,^value^ encoding.
        if isinstance(value, str):
            candidates = [v.strip().strip("^") for v in value.split(",") if v.strip()]
        elif isinstance(value, (list, tuple)):
            candidates = [str(v) for v in value]
        else:
            return [Issue(name, "is a multi-select and needs a list of string keys.")]

        try:
            options = self.metadata.enum(module, name)
        except Exception:  # noqa: BLE001
            return []
        if not options:
            return []

        bad = [v for v in candidates if v and v not in options]
        if not bad:
            return []
        shown, extra = _truncate_values(options)
        suffix = f" (+{extra} more)" if extra else ""
        return [Issue(
            name,
            f"contains invalid value(s) {', '.join(repr(v) for v in bad)}. "
            f"Valid keys: {', '.join(repr(v) for v in shown)}{suffix}.",
        )]

    def _check_int(self, name: str, value: Any) -> list[Issue]:
        if isinstance(value, bool):
            return [Issue(name, "is an integer field; got a boolean.")]
        if isinstance(value, int):
            return []
        if isinstance(value, float) and value.is_integer():
            return []
        try:
            int(str(value).strip())
        except (TypeError, ValueError):
            return [Issue(
                name,
                f"is an integer field but got {value!r}. Sugar would store the raw string, "
                "truncated to the column width.",
            )]
        return []

    def _check_float(self, name: str, value: Any) -> list[Issue]:
        if isinstance(value, bool):
            return [Issue(name, "is a numeric field; got a boolean.")]
        if isinstance(value, (int, float)):
            return []
        try:
            float(str(value).strip().replace(",", ""))
        except (TypeError, ValueError):
            return [Issue(
                name,
                f"is a numeric field but got {value!r}. Sugar would store the raw string.",
            )]
        return []

    def _check_bool(self, name: str, value: Any) -> list[Issue]:
        if isinstance(value, bool) or isinstance(value, int) and value in (0, 1):
            return []
        if isinstance(value, str) and value.strip().lower() in (_TRUE | _FALSE):
            return []
        return [Issue(name, f"is a checkbox and needs true or false, got {value!r}.")]

    def _check_pattern(
        self, name: str, value: Any, pattern: re.Pattern[str], expected: str
    ) -> list[Issue]:
        if not isinstance(value, str) or not pattern.match(value.strip()):
            return [Issue(name, f"needs {expected}, got {value!r}.")]
        return []

    def _check_text(self, name: str, vardef: Mapping[str, Any], value: Any) -> list[Issue]:
        if isinstance(value, (dict, list, tuple)):
            return [Issue(name, f"is a text field but got {type(value).__name__}.")]
        text = str(value)
        try:
            limit = int(vardef.get("len") or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit and len(text) > limit:
            return [Issue(
                name,
                f"is {len(text)} characters but the column holds {limit}. Sugar would "
                "truncate it silently.",
            )]
        return []
