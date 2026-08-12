"""Tests for ACL interpretation.

The design doc calls this the highest-risk area: because Sugar strips ``yes`` values, an
inverted check silently grants everything rather than failing loudly. These tests assert the
inversion in both directions, over both the synthetic edge cases and the recorded payload
from the reference instance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import load_fixture

from sugar.acl import (
    ALLOW_ALL_FIELD,
    AclIndex,
    FieldAcl,
    ModuleAcl,
    normalize_fields,
    parse_field_acl,
    parse_module_acl,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="module")
def recorded_acl() -> dict:
    return load_fixture("me_acl_admin.json")


@pytest.fixture(scope="module")
def restricted_acl() -> dict:
    """A genuinely restricted user, captured via /oauth2/sudo.

    The admin fixture cannot exercise hidden fields or broad module denial, because an
    admin has almost none. This one has 49 denied modules and 20 fields with ``read: no``.
    """
    return load_fixture("me_acl_nonadmin.json")


# -- the inversion -----------------------------------------------------------


def test_absent_key_means_allowed():
    """The whole trap: no key at all is full permission, not zero permission."""
    acl = parse_module_acl("Accounts", {"fields": [], "_hash": "x"})
    assert acl.accessible
    for action in ("view", "list", "edit", "delete", "create", "export", "massupdate"):
        assert acl.allows(action), f"{action} should be allowed when unlisted"


def test_empty_block_means_allowed():
    assert parse_module_acl("Accounts", {}).accessible
    assert parse_module_acl("Accounts", None).accessible


def test_explicit_no_denies():
    acl = parse_module_acl("Leads", {"edit": "no", "delete": "no", "fields": []})
    assert acl.accessible
    assert acl.allows("view")
    assert not acl.allows("edit")
    assert not acl.allows("delete")


def test_access_no_gates_everything():
    """`access: no` disables the module wholesale, even for actions not listed."""
    acl = parse_module_acl("CJ_Forms", {"access": "no", "view": "no", "fields": []})
    assert not acl.accessible
    assert not acl.readable
    assert not acl.allows("list")  # not listed as denied, but access gates it


def test_yes_is_not_a_denial():
    """Sugar strips these, but a truthiness bug would read "yes" as a denial."""
    acl = parse_module_acl("Accounts", {"edit": "yes", "delete": "yes", "fields": []})
    assert acl.allows("edit")
    assert acl.allows("delete")


# -- the shape trap ----------------------------------------------------------


def test_fields_empty_php_array_is_a_list():
    """175 of 184 modules in the reference payload send `[]`, not `{}`."""
    assert normalize_fields([]) == {}
    assert normalize_fields({}) == {}
    assert normalize_fields(None) == {}


def test_fields_populated_is_an_object():
    raw = {"secret": {"read": "no"}}
    assert normalize_fields(raw) == raw


def test_module_with_list_fields_parses():
    acl = parse_module_acl("Notes", {"fields": [], "_hash": "abc"})
    assert acl.fields == {}
    assert acl.field("anything") is ALLOW_ALL_FIELD


# -- field codes -------------------------------------------------------------


def test_unlisted_field_is_fully_accessible():
    acl = parse_module_acl("Accounts", {"fields": {}})
    field = acl.field("name")
    assert field.readable and field.writable and field.creatable


def test_read_only_field():
    field = parse_field_acl({"write": "no", "create": "no"})
    assert field.readable
    assert not field.writable
    assert not field.creatable
    assert field.read_only
    assert not field.hidden


def test_hidden_field():
    field = parse_field_acl({"read": "no"})
    assert not field.readable
    assert field.hidden


def test_license_gated_field_is_read_only_and_flagged():
    """The shape covering 124 of 126 restrictions in the recorded payload."""
    field = parse_field_acl({"create": "no", "write": "no", "license": "no"})
    assert field.readable
    assert not field.writable
    assert field.license_blocked
    assert "license" in field.reason()


def test_license_no_blocks_writes_even_when_write_unlisted():
    field = parse_field_acl({"license": "no"})
    assert not field.writable
    assert not field.creatable


def test_all_no_field():
    field = parse_field_acl({"read": "no", "write": "no", "create": "no"})
    assert not field.readable and not field.writable


# -- index and pre-flight ----------------------------------------------------


def test_from_me_detects_admin_via_type():
    """There is no `is_admin` key on /me — admin is `type == "admin"`."""
    assert AclIndex.from_me({"current_user": {"type": "admin", "acl": {}}}).is_admin
    assert not AclIndex.from_me({"current_user": {"type": "regular", "acl": {}}}).is_admin


def test_from_me_accepts_wrapped_and_bare():
    acl = {"Accounts": {"fields": []}}
    assert "Accounts" in AclIndex.from_me({"current_user": {"acl": acl}})
    assert "Accounts" in AclIndex.from_me({"acl": acl})


def test_unknown_module_is_unrestricted():
    """A module missing from the payload has no restrictions recorded, so allow it."""
    index = AclIndex({"Accounts": {"fields": []}})
    assert index.module("CustomModule_c").accessible
    assert index.can("CustomModule_c", "edit")


def test_check_write_allows_clean_case():
    index = AclIndex({"Accounts": {"fields": []}})
    assert index.check_write("Accounts", "update", ["name", "industry"]) is None


def test_check_write_blocks_denied_module():
    index = AclIndex({"CJ_Forms": {"access": "no", "fields": []}})
    message = index.check_write("CJ_Forms", "create", [])
    assert message and "denied" in message


def test_check_write_blocks_denied_action():
    index = AclIndex({"Accounts": {"delete": "no", "fields": []}})
    assert index.check_write("Accounts", "update") is None
    message = index.check_write("Accounts", "delete")
    assert message and "'delete'" in message


def test_check_write_names_the_blocked_fields_and_why():
    index = AclIndex(
        {"Accounts": {"fields": {"locked": {"write": "no", "create": "no", "license": "no"}}}}
    )
    message = index.check_write("Accounts", "update", ["name", "locked"])
    assert message
    assert "locked" in message
    assert "license" in message
    assert "name" not in message.split("locked")[0].split(":")[-1]


def test_check_write_uses_create_permission_for_creates():
    index = AclIndex({"Accounts": {"fields": {"f": {"create": "no"}}}})
    assert index.check_write("Accounts", "create", ["f"]) is not None
    assert index.check_write("Accounts", "update", ["f"]) is None


# -- against the recorded instance payload -----------------------------------


def test_recorded_payload_shape(recorded_acl):
    index = AclIndex(recorded_acl)
    assert len(recorded_acl) == 184
    # The 11 modules genuinely denied on the reference instance.
    denied = index.denied_modules()
    assert len(denied) == 11
    assert "CJ_Forms" in denied
    assert "DRI_Workflows" in denied


def test_recorded_payload_does_not_deny_core_modules(recorded_acl):
    """The inversion bug's signature: core modules appearing inaccessible."""
    index = AclIndex(recorded_acl)
    for module in ("Accounts", "Contacts", "Opportunities", "Cases", "Leads", "Notes"):
        assert index.module(module).accessible, f"{module} must be accessible"
        assert index.can(module, "edit"), f"{module} edit must be allowed"


def test_recorded_accounts_license_gated_fields(recorded_acl):
    index = AclIndex(recorded_acl)
    accounts = index.module("Accounts")
    gated = accounts.field("dri_workflow_template_id")
    assert gated.readable
    assert not gated.writable
    assert gated.license_blocked
    # A normal field on the same module stays fully writable.
    assert accounts.field("name").writable


def test_recorded_summary_only_reports_restrictions(recorded_acl):
    index = AclIndex(recorded_acl)
    assert index.module("Notes").summary() == {}
    accounts = index.module("Accounts").summary()
    assert "read_only_fields" in accounts
    assert "denied_actions" not in accounts


# -- against a genuinely restricted (non-admin) user -------------------------


def test_restricted_user_is_not_admin(restricted_acl):
    index = AclIndex(restricted_acl, is_admin=False)
    assert not index.is_admin


def test_restricted_user_has_many_denied_modules(restricted_acl):
    """49 denied here versus 11 for the admin — the case the admin fixture cannot cover."""
    index = AclIndex(restricted_acl)
    denied = index.denied_modules()
    assert len(denied) == 49
    for module in ("Bugs", "Contracts", "ACLRoles", "Administration"):
        assert module in denied
        assert not index.module(module).accessible


def test_restricted_user_still_reaches_core_modules(restricted_acl):
    """The inversion bug's signature would be everything denied."""
    index = AclIndex(restricted_acl)
    for module in ("Accounts", "Contacts", "Opportunities", "Users"):
        assert index.module(module).accessible


def test_hidden_fields_are_detected(restricted_acl):
    """`{"create":"no","read":"no","write":"no"}` — 20 of these exist in this payload."""
    index = AclIndex(restricted_acl)
    users = index.module("Users")
    for name in ("authenticate_id", "external_auth_only", "is_group", "portal_only"):
        field = users.field(name)
        assert field.hidden, f"{name} must be hidden"
        assert not field.readable


def test_hidden_fields_are_excluded_from_readable(restricted_acl):
    index = AclIndex(restricted_acl)
    users = index.module("Users")
    names = ["user_name", "authenticate_id", "is_group", "last_name"]
    assert users.readable_fields(names) == ["user_name", "last_name"]


def test_read_only_fields_distinguished_from_hidden(restricted_acl):
    """464 fields here are `{"create":"no","write":"no"}` — readable but not writable."""
    index = AclIndex(restricted_acl)
    read_only = [
        (module, name)
        for module, acl in index._modules.items()
        for name, spec in acl.fields.items()
        if spec.read_only
    ]
    assert len(read_only) > 100
    module, name = read_only[0]
    field = index.module(module).field(name)
    assert field.readable and not field.writable and not field.hidden


def test_module_readable_but_not_editable(restricted_acl):
    """Users is viewable but denies create/edit/delete — a common real shape."""
    index = AclIndex(restricted_acl)
    users = index.module("Users")
    assert users.accessible
    assert users.readable
    assert not users.allows("edit")
    assert not users.allows("create")
    assert not users.allows("delete")


def test_admin_and_developer_flags_are_ignored(restricted_acl):
    """Both are "no" on all 184 modules for a non-admin.

    They grant Studio rights, not data access. Counting them as denials would report every
    module as restricted and drown out the denials that matter.
    """
    index = AclIndex(restricted_acl)
    accounts = index.module("Accounts")
    assert accounts.accessible
    assert "admin" not in accounts.denials
    assert "developer" not in accounts.denials
    assert "admin" not in (accounts.summary().get("denied_actions") or [])


def test_check_write_blocks_on_the_real_payload(restricted_acl):
    index = AclIndex(restricted_acl)
    # Module-level denial.
    assert index.check_write("Users", "update", ["last_name"]) is not None
    # Wholly denied module.
    assert index.check_write("Bugs", "create", []) is not None


def test_summary_reports_hidden_and_read_only_separately(restricted_acl):
    index = AclIndex(restricted_acl)
    summary = index.module("Users").summary()
    assert "authenticate_id" in summary["hidden_fields"]
    assert set(summary["denied_actions"]) >= {"create", "delete", "edit"}
