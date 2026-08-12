"""Metadata: lazy fetch, aggressive pruning, disk cache, label and enum resolution.

This is where most of the engineering effort goes, because ``GET /metadata`` is enormous and
naive use blows the context window on a single call. On the reference instance a single
module costs **299 KB**, and the model needs perhaps 3 KB of it:

======  ===========  =========================================================
size    section      kept?
======  ===========  =========================================================
158 KB  views        no — client rendering metadata
112 KB  fields       pruned to a projection, ~15 of ~40 keys per vardef
 13 KB  layouts      no
 11 KB  dependencies no — SugarLogic formulas
  2 KB  filters      no
======  ===========  =========================================================

Dropping the non-field sections wholesale is the single biggest win and happens before any
vardef is examined. Note the ordering consequence: pruning must never be the model's job.

Revalidation is cheap. ``only_hash=true`` returns 117 bytes against 299 KB, so a cached
module is confirmed current for the cost of one small request.

Three facts about this API that the payloads do not advertise:

* **Module metadata carries no ACL.** The ``acl`` block lives on ``GET /me`` and covers every
  module at once — see :mod:`sugar.acl`. Nothing here fetches per-module permissions.
* **Vardefs carry no human label**, only a ``vname`` key like ``LBL_INDUSTRY``. Labels come
  from ``GET /lang/:lang``, which has no per-module variant and returns ~1.7 MB for the whole
  application. It is fetched once, cached, and never sent to the model.
* **Enum options are a dropdown *name*, not values.** ``GET /<module>/enum/<field>`` resolves
  them, and resolves them *as customized on this instance* — the reference instance's
  ``industry_dom`` is a bespoke list, nothing like the stock one.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .acl import AclIndex, ModuleAcl
from .client import SugarClient
from .errors import SugarError

log = logging.getLogger("sugarmcp.metadata")

# Sections of a module payload worth keeping. Everything else — views, layouts,
# dependencies, filters, menu, fieldTemplates — is client rendering data.
KEPT_MODULE_SECTIONS = frozenset({"fields", "_hash"})

# Module-level booleans worth reporting, mapped to the name we expose.
MODULE_FLAGS = {
    "isAudited": "audited",
    "ftsEnabled": "full_text_search",
    "globalSearchEnabled": "global_search",
    "favoritesEnabled": "favorites",
    "followingEnabled": "following",
    "isBwcEnabled": "legacy_bwc",
    "dupCheckEnabled": "duplicate_check",
}

# Vardef keys that survive projection. Chosen so the model can answer "what can I read,
# write and filter on" and nothing else. Roughly a 100x reduction against the raw vardef.
KEPT_FIELD_KEYS = frozenset({
    "name", "type", "dbType", "len", "required", "readonly", "calculated", "source",
    "default", "options", "subpanel_link", "sortable", "pii",
    # relationship plumbing
    "relationship", "module", "bean_name", "id_name", "rname", "link",
})

# Dropped explicitly rather than by omission, so the intent is documented: these are the
# large, noisy keys that dominate a raw vardef.
_NOISE_KEYS = frozenset({
    "dependency", "validation", "full_text_search", "popupHelp", "comment", "comments",
    "duplicate_merge", "duplicate_merge_dom_value", "duplicate_on_record_copy",
    "merge_filter", "massupdate", "hidemassupdate", "importable", "reportable",
    "unified_search", "no_default", "size", "help", "studio", "_hash", "group",
    "isnull", "mandatory_fetch", "audited", "labelValue", "vname", "enforced",
})


class MetadataCache:
    """Disk cache keyed by Sugar's per-section ``_hash``.

    Layout: ``<cache_dir>/cache/<instance-user-platform hash>/<kind>/<key>.json``. The
    identity component is a hash of (url, username, platform), so two users on the same
    instance never share a cached view of it — metadata is ACL-shaped and must not leak
    across accounts.
    """

    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, key: str) -> Path:
        # Module names are safe filename material, but custom ones can carry characters
        # that are not; keep it conservative.
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return self.root / kind / f"{safe}.json"

    def read(self, kind: str, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(kind, key).read_text())
        except (OSError, ValueError):
            return None

    def write(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        path = self._path(kind, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot leave a truncated cache file
            # that would be read back as valid JSON on the next run.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)
        except OSError as exc:
            log.warning("Could not write cache %s/%s: %s", kind, key, exc)

    def clear(self) -> None:
        import shutil

        try:
            shutil.rmtree(self.root)
        except OSError:
            pass


def project_field(name: str, raw: Mapping[str, Any], label: str | None = None) -> dict[str, Any]:
    """Reduce one vardef to the projection the model actually needs.

    Args:
        name: field name, used when the vardef omits its own ``name``.
        raw: the vardef as Sugar returned it.
        label: resolved human label, if one was found.

    Returns:
        A dict carrying only :data:`KEPT_FIELD_KEYS`, plus ``label`` and normalized
        ``readonly`` / ``related_module``.
    """
    out: dict[str, Any] = {"name": raw.get("name", name)}

    field_type = raw.get("type")
    if field_type:
        out["type"] = field_type

    resolved_label = label or raw.get("labelValue")
    if resolved_label:
        out["label"] = resolved_label
    elif raw.get("vname"):
        # Better a raw LBL_ key than nothing — it usually reads well enough to guess.
        out["label"] = str(raw["vname"])

    for key in ("len", "required", "default", "sortable", "pii"):
        if key in raw and raw[key] not in (None, "", False):
            out[key] = raw[key]

    # readonly appears both as a flag and as a SugarLogic formula; either one means the
    # model must not attempt to write the field.
    if raw.get("readonly") or raw.get("readonly_formula"):
        out["readonly"] = True
    if raw.get("calculated"):
        out["calculated"] = True

    source = raw.get("source")
    if source == "custom_fields":
        out["custom"] = True  # says everything `source` would, in fewer bytes
    elif source and source != "db":
        out["source"] = source

    # Enum options arrive as a dropdown *name*; the values need a separate call. Restricted
    # to the types that actually select from one: non-enum fields often carry an `options`
    # key naming a *search filter* dropdown (date_modified says `date_range_search_dom`),
    # which is not a set of values the field can hold and would mislead a writer.
    options = raw.get("options")
    if field_type in ("enum", "multienum", "radioenum", "dynamicenum"):
        if isinstance(options, str) and options:
            out["options"] = options
        elif isinstance(options, (dict, list)) and options:
            out["option_values"] = options

    # Relationship plumbing, so the model can navigate links without guessing.
    if field_type == "link":
        if raw.get("module"):
            out["related_module"] = raw["module"]
        if raw.get("relationship"):
            out["relationship"] = raw["relationship"]
    elif field_type in ("relate", "parent"):
        for src, dst in (("module", "related_module"), ("id_name", "id_field"),
                         ("rname", "related_display_field"), ("link", "link")):
            if raw.get(src):
                out[dst] = raw[src]

    return out


# Grammar for the compact field encoding, quoted verbatim in the describe tool's docstring
# so the model is told how to read it rather than left to infer.
COMPACT_LEGEND = (
    "Each field is 'type[(len)] [flags] [| label]'. Flags: req=required, ro=read-only, "
    "ro:license=read-only for lack of a license, calc=calculated, cf=custom field, "
    "opts=<dropdown> (resolve values with sugar_get_enum), ->Module=related module. "
    "The label is shown only when it says more than the field name does."
)


def _label_adds_information(name: str, label: str | None) -> bool:
    """True when a label is worth its bytes.

    Most labels are the field name with punctuation — ``account_manager_user_id_c`` labelled
    "account manager user id". Repeating that for 175 fields is pure overhead; only a label
    that differs from the mechanical transformation earns its place.
    """
    if not label:
        return False
    normalized_label = label.strip().rstrip(":").replace("-", " ").replace("_", " ").lower()
    normalized_name = name.removesuffix("_c").replace("_", " ").lower()
    return " ".join(normalized_label.split()) != " ".join(normalized_name.split())


def compact_field(entry: Mapping[str, Any]) -> str:
    """Render a projected field as one terse line.

    The dict form repeats its keys once per field, which on a 255-field module is several
    kilobytes of the word "label". This form costs roughly a third as much and carries the
    same information.
    """
    name = str(entry.get("name", ""))
    parts = [str(entry.get("type") or "?")]
    if entry.get("len"):
        parts[0] += f"({entry['len']})"

    if entry.get("required"):
        parts.append("req")
    if entry.get("readonly"):
        parts.append("ro:license" if entry.get("readonly_reason") == "license" else "ro")
    if entry.get("calculated"):
        parts.append("calc")
    if entry.get("custom"):
        parts.append("cf")
    if entry.get("options"):
        parts.append(f"opts={entry['options']}")
    if entry.get("related_module"):
        target = str(entry["related_module"])
        field = entry.get("related_display_field")
        parts.append(f"->{target}.{field}" if field else f"->{target}")

    line = " ".join(parts)
    label = entry.get("label")
    if _label_adds_information(name, label):
        line += f" | {label}"
    return line


class MetadataManager:
    """Owns the metadata cache and every metadata-derived question the tools ask."""

    def __init__(self, client: SugarClient, cache_dir: Path, language: str = "en_us"):
        self.client = client
        self.cache = MetadataCache(cache_dir)
        self.language = language
        self._server_info: dict[str, Any] | None = None
        self._module_list: dict[str, Any] | None = None
        self._acl: AclIndex | None = None
        self._me: dict[str, Any] | None = None
        self._labels: dict[str, Any] | None = None
        self._modules: dict[str, dict[str, Any]] = {}
        self._enums: dict[str, dict[str, str]] = {}

    # -- startup ------------------------------------------------------------

    def bootstrap(self) -> dict[str, Any]:
        """Fetch the small startup payload: server info and the module list.

        Deliberately narrow. The unfiltered ``/metadata`` call is the one that must never
        happen.
        """
        if self._server_info is None:
            payload = self.client.get(
                "metadata", {"type_filter": "server_info,full_module_list"}
            )
            self._server_info = payload.get("server_info") or {}
            self._module_list = payload.get("full_module_list") or {}
            self._module_list.pop("_hash", None)
        return self._server_info

    @property
    def server_info(self) -> dict[str, Any]:
        self.bootstrap()
        return self._server_info or {}

    def me(self) -> dict[str, Any]:
        if self._me is None:
            self._me = self.client.get("me")
        return self._me

    def acl(self) -> AclIndex:
        """The current user's ACLs for every module, from a single ``/me`` call."""
        if self._acl is None:
            self._acl = AclIndex.from_me(self.me())
        return self._acl

    def module_names(self) -> list[str]:
        self.bootstrap()
        return sorted(self._module_list or {})

    def list_modules(self, *, include_inaccessible: bool = False) -> list[dict[str, Any]]:
        """Modules this user can reach, with labels and a custom-module flag.

        Modules denied by ACL are omitted rather than flagged: the model should not be
        offered a module it will only get a 403 from.
        """
        self.bootstrap()
        acl = self.acl()
        labels = self._app_labels().get("moduleList", {}) or {}
        visible = set(self._visible_module_list())

        out = []
        for name in sorted(self._module_list or {}):
            module_acl = acl.module(name)
            if not module_acl.accessible and not include_inaccessible:
                continue
            entry: dict[str, Any] = {
                "module": name,
                "label": labels.get(name) or name,
            }
            # A module absent from the user's own module_list is reachable via the API but
            # hidden from their navigation — worth distinguishing.
            if name not in visible:
                entry["in_navigation"] = False
            if _looks_custom(name):
                entry["custom"] = True
            if not module_acl.accessible:
                entry["accessible"] = False
            out.append(entry)
        return out

    def _visible_module_list(self) -> list[str]:
        user = self.me().get("current_user", {})
        return list(user.get("module_list") or [])

    # -- per module ---------------------------------------------------------

    def describe(
        self,
        module: str,
        *,
        fields: Iterable[str] | None = None,
        include_links: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Pruned, ACL-filtered field definitions for one module.

        Tiered on purpose. A module like Accounts has 255 fields, so even a well-pruned
        dict-per-field runs to tens of kilobytes — the pruning wins the payload back from
        Sugar, but the *shape* of the answer is what wins back the context window:

        * no ``fields`` argument — every field as one compact line (:func:`compact_field`);
        * ``fields=[...]`` — full detail for just those, which is what a caller actually
          needs once it knows which fields it cares about;
        * ``include_links`` — off by default; 76 of Accounts' entries are links, and a
          caller wanting relationships can ask for them.

        Fields the user cannot read are dropped entirely — the model should not ask for
        what it cannot have — and fields it cannot write are marked read-only so it does
        not attempt writes that would 403.
        """
        acl = self.acl().module(module)
        if not acl.accessible:
            raise SugarError(
                label="not_authorized",
                message=f"Access to module {module!r} is denied for this user.",
                status_code=403,
            )

        raw = self._module_metadata(module, refresh=refresh)
        raw_fields = raw.get("fields") or {}
        labels = self._module_labels(module)

        wanted = set(fields) if fields else None
        projected: dict[str, Any] = {}
        links: dict[str, Any] = {}
        hidden_count = 0

        for name, vardef in raw_fields.items():
            if name == "_hash" or not isinstance(vardef, Mapping):
                continue
            if wanted is not None and name not in wanted:
                continue

            field_acl = acl.field(name)
            if not field_acl.readable:
                hidden_count += 1
                continue

            entry = project_field(name, vardef, labels.get(str(vardef.get("vname") or "")))
            if not field_acl.writable:
                entry["readonly"] = True
                if field_acl.license_blocked:
                    entry["readonly_reason"] = "license"

            # Links are always collected so the count is honest even when they are omitted
            # from the response.
            if entry.get("type") == "link":
                links[name] = entry
            else:
                projected[name] = entry

        # Detail mode when the caller named fields; compact otherwise.
        detailed = wanted is not None

        result: dict[str, Any] = {
            "module": module,
            "label": self._app_labels().get("moduleList", {}).get(module, module),
            "field_count": len(projected),
        }
        if detailed:
            # The dict key already carries the name; repeating it inside is dead weight.
            result["fields"] = {
                n: {k: v for k, v in e.items() if k != "name"} for n, e in projected.items()
            }
        else:
            result["fields"] = {n: compact_field(e) for n, e in projected.items()}
            result["legend"] = COMPACT_LEGEND

        if include_links:
            result["links"] = (
                links if detailed else {n: compact_field(e) for n, e in links.items()}
            )
            result["link_count"] = len(links)
        elif links:
            # Say they exist and how to get them, rather than spending 14 KB on them.
            result["link_count"] = len(links)
            result["links_note"] = (
                f"{len(links)} relationship links not shown; call again with "
                "include_links=true to list them."
            )
        if hidden_count:
            result["hidden_field_count"] = hidden_count

        flags = {
            exposed: bool(raw.get(key))
            for key, exposed in MODULE_FLAGS.items()
            if raw.get(key)
        }
        if flags:
            result["flags"] = flags

        acl_summary = acl.summary()
        if acl_summary:
            result["acl"] = acl_summary

        return result

    def field(self, module: str, name: str) -> dict[str, Any] | None:
        """One projected field definition, or None if absent or unreadable."""
        raw = self._module_metadata(module)
        vardef = (raw.get("fields") or {}).get(name)
        if not isinstance(vardef, Mapping):
            return None
        if not self.acl().module(module).field(name).readable:
            return None
        labels = self._module_labels(module)
        return project_field(name, vardef, labels.get(str(vardef.get("vname") or "")))

    def _module_metadata(self, module: str, *, refresh: bool = False) -> dict[str, Any]:
        """Fetch (or revalidate) one module's metadata, pruned before it is cached.

        Pruning happens on the way *into* the cache, so the 299 KB payload exists only for
        the life of one HTTP response. Revalidation uses ``only_hash=true`` — 117 bytes.
        """
        if not refresh and module in self._modules:
            return self._modules[module]

        cached = None if refresh else self.cache.read("modules", module)

        if cached and cached.get("_hash"):
            if self._hash_matches(module, cached["_hash"]):
                self._modules[module] = cached
                return cached
            log.info("Metadata for %s changed upstream; refetching", module)

        payload = self.client.get(
            "metadata",
            {
                # str_getcsv() parses module_filter, so a name containing a comma needs
                # quoting. Quoting unconditionally is harmless and covers custom modules.
                "type_filter": "modules",
                "module_filter": f'"{module}"',
            },
        )
        raw = (payload.get("modules") or {}).get(module)
        if raw is None:
            raise SugarError(
                label="not_found",
                message=(
                    f"Sugar returned no metadata for module {module!r}. Check the name "
                    "with sugar_list_modules — module names are case-sensitive."
                ),
                status_code=404,
            )

        pruned = self._prune_module(raw)
        self.cache.write("modules", module, pruned)
        self._modules[module] = pruned
        return pruned

    def _prune_module(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Drop views/layouts/dependencies wholesale, then project every vardef.

        The wholesale drop is ~60% of the payload on its own and costs nothing to decide.
        """
        out: dict[str, Any] = {
            key: raw[key] for key in KEPT_MODULE_SECTIONS if key in raw
        }
        out.update({
            key: raw[key] for key in MODULE_FLAGS if raw.get(key)
        })
        fields = raw.get("fields")
        if isinstance(fields, Mapping):
            out["fields"] = {
                name: {k: v for k, v in vardef.items() if k not in _NOISE_KEYS}
                for name, vardef in fields.items()
                if isinstance(vardef, Mapping)
            }
            # Keep vname — it is the only join key to the label table.
            for name, vardef in fields.items():
                if isinstance(vardef, Mapping) and vardef.get("vname"):
                    out["fields"][name]["vname"] = vardef["vname"]
                if isinstance(vardef, Mapping) and vardef.get("labelValue"):
                    out["fields"][name]["labelValue"] = vardef["labelValue"]
                if isinstance(vardef, Mapping) and vardef.get("readonly_formula"):
                    out["fields"][name]["readonly_formula"] = True
        out["_cached_at"] = time.time()
        return out

    def _hash_matches(self, module: str, known_hash: str) -> bool:
        """Cheap revalidation: 117 bytes instead of 299 KB."""
        try:
            payload = self.client.get(
                "metadata",
                {"type_filter": "modules", "module_filter": f'"{module}"', "only_hash": True},
            )
        except SugarError:
            # If revalidation fails, trust the cache rather than failing the call.
            return True
        current = ((payload.get("modules") or {}).get(module) or {}).get("_hash")
        return bool(current) and current == known_hash

    # -- enums --------------------------------------------------------------

    def enum(self, module: str, field: str, *, refresh: bool = False) -> dict[str, str]:
        """Resolve a dropdown to its ``{key: label}`` map, as customized on this instance.

        Preferred over digging option keys out of ``app_list_strings``: this reflects the
        instance's actual customization, and Sugar ETags it (3600s, or 60s for
        function-backed vardefs).
        """
        cache_key = f"{module}.{field}"
        if not refresh and cache_key in self._enums:
            return self._enums[cache_key]

        if not refresh:
            cached = self.cache.read("enums", cache_key)
            if cached and isinstance(cached.get("values"), dict):
                self._enums[cache_key] = cached["values"]
                return cached["values"]

        values = self.client.get(f"{module}/enum/{field}")
        if not isinstance(values, dict):
            values = {}
        self.cache.write("enums", cache_key, {"values": values, "_cached_at": time.time()})
        self._enums[cache_key] = values
        return values

    def enums_for(self, module: str, fields: Iterable[str]) -> dict[str, dict[str, str]]:
        """Resolve several dropdowns in one round trip via ``POST /bulk``.

        A module like Accounts has 28 enum and multienum fields; serially that is 28 request
        round trips, batched it is one.
        """
        names = [f for f in fields if f"{module}.{f}" not in self._enums]
        resolved = {f: self._enums[f"{module}.{f}"] for f in fields if f"{module}.{f}" in self._enums}
        if not names:
            return resolved

        results = self.client.bulk(
            [{"method": "GET", "url": f"{module}/enum/{name}"} for name in names]
        )
        for name, value in zip(names, results):
            if isinstance(value, dict) and "error" not in value:
                self._enums[f"{module}.{name}"] = value
                self.cache.write(
                    "enums", f"{module}.{name}", {"values": value, "_cached_at": time.time()}
                )
                resolved[name] = value
        return resolved

    # -- labels -------------------------------------------------------------

    def _app_labels(self) -> dict[str, Any]:
        """The whole application label table, fetched once and cached to disk.

        ``GET /lang/:lang`` has no per-module filter and returns ~1.7 MB. It is expensive
        once and free thereafter — and it never reaches the model.
        """
        if self._labels is not None:
            return self._labels

        cached = self.cache.read("lang", self.language)
        if cached:
            self._labels = cached
            return cached

        try:
            payload = self.client.get(f"lang/{self.language}")
        except SugarError as exc:
            log.warning("Could not fetch labels (%s); falling back to LBL_ keys", exc.label)
            self._labels = {}
            return self._labels

        trimmed = {
            "mod_strings": payload.get("mod_strings") or {},
            "moduleList": (payload.get("app_list_strings") or {}).get("moduleList") or {},
            "_hash": payload.get("_hash"),
        }
        self.cache.write("lang", self.language, trimmed)
        self._labels = trimmed
        return trimmed

    def _module_labels(self, module: str) -> dict[str, str]:
        """``{LBL_KEY: "Human Label"}`` for one module."""
        strings = self._app_labels().get("mod_strings") or {}
        module_strings = strings.get(module) or {}
        return {k: v for k, v in module_strings.items() if isinstance(v, str)}

    # -- invalidation -------------------------------------------------------

    def invalidate(self, module: str | None = None) -> None:
        """Drop cached metadata. Called on a 412 ``metadata_out_of_date``."""
        if module:
            self._modules.pop(module, None)
        else:
            self._modules.clear()
            self._enums.clear()
            self._labels = None
            self._server_info = None
            self._module_list = None
            self._acl = None
            self._me = None
            self.cache.clear()


def _looks_custom(module: str) -> bool:
    """Heuristic for a Studio/module-builder module.

    Custom modules conventionally carry a package prefix (``abc_Widgets``) or a ``_c``
    suffix. Stock modules never do. This is presentation only — nothing depends on it.
    """
    if module.endswith("_c"):
        return True
    prefix, sep, rest = module.partition("_")
    return bool(sep and rest and prefix.islower() and len(prefix) <= 6)
