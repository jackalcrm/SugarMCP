"""Tests for endpoint-catalog parsing and the raw-call guard.

Parsed against the recorded ``help.html`` — 3.9 MB of real output from a customized
instance, which is the only way to be confident about a scraper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import load_fixture

from sugar.discovery import parse_help_html, refuses_path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="module")
def endpoints() -> list[dict]:
    page = load_fixture("help.html")
    return parse_help_html(page)


# -- parsing -----------------------------------------------------------------


def test_every_row_parses(endpoints):
    """707 rows in the recorded page; a silent drop would shrink the catalog."""
    assert len(endpoints) == 707


def test_no_endpoint_is_missing_a_path(endpoints):
    assert all(e.get("path") for e in endpoints)


def test_verbs_are_extracted(endpoints):
    verbs = {e["method"] for e in endpoints}
    assert {"GET", "POST", "PUT", "DELETE"} <= verbs


def test_path_variables_are_readable(endpoints):
    """Core substitutes '?' tokens with ':<pathVar>' — the readable form must survive."""
    paths = {e["path"] for e in endpoints}
    assert any(":record" in p for p in paths)
    assert "?" not in " ".join(paths)


def test_descriptions_are_captured(endpoints):
    described = [e for e in endpoints if e.get("description")]
    assert len(described) > len(endpoints) * 0.8


def test_source_files_are_captured(endpoints):
    """627 of 707 rows carry a source path; the remaining 80 render without one.

    That gap is not a parser bug — it is the HTML page being lossy, the same reason the
    JSON endpoint in the Sugar-side package is worth installing. Pinned exactly so a real
    regression in the scraper shows up as a change rather than hiding under a threshold.
    """
    with_source = [e for e in endpoints if e.get("source")]
    assert len(with_source) == 627


def test_custom_endpoints_are_flagged(endpoints):
    """The payoff: telling instance customization from stock functionality."""
    custom = [e for e in endpoints if e.get("custom")]
    assert len(custom) == 20
    assert all("custom/" in e["source"] for e in custom)

    paths = {e["path"] for e in custom}
    assert "/Leads/:leadId/convert" in paths


def test_stock_endpoints_are_not_flagged_custom(endpoints):
    ping = next(e for e in endpoints if e["path"] == "/ping")
    assert not ping.get("custom")


def test_exceptions_are_parsed(endpoints):
    with_exceptions = [e for e in endpoints if e.get("exceptions")]
    assert with_exceptions
    assert "None" not in {x for e in with_exceptions for x in e["exceptions"]}


def test_html_entities_are_decoded(endpoints):
    """Core renders the module placeholder as &lt;module&gt;."""
    paths = " ".join(e["path"] for e in endpoints)
    assert "&lt;" not in paths and "&gt;" not in paths
    assert "<module>" in paths


def test_parser_tolerates_junk():
    assert parse_help_html("") == []
    assert parse_help_html("<html><body>nothing here</body></html>") == []


# -- the typed-tool guard ----------------------------------------------------


@pytest.mark.parametrize("path", [
    "Accounts/filter",
    "/Accounts/filter",
    "Contacts/count",
    "globalsearch",
    "metadata",
    "Accounts/enum/industry",
    "oauth2/token",
])
def test_typed_tool_paths_are_refused(path):
    """A raw call to these bypasses field projection, clamping and validation."""
    assert refuses_path(path) is not None


@pytest.mark.parametrize("path", [
    "ping",
    "Accounts/abc-123/opportunity_stats",
    "Leads/abc-123/convert",
    "me",
    "Accounts/AccountPlans/abc-123",
])
def test_legitimate_escape_hatch_paths_are_allowed(path):
    assert refuses_path(path) is None


def test_guard_matches_whole_segments_only():
    """A field or module merely *containing* a marker must not be blocked."""
    assert refuses_path("CustomFilterReports/abc") is None
    assert refuses_path("Accounts/counties") is None


@pytest.mark.parametrize("path", [
    "Reports/abc-123/filter",        # a saved report's filter definition
    "Reports/abc-123/records",       # running a report
    "Reports/abc-123/record_count",
    "Reports/saved_reports",
    "Accounts/abc-123/link/contacts/count",
    "Administration/search/status",
])
def test_reports_and_nested_paths_are_not_falsely_refused(path):
    """The guard matches route *shape*, not any segment containing the word.

    Sugar reuses these words at other positions: `Reports/:id/filter` returns a saved
    report's filter definition and is unrelated to `Accounts/filter`. An earlier
    substring-based guard blocked it.
    """
    assert refuses_path(path) is None
