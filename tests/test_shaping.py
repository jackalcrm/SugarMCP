"""Tests for result shaping and query-parameter encoding."""

from __future__ import annotations

from sugar.shaping import (
    clamp,
    describe_truncation,
    encode_filter_params,
    record_web_url,
    resolve_fields,
    shape_list,
    trim_record,
)


# -- field projection --------------------------------------------------------


def test_id_is_always_included():
    """Without id a caller cannot fetch or update the record afterwards."""
    assert "id" in resolve_fields(["name"])
    assert "id" in resolve_fields(None)


def test_requested_order_is_preserved_and_deduplicated():
    assert resolve_fields(["id", "name", "name", "industry"]) == ["id", "name", "industry"]


def test_unknown_fields_dropped_when_metadata_is_available():
    fields = resolve_fields(["id", "name", "nope"], available={"id": {}, "name": {}})
    assert fields == ["id", "name"]


def test_unknown_fields_kept_when_metadata_is_unavailable():
    """A failed describe must not silently strip the caller's projection."""
    assert resolve_fields(["id", "custom_c"]) == ["id", "custom_c"]


# -- clamping ----------------------------------------------------------------


def test_clamp_applies_default_and_ceiling():
    assert clamp(None, 20, 100) == 20
    assert clamp(9999, 20, 100) == 100
    assert clamp(5, 20, 100) == 5


def test_clamp_rejects_nonsense():
    assert clamp(0, 20, 100) == 1
    assert clamp(-5, 20, 100) == 1
    assert clamp("abc", 20, 100) == 20  # type: ignore[arg-type]


# -- record trimming ---------------------------------------------------------


def test_empty_acl_is_stripped():
    """`_acl` is only the diff from the module ACL, so an empty fields map says nothing."""
    assert "_acl" not in trim_record({"id": "1", "_acl": {"fields": {}}})
    assert "_acl" not in trim_record({"id": "1", "_acl": {"fields": []}})


def test_non_empty_acl_is_kept():
    record = trim_record({"id": "1", "_acl": {"fields": {"secret": {"read": "no"}}}})
    assert record["_acl"]["fields"]["secret"]


def test_scaffolding_stripped():
    record = trim_record({"id": "1", "_module": "Accounts", "_hash": "abc", "name": "X"})
    assert record == {"id": "1", "name": "X"}


def test_empty_values_dropped():
    record = trim_record({"id": "1", "description": "", "tags": [], "phone": None})
    assert record == {"id": "1"}


def test_long_text_truncated_and_marked():
    record = trim_record({"id": "1", "description": "x" * 900}, text_limit=100)
    assert len(record["description"]) == 100
    assert record["_truncated"] == ["description"]


def test_short_text_untouched():
    record = trim_record({"id": "1", "description": "short"}, text_limit=100)
    assert record["description"] == "short"
    assert "_truncated" not in record


# -- list shaping ------------------------------------------------------------


def test_shape_list_reports_pagination():
    shaped = shape_list({"records": [{"id": "1"}, {"id": "2"}], "next_offset": 2})
    assert shaped["count"] == 2
    assert shaped["next_offset"] == 2
    assert shaped["more_available"] is True


def test_exhausted_result_set():
    shaped = shape_list({"records": [{"id": "1"}], "next_offset": -1})
    assert shaped["more_available"] is False


def test_shape_list_tolerates_a_missing_records_key():
    assert shape_list({})["records"] == []


def test_truncation_note_only_when_truncation_happened():
    plain = shape_list({"records": [{"id": "1", "description": "short"}]})
    assert describe_truncation(plain) is None

    cut = shape_list({"records": [{"id": "1", "description": "x" * 900}]})
    assert "truncated" in (describe_truncation(cut) or "")


# -- filter encoding ---------------------------------------------------------


def test_operator_filter_encoded_in_bracket_notation():
    """The link-filter route is GET-only, so filters must go in the query string."""
    params = encode_filter_params([{"name": {"$starts": "Acme"}}])
    assert params == {"filter[0][name][$starts]": "Acme"}


def test_bare_value_filter_encoded():
    assert encode_filter_params([{"status": "New"}]) == {"filter[0][status]": "New"}


def test_multiple_conditions_are_indexed_separately():
    params = encode_filter_params([{"a": "1"}, {"b": "2"}])
    assert params == {"filter[0][a]": "1", "filter[1][b]": "2"}


def test_nested_macro_filter_encoded():
    params = encode_filter_params([{"$or": [{"status": "New"}, {"status": "Open"}]}])
    assert params == {
        "filter[0][$or][0][status]": "New",
        "filter[0][$or][1][status]": "Open",
    }


def test_in_operator_list_is_indexed():
    params = encode_filter_params([{"status": {"$in": ["New", "Open"]}}])
    assert params == {
        "filter[0][status][$in][0]": "New",
        "filter[0][status][$in][1]": "Open",
    }


def test_booleans_rendered_the_way_sugar_expects():
    assert encode_filter_params([{"deleted": False}]) == {"filter[0][deleted]": "false"}


# -- record web URLs ---------------------------------------------------------


def test_record_web_url_uses_sidecar_hash_route():
    assert (
        record_web_url("http://host", "Accounts", "abc-123")
        == "http://host/#Accounts/abc-123"
    )


def test_record_web_url_preserves_subpath_install():
    """A subpath install (base already has no trailing slash) keeps the /sugar segment."""
    assert (
        record_web_url("http://docker.local:8088/sugar", "Contacts", "id9")
        == "http://docker.local:8088/sugar/#Contacts/id9"
    )
