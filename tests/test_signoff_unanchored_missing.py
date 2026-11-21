"""Issue #5124: ``unanchored_evidence`` names the missing fields."""

from __future__ import annotations

from klayout_tools.signoff import _binding_check, _generic_item_binding_refusal
from klayout_tools.signoff_evidence_pin import unanchored_detail


def _missing(item_id, envelope, spec):
    return _binding_check("generic", item_id, envelope, spec)[1]


_GOOD = {
    "t1_item": 2,
    "provenance": {"input": {"path": "a.gds", "content_hash": "sha256:ab"}},
}


def test_bare_path_pin_missing() -> None:
    assert _missing(2, _GOOD, {}) == ["manifest content_hash"]
    assert (
        _generic_item_binding_refusal("generic", 2, _GOOD, {}) == "unanchored_evidence"
    )


def test_each_field_and_combined() -> None:
    spec = {"content_hash": "sha256:ab"}
    no_item = {k: v for k, v in _GOOD.items() if k != "t1_item"}
    assert _missing(2, no_item, spec) == ["t1_item"]
    no_path = {"t1_item": 2, "provenance": {"input": {}}}
    assert _missing(2, no_path, spec) == ["provenance.input.path"]
    assert _missing(2, {}, {}) == [
        "t1_item",
        "provenance.input.path",
        "manifest content_hash",
    ]


def test_met_and_wrong_item_carry_no_missing() -> None:
    spec = {"content_hash": "sha256:ab"}
    assert _missing(2, _GOOD, spec) == []
    assert _generic_item_binding_refusal("generic", 2, _GOOD, spec) is None
    wrong = dict(_GOOD, t1_item=1)
    assert _generic_item_binding_refusal("generic", 2, wrong, {}) == "wrong_item"


def test_detail_entry_shape() -> None:
    resolution = {"content_hash": "sha256:ab"}
    detail = unanchored_detail(
        ["manifest content_hash"], {"file": "e.json"}, resolution
    )
    assert detail == {
        "missing": ["manifest content_hash"],
        "manifest_entry": {"file": "e.json", "content_hash": "sha256:ab"},
    }
    assert "manifest_entry" not in unanchored_detail(["t1_item"], {}, resolution)
