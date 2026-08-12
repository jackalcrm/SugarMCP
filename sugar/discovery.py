"""Endpoint discovery: what REST surface does this instance actually have?

Custom endpoints appear automatically, because ``HelpApi`` walks the same service dictionary
that routes real requests. On the reference instance that surfaces 20 custom endpoints —
things like ``POST /Leads/:leadId/convert`` and ``PUT /me`` — that no generic Sugar client
would know about.

Two sources, in order of preference:

* **``GET /mcp/help``** (JSON), from the optional Sugar-side package.
* **``GET /help``** (HTML), core, always present, and lossy. The renderer *skips endpoints
  whose ``shortHelp`` is empty*, and several core ``longHelp`` paths point at
  ``include/api/html/``, a directory that does not exist in 25.x. It is also ~3.9 MB of
  markup for ~700 endpoints. That lossiness is the argument for installing the package.

The catalog is never fetched at startup — only on first use of a tool that needs it.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any, Iterable

from .client import SugarClient
from .errors import SugarError
from .metadata import MetadataCache

log = logging.getLogger("sugarmcp.discovery")

# Each endpoint row in the rendered help page opens with this div.
_ROW = re.compile(r'<div class="row-fluid line">')
_SPAN = re.compile(r'<div class="span\d[^"]*">(.*?)</div>', re.S)
_PATH = re.compile(r'data-target="#endpoint_\w+_full">\s*(.*?)\s*</span>', re.S)
_SOURCE = re.compile(r'sicon-document"></i>\s*(\S+\.php)\s*</div>')
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# Routes a typed tool already covers. Matched on the *shape* of the path, not on whether a
# segment appears anywhere in it: Sugar reuses these words at other positions for unrelated
# endpoints — `Reports/:id/filter` returns a saved report's filter definition and has nothing
# to do with `Accounts/filter`, and blocking it was a real bug.
#
# Each entry is (predicate over path segments, advice).
TYPED_TOOL_ROUTES = (
    # /<module>/filter and /<module>/filter/count
    (lambda s: len(s) == 2 and s[1] == "filter", "use sugar_query_records"),
    (lambda s: len(s) == 3 and s[1] == "filter" and s[2] == "count",
     "use sugar_count_records"),
    # /<module>/count
    (lambda s: len(s) == 2 and s[1] == "count", "use sugar_count_records"),
    # /<module>/enum/<field>
    (lambda s: len(s) == 3 and s[1] == "enum", "use sugar_get_enum"),
    (lambda s: bool(s) and s[0] == "globalsearch", "use sugar_search"),
    (lambda s: bool(s) and s[0] == "metadata",
     "use sugar_describe_module or sugar_list_modules"),
    (lambda s: bool(s) and s[0] == "oauth2", "authentication is handled by the server"),
)


def _text(fragment: str) -> str:
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", fragment))).strip()


def parse_help_html(page: str) -> list[dict[str, Any]]:
    """Extract the endpoint catalog from the rendered help page.

    Verified against the reference instance: 707 of 707 rows parsed, including all 20
    custom endpoints.
    """
    endpoints: list[dict[str, Any]] = []

    for block in _ROW.split(page)[1:]:
        path_match = _PATH.search(block)
        if not path_match:
            continue

        spans = _SPAN.findall(block)
        # The first span holds the HTTP verb followed by the clickable path span; stripping
        # tags leaves "GET /Accounts/:record", so the verb is the first token.
        head = _text(spans[0]) if spans else ""
        verb = head.split()[0] if head else "GET"

        entry: dict[str, Any] = {
            "method": verb,
            "path": _text(path_match.group(1)),
            "handler": _text(spans[1]) if len(spans) > 1 else "",
            "description": _text(spans[2]) if len(spans) > 2 else "",
        }

        exceptions = _text(spans[3]) if len(spans) > 3 else ""
        if exceptions and exceptions != "None":
            entry["exceptions"] = [
                part.strip() for part in re.split(r"\s{2,}|,", exceptions) if part.strip()
            ]

        source = _SOURCE.findall(block)
        if source:
            entry["source"] = source[0]
            # Anything under custom/ was added by this instance rather than shipped.
            entry["custom"] = "custom/" in source[0]

        endpoints.append(entry)

    return endpoints


class EndpointCatalog:
    """The instance's REST surface, fetched lazily and cached to disk."""

    def __init__(self, client: SugarClient, cache: MetadataCache, version: str = ""):
        self.client = client
        self.cache = cache
        self.version = version
        self._endpoints: list[dict[str, Any]] | None = None
        self._source: str = ""

    def load(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._endpoints is not None and not refresh:
            return self._endpoints

        if not refresh:
            cached = self.cache.read("endpoints", "catalog")
            # Invalidated on a Sugar version change: an upgrade adds and removes routes.
            if cached and cached.get("version") == self.version:
                self._endpoints = cached.get("endpoints") or []
                self._source = cached.get("source", "cache")
                return self._endpoints

        endpoints, source = self._fetch()
        self._endpoints = endpoints
        self._source = source
        self.cache.write("endpoints", "catalog", {
            "version": self.version,
            "source": source,
            "endpoints": endpoints,
            "_cached_at": time.time(),
        })
        return endpoints

    def _fetch(self) -> tuple[list[dict[str, Any]], str]:
        """JSON first, HTML second."""
        try:
            payload = self.client.probe("mcp/help")
        except SugarError:
            payload = None

        if isinstance(payload, dict) and isinstance(payload.get("endpoints"), list):
            log.info("Endpoint catalog from /mcp/help (%d endpoints)",
                     len(payload["endpoints"]))
            return payload["endpoints"], "mcp/help"

        log.info("No /mcp/help endpoint; falling back to parsing /help HTML")
        page = self.client.get("help")
        if not isinstance(page, str):
            page = str(page)
        endpoints = parse_help_html(page)
        log.info("Parsed %d endpoints from help HTML", len(endpoints))
        return endpoints, "help-html"

    @property
    def source(self) -> str:
        return self._source

    def search(
        self,
        *,
        query: str = "",
        module: str = "",
        method: str = "",
        custom_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Filter the catalog. Returning all ~700 endpoints would be useless to a model."""
        results: Iterable[dict[str, Any]] = self.load()

        if method:
            wanted = method.upper()
            results = [e for e in results if e.get("method", "").upper() == wanted]
        if module:
            results = [e for e in results if _first_segment(e.get("path", "")).lower()
                       in (module.lower(), "<module>", ":module")]
        if custom_only:
            results = [e for e in results if e.get("custom")]
        if query:
            needle = query.lower()
            results = [
                e for e in results
                if needle in e.get("path", "").lower()
                or needle in e.get("description", "").lower()
            ]

        return list(results)[:limit]


def _first_segment(path: str) -> str:
    return path.lstrip("/").split("/")[0] if path else ""


def refuses_path(path: str) -> str | None:
    """Return a reason if this path should go through a typed tool instead.

    A raw escape hatch that can reach ``/Accounts/filter`` lets the model skip field
    projection and result clamping, which is how a single call ends up returning 24 KB of
    unshaped records. Steering it back is cheap; recovering the context is not.
    """
    segments = [s for s in path.strip("/").split("/") if s]
    for matches, advice in TYPED_TOOL_ROUTES:
        if matches(segments):
            return f"/{'/'.join(segments)} is handled by a dedicated tool — {advice}."
    return None
