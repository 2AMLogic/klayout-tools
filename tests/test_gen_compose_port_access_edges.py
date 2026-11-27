"""Residual validation/edge paths of ``ports_from_labels`` parsing and label
resolution (``gen_compose_port_access.py``): the branches the derivation suite
does not reach, kept here so that file stays under the file-size hard limit.
Synthetic sky130 macros only (open PDK layer numbers)."""

import pytest

from klayout_tools.gen_compose import GenComposeError
from test_gen_compose import _isolate as _isolate
from test_gen_compose_port_derivation import (
    MET1,
    MET1_PIN,
    MET2,
    MET2_PIN,
    _blocks,
    _make_macro,
    _spec,
    _two_strip_macro,
)


@pytest.mark.parametrize(
    "access,match",
    [
        ("metal3", "access must be a JSON object"),
        ({"up_to": "metal3", "grid_um": 0}, "grid_um must be a number > 0"),
        (
            {"up_to": "metal3", "max_candidates": 0},
            "max_candidates must be an integer >= 1",
        ),
    ],
)
def test_access_value_validation(tmp_path, access, match):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match=match):
        _blocks(gds, _spec(["A"], access))


@pytest.mark.parametrize(
    "spec,match",
    [
        ([], "ports_from_labels must be a JSON object"),
        (
            {"layers": [{"layer": 68, "datatype": 5}], "names": ["A"], "bogus": 1},
            "unknown field",
        ),
        ({"layers": [], "names": ["A"]}, "layers must be a non-empty"),
        (
            {"layers": [{"layer": 68, "datatype": 5}], "names": []},
            "names must be a non-empty array",
        ),
    ],
)
def test_ports_from_labels_validation(tmp_path, spec, match):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match=match):
        _blocks(gds, spec)


@pytest.mark.parametrize(
    "names,match",
    [
        ([42], "must be a label string or an object with 'name'"),
        ([""], "needs a non-empty label name"),
        (["A", "A"], "duplicate name 'A'"),
    ],
)
def test_names_entry_validation(tmp_path, names, match):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match=match):
        _blocks(gds, _spec(names))


def test_name_object_without_access_uses_shared(tmp_path):
    """A ``{"name": ...}`` entry with no ``access`` key falls back to the
    shared one (absent here) -> port at the label position, no landing."""
    gds = _two_strip_macro(tmp_path)
    reports = _blocks(gds, _spec([{"name": "A"}, "B"]))["m"]["derived_ports"]
    assert [r["name"] for r in reports] == ["A", "B"]
    assert all(r["landing"] is None for r in reports)


def test_unsupported_variant_names_deck_error(tmp_path):
    gds = _two_strip_macro(tmp_path)
    with pytest.raises(GenComposeError, match="no extraction deck for 'bogus-xyz'"):
        _blocks(gds, _spec(["A"]), variant="bogus-xyz")


def test_non_text_shape_on_label_layer_is_skipped(tmp_path):
    gds = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 10.0, 0.5)), (MET1_PIN, (7.5, 0.1, 8.5, 0.4))],
        [(MET1_PIN, "A", 8.0, 0.25)],
    )
    reports = _blocks(gds, _spec(["A"]))["m"]["derived_ports"]
    assert [r["name"] for r in reports] == ["A"]


def test_label_on_metal_layer_itself_resolves(tmp_path):
    """``metal_for_label``: the layer IS a deck metal -> probed directly."""
    gds = _make_macro(
        tmp_path, [(MET1, (0.0, 0.0, 10.0, 0.5))], [(MET1, "A", 8.0, 0.25)]
    )
    reports = _blocks(gds, _spec(["A"], layers=((68, 20),)))["m"]["derived_ports"]
    assert [r["name"] for r in reports] == ["A"]


def test_label_on_unmapped_layer_falls_back_to_all_metals(tmp_path):
    """``metal_for_label``: neither a metal nor a ``metal_labels`` entry and
    no metal shares its GDS layer number -> every deck metal is probed."""
    gds = _make_macro(
        tmp_path, [(MET1, (0.0, 0.0, 10.0, 0.5))], [((234, 0), "A", 8.0, 0.25)]
    )
    reports = _blocks(gds, _spec(["A"], layers=((234, 0),)))["m"]["derived_ports"]
    assert [r["name"] for r in reports] == ["A"]


def test_label_on_missing_metal_sits_on_no_conductor(tmp_path):
    """``(69, 5)`` maps to met2, which this macro does not draw: the probe has
    no region for it -> no conductor carries the label."""
    gds = _make_macro(
        tmp_path, [(MET1, (0.0, 0.0, 10.0, 0.5))], [(MET2_PIN, "A", 8.0, 0.25)]
    )
    with pytest.raises(GenComposeError, match="sits on no conductor"):
        _blocks(gds, _spec(["A"], layers=((69, 5),)))


def test_label_touching_two_unconnected_metals_is_ambiguous(tmp_path):
    """The all-metals fallback probes stacked met1+met2 with no via between:
    the label cannot tell which of the two unconnected nets it names."""
    gds = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 10.0, 0.5)), (MET2, (0.0, -0.5, 10.0, 1.5))],
        [((234, 0), "A", 8.0, 0.25)],
    )
    with pytest.raises(GenComposeError, match="touches several unconnected nets"):
        _blocks(gds, _spec(["A"], layers=((234, 0),)))


def test_same_name_on_two_nets_is_ambiguous(tmp_path):
    gds = _make_macro(
        tmp_path,
        [(MET1, (0.0, 0.0, 4.0, 0.5)), (MET1, (6.0, 0.0, 10.0, 0.5))],
        [(MET1_PIN, "A", 2.0, 0.25), (MET1_PIN, "A", 8.0, 0.25)],
    )
    with pytest.raises(GenComposeError, match="more than one.*distinct net"):
        _blocks(gds, _spec(["A"]))
