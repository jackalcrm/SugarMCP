"""Tests for pre-flight write validation and filter checking.

Every case in the first class is a write that a live Sugar 25.2 instance **accepted and
stored**. Sugar's REST API does not validate writes, so these assertions are not about error
message quality — they are the only thing preventing silent data corruption.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import load_fixture

from sugar.acl import AclIndex
from sugar.validation import (
    ValidationError,
    WriteValidator,
    check_filter,
    filter_field_names,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FakeMetadata:
    """Stands in for MetadataManager, backed by the recorded Accounts payload."""

    def __init__(self, vardefs: dict[str, Any], acl: dict[str, Any] | None = None,
                 enums: dict[str, dict[str, str]] | None = None):
        self._vardefs = vardefs
        self._acl = AclIndex(acl or {})
        self._enums = enums or {}

    def acl(self) -> AclIndex:
        return self._acl

    def _module_metadata(self, module: str, *, refresh: bool = False) -> dict[str, Any]:
        return {"fields": self._vardefs}

    def enum(self, module: str, field: str, *, refresh: bool = False) -> dict[str, str]:
        return self._enums.get(field, {})


@pytest.fixture(scope="module")
def accounts_vardefs() -> dict:
    raw = load_fixture("metadata_modules_Accounts.json")
    return {
        name: vardef
        for name, vardef in raw["modules"]["Accounts"]["fields"].items()
        if isinstance(vardef, dict)
    }


@pytest.fixture
def validator(accounts_vardefs) -> WriteValidator:
    return WriteValidator(FakeMetadata(  # type: ignore[arg-type]
        accounts_vardefs,
        enums={"industry": {"": "", "101": "Chemicals", "102": "Agro"}},
    ))


def problems(issues) -> str:
    return " | ".join(str(issue) for issue in issues)


# -- writes Sugar accepted but should not have ------------------------------


def test_missing_required_field_is_caught(validator):
    """Sugar created a nameless Account when `name` was omitted."""
    issues = validator.validate("Accounts", {"billing_address_city": "X"}, verb="create")
    assert issues
    assert "name" in problems(issues)


def test_unknown_field_is_caught_with_a_suggestion(validator):
    """Sugar silently discards unknown fields."""
    issues = validator.validate("Accounts", {"name": "X", "industy": "101"})
    assert issues
    text = problems(issues)
    assert "industy" in text
    assert "industry" in text  # the suggestion


def test_invalid_enum_value_is_caught(validator):
    """Sugar stored 'NOT_A_VALID_CODE' in industry verbatim."""
    issues = validator.validate("Accounts", {"industry": "NOT_A_VALID_CODE"})
    assert issues
    text = problems(issues)
    assert "not a valid value" in text
    assert "'101'" in text  # lists the valid keys


def test_valid_enum_value_passes(validator):
    assert validator.validate("Accounts", {"industry": "101"}) == []


def test_empty_enum_value_passes(validator):
    """The blank option is a real dropdown entry."""
    assert validator.validate("Accounts", {"industry": ""}) == []


def test_readonly_field_is_caught(validator):
    """Sugar accepted a write to date_entered and ignored it."""
    issues = validator.validate("Accounts", {"date_entered": "2020-01-01T00:00:00+00:00"})
    assert issues
    assert "read-only" in problems(issues)


def test_overlong_text_is_caught(validator):
    """Sugar truncated 400 characters into a len-150 column without complaint."""
    issues = validator.validate("Accounts", {"name": "Z" * 400})
    assert issues
    assert "150" in problems(issues)


def test_text_within_the_limit_passes(validator):
    assert validator.validate("Accounts", {"name": "Acme Corp"}) == []


def test_link_field_is_rejected_with_direction(validator):
    issues = validator.validate("Accounts", {"contacts": ["some-id"]})
    assert issues
    assert "sugar_link_records" in problems(issues)


# -- type checks -------------------------------------------------------------


@pytest.mark.parametrize("value", ["not-a-number", "12abc", {}, []])
def test_bad_int_values_rejected(value):
    validator = WriteValidator(FakeMetadata({"count_c": {"name": "count_c", "type": "int"}}))
    assert validator.validate("M", {"count_c": value})


@pytest.mark.parametrize("value", [42, "42", -7, 0])
def test_good_int_values_accepted(value):
    validator = WriteValidator(FakeMetadata({"count_c": {"name": "count_c", "type": "int"}}))
    assert validator.validate("M", {"count_c": value}) == []


def test_boolean_is_not_an_integer():
    """True would be stored as 1, which is almost never what was meant."""
    validator = WriteValidator(FakeMetadata({"count_c": {"name": "count_c", "type": "int"}}))
    assert validator.validate("M", {"count_c": True})


@pytest.mark.parametrize("value", [True, False, "true", "false", 1, 0, "1", "0"])
def test_bool_values_accepted(value):
    validator = WriteValidator(FakeMetadata({"flag_c": {"name": "flag_c", "type": "bool"}}))
    assert validator.validate("M", {"flag_c": value}) == []


def test_bad_bool_rejected():
    validator = WriteValidator(FakeMetadata({"flag_c": {"name": "flag_c", "type": "bool"}}))
    issues = validator.validate("M", {"flag_c": "maybe"})
    assert issues
    assert "checkbox" in problems(issues)


@pytest.mark.parametrize("value", ["31/01/2026", "2026-1-1", "January 31 2026", ""])
def test_bad_dates_rejected(value):
    validator = WriteValidator(FakeMetadata({"d_c": {"name": "d_c", "type": "date"}}))
    assert validator.validate("M", {"d_c": value})


def test_good_date_accepted():
    validator = WriteValidator(FakeMetadata({"d_c": {"name": "d_c", "type": "date"}}))
    assert validator.validate("M", {"d_c": "2026-01-31"}) == []


@pytest.mark.parametrize("value", [
    "2026-01-31T14:30:00+00:00", "2026-01-31T14:30:00Z", "2026-01-31 14:30",
])
def test_good_datetimes_accepted(value):
    validator = WriteValidator(FakeMetadata({"dt_c": {"name": "dt_c", "type": "datetime"}}))
    assert validator.validate("M", {"dt_c": value}) == []


def test_multienum_validates_each_value():
    validator = WriteValidator(FakeMetadata(
        {"tags_c": {"name": "tags_c", "type": "multienum"}},
        enums={"tags_c": {"a": "A", "b": "B"}},
    ))
    assert validator.validate("M", {"tags_c": ["a", "b"]}) == []
    issues = validator.validate("M", {"tags_c": ["a", "zzz"]})
    assert issues
    assert "zzz" in problems(issues)


# -- required-field handling -------------------------------------------------


def test_required_field_with_a_default_is_not_demanded():
    """Sugar fills the default, so the caller need not supply it."""
    validator = WriteValidator(FakeMetadata({
        "status_c": {"name": "status_c", "type": "enum", "required": True,
                     "default": "Active"},
    }, enums={"status_c": {"Active": "Active"}}))
    assert validator.validate("M", {}, verb="create") == []


@pytest.mark.parametrize("default", ["", [], [""], None])
def test_required_field_with_an_empty_default_is_still_demanded(default):
    """Sugar spells "no default" several ways; none of them populate the field."""
    validator = WriteValidator(FakeMetadata({
        "thing_c": {"name": "thing_c", "type": "varchar", "required": True,
                    "default": default},
    }))
    assert validator.validate("M", {}, verb="create")


def test_system_required_fields_are_not_demanded(validator):
    """id, team_count and team_name are flagged required but are Sugar's to manage."""
    issues = validator.validate("Accounts", {"name": "Acme"}, verb="create")
    text = problems(issues)
    for system in ("team_count", "team_name", "id"):
        assert system not in text


def test_required_only_enforced_on_create(validator):
    assert validator.validate("Accounts", {"billing_address_city": "X"}, verb="update") == []


# -- ACL integration ---------------------------------------------------------


def test_denied_module_blocks_the_write(accounts_vardefs):
    validator = WriteValidator(FakeMetadata(
        accounts_vardefs, acl={"Accounts": {"access": "no", "fields": []}}
    ))
    issues = validator.validate("Accounts", {"name": "X"})
    assert issues and "denied" in problems(issues)


def test_denied_action_blocks_the_write(accounts_vardefs):
    validator = WriteValidator(FakeMetadata(
        accounts_vardefs, acl={"Accounts": {"edit": "no", "fields": []}}
    ))
    assert validator.validate("Accounts", {"name": "X"}, verb="update")
    # create is a different action and is still allowed
    assert not any(
        "permission" in str(i)
        for i in validator.validate("Accounts", {"name": "X"}, verb="create")
    )


def test_license_gated_field_blocks_the_write(accounts_vardefs):
    validator = WriteValidator(FakeMetadata(accounts_vardefs, acl={
        "Accounts": {"fields": {"dri_workflow_template_id": {
            "write": "no", "create": "no", "license": "no"}}}
    }))
    issues = validator.validate("Accounts", {"dri_workflow_template_id": "x"})
    assert issues and "license" in problems(issues)


# -- error rendering ---------------------------------------------------------


def test_validation_error_renders_for_the_model():
    error = ValidationError("Accounts", [])
    payload = error.as_tool_error()
    assert payload["error_label"] == "validation_failed"
    assert "Sugar does not validate writes" in payload["guidance"]


# -- filter checking ---------------------------------------------------------


def test_filter_field_extraction_descends_macros():
    names = filter_field_names([{"$or": [{"a": "1"}, {"$and": [{"b": "2"}]}]}, {"c": "3"}])
    assert set(names) == {"a", "b", "c"}


def test_unknown_filter_field_is_an_error(accounts_vardefs):
    metadata = FakeMetadata(accounts_vardefs)
    errors, warnings = check_filter(metadata, "Accounts", [{"industy": "101"}])
    assert errors and "industry" in errors[0]
    assert not warnings


def test_non_db_filter_field_is_a_warning():
    """The dangerous case: Sugar drops the condition and returns everything."""
    metadata = FakeMetadata({
        "name": {"name": "name", "type": "name", "source": "non-db"},
    })
    errors, warnings = check_filter(metadata, "Contacts", [{"name": {"$starts": "Z"}}])
    assert not errors
    assert warnings and "silently drops" in warnings[0]


def test_valid_filter_is_clean(accounts_vardefs):
    metadata = FakeMetadata(accounts_vardefs)
    errors, warnings = check_filter(metadata, "Accounts", [{"industry": "101"}])
    assert not errors and not warnings


def test_related_field_filter_is_not_flagged(accounts_vardefs):
    """link.remote_field cannot be checked without the far module; allow it through."""
    metadata = FakeMetadata(accounts_vardefs)
    errors, warnings = check_filter(metadata, "Accounts", [{"contacts.last_name": "X"}])
    assert not errors and not warnings


def test_owner_macro_is_not_treated_as_a_field(accounts_vardefs):
    metadata = FakeMetadata(accounts_vardefs)
    errors, _ = check_filter(metadata, "Accounts", [{"$owner": ""}])
    assert not errors
