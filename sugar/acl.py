"""ACL interpretation — the one place permissions are read, and the easiest place to get
them backwards.

``MetaDataManager::getAclForModule()`` **strips ``yes`` values** for brevity, so the payload
lists only *denials*. **Absence of a key means allowed.** Reading it as a positive allowlist
inverts every permission in the server: a user with no restrictions would appear to have no
access at all, and — far worse in the other direction — a naive ``acl.get("edit") == "yes"``
check would deny everything while a naive truthiness check on a denial dict would *grant*
everything. Hence one module, one entry point, and tests over recorded fixtures.

The live payload also carries a shape trap. ``fields`` is a PHP array that serializes as
``[]`` when empty and ``{...}`` when populated — in the reference instance, 175 modules
return a JSON **list** and 9 return a JSON **object**. Anything that assumes a dict breaks
on the common case.

Field codes, from ``SugarACL``:

===============================  ============================================
``{}`` (field absent)            read + write
``{"write":"no","create":"no"}`` read-only
``{"read":"no"}``                hidden — drop it from output entirely
all ``no``                       no access
``"license":"no"``               license-gated; behaves read-only, reported separately
===============================  ============================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# Module-level actions Sugar reports. `access` gates the module as a whole; the rest are
# per-operation. Any of these may simply be absent, which means allowed.
#
# Deliberately excludes `admin` and `developer`, which Sugar also reports. Those grant Studio
# and module-admin rights, not data access, and every non-admin gets `"no"` for both on
# *every* module — on the reference instance that is 184 modules × 2. Treating them as data
# permissions would flood every ACL summary with denials that say nothing about whether a
# record can be read or written.
MODULE_ACTIONS = (
    "access", "view", "list", "edit", "delete", "create",
    "import", "export", "massupdate",
)

# The action a write tool must hold before calling Sugar, by tool verb.
ACTION_FOR_VERB = {
    "create": "create",
    "update": "edit",
    "delete": "delete",
    "link": "edit",
    "unlink": "edit",
}


def _denied(value: Any) -> bool:
    """True when Sugar has explicitly denied something.

    Sugar writes the string ``"no"``. Everything else — including absence, ``"yes"``, and
    the empty string — means allowed. Written as an explicit equality rather than a
    truthiness test precisely because ``bool("no")`` is ``True``.
    """
    return isinstance(value, str) and value.lower() == "no"


def normalize_fields(raw: Any) -> dict[str, dict[str, Any]]:
    """Coerce the ``fields`` member to a dict regardless of how PHP serialized it.

    An empty PHP array becomes ``[]`` in JSON, not ``{}``. A populated one becomes an
    object. Both mean "map of field name to restriction".
    """
    if isinstance(raw, Mapping):
        return {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}
    # [] (empty) — or a list, which PHP only produces when there are no string keys.
    return {}


@dataclass(frozen=True)
class FieldAcl:
    """Effective permissions for one field."""

    readable: bool = True
    writable: bool = True
    creatable: bool = True
    license_blocked: bool = False

    @property
    def hidden(self) -> bool:
        return not self.readable

    @property
    def read_only(self) -> bool:
        return self.readable and not self.writable

    def reason(self) -> str:
        if self.hidden:
            return "no read access"
        if self.license_blocked:
            return "requires a license this user does not have"
        if self.read_only:
            return "read-only for this user"
        return ""


ALLOW_ALL_FIELD = FieldAcl()


def parse_field_acl(raw: Any) -> FieldAcl:
    """Interpret one field's restriction dict. Absence or ``{}`` means full access."""
    if not isinstance(raw, Mapping) or not raw:
        return ALLOW_ALL_FIELD

    licensed_out = _denied(raw.get("license"))
    return FieldAcl(
        readable=not _denied(raw.get("read")) and not _denied(raw.get("access")),
        # A license denial blocks writes even when `write` itself is not spelled out.
        writable=not _denied(raw.get("write")) and not licensed_out,
        creatable=not _denied(raw.get("create")) and not licensed_out,
        license_blocked=licensed_out,
    )


@dataclass(frozen=True)
class ModuleAcl:
    """Effective permissions for one module, plus its per-field overrides."""

    name: str
    denials: frozenset[str] = frozenset()
    fields: Mapping[str, FieldAcl] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.fields is None:
            object.__setattr__(self, "fields", {})

    def allows(self, action: str) -> bool:
        """True unless Sugar explicitly denied this action.

        ``access: "no"`` disables the module wholesale, so it gates every other action.
        """
        if "access" in self.denials:
            return False
        return action not in self.denials

    @property
    def accessible(self) -> bool:
        return self.allows("access")

    @property
    def readable(self) -> bool:
        return self.accessible and (self.allows("view") or self.allows("list"))

    def field(self, name: str) -> FieldAcl:
        """Effective ACL for a field. Unlisted fields inherit full access."""
        return self.fields.get(name, ALLOW_ALL_FIELD)

    def readable_fields(self, names: Iterable[str]) -> list[str]:
        return [n for n in names if self.field(n).readable]

    def writable_fields(self, names: Iterable[str]) -> list[str]:
        return [n for n in names if self.field(n).writable]

    def summary(self) -> dict[str, Any]:
        """Compact, model-facing description. Only mentions what is denied."""
        out: dict[str, Any] = {}
        if self.denials:
            out["denied_actions"] = sorted(self.denials)
        hidden = sorted(n for n, f in self.fields.items() if f.hidden)
        read_only = sorted(n for n, f in self.fields.items() if f.read_only)
        if hidden:
            out["hidden_fields"] = hidden
        if read_only:
            out["read_only_fields"] = read_only
        return out


ALLOW_ALL_MODULE = ModuleAcl(name="", denials=frozenset(), fields={})


def parse_module_acl(name: str, raw: Any) -> ModuleAcl:
    """Interpret one module's ACL block from ``/me`` or module metadata."""
    if not isinstance(raw, Mapping):
        return ModuleAcl(name=name)

    denials = frozenset(
        action for action in MODULE_ACTIONS if _denied(raw.get(action))
    )
    fields = {
        field_name: parse_field_acl(restriction)
        for field_name, restriction in normalize_fields(raw.get("fields")).items()
    }
    # Fields with no effective restriction add nothing but noise downstream.
    fields = {k: v for k, v in fields.items() if v != ALLOW_ALL_FIELD}
    return ModuleAcl(name=name, denials=denials, fields=fields)


class AclIndex:
    """All module ACLs for the current user, as returned by ``GET /me``.

    Sugar delivers the whole set in the ``current_user.acl`` block of a single ``/me`` call,
    which is why the server does not need a per-module ACL fetch.
    """

    def __init__(self, raw: Mapping[str, Any] | None = None, *, is_admin: bool = False):
        self.is_admin = is_admin
        self._modules: dict[str, ModuleAcl] = {
            name: parse_module_acl(name, block)
            for name, block in (raw or {}).items()
        }

    @classmethod
    def from_me(cls, me: Mapping[str, Any]) -> "AclIndex":
        """Build from a ``GET /me`` response, accepting it wrapped or unwrapped."""
        user = me.get("current_user", me) if isinstance(me, Mapping) else {}
        return cls(
            user.get("acl") or {},
            # Sugar reports admin as type == "admin"; there is no is_admin key on /me.
            is_admin=str(user.get("type") or "").lower() == "admin",
        )

    def __contains__(self, module: str) -> bool:
        return module in self._modules

    def module(self, name: str) -> ModuleAcl:
        """ACL for a module. Modules absent from the payload are unrestricted."""
        return self._modules.get(name, ModuleAcl(name=name))

    def accessible_modules(self, names: Iterable[str] | None = None) -> list[str]:
        candidates = list(names) if names is not None else list(self._modules)
        return [n for n in candidates if self.module(n).accessible]

    def denied_modules(self) -> list[str]:
        return sorted(n for n, acl in self._modules.items() if not acl.accessible)

    def can(self, module: str, action: str) -> bool:
        return self.module(module).allows(action)

    def check_write(self, module: str, verb: str, fields: Iterable[str] = ()) -> str | None:
        """Pre-flight a write. Returns an error message, or None if it should proceed.

        Catching this here turns a Sugar 403 into a specific, corrective message before the
        HTTP call is ever made.
        """
        acl = self.module(module)
        if not acl.accessible:
            return f"Access to module {module!r} is denied for this user."

        action = ACTION_FOR_VERB.get(verb, verb)
        if not acl.allows(action):
            return f"You do not have {action!r} permission on module {module!r}."

        blocked = []
        for name in fields:
            field_acl = acl.field(name)
            permitted = field_acl.creatable if verb == "create" else field_acl.writable
            if not permitted:
                blocked.append(f"{name} ({field_acl.reason()})")
        if blocked:
            return (
                f"These {module} fields cannot be written by this user: "
                + "; ".join(sorted(blocked))
            )
        return None
