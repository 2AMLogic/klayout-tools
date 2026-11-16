"""Per-isolated-region NMOS substrate scoping (issue #1128): gf180mcu's
`substrate_isolation=(12, 0)` (DNWELL) lets an NMOS body -- and any derived
substrate-tie tap geometry (`tap_pplus`, issue #1084) -- inside a connected
DNWELL island resolve to that island's own synthesized identity instead of
always collapsing onto the deck-wide `vsubs` global. Mirrors the
`tap_nplus`/`tap_pplus` tests in
`tests/test_extract_body_nets_gf180mcu.py`: a fixture builder plus one test
per acceptance-criterion case.

Split out of `tests/test_extract.py` (issue #5596). The fixture builder is
called only from here, so it moved with the tests; the `_nfet_by_drain`
device lookup stayed in `tests/test_extract.py`, which
`tests/test_extract_ihp_nbulay.py` also imports it from.
"""

from __future__ import annotations

import dataclasses

import klayout.db as kdb

from klayout_tools.decks import get_extraction_deck
from klayout_tools.extract import (
    _describe_devices,
    _extract_netlist,
    run_extract,
)
from test_extract import _nfet_by_drain, _write_gds


def _make_gf180mcu_isolated_nmos_layout() -> kdb.Layout:
    """Three independent NMOS-only units, far apart so none touch: two each
    fully enclosed by their own, physically separate `DNWELL` (12/0) island
    -- each with its own `Pplus`-over-`Comp` derived substrate tie (issue
    #1084) contacted and labelled with a distinct net name -- and a third
    with no `DNWELL` anywhere near it and no tie at all, exercising the
    "outside every isolation island" fallback path.

    Each unit's `Comp` active strip is 2000 x 1000 with a `Poly2` gate
    crossing it at x+800..x+1200, contacted/labelled `S<n>`/`D<n>` on either
    side -- the drain label is what a test looks a specific unit's own
    `nfet` device up by, since `report["devices"]` otherwise has three
    indistinguishable `class: "nfet"` entries."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    def nmos_unit(x0: int, index: int, *, dnwell: bool, tie_label: str | None) -> None:
        draw(22, 0, kdb.Box(x0, 0, x0 + 2000, 1000))  # Comp (nmos active)
        draw(30, 0, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))  # Poly2 (gate)
        draw(33, 0, kdb.Box(x0 + 100, 300, x0 + 300, 700))  # Contact (S)
        draw(33, 0, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))  # Contact (D)
        draw(34, 0, kdb.Box(x0, 200, x0 + 400, 800))  # Metal1 (S)
        draw(34, 0, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))  # Metal1 (D)
        label(34, 10, f"S{index}", x0 + 200, 500)
        label(34, 10, f"D{index}", x0 + 1800, 500)

        if dnwell:
            # DNWELL island fully enclosing this unit's active/gate/tie
            # geometry (and nothing else's -- islands are far apart).
            draw(12, 0, kdb.Box(x0 - 900, -900, x0 + 2900, 1900))

        if tie_label is not None:
            # Substrate tie: Pplus-covered Comp, off to the side so it never
            # touches the transistor's own Comp strip, inside this same
            # unit's DNWELL box.
            draw(22, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))  # Comp
            draw(31, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))  # Pplus
            draw(33, 0, kdb.Box(x0 - 680, -650, x0 - 520, -150))  # Contact
            draw(34, 0, kdb.Box(x0 - 750, -600, x0 - 450, -200))  # Metal1
            label(34, 10, tie_label, x0 - 600, -400)

    nmos_unit(0, 0, dnwell=True, tie_label="TIE0")
    nmos_unit(20000, 1, dnwell=True, tie_label="TIE1")
    nmos_unit(40000, 2, dnwell=False, tie_label=None)

    return layout


def test_gf180mcu_nmos_body_scopes_to_isolated_dnwell_region(tmp_path):
    """Two NMOS devices, each fully enclosed by its own, physically separate
    `DNWELL` island and each with its own labelled substrate tie inside that
    island, resolve to *two distinct* nets -- their own tie's real label,
    absorbing the per-island synthesized identity the same way a drawn tie
    absorbs `vsubs` in the non-isolated case (issue #1084) -- not to one
    shared `vsubs` global (issue #1128). A third NMOS outside every DNWELL
    island, with no tie drawn, still falls back to the single deck-wide
    `vsubs` global exactly as before this feature existed."""
    path = _write_gds(_make_gf180mcu_isolated_nmos_layout(), tmp_path / "isolated.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "isolated.spice"))

    devices = report["devices"]
    nfet0 = _nfet_by_drain(devices, "D0")
    nfet1 = _nfet_by_drain(devices, "D1")
    nfet2 = _nfet_by_drain(devices, "D2")

    assert nfet0["nets"]["b"] == "TIE0"
    assert nfet1["nets"]["b"] == "TIE1"
    assert nfet0["nets"]["b"] != nfet1["nets"]["b"]
    assert nfet2["nets"]["b"] == "vsubs"

    # The synthesized per-island names never show up as their own net
    # entries: each real tie fully absorbs its island's placeholder
    # identity, exactly like the single-global case.
    net_names = {n["name"] for n in report["nets"]}
    assert "vsubs_iso0" not in net_names
    assert "vsubs_iso1" not in net_names
    assert "TIE0" in net_names
    assert "TIE1" in net_names


def test_gf180mcu_nmos_body_isolated_region_without_tap_gets_own_synthesized_net(
    tmp_path,
):
    """An isolated `DNWELL` region with **no** substrate tie drawn inside it
    at all still resolves its NMOS body to that island's own synthesized
    `f"{substrate_net}_iso{n}"` identity -- not silently dropped, and not
    collapsed onto the single deck-wide `vsubs` global a tie-less,
    non-isolated NMOS would fall back to. A second, differently-isolated
    unit with a real tie resolves to its own tie's label as usual,
    confirming the two islands' identities stay independent regardless of
    which one has a drawn tie."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    def nmos_unit(x0, index, *, tie_label):
        draw(22, 0, kdb.Box(x0, 0, x0 + 2000, 1000))
        draw(30, 0, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))
        draw(33, 0, kdb.Box(x0 + 100, 300, x0 + 300, 700))
        draw(33, 0, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))
        draw(34, 0, kdb.Box(x0, 200, x0 + 400, 800))
        draw(34, 0, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))
        label(34, 10, f"S{index}", x0 + 200, 500)
        label(34, 10, f"D{index}", x0 + 1800, 500)
        draw(12, 0, kdb.Box(x0 - 900, -900, x0 + 2900, 1900))  # DNWELL
        if tie_label is not None:
            draw(22, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))  # Comp
            draw(31, 0, kdb.Box(x0 - 700, -700, x0 - 500, -100))  # Pplus
            draw(33, 0, kdb.Box(x0 - 680, -650, x0 - 520, -150))  # Contact
            draw(34, 0, kdb.Box(x0 - 750, -600, x0 - 450, -200))  # Metal1
            label(34, 10, tie_label, x0 - 600, -400)

    nmos_unit(0, 0, tie_label=None)  # isolated, no tie drawn
    nmos_unit(20000, 1, tie_label="TIE1")  # isolated, real tie

    path = _write_gds(layout, tmp_path / "notie.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "notie.spice"))

    devices = report["devices"]
    nfet0 = _nfet_by_drain(devices, "D0")
    nfet1 = _nfet_by_drain(devices, "D1")

    assert nfet0["nets"]["b"] == "vsubs_iso0"
    assert nfet1["nets"]["b"] == "TIE1"
    assert nfet0["nets"]["b"] != nfet1["nets"]["b"]

    net_names = {n["name"] for n in report["nets"]}
    assert "vsubs_iso0" in net_names
    assert "vsubs" not in net_names  # never falls back to the deck-wide global


def test_gf180mcu_nmos_body_isolation_scoping_is_opt_in(tmp_path):
    """The exact same DNWELL-isolated layout as
    `test_gf180mcu_nmos_body_scopes_to_isolated_dnwell_region`, but with
    `substrate_isolation` reset to `None` on the deck (the pre-#1128
    default, and every deck's behaviour before this field existed): both
    isolated NMOS devices' bodies resolve to their own tie's real label
    exactly as they would with no `DNWELL` drawn at all, and there is no
    per-island split -- confirming the DNWELL geometry itself is inert
    unless a deck opts in, not merely that gf180mcu's own curated deck
    happens to produce two distinct nets."""
    deck = get_extraction_deck("gf180mcu")
    assert deck.substrate_isolation == (12, 0)  # the curated deck opts in
    disabled_deck = dataclasses.replace(deck, substrate_isolation=None)

    layout = _make_gf180mcu_isolated_nmos_layout()
    (
        netlist,
        _warnings,
        _parasitic_nets,
        _black_box_regions,
        _dummy_devices_dropped,
        _unmodelled_poly,
        _abstracted_cells,
        _dead_metal,
        _mom_crosscheck,
        _net_label_positions,
        _device_instance_paths,
        _def_pin_promotion,
        _substrate_spreading,
        _pin_renames,
    ) = _extract_netlist(layout, layout.top_cell(), disabled_deck)
    circuit = netlist.circuit_by_name(layout.top_cell().name)
    devices, _device_counts = _describe_devices(circuit)

    nfet0 = _nfet_by_drain(devices, "D0")
    nfet1 = _nfet_by_drain(devices, "D1")
    nfet2 = _nfet_by_drain(devices, "D2")
    # With isolation scoping disabled, `DNWELL` is inert: every NMOS body
    # (isolated or not) and both real ties still funnel through the single
    # deck-wide `nfet_body`/`substrate_net` mechanism (issue #1084), exactly
    # as they would with no `DNWELL` drawn at all -- so the two *differently*
    # labelled ties collide onto one merged net (KLayout's own
    # multi-label-conflict naming, `"TIE0|TIE1"`), the same collision a
    # `DNWELL`-free layout drawing these identical two ties would produce.
    # All three devices land on that one merged net -- no per-island split.
    assert nfet0["nets"]["b"] == nfet1["nets"]["b"] == nfet2["nets"]["b"] == "TIE0|TIE1"
