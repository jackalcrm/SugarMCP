"""Session provider — the seam between the transport and everything below it.

Under stdio there is exactly one context, built from the environment. A future
streamable-HTTP transport replaces :func:`get_context` with a session-keyed lookup and adds
an auth shim; nothing in ``tools/`` or ``sugar/`` changes, because none of it holds
module-level mutable token state.

Construction is **lazy**. A Sugar instance that is down, or credentials that are wrong, must
not stop the MCP server from starting: an MCP client that cannot complete initialization
reports the server as failed and shows no tools at all, which is a far worse failure mode
than a tool returning an actionable error on first use.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .client import SugarClient
from .config import SugarConfig
from .discovery import EndpointCatalog
from .metadata import MetadataManager
from .session import SugarSession

log = logging.getLogger("sugarmcp.context")


class SugarContext:
    """Bundles the session, client and metadata manager for one user."""

    def __init__(self, config: SugarConfig):
        self.config = config
        self.session = SugarSession(config)
        # The client tells the metadata manager to drop its cache on a 412, which is how
        # `metadata_out_of_date` becomes a transparent retry rather than a tool failure.
        self.client = SugarClient(self.session, on_metadata_stale=self._invalidate_metadata)
        self.metadata = MetadataManager(self.client, config.metadata_cache_dir)
        self._catalog: EndpointCatalog | None = None

    @property
    def catalog(self) -> EndpointCatalog:
        """The endpoint catalog, built on first use.

        Deliberately not constructed in __init__: discovering the REST surface costs a
        3.9 MB HTML fetch on an instance without the Sugar-side package, and most sessions
        never ask for it.
        """
        if self._catalog is None:
            version = str(self.metadata.server_info.get("version") or "")
            self._catalog = EndpointCatalog(
                self.client, self.metadata.cache, version=version
            )
        return self._catalog

    def _invalidate_metadata(self) -> None:
        log.info("Sugar reported metadata_out_of_date; clearing cache")
        self.metadata.invalidate()

    def close(self) -> None:
        self.session.close()

    def capabilities(self) -> dict[str, Any]:
        """Probe the optional Sugar-side package once, and report what we are running on."""
        caps = self.session.caps
        if caps.mcp_help is None:
            caps.mcp_help = self.client.probe("mcp/help") is not None
        if caps.mcp_schema is None:
            caps.mcp_schema = self.client.probe("mcp/schema/Accounts") is not None
        caps.server_info = self.metadata.server_info
        return caps.as_dict()


_context: SugarContext | None = None
_lock = threading.Lock()


def get_context() -> SugarContext:
    """The process-wide context, built on first use."""
    global _context
    with _lock:
        if _context is None:
            _context = SugarContext(SugarConfig.from_env())
        return _context


def set_context(context: SugarContext | None) -> None:
    """Replace the context. For tests, and for the eventual per-session transport."""
    global _context
    with _lock:
        _context = context
