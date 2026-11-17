"""Per-edge resistance-provenance citations (`klt power`, issue #5791).

`stackup[].sheet_resistance_source` / `vias[].resistance_ohm_source` are
echoed on every edge the declaring role contributes. Split from
`test_power.py` (the same per-file line-ratchet relief as the `test_pex_*`
splits); fixtures come from `test_power.py` the way
`test_spec_unknown_keys.py` reuses `test_erc`'s.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.power import PowerError, run_power
from test_power import (
    _basic_fixture,
    _basic_spec,
    _deck_via,
    _gf180_spec,
    _gf180_via_fixture,
    _l_bus_fixture,
    _l_bus_spec,
    _only_via_edge,
    _via_edge_ohms,
)


def test_resistance_citations_echo_per_edge_and_default_to_null(tmp_path):
    """`stackup[].sheet_resistance_source` and `vias[].resistance_ohm_source`
    are echoed on every edge the role contributes as
    `resistance_ohm_source`; a role declaring none contributes `null` -- the
    same convention `current_limit_source` already pins."""
    gds, spec = tmp_path / "basic.gds", tmp_path / "cited.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)
    payload = json.loads(spec.read_text())
    payload["stackup"][0]["sheet_resistance_source"] = "unit-test met1 citation"
    payload["vias"][0]["resistance_ohm_source"] = "unit-test via1 citation"
    spec.write_text(json.dumps(payload))

    report = run_power(str(gds), str(spec))
    vpwr = next(n for n in report["networks"] if n["net"] == "VPWR")
    island_a = next(i for i in vpwr["islands"] if i["node_count"] == 2)
    island_b = next(i for i in vpwr["islands"] if i["node_count"] == 4)

    # Island A is met1-only, so its single edge is the box-model metal path.
    met1_edge = island_a["edges"][0]
    assert met1_edge["kind"] == "metal"
    assert met1_edge["resistance_ohm_source"] == "unit-test met1 citation"

    # Island B's via edge carries the via role's citation.
    via_edge = next(e for e in island_b["edges"] if e["kind"] == "via")
    assert via_edge["resistance_ohm_source"] == "unit-test via1 citation"

    # met2 declared no citation -- null, never inherited from the via that
    # names its net (mirrors met2's `current_limit_source is None` pin).
    met2_edge = next(e for e in island_b["edges"] if e["layer"] == "met2")
    assert met2_edge["resistance_ohm_source"] is None

    # met1's citation is per role, so island C's VGND rail carries it too.
    vgnd = next(n for n in report["networks"] if n["net"] == "VGND")
    assert vgnd["islands"][0]["edges"][0]["resistance_ohm_source"] == (
        "unit-test met1 citation"
    )


def test_via_citation_without_resistance_ohm_is_rejected_even_under_deck(
    tmp_path,
):
    """`resistance_ohm_source` without `resistance_ohm` would assert spec
    provenance for a number `--deck` is about to fill in (the report's own
    `resistance_source` would say `deck:...`), so it is rejected outright."""
    gds, spec = tmp_path / "g.gds", tmp_path / "g.json"
    _gf180_via_fixture(gds)
    _gf180_spec(
        spec,
        vias=[_deck_via(resistance_ohm_source="unit-test via1 citation")],
    )

    with pytest.raises(
        PowerError,
        match=r"vias\[0\] gives 'resistance_ohm_source' without 'resistance_ohm'",
    ):
        run_power(str(gds), str(spec), deck="gf180mcu")


def test_deck_explicit_resistance_keeps_the_spec_citation(tmp_path):
    """An explicit `resistance_ohm` wins over the deck (including under
    `--deck`), so its citation stays truthful and the report's
    `resistance_source` stays `"spec"`."""
    gds, spec = tmp_path / "g.gds", tmp_path / "g.json"
    _gf180_via_fixture(gds)
    _gf180_spec(
        spec,
        vias=[
            _deck_via(
                resistance_ohm=4.5,
                resistance_ohm_source="unit-test via1 citation",
            )
        ],
    )

    report = run_power(str(gds), str(spec), deck="gf180mcu", resistance_corner="high")

    assert _via_edge_ohms(report) == [4.5]  # the spec's value, not the high corner's 15
    assert report["vias"][0]["resistance_source"] == "spec"
    assert _only_via_edge(report)["resistance_ohm_source"] == "unit-test via1 citation"


def test_decomposed_polygon_edges_all_carry_the_citation(tmp_path):
    """A non-rectangular metal polygon's citation reaches every decomposed
    sub-segment edge, not just the box model's single edge (#5791)."""
    gds, spec = tmp_path / "lshape.gds", tmp_path / "lshape.power.json"
    _l_bus_fixture(gds)
    _l_bus_spec(spec)
    payload = json.loads(spec.read_text())
    payload["stackup"][0]["sheet_resistance_source"] = "unit-test met1 citation"
    spec.write_text(json.dumps(payload))

    report = run_power(str(gds), str(spec))
    island = report["networks"][0]["islands"][0]
    assert island["edge_count"] > 1  # decomposed, not the one-edge box model
    assert all(
        edge["resistance_ohm_source"] == "unit-test met1 citation"
        for edge in island["edges"]
    )


@pytest.mark.parametrize(
    ("raw", "coerced"),
    [(42, "42"), (True, "True"), (0.1, "0.1")],
)
def test_resistance_citations_are_string_coerced_not_type_checked(
    tmp_path, raw, coerced
):
    """A non-string citation is `str()`-coerced exactly like
    `current_limit_source`, never rejected."""
    gds, spec = tmp_path / "basic.gds", tmp_path / "coerce.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)
    payload = json.loads(spec.read_text())
    payload["stackup"][0]["sheet_resistance_source"] = raw
    payload["vias"][0]["resistance_ohm_source"] = raw
    spec.write_text(json.dumps(payload))

    report = run_power(str(gds), str(spec))
    # Only the roles that declared a citation are coerced; met2 declared
    # none and stays `null`.
    for edge in (
        e
        for n in report["networks"]
        for i in n["islands"]
        for e in i["edges"]
        if e["layer"] == "met1" or e["kind"] == "via"
    ):
        assert edge["resistance_ohm_source"] == coerced


def test_resistance_citations_accept_the_comment_annotation_alongside(tmp_path):
    gds, spec = tmp_path / "basic.gds", tmp_path / "annotated.power.json"
    _basic_fixture(gds)
    _basic_spec(spec)
    payload = json.loads(spec.read_text())
    payload["stackup"][0]["_comment"] = "met1 sheet R from the foundry doc"
    payload["stackup"][0]["sheet_resistance_source"] = "unit-test met1 citation"
    payload["vias"][0]["_comment"] = "via1 R measured"
    payload["vias"][0]["resistance_ohm_source"] = "unit-test via1 citation"
    spec.write_text(json.dumps(payload))

    report = run_power(str(gds), str(spec))
    vpwr = next(n for n in report["networks"] if n["net"] == "VPWR")
    via_edge = next(
        e for i in vpwr["islands"] for e in i["edges"] if e["kind"] == "via"
    )
    assert via_edge["resistance_ohm_source"] == "unit-test via1 citation"
