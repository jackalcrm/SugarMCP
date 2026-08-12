"""Tests for field projection and the compact encoding.

Run against the recorded Accounts metadata, so the assertions are about vardefs Sugar
actually emits rather than ones convenient to invent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import load_fixture

from sugar.metadata import (
    _label_adds_information,
    _looks_custom,
    compact_field,
    project_field,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="module")
def accounts_fields() -> dict:
    raw = load_fixture("metadata_modules_Accounts.json")
    return raw["modules"]["Accounts"]["fields"]


# -- projection --------------------------------------------------------------


def test_projection_keeps_the_essentials(accounts_fields):
    field = project_field("name", accounts_fields["name"], "Name")
    assert field["type"] == "name"
    assert field["len"] == 150
    assert field["required"] is True
    assert field["label"] == "Name"


def test_projection_drops_the_noise(accounts_fields):
    """The keys that dominate a raw vardef must not survive."""
    field = project_field("name", accounts_fields["name"], "Name")
    for noisy in ("full_text_search", "comment", "comments", "duplicate_merge",
                  "merge_filter", "massupdate", "importable", "audited",
                  "duplicate_on_record_copy", "duplicate_merge_dom_value"):
        assert noisy not in field


def test_projection_shrinks_the_vardef(accounts_fields):
    raw = accounts_fields["billing_address_state"]
    projected = project_field("billing_address_state", raw, "Billing State")
    assert len(json.dumps(projected)) < len(json.dumps(raw)) / 3


def test_enum_keeps_dropdown_name(accounts_fields):
    field = project_field("industry", accounts_fields["industry"], "Industry")
    assert field["options"] == "industry_dom"


def test_non_enum_options_are_suppressed():
    """date_modified carries options=date_range_search_dom — a *search filter* dropdown,
    not a set of values the field can hold. Advertising it would mislead a writer."""
    field = project_field(
        "date_modified", {"type": "datetime", "options": "date_range_search_dom"}
    )
    assert "options" not in field
    assert "option_values" not in field


def test_custom_field_flagged_without_redundant_source(accounts_fields):
    field = project_field("glink_number_c", accounts_fields["glink_number_c"])
    assert field["custom"] is True
    # `source: custom_fields` says nothing `custom` does not.
    assert "source" not in field


def test_readonly_formula_counts_as_readonly(accounts_fields):
    """assigned_user_id is writable by flag but carries readonly_formula."""
    field = project_field("assigned_user_id", accounts_fields["assigned_user_id"])
    assert field["readonly"] is True


def test_link_projection(accounts_fields):
    field = project_field("contacts", accounts_fields["contacts"])
    assert field["type"] == "link"
    assert field["related_module"] == "Contacts"
    assert field["relationship"] == "accounts_contacts"


def test_relate_projection_exposes_navigation_keys(accounts_fields):
    relate = next(
        (n, v) for n, v in accounts_fields.items()
        if isinstance(v, dict) and v.get("type") == "relate" and v.get("id_name")
    )
    field = project_field(relate[0], relate[1])
    assert field["related_module"] == relate[1]["module"]
    assert field["id_field"] == relate[1]["id_name"]


def test_projection_survives_every_recorded_vardef(accounts_fields):
    """No vardef in the real payload may crash the projection."""
    for name, vardef in accounts_fields.items():
        if name == "_hash" or not isinstance(vardef, dict):
            continue
        field = project_field(name, vardef)
        assert field["name"]
        compact_field(field)  # must render too


# -- compact encoding --------------------------------------------------------


def test_compact_basic():
    assert compact_field({"name": "name", "type": "name", "len": 150, "required": True}) \
        == "name(150) req"


def test_compact_flags_are_all_rendered():
    line = compact_field({
        "name": "f", "type": "enum", "readonly": True, "readonly_reason": "license",
        "calculated": True, "custom": True, "options": "some_dom",
    })
    assert "ro:license" in line
    assert "calc" in line
    assert "cf" in line
    assert "opts=some_dom" in line


def test_compact_relate_target():
    line = compact_field({
        "name": "assigned_user_name", "type": "relate",
        "related_module": "Users", "related_display_field": "full_name",
    })
    assert "->Users.full_name" in line


def test_compact_omits_label_that_merely_restates_the_name():
    """80 custom fields labelled with their own name in words is pure overhead."""
    assert not _label_adds_information(
        "account_manager_user_id_c", "account manager user id"
    )
    assert "|" not in compact_field({
        "name": "account_manager_user_id_c", "type": "varchar",
        "label": "account manager user id",
    })


def test_compact_keeps_a_label_that_says_something_new():
    assert _label_adds_information("assigned_user_name", "Assigned to (Account Owner)")
    line = compact_field({
        "name": "assigned_user_name", "type": "relate",
        "label": "Assigned to (Account Owner)",
    })
    assert line.endswith("| Assigned to (Account Owner)")


def test_compact_ignores_trailing_colon_in_label():
    """Sugar labels a lot of fields "Name:" — punctuation is not information."""
    assert not _label_adds_information("name", "Name:")


def test_compact_is_substantially_smaller(accounts_fields):
    projected = {
        n: project_field(n, v)
        for n, v in accounts_fields.items()
        if n != "_hash" and isinstance(v, dict) and v.get("type") != "link"
    }
    compact = {n: compact_field(e) for n, e in projected.items()}
    assert len(json.dumps(compact)) < len(json.dumps(projected)) / 2


# -- custom module heuristic -------------------------------------------------


@pytest.mark.parametrize("name", ["actp_Account_Plan", "glink_Configurations", "abc_Widgets_c"])
def test_custom_modules_detected(name):
    assert _looks_custom(name)


@pytest.mark.parametrize("name", ["Accounts", "Opportunities", "Contacts", "KBContents"])
def test_stock_modules_not_flagged_custom(name):
    assert not _looks_custom(name)
