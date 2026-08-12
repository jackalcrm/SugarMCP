"""Shared test configuration.

Fixtures under ``fixtures/`` are verbatim captures from a real Sugar instance and are
deliberately **not committed** — they carry customer-identifying detail (custom field and
module names, instance identifiers, sample records in the help page).

The consequence is that most of this suite cannot run on a fresh clone. Rather than fail with
a confusing ``FileNotFoundError``, those tests skip with a message naming the command that
restores them. The tests that need no captured data — client retry policy, shaping, config,
and the synthetic half of the ACL and validation suites — run regardless.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

CAPTURE_HINT = (
    "Recorded fixture {name!r} is not present. These are not committed because they are "
    "captures from a real instance. Restore them with:\n"
    "    uv run scripts/capture_fixtures.py\n"
    "and for the restricted-user ACL fixture:\n"
    "    uv run scripts/capture_acl_fixture.py <user_name> nonadmin"
)


def load_fixture(name: str):
    """Return a parsed fixture, or skip the test if it has not been captured."""
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(CAPTURE_HINT.format(name=name), allow_module_level=True)
    if path.suffix == ".json":
        return json.loads(path.read_text())
    return path.read_text(encoding="utf-8", errors="replace")


def require_fixture(name: str) -> Path:
    """Return a fixture's path, or skip the test if it has not been captured."""
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(CAPTURE_HINT.format(name=name), allow_module_level=True)
    return path
