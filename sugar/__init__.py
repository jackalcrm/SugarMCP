"""SugarCRM access layer for the MCP server.

Nothing in this package imports from ``mcp``. The tools layer is a thin wrapper over these
modules, which keeps the whole Sugar surface unit-testable without an MCP client and makes
the stdio → streamable-HTTP transport swap a change to session provisioning only.
"""

from .client import SugarClient
from .config import ConfigError, SugarConfig
from .errors import Retry, SugarError, classify
from .session import Capabilities, SugarSession

__all__ = [
    "Capabilities",
    "ConfigError",
    "Retry",
    "SugarClient",
    "SugarConfig",
    "SugarError",
    "SugarSession",
    "classify",
]

__version__ = "0.1.0"
