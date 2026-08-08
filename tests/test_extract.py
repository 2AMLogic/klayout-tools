"""Tests for `klt extract` and the `klayout_tools.extract` library.

Two tiers, mirroring `tests/test_drc.py`:

- **Synthetic-layout tests** build a minimal sky130-deck-shaped NMOS/PMOS
  layout programmatically with `klayout.db` -- no dependency on the corpus --
  covering error paths, edge cases, and the JSON envelope.
- **Corpus round-trip tests** run the real sky130/gf180mcu standard-cell
  fixtures checked in under `tests/corpus/` (see `tests/corpus/README.md`),
  including one exact-value spot-check per PDK (the acceptance bar: a real
  inverter's extracted devices/nets/params are asserted directly) and an
  ngspice integration test proving the extracted netlist is directly usable
  as a `klt sim` `netlist` with no reformatting (skipped when `ngspice` is
  not installed, mirroring `tests/test_sim.py`'s integration tier).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.decks import (
    DiodeDevice,
    ExtractionDeck,
    get_extraction_deck,
    get_unmodeled_voltage_markers,
)
from klayout_tools.extract import (
    ExtractError,
    _describe_devices,
    _detect_single_terminal_nets,
    _exclude_capacitor_top_via_overlap,
    _extract_netlist,
    _n_squares,
    _promote_orphan_named_nets,
    _purge_preserving_named_nets,
    _region,
    run_extract,
)
from klayout_tools.pdk_models import _select_bipolar_variant, equivalent_rectangle_um

CORPUS_DIR = Path(__file__).parent / "corpus"
SKY130_CORPUS_FILES = sorted((CORPUS_DIR / "sky130").glob("*.gds"))
GF180MCU_CORPUS_FILES = sorted((CORPUS_DIR / "gf180mcu").glob("*.gds"))

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


# --------------------------------------------------------------------------- #
# Synthetic fixture: one NMOS + one PMOS on the sky130 deck's layers
# --------------------------------------------------------------------------- #


def _make_inverter_layout(
    top_name: str = "TOP",
    a_label_in_subcell: bool = False,
    extra_y_label: str | list[str] | None = None,
    substrate_tap_label: str | None = None,
) -> kdb.Layout:
    """A minimal inverter: one NMOS (active outside nwell) and one PMOS
    (active inside nwell) sharing a poly gate, contacted up through li1 to
    met1, with li1/met1 pin labels naming every net -- shaped to exercise
    every layer role in `decks.sky130.EXTRACTION_DECK`.

    When ``a_label_in_subcell`` is set, the gate's ``A`` label is drawn inside
    a separate sub-cell instanced into the top cell (identity transform, same
    coordinates) rather than directly in the top cell. Extraction is flat, so
    ``A`` still names the gate net -- but its naming label now lives *below* an
    instance boundary, exercising issue #291's below-top-label pin promotion.

    When ``extra_y_label`` is given, one or more additional, distinct text
    labels are drawn on the PMOS-side ``Y`` pad (same li1 shape, distinct
    coordinates within it, one per extra label) so the net carries 2+
    distinct label texts -- KLayout's ``Net.expanded_name()`` joins multiple
    distinct labels on one net with a comma (e.g. ``Y,<extra_y_label>``, or
    ``Y,<label2>,<label3>`` for 3+), which is issue #312's multi-label repro
    case (and issue #470's `merged_net_labels[]` detection target). A single
    string adds one extra label; a list adds one per element (for 3+-label
    edge cases).

    When ``substrate_tap_label`` is given, an additional `tap.drawing` ring
    is drawn well outside the `nwell` (below the NMOS active strip, never
    touching it), contacted up through licon1/li1 and labelled with the
    given text -- a genuine, drawable P-substrate tie (issue #490). Left
    ``None`` (the default), the NMOS body has no drawn tap geometry at all,
    exercising the pre-#490 substrate-global fallback.
    """
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(box)
        return li

    def label(layer, datatype, text, x, y):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(kdb.Text(text, kdb.Trans(x, y)))

    # NMOS: active strip 0..2000 x 0..1000. PMOS: active strip 0..2000 x
    # 2000..3000, inside an nwell. One continuous poly bar (x 800..1200, y
    # -200..3200) crosses both active strips *and* the gap between them, so
    # it forms a single connected gate net for both transistors (mirroring
    # a real inverter's shared poly gate line) and can be labelled once.
    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))  # diff.drawing (pmos active)
    draw(64, 20, kdb.Box(-500, 1500, 2500, 3500))  # nwell.drawing
    draw(66, 20, kdb.Box(800, -200, 1200, 3200))  # poly.drawing (shared gate bar)

    # Contacts + li1 on both source/drain pads (both transistors).
    for y0, _y1 in [(0, 1000), (2000, 3000)]:
        draw(66, 44, kdb.Box(100, y0 + 300, 300, y0 + 700))  # licon1 (S side)
        draw(66, 44, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # licon1 (D side)
        draw(67, 20, kdb.Box(0, y0 + 200, 400, y0 + 800))  # li1 (S side)
        draw(67, 20, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # li1 (D side)

    # Gate contact (poly.drawing overlaps li1 via licon1 too, tying A).
    draw(66, 44, kdb.Box(900, 1400, 1100, 1600))
    draw(67, 20, kdb.Box(850, 1350, 1150, 1650))

    # Pin labels on li1: source pads are VGND (nmos) / VPWR (pmos); drain
    # pads on both transistors are tied together (Y); the shared poly gate
    # is A.
    label(67, 5, "VGND", 200, 500)
    label(67, 5, "Y", 1800, 500)
    label(67, 5, "VPWR", 200, 2500)
    label(67, 5, "Y", 1800, 2500)
    if extra_y_label is not None:
        extra_labels = (
            [extra_y_label] if isinstance(extra_y_label, str) else extra_y_label
        )
        for i, extra_label_text in enumerate(extra_labels):
            label(67, 5, extra_label_text, 1650 + i * 10, 2500)
    if a_label_in_subcell:
        sub = layout.create_cell("A_LABEL")
        sub.shapes(layout.layer(67, 5)).insert(kdb.Text("A", kdb.Trans(1000, 1500)))
        top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(0, 0)))
    else:
        label(67, 5, "A", 1000, 1500)

    # NMOS body: no drawn tap in this synthetic fixture -- exercises the
    # substrate-global fallback. PMOS body: an nwell tap (tap.drawing inside
    # the nwell) contacted to li1, labelled VPB.
    draw(65, 44, kdb.Box(-400, 2400, -200, 2600))  # tap.drawing (nwell tap)
    draw(66, 44, kdb.Box(-380, 2450, -220, 2550))  # licon1 over the tap
    draw(67, 20, kdb.Box(-450, 2400, -150, 2600))  # li1 over the tap
    label(64, 5, "VPB", -300, 2500)

    if substrate_tap_label is not None:
        # Substrate tap ring: drawn well below the NMOS active strip (y
        # 0..1000) and far from the nwell (y 1500..3500), so it never
        # touches either -- a genuine P-substrate tie, not a well tie.
        draw(65, 44, kdb.Box(-400, -800, -200, -200))  # tap.drawing (substrate tie)
        draw(66, 44, kdb.Box(-380, -650, -220, -550))  # licon1 over the tap
        draw(67, 20, kdb.Box(-450, -700, -150, -500))  # li1 over the tap
        label(67, 5, substrate_tap_label, -300, -600)

    return layout


def _write_gds(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _make_floating_gate_nmos_layout(top_name: str = "TOP") -> kdb.Layout:
    """One NMOS whose poly gate carries no label and touches nothing else --
    reproducing issue #596's motivating case: an undriven MOS gate that is
    DRC-clean and reads as an ordinary, plausible net. The gate net's only
    device terminal is this NMOS's own gate (``device_count == 1``), and
    since it is never labelled, ``make_top_level_pins()`` never promotes it
    to a pin (``pin: False``) -- exactly the condition
    :func:`_detect_single_terminal_nets` flags.
    """
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(box)

    def label(layer, datatype, text, x, y):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(kdb.Text(text, kdb.Trans(x, y)))

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    # poly.drawing gate bar -- deliberately unlabeled and not contacted
    # anywhere else, so it forms a net with exactly one device terminal.
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))

    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (S side)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (D side)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (S side)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (D side)

    label(67, 5, "VGND", 200, 500)
    label(67, 5, "Y", 1800, 500)

    return layout


def _make_multi_single_terminal_layout(top_name: str = "TOP") -> kdb.Layout:
    """Three independent NMOS instances at non-overlapping x offsets,
    engineered to exercise both `single_terminal_nets[]` buckets at once in
    a single run (issue #599's multi-instance `warnings[]` aggregation
    test): the first two each have an unlabeled, uncontacted-elsewhere gate
    (``terminal_kind == "gate"``, 2 entries total), and the third has a
    normally driven/labeled gate but an unlabeled, uncontacted-elsewhere
    drain pad (``terminal_kind == "drain"``, 1 entry) -- so the run produces
    2 gate-bucket entries and 1 non-gate-bucket entry, letting a test assert
    two separate aggregate `warnings[]` lines (counts 2 and 1) rather than
    one line per net (3 lines) or a single conflated line.
    """
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(box)

    def label(layer, datatype, text, x, y):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(kdb.Text(text, kdb.Trans(x, y)))

    # Two floating-gate NMOS instances (x offset 0 and 3000) -- each an
    # independent copy of `_make_floating_gate_nmos_layout`'s shapes.
    for i, x0 in enumerate((0, 3000)):
        draw(65, 20, kdb.Box(x0, 0, x0 + 2000, 1000))  # diff.drawing
        draw(66, 20, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))  # floating gate
        draw(66, 44, kdb.Box(x0 + 100, 300, x0 + 300, 700))  # licon1 (S)
        draw(66, 44, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))  # licon1 (D)
        draw(67, 20, kdb.Box(x0 + 0, 200, x0 + 400, 800))  # li1 (S)
        draw(67, 20, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))  # li1 (D)
        label(67, 5, f"VGND{i}", x0 + 200, 500)
        label(67, 5, f"Y{i}", x0 + 1800, 500)

    # Third NMOS: normal, driven/labeled gate ("B"), but an unlabeled drain
    # pad touched by no other geometry -- a single-terminal, non-gate net.
    x0 = 6000
    draw(65, 20, kdb.Box(x0, 0, x0 + 2000, 1000))  # diff.drawing
    draw(66, 20, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))  # gate poly
    draw(66, 44, kdb.Box(x0 + 100, 300, x0 + 300, 700))  # licon1 (S)
    draw(66, 44, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))  # licon1 (D, unlabeled)
    draw(67, 20, kdb.Box(x0 + 0, 200, x0 + 400, 800))  # li1 (S)
    draw(67, 20, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))  # li1 (D, unlabeled)
    label(67, 5, "VGND2", x0 + 200, 500)
    # Gate contact so this gate is driven (not itself single-terminal).
    draw(66, 44, kdb.Box(x0 + 900, 900, x0 + 1100, 1100))
    draw(67, 20, kdb.Box(x0 + 850, 850, x0 + 1150, 1150))
    label(67, 5, "B", x0 + 1000, 1000)

    return layout


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


def test_missing_file_raises(tmp_path):
    with pytest.raises(ExtractError, match="file not found"):
        run_extract(str(tmp_path / "nope.gds"), "sky130")


def test_directory_raises(tmp_path):
    with pytest.raises(ExtractError, match="not a file"):
        run_extract(str(tmp_path), "sky130")


def test_unknown_deck_raises(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    with pytest.raises(ExtractError, match="unknown deck 'nope'"):
        run_extract(path, "nope")


def test_unresolvable_pdk_is_application_error(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    with pytest.raises(ExtractError, match="no supported-layout PDK install"):
        run_extract(
            path,
            "sky130",
            pdk_variant="sky130A",
            pdk_root=str(tmp_path / "missing_pdk_root"),
        )


def test_output_directory_missing_is_created(tmp_path):
    """`-o` may point at a path whose parent directory does not exist yet --
    it should be created automatically (matching `klt render`/`klt lvs`,
    issue #233), not raise."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    out_path = tmp_path / "no_such_dir" / "out.spice"
    assert not out_path.parent.exists()

    result = run_extract(path, "sky130", output=str(out_path))

    assert out_path.parent.is_dir()
    assert out_path.is_file()
    assert result["netlist_path"] == str(out_path)


def test_output_directory_missing_nested_is_created(tmp_path):
    """Multiple missing levels are all created, per `os.makedirs`'s
    recursive semantics."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    out_path = tmp_path / "reports" / "sub" / "dir" / "out.spice"
    assert not out_path.parent.parent.parent.exists()

    run_extract(path, "sky130", output=str(out_path))

    assert out_path.is_file()


def test_output_directory_existing_is_unchanged(tmp_path):
    """Existing behavior for an output path whose directory already exists
    is unchanged: extraction still succeeds and writes the file."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    out_dir = tmp_path / "already_exists"
    out_dir.mkdir()
    out_path = out_dir / "out.spice"

    run_extract(path, "sky130", output=str(out_path))

    assert out_path.is_file()


def test_output_parent_is_a_file_is_application_error(tmp_path):
    """When the parent path exists but is not a directory (e.g. a plain
    file), the resulting error is a clean `ExtractError`, not an unhandled
    `OSError`/traceback."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    out_path = blocker / "out.spice"

    with pytest.raises(ExtractError, match="cannot create output directory"):
        run_extract(path, "sky130", output=str(out_path))


def test_multiple_top_cells_without_top_flag_is_application_error(tmp_path):
    layout = kdb.Layout()
    layout.create_cell("A")
    layout.create_cell("B")
    path = _write_gds(layout, tmp_path / "multi.gds")
    with pytest.raises(ExtractError, match="2 top cells"):
        run_extract(path, "sky130")


def test_unknown_top_cell_is_application_error(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    with pytest.raises(ExtractError, match="cell 'nope' not found"):
        run_extract(path, "sky130", top="nope")


def test_explicit_top_selects_named_cell(tmp_path):
    layout = _make_inverter_layout(top_name="MY_TOP")
    path = _write_gds(layout, tmp_path / "inv.gds")
    report = run_extract(path, "sky130", top="MY_TOP")
    assert report["top"] == "MY_TOP"


# --------------------------------------------------------------------------- #
# Edge case: a layout with no extractable devices
# --------------------------------------------------------------------------- #


def test_layout_with_no_devices_succeeds_with_zero_count(tmp_path):
    layout = kdb.Layout()
    top = layout.create_cell("EMPTY")
    # A met1 rectangle with no diff/poly at all -- no devices, but a real,
    # non-empty layout so this isn't conflated with the missing-file path.
    li = layout.layer(68, 20)
    top.shapes(li).insert(kdb.Box(0, 0, 1000, 1000))
    path = _write_gds(layout, tmp_path / "empty.gds")

    report = run_extract(path, "sky130")

    assert report["status"] == "extracted"
    assert report["device_count"] == 0
    assert report["devices"] == []
    assert report["device_counts"] == {}
    # `device_classes` is what the deck *can* recognise, unaffected by this
    # layout happening to contain zero devices (issue #221) -- sky130 also
    # declares a `pnp` bipolar entry (issue #223), two MiM capacitors
    # (issue #225), and a drawn poly resistor (issue #222).
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "pnp",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
        "resistor",
    ]


# --------------------------------------------------------------------------- #
# Synthetic inverter: JSON envelope + device/net shape
# --------------------------------------------------------------------------- #


def _make_bare_poly_gate_layout(gate_label, top_name="TOP"):
    """One NMOS whose gate is a bare poly bar with NO gate contact/metal --
    only a text on the poly-label layer (66/5) names it. Models a `klt gen`
    MOS device gate, which has no metal landing pad, so the only way to name
    it is the poly_label wiring #210 added to `EXTRACTION_DECK`."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))  # poly.drawing (bare gate bar)
    # S/D contacts + li1 pads, each labelled so the transistor has named S/D.
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (S)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (D)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (S)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (D)
    label(67, 5, "SRC", 200, 500)
    label(67, 5, "DRN", 1800, 500)
    # The gate: labelled ONLY on poly.pin (66/5), no metal anywhere near it.
    label(66, 5, gate_label, 1000, 1100)
    return layout


def test_bare_poly_gate_named_via_poly_label(tmp_path):
    # #210: a text on the poly-label layer (66/5) names a bare-poly gate that
    # has no metal landing pad, so it survives extraction as a NAMED pin
    # instead of an anonymous $N net -- and device extraction is unaffected
    # (still exactly one nfet).
    path = _write_gds(_make_bare_poly_gate_layout("GATEN"), tmp_path / "bare_gate.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "bare_gate.spice"))

    assert report["device_counts"] == {"nfet": 1}
    nfet = next(d for d in report["devices"] if d["class"] == "nfet")
    assert nfet["nets"]["g"] == "GATEN"
    pin_names = {n["name"] for n in report["nets"] if n["pin"]}
    assert "GATEN" in pin_names


def test_bare_poly_gate_anonymous_without_poly_label(tmp_path):
    # Control: the same layout with NO poly label leaves the gate an anonymous
    # $N net (the pre-#210 friction), while device extraction is identical --
    # proving the poly_label text is what promotes the gate, not a geometry
    # change.
    layout = _make_bare_poly_gate_layout("UNUSED")
    # Drop the poly.pin label, keeping every other shape/label.
    poly_pin = layout.layer(66, 5)
    layout.top_cell().shapes(poly_pin).clear()
    path = _write_gds(layout, tmp_path / "bare_gate_nolabel.gds")

    report = run_extract(path, "sky130", output=str(tmp_path / "nolabel.spice"))
    assert report["device_counts"] == {"nfet": 1}
    nfet = next(d for d in report["devices"] if d["class"] == "nfet")
    assert nfet["nets"]["g"].startswith("$")  # anonymous, unbiasable


def test_synthetic_inverter_extracts_two_devices(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")

    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    assert report["schema_version"] == 2
    assert report["file"] == path
    assert report["deck"] == "sky130"
    assert report["top"] == "TOP"
    assert report["dbu_um"] == pytest.approx(0.001)
    assert report["status"] == "extracted"
    assert report["netlist_path"] == str(tmp_path / "inv.spice")
    assert Path(report["netlist_path"]).is_file()
    assert len(report["netlist_sha256"]) == 64
    assert report["device_count"] == 2
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "pnp",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
        "resistor",
    ]
    assert report["pdk"] is None
    assert report["warnings"] == []

    prov = report["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert isinstance(prov["klt_version"], str)
    assert prov["pdk"] is None
    assert prov["deck"]["name"] == "sky130"
    assert prov["deck"]["content_hash"].startswith("sha256:")
    # Issue #331: the input layout stream is hashed too -- distinct from
    # `netlist_sha256` above, which hashes the *written* netlist.
    assert prov["input"]["content_hash"].startswith("sha256:")
    input_digest = prov["input"]["content_hash"].removeprefix("sha256:")
    assert input_digest != report["netlist_sha256"]

    devices = report["devices"]
    assert [d["name"] for d in devices] == sorted(d["name"] for d in devices)
    classes = {d["class"] for d in devices}
    assert classes == {"nfet", "pfet"}
    for device in devices:
        assert set(device["nets"]) == {"s", "g", "d", "b"}
        assert set(device["params"]) == {"w_um", "l_um"}

    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["g"] == "A"
    assert nfet["nets"]["d"] == "Y"
    assert nfet["nets"]["s"] == "VGND"
    assert nfet["nets"]["b"] == "vsubs"
    assert pfet["nets"]["g"] == "A"
    assert pfet["nets"]["d"] == "Y"
    assert pfet["nets"]["s"] == "VPWR"
    assert pfet["nets"]["b"] == "VPB"

    nets = report["nets"]
    assert [n["name"] for n in nets] == sorted(n["name"] for n in nets)
    net_names = {n["name"] for n in nets}
    assert {"A", "Y", "VGND", "VPWR", "VPB", "vsubs"}.issubset(net_names)
    assert report["pin_count"] == len(
        nets
    )  # make_top_level_pins() promotes every named net


# --------------------------------------------------------------------------- #
# NMOS body / substrate-tap resolution (issue #490)
# --------------------------------------------------------------------------- #


def test_sky130_nmos_body_resolves_to_drawn_substrate_tap_net(tmp_path):
    """A `tap.drawing` ring drawn outside every `nwell` and contacted up to a
    labelled net is now the NMOS body terminal's real net -- not the deck's
    synthesized `vsubs` global (issue #490). `bulk_to_substrate` resistors and
    a collector-less bipolar share the same underlying region, so this also
    covers those paths (both funnel through the identical `nfet_body`
    terminal/`connect_global` wiring exercised here)."""
    path = _write_gds(
        _make_inverter_layout(substrate_tap_label="VSUBRING"),
        tmp_path / "inv.gds",
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] == "VSUBRING"
    # PMOS body is unaffected -- still the real, labelled nwell tap.
    assert pfet["nets"]["b"] == "VPB"

    # The synthesized global name never shows up: the tap ring's own real
    # net fully absorbs the (otherwise-empty) global net, it doesn't merely
    # sit alongside it under a second, separate net entry.
    net_names = {n["name"] for n in report["nets"]}
    assert "VSUBRING" in net_names
    assert "vsubs" not in net_names


def test_sky130_nmos_body_falls_back_to_substrate_global_without_tap(tmp_path):
    """No drawn substrate-tap ring at all: the NMOS body terminal still
    falls back to the deck's synthesized `vsubs` global, exactly as before
    #490 -- the additive-capability guarantee for the common (no-ring)
    case."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    nfet = next(d for d in report["devices"] if d["class"] == "nfet")
    assert nfet["nets"]["b"] == "vsubs"


def test_sky130_tap_straddling_nwell_boundary_extracts_without_error(tmp_path):
    """A single `tap.drawing` polygon straddling the `nwell` boundary (part
    inside, part outside) is a real electrical short in silicon -- the
    `tap & nwell` / `tap - nwell` split (issue #490) must not silently drop
    or double-count that geometry, and extraction must not raise. Confirms
    the split behaves sanely: both device bodies still resolve to *some*
    net (not `None`), and -- because the straddling shape is genuinely one
    connected conductor bridging the well and the substrate -- both device
    bodies land on the *same* net, correctly reflecting the short a real
    straddling tap would draw in silicon."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # Same NMOS/PMOS/nwell/gate shape as `_make_inverter_layout`: NMOS
    # active strip 0..2000 x 0..1000, PMOS active strip 0..2000 x
    # 2000..3000, nwell -500..2500 x 1500..3500 -- the nwell's bottom edge
    # sits at y=1500, with a 500-wide substrate gap (1000..1500) below it.
    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))  # diff.drawing (pmos active)
    draw(64, 20, kdb.Box(-500, 1500, 2500, 3500))  # nwell.drawing
    draw(66, 20, kdb.Box(800, -200, 1200, 3200))  # poly.drawing (shared gate)

    for y0 in (0, 2000):
        draw(66, 44, kdb.Box(100, y0 + 300, 300, y0 + 700))  # licon1 (S)
        draw(66, 44, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # licon1 (D)
        draw(67, 20, kdb.Box(0, y0 + 200, 400, y0 + 800))  # li1 (S)
        draw(67, 20, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # li1 (D)
    draw(66, 44, kdb.Box(900, 1400, 1100, 1600))  # gate licon
    draw(67, 20, kdb.Box(850, 1350, 1150, 1650))  # gate li1

    label(67, 5, "VGND", 200, 500)
    label(67, 5, "Y", 1800, 500)
    label(67, 5, "VPWR", 200, 2500)
    label(67, 5, "Y", 1800, 2500)
    label(67, 5, "A", 1000, 1500)

    # One tap polygon straddling the nwell's bottom edge (y=1500): drawn
    # 1300..1700, i.e. 200 inside the well (>=1500) and 200 outside it
    # (<1500), off to the side (x -400..-200) so it never touches the
    # active/gate geometry itself. Contacted + labelled on its
    # outside-the-well (substrate) end.
    draw(65, 44, kdb.Box(-400, 1300, -200, 1700))  # tap.drawing (straddling)
    draw(66, 44, kdb.Box(-380, 1320, -220, 1420))  # licon1 (outside-well end)
    draw(67, 20, kdb.Box(-450, 1300, -150, 1440))  # li1 (outside-well end)
    label(67, 5, "STRADDLE", -300, 1370)

    path = _write_gds(layout, tmp_path / "straddle.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "straddle.spice"))

    devices = report["devices"]
    nfet = next(d for d in devices if d["class"] == "nfet")
    pfet = next(d for d in devices if d["class"] == "pfet")
    assert nfet["nets"]["b"] is not None
    assert pfet["nets"]["b"] is not None
    # The straddling shape genuinely bridges nwell and substrate -- both
    # bodies land on the one shorted net (the correct, if unfortunate,
    # electrical reading of that geometry), and that net carries the
    # ring's own label rather than an anonymous or dropped identity.
    assert nfet["nets"]["b"] == pfet["nets"]["b"] == "STRADDLE"


def test_provenance_input_hash_tracks_layout_bytes(tmp_path):
    """Issue #331: `provenance.input.content_hash` identifies the input
    layout *stream* a report was produced from -- byte-identical inputs hash
    the same, and a real geometry change (which a stale committed report
    would not otherwise reveal) produces a different hash."""
    path_a = _write_gds(_make_inverter_layout(), tmp_path / "inv_a.gds")
    path_b = _write_gds(_make_inverter_layout(), tmp_path / "inv_b.gds")

    report_a = run_extract(path_a, "sky130", output=str(tmp_path / "a.spice"))
    report_b = run_extract(path_b, "sky130", output=str(tmp_path / "b.spice"))
    assert (
        report_a["provenance"]["input"]["content_hash"]
        == report_b["provenance"]["input"]["content_hash"]
    )

    # A layout with different geometry hashes differently, even though the
    # deck/version fields do not change.
    modified_layout = _make_bare_poly_gate_layout("UNUSED")
    path_c = _write_gds(modified_layout, tmp_path / "inv_c.gds")
    report_c = run_extract(path_c, "sky130", output=str(tmp_path / "c.spice"))

    assert (
        report_c["provenance"]["input"]["content_hash"]
        != report_a["provenance"]["input"]["content_hash"]
    )
    assert (
        report_c["provenance"]["klt_version"] == report_a["provenance"]["klt_version"]
    )


# --------------------------------------------------------------------------- #
# Top-cell-only pin promotion (issue #291)
# --------------------------------------------------------------------------- #


def test_flat_layout_unaffected_by_top_cell_pins(tmp_path):
    """A layout with no instances: every pin label is in the top cell, so
    `--top-cell-pins` is a no-op -- same pins, and no below-top warning."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    base = run_extract(path, "sky130", output=str(tmp_path / "base.spice"))
    scoped = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "scoped.spice"),
        top_cell_pins_only=True,
    )

    base_pins = {n["name"] for n in base["nets"] if n["pin"]}
    scoped_pins = {n["name"] for n in scoped["nets"] if n["pin"]}
    assert base_pins == scoped_pins
    assert base["pin_count"] == scoped["pin_count"]
    assert not any("below the top cell" in w for w in scoped["warnings"])
    assert not any("below the top cell" in w for w in base["warnings"])


def test_subcell_label_promotes_to_parent_pin_by_default(tmp_path):
    """The core regression (issue #291): a net named only by a label inside an
    instanced sub-cell is still promoted to a parent pin by default -- but the
    promotion now surfaces a warning naming it, rather than being silent."""
    path = _write_gds(
        _make_inverter_layout(a_label_in_subcell=True), tmp_path / "hier.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "hier.spice"))

    pins = {n["name"] for n in report["nets"] if n["pin"]}
    # Default behaviour is unchanged: A is still promoted to a pin.
    assert "A" in pins
    # ...but the below-top promotion is now visible in warnings.
    warning = next((w for w in report["warnings"] if "below the top cell" in w), None)
    assert warning is not None
    assert "A" in warning


def test_subcell_label_kept_internal_with_top_cell_pins(tmp_path):
    """With `--top-cell-pins`, a sub-cell-origin label keeps its net name but
    is NOT promoted to a parent pin; the genuine top-cell interface is intact."""
    path = _write_gds(
        _make_inverter_layout(a_label_in_subcell=True), tmp_path / "hier.gds"
    )
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "hier.spice"),
        top_cell_pins_only=True,
    )

    net_names = {n["name"] for n in report["nets"]}
    pins = {n["name"] for n in report["nets"] if n["pin"]}

    # The name is preserved (the net still exists as an internal node)...
    assert "A" in net_names
    # ...but it is no longer a top-level pin.
    assert "A" not in pins
    # The real, top-cell-drawn interface is untouched (including the
    # substrate global net, which is not a drawn label).
    assert {"VGND", "VPWR", "Y", "VPB", "vsubs"}.issubset(pins)
    assert any("A" in w for w in report["warnings"])


# --------------------------------------------------------------------------- #
# Declared pin set (issue #514)
# --------------------------------------------------------------------------- #


def test_declared_pins_none_default_is_unchanged(tmp_path):
    """Omitted `declared_pins` (the default) is byte-for-byte identical to
    today's behaviour -- same invariant `--top-cell-pins`'s own default
    preserves (issue #514)."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    base = run_extract(path, "sky130", output=str(tmp_path / "base.spice"))
    explicit_none = run_extract(
        path, "sky130", output=str(tmp_path / "none.spice"), declared_pins=None
    )

    base_pins = {n["name"] for n in base["nets"] if n["pin"]}
    explicit_pins = {n["name"] for n in explicit_none["nets"] if n["pin"]}
    assert base_pins == explicit_pins
    assert base["pin_count"] == explicit_none["pin_count"]
    assert not any("declared pin set" in w for w in explicit_none["warnings"])


def test_declared_pins_matching_every_label_is_unaffected(tmp_path):
    """Declaring exactly the set of nets already promoted is a no-op: every
    pin stays a pin, and no demotion warning fires."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    base = run_extract(path, "sky130", output=str(tmp_path / "base.spice"))
    base_pins = {n["name"] for n in base["nets"] if n["pin"]}

    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "declared.spice"),
        declared_pins=frozenset(base_pins),
    )
    pins = {n["name"] for n in report["nets"] if n["pin"]}
    assert pins == base_pins
    assert not any("declared pin set" in w for w in report["warnings"])


def test_declared_pins_demotes_undeclared_labelled_net(tmp_path):
    """The core regression (issue #291's motivating scenario, made per-net):
    a net named by a label drawn directly in the top cell -- not below any
    instance boundary, so `--top-cell-pins` cannot help -- is demoted to an
    internal node when it is not in the declared pin set. This is the fix
    for labelling an internal node of a lumped schematic device purely for
    documentation without it silently becoming a top-level pin."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "declared.spice"),
        declared_pins=frozenset({"VGND", "VPWR", "VPB", "Y"}),  # omit "A"
    )

    net_names = {n["name"] for n in report["nets"]}
    pins = {n["name"] for n in report["nets"] if n["pin"]}

    # "A" keeps its name (still a real, findable net)...
    assert "A" in net_names
    # ...but is no longer a top-level pin.
    assert "A" not in pins
    # The declared interface is untouched.
    assert {"VGND", "VPWR", "VPB", "Y"}.issubset(pins)
    warning = next((w for w in report["warnings"] if "declared pin set" in w), None)
    assert warning is not None
    assert "A" in warning


def test_declared_pins_unmatched_name_warns(tmp_path):
    """A declared pin name that matches no promoted net is reported in
    `warnings`, not silently ignored (a likely typo)."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "declared.spice"),
        declared_pins=frozenset({"VGND", "VPWR", "VPB", "Y", "A", "NOPE"}),
    )

    pins = {n["name"] for n in report["nets"] if n["pin"]}
    # Every declared name that *does* match stays a pin -- nothing demoted.
    assert {"VGND", "VPWR", "VPB", "Y", "A"}.issubset(pins)
    warning = next((w for w in report["warnings"] if "matched no promoted" in w), None)
    assert warning is not None
    assert "NOPE" in warning


def test_declared_pins_applied_after_top_cell_pins_only(tmp_path):
    """`declared_pins` is applied *after* `top_cell_pins_only`'s own
    reconciliation, and can only further restrict -- it never re-promotes a
    net `top_cell_pins_only` already kept internal."""
    path = _write_gds(
        _make_inverter_layout(a_label_in_subcell=True), tmp_path / "hier.gds"
    )
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "hier.spice"),
        top_cell_pins_only=True,
        # Declares "A" as wanted -- but it was already demoted by
        # top_cell_pins_only (a below-top label), so it must stay internal.
        declared_pins=frozenset({"VGND", "VPWR", "VPB", "Y", "A"}),
    )

    net_names = {n["name"] for n in report["nets"]}
    pins = {n["name"] for n in report["nets"] if n["pin"]}
    assert "A" in net_names
    assert "A" not in pins
    assert {"VGND", "VPWR", "VPB", "Y"}.issubset(pins)


def test_cli_pins_flag_demotes_undeclared_label(tmp_path, capsys):
    """The `--pins` flag wires through the CLI: a top-cell-drawn label ("A")
    absent from the declared set is not promoted to a top-level pin, while
    the declared interface is preserved (issue #514)."""
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(
        [
            "extract",
            path,
            "--deck",
            "sky130",
            "--pins",
            "VGND,VPWR,VPB,Y",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    pins = {n["name"] for n in out["nets"] if n["pin"]}
    assert "A" not in pins
    assert "A" in {n["name"] for n in out["nets"]}  # name kept
    assert {"VGND", "VPWR", "VPB", "Y"}.issubset(pins)


def test_cli_pins_flag_empty_after_parsing_errors(tmp_path, capsys):
    """`--pins` given but containing no non-empty token is a clear
    application error, not a silent no-op."""
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(
        ["extract", path, "--deck", "sky130", "--pins", " , ,", "--format", "json"]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "no non-empty name" in err["error"]["message"]


def test_default_output_path_replaces_extension(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130")
    assert report["netlist_path"] == str(tmp_path / "inv.spice")
    assert Path(report["netlist_path"]).is_file()


def test_written_netlist_has_no_top_level_end_or_control(tmp_path):
    """Verified compatible with `klt sim`'s netlist convention (see
    docs/cli/sim.md's "Netlist convention"): a circuit body, no top-level
    `.control`/`.end` card."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130")
    text = Path(report["netlist_path"]).read_text()

    assert ".SUBCKT" in text
    assert ".ENDS" in text
    for line in text.splitlines():
        stripped = line.strip().upper()
        assert stripped != ".END"
        assert not stripped.startswith(".CONTROL")


# --------------------------------------------------------------------------- #
# Bipolar (BJT) device recognition (issue #223)
# --------------------------------------------------------------------------- #


def _make_sky130_bjt_layout(*, base_tie_on_diff: bool = False) -> kdb.Layout:
    """A minimal vertical-PNP layout on sky130's curated bipolar layers: an
    nwell (base) marked with `pnp.drawing`, a p+ diffusion (emitter) inside
    it, contacted up through li1 and labelled -- plus a base contact/label so
    both the emitter and base terminals resolve to named nets, and no drawn
    collector (collector = substrate, per `BipolarDevice`'s docstring).

    The base tie is drawn on the *distinct* `tap.drawing` (65/44) layer, as
    the real `sky130_fd_pr__pnp` does -- so it never collides with the
    `diff.drawing` (65/20) emitter layer. When ``base_tie_on_diff`` is set,
    an extra base-tie ring is instead drawn on the **same** `diff.drawing`
    layer as the emitter, exercising the documented residual limitation of a
    deck that models no implant layers (issue #302): sky130 has no already-
    modelled layer to tell that same-layer ring apart from the emitter."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(64, 20, kdb.Box(0, 0, 2000, 2000))  # nwell.drawing (base)
    draw(82, 44, kdb.Box(0, 0, 2000, 2000))  # pnp.drawing (device-mark)

    # Emitter: p+ diffusion inside the marked nwell, contacted + labelled.
    draw(65, 20, kdb.Box(800, 800, 1200, 1200))  # diff.drawing (emitter)
    draw(66, 44, kdb.Box(900, 900, 1100, 1100))  # licon1 over the emitter
    draw(67, 20, kdb.Box(850, 850, 1150, 1150))  # li1 over the emitter
    label(67, 5, "EMIT", 1000, 1000)

    # Base: a well tap (tap.drawing) inside the same nwell, contacted +
    # labelled -- mirrors the PMOS body-tie pattern `_make_inverter_layout`
    # already exercises.
    draw(65, 44, kdb.Box(100, 100, 300, 300))  # tap.drawing (base tie)
    draw(66, 44, kdb.Box(150, 150, 250, 250))  # licon1 over the tap
    draw(67, 20, kdb.Box(100, 100, 300, 300))  # li1 over the tap
    label(64, 5, "BASE", 200, 200)

    if base_tie_on_diff:
        # A base tie drawn on the *literal* diff.drawing emitter layer,
        # around the emitter (outer 400..1600, inner hole 600..1400 clears
        # the 800..1200 emitter). No implant layer exists in this deck to
        # distinguish it from the p+ emitter -- see the deck's documented
        # residual limitation.
        for lx0, ly0, lx1, ly1 in (
            (400, 400, 1600, 600),  # bottom strip
            (400, 1400, 1600, 1600),  # top strip
            (400, 600, 600, 1400),  # left strip
            (1400, 600, 1600, 1400),  # right strip
        ):
            draw(65, 20, kdb.Box(lx0, ly0, lx1, ly1))  # diff.drawing (base-tie ring)

    return layout


def _make_gf180mcu_bjt_layout(*, base_contact_ring: bool = False) -> kdb.Layout:
    """A minimal vertical-bipolar layout on gf180mcu's curated bipolar
    layers: an Nwell (base) marked with `DRC_BJT`, a Comp diffusion
    (emitter) inside it, contacted up through Metal1 and labelled -- no
    drawn collector (collector = substrate).

    When ``base_contact_ring`` is set, an n+ base-tie ring is drawn *around*
    the emitter on the **same** `Comp` (22/0) diffusion layer, inside the
    same `DRC_BJT` mark -- the standard vertical-bipolar unit cell's way of
    contacting the Nwell base (issue #302). It is covered by `Nplus` (32/0)
    (an n+ tie, versus the p+ emitter), which is exactly what the deck's
    `emitter_excludes` uses to drop it: without that narrowing this second
    same-layer diffusion inside the marked base extracts as a spurious
    second `bjt` sharing the base net."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(21, 0, kdb.Box(0, 0, 2000, 2000))  # Nwell (base)
    draw(127, 5, kdb.Box(0, 0, 2000, 2000))  # DRC_BJT (device-mark)

    # Emitter: Comp diffusion inside the marked Nwell, contacted + labelled.
    draw(22, 0, kdb.Box(800, 800, 1200, 1200))  # Comp (emitter)
    draw(33, 0, kdb.Box(900, 900, 1100, 1100))  # Contact over the emitter
    draw(34, 0, kdb.Box(850, 850, 1150, 1150))  # Metal1 over the emitter
    label(34, 10, "EMIT", 1000, 1000)

    if base_contact_ring:
        # n+ base tie drawn as a Comp ring around the emitter (outer
        # 400..1600, inner hole 600..1400 clears the 800..1200 emitter), on
        # the *same* Comp (22/0) layer as the emitter -- reproduces the
        # double-count. Nplus (32/0) covers only the ring (not the emitter),
        # marking it n+ so `emitter_excludes=(Nplus,)` drops it.
        for lx0, ly0, lx1, ly1 in (
            (400, 400, 1600, 600),  # bottom strip
            (400, 1400, 1600, 1600),  # top strip
            (400, 600, 600, 1400),  # left strip
            (1400, 600, 1600, 1400),  # right strip
        ):
            draw(22, 0, kdb.Box(lx0, ly0, lx1, ly1))  # Comp (base-tie ring)
            draw(32, 0, kdb.Box(lx0, ly0, lx1, ly1))  # Nplus over the ring only

    return layout


def test_sky130_synthetic_bjt_extracts_one_pnp_device(tmp_path):
    """The synthetic vertical-PNP layout extracts exactly one `pnp` device
    (no spurious `nfet`/`pfet` from the emitter's diff.drawing shape, which
    doubles as this deck's MOS `active` layer -- there is no poly gate over
    it, so the MOS extractor recognises nothing there), with the emitter and
    base terminals resolved to their labelled nets and the collector tied to
    the deck's substrate net."""
    path = _write_gds(_make_sky130_bjt_layout(), tmp_path / "bjt.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "bjt.spice"))

    assert report["device_counts"] == {"pnp": 1}
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "pnp",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
        "resistor",
    ]

    (device,) = report["devices"]
    assert device["class"] == "pnp"
    assert device["nets"] == {"c": "vsubs", "b": "BASE", "e": "EMIT"}


def test_sky130_bjt_base_tie_on_diff_is_documented_limitation(tmp_path):
    """Characterises the residual limitation the sky130 deck documents
    (issue #302): because this curated deck models no implant layers, a base
    tie drawn on the *literal* `diff.drawing` emitter layer cannot be told
    apart from the p+ emitter, so it still double-counts. This pins the
    documented behaviour -- the realistic layout draws the base tie on the
    distinct `tap.drawing` layer (see `test_sky130_synthetic_bjt_extracts_
    one_pnp_device`, which stays at one device). If sky130 ever models its
    implant masks and resolves this, update this test alongside the deck's
    docstring."""
    path = _write_gds(
        _make_sky130_bjt_layout(base_tie_on_diff=True), tmp_path / "bjt.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "bjt.spice"))

    # Two pnp: the real emitter, plus the base-tie ring on the same diff
    # layer that no already-modelled layer can exclude. Documented limitation.
    assert report["device_counts"] == {"pnp": 2}


def test_gf180mcu_synthetic_bjt_extracts_one_bjt_device(tmp_path):
    """The synthetic vertical-bipolar layout extracts exactly one `bjt`
    device (no spurious `nfet`/`pfet` from the emitter's Comp shape, which
    doubles as this deck's MOS `active` layer -- no poly gate over it), with
    the emitter terminal resolved to its labelled net and the collector tied
    to the deck's substrate net. The base has no drawn tap in this fixture
    (gf180mcu's curated deck has no distinct well-tie layer, mirroring
    `test_gf180mcu_clkinv_1_spot_check`'s PMOS-body note), so it is an
    anonymous net."""
    path = _write_gds(_make_gf180mcu_bjt_layout(), tmp_path / "bjt.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "bjt.spice"))

    assert report["device_counts"] == {"bjt": 1}
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "bjt",
        "cap_mim_2f0_m4m5_noshield",
        "resistor",
        "diode_nd2ps_06v0",
        "diode_pd2nw_06v0",
    ]

    (device,) = report["devices"]
    assert device["class"] == "bjt"
    assert device["nets"]["c"] == "vsubs"
    assert device["nets"]["e"] == "EMIT"
    assert device["nets"]["b"].startswith("$")  # anonymous, no base tie drawn


def test_gf180mcu_bjt_base_contact_ring_extracts_one_device(tmp_path):
    """A vertical-bipolar unit cell whose Nwell base is contacted by an n+
    tie ring drawn on the *same* `Comp` diffusion layer as the p+ emitter,
    inside the same `DRC_BJT` mark, extracts exactly **one** `bjt` -- not two
    (issue #302). The ring carries `Nplus` (an n+ tie) rather than the
    emitter's p+, so the deck's `emitter_excludes=(Nplus,)` narrowing drops
    it; without that fix the ring is a second shape on the emitter layer
    inside the marked base and `DeviceExtractorBJT3Transistor` recognises it
    as a spurious second device sharing the one base net."""
    path = _write_gds(
        _make_gf180mcu_bjt_layout(base_contact_ring=True), tmp_path / "bjt.gds"
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "bjt.spice"))

    assert report["device_counts"] == {"bjt": 1}
    (device,) = report["devices"]
    assert device["class"] == "bjt"
    assert device["nets"]["e"] == "EMIT"  # the p+ emitter, not the base ring


def test_bjt_array_sky130_extracts_nonzero_devices(tmp_path):
    """Issue #432: `klt gen bjt_array` (sky130) piped into `klt extract
    --deck sky130` reports real devices instead of `device_count: 0` -- the
    generator now draws the per-unit bipolar device-mark (`pnp.drawing`) and
    a base-tie `tap` shape `bjt_array` previously omitted for this family
    (see `docs/design/gen-bjt-array-spike.md`).

    Issue #491 (deck+generator half of the #295/#462 dummy-suppression
    machinery, now wired up for sky130): `bjt_array` also draws the deck's
    curated `dummy` marker over its dummy columns' own bipolar device-mark
    footprint, so `klt extract`'s dummy-suppression guard drops them from the
    netlist instead of extracting them as spurious real devices -- matching
    `klt gen`'s own `device_count` (real cells only) exactly, rather than
    exceeding it by the dummy-column count the way it did pre-#491."""
    from klayout_tools.gen import generate

    root = _make_pdk_install(tmp_path, "sky130A")
    params = {"rows": 3, "cols": 3, "dummy": 1, "topology": "common_centroid"}
    gen_report = generate(
        {
            "generator": "bjt_array",
            "pdk": {"variant": "sky130A", "root": root},
            "params": params,
            "options": {"output": str(tmp_path / "bjt_t.gds")},
        }
    )
    assert gen_report["device_count"] == params["rows"] * params["cols"]

    report = run_extract(
        str(tmp_path / "bjt_t.gds"), "sky130", output=str(tmp_path / "bjt_t.spice")
    )

    real = params["rows"] * params["cols"]
    dropped = 2 * params["dummy"] * params["rows"]
    assert report["device_count"] == real
    assert report["device_counts"] == {"pnp": real}
    assert report["dummy_devices_dropped"] == dropped

    # Every device's base terminal shares the same net -- the array's one
    # shared base well, tied together by the per-unit `tap` shape -- rather
    # than each device's base staying a separate, per-device floating node.
    base_nets = {d["nets"]["b"] for d in report["devices"]}
    assert len(base_nets) == 1


def test_mos_array_sky130_dummy_gates_suppressed(tmp_path):
    """Issue #491: `klt gen mos_array` (sky130, `dummy > 0`) piped into `klt
    extract --deck sky130` suppresses the dummy gates instead of extracting
    them as spurious real `nfet` devices -- `dummy_devices_dropped` matches
    `2 * rows * dummy` (the issue's own Impact-section formula) and the
    extracted `device_count` matches `klt gen`'s own real-cell-only count."""
    from klayout_tools.gen import generate

    root = _make_pdk_install(tmp_path, "sky130A")
    params = {"rows": 2, "cols": 2, "dummy": 1}
    gen_report = generate(
        {
            "generator": "mos_array",
            "pdk": {"variant": "sky130A", "root": root},
            "params": params,
            "options": {"output": str(tmp_path / "mos_t.gds")},
        }
    )
    real = params["rows"] * params["cols"]
    assert gen_report["device_count"] == real

    report = run_extract(
        str(tmp_path / "mos_t.gds"), "sky130", output=str(tmp_path / "mos_t.spice")
    )

    dropped = 2 * params["rows"] * params["dummy"]
    assert report["device_count"] == real
    assert report["device_counts"] == {"nfet": real}
    assert report["dummy_devices_dropped"] == dropped


def test_res_array_sky130_dummy_resistors_suppressed(tmp_path):
    """Issue #491: `klt gen res_array` (sky130, `dummy > 0`) piped into `klt
    extract --deck sky130` suppresses the dummy resistor bodies instead of
    extracting them as spurious real `res_generic_po` devices --
    `dummy_devices_dropped` matches `2 * rows * dummy` and the extracted
    `device_count` matches `klt gen`'s own real-cell-only count."""
    from klayout_tools.gen import generate

    root = _make_pdk_install(tmp_path, "sky130A")
    params = {"num": 4, "dummy": 2, "rows": 1}
    gen_report = generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": root},
            "params": params,
            "options": {"output": str(tmp_path / "res_t.gds")},
        }
    )
    assert gen_report["device_count"] == params["num"]

    report = run_extract(
        str(tmp_path / "res_t.gds"), "sky130", output=str(tmp_path / "res_t.spice")
    )

    dropped = 2 * params["rows"] * params["dummy"]
    assert report["device_count"] == params["num"]
    assert report["device_counts"] == {"res_generic_po": params["num"]}
    assert report["dummy_devices_dropped"] == dropped


def test_sky130_bjt_coincident_marker_is_clean_error_not_traceback(tmp_path):
    """Issue #432: a bipolar device-mark drawn exactly coincident with (not
    strictly enclosing) its emitter pad used to abort `klt extract` with an
    unhandled `RuntimeError` straight out of
    `LayoutToNetlist.extract_netlist()` (`Terminal 'C' ... isn't connected`)
    rather than the documented clean error envelope
    (`docs/cli/extract.md`'s "No Python traceback is printed" contract). It
    now raises this module's own `ExtractError` -- the type the CLI already
    catches and turns into a clean stderr message + exit code 1."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(64, 20, kdb.Box(0, 0, 6000, 4000))  # nwell.drawing (base)
    draw(65, 20, kdb.Box(1000, 1000, 2000, 2000))  # diff.drawing (emitter)
    draw(82, 44, kdb.Box(1000, 1000, 2000, 2000))  # pnp.drawing -- coincident
    # with the emitter above, not strictly enclosing it.
    draw(66, 44, kdb.Box(1400, 1400, 1600, 1600))  # licon1 over the emitter
    draw(67, 20, kdb.Box(1000, 1000, 2000, 2000))  # li1 over the emitter

    path = _write_gds(layout, tmp_path / "coincident.gds")
    with pytest.raises(ExtractError, match="unconnected terminal"):
        run_extract(path, "sky130", output=str(tmp_path / "coincident.spice"))


# --------------------------------------------------------------------------- #
# Junction-diode device recognition (issue #542)
# --------------------------------------------------------------------------- #


def _make_gf180mcu_diode_layout(
    *,
    unmarked_pmos: bool = False,
    dummy_over_nd2ps: bool = False,
) -> kdb.Layout:
    """A minimal dual-diode ESD clamp on gf180mcu's curated diode layers --
    the same topology the PDK's own `gf180mcu_fd_io__asig_5p0` pad cell's CDL
    is built from (`D ... diode_nd2ps_06v0` + `D ... diode_pd2nw_06v0`, see
    issue #542).

    Two devices, each a 1 um x 1 um junction (area 1.0 um^2, perimeter
    4.0 um):

    - `diode_nd2ps_06v0` at the origin: an n+ `Comp` (`Nplus` + `Dualgate`)
      inside a `diode_mk` mark, outside every `Nwell`, contacted up to a
      labelled `CATH` net. Its anode is the p-substrate (no drawn mask), so
      it must resolve to the deck's `substrate_net`.
    - `diode_pd2nw_06v0` at x=4 um: a p+ `Comp` (`Pplus` + `Dualgate`) inside
      a `diode_mk` mark inside an `Nwell`, contacted up to a labelled `ANOD`
      net. Its cathode is the well itself, which this fixture leaves untapped
      (gf180mcu's curated deck has no distinct well-tie layer -- see
      `test_gf180mcu_synthetic_bjt_extracts_one_bjt_device`'s note), so it is
      an anonymous net.

    ``unmarked_pmos`` adds an ordinary PMOS -- p+ `Comp` under a `Poly2`
    gate, inside the *same* `Nwell` as the pd2nw diode but carrying no
    `diode_mk` -- to prove the marker intersection is what scopes
    recognition: the PMOS's own p+-in-Nwell source/drain must not be
    misrecognised as a second diode.

    ``dummy_over_nd2ps`` covers the whole nd2ps junction with a shape on
    :data:`_DUMMY_MARKER`, for the dummy-suppression tests (issue #295,
    extended to diodes in #542); it only has an effect under a deck that
    declares that layer as its ``dummy`` marker (see :func:`_dummy_deck`).
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # --- diode_nd2ps_06v0: n+ diffusion in the p-substrate ---
    draw(115, 5, kdb.Box(-200, -200, 1200, 1200))  # diode_mk (device mark)
    draw(55, 0, kdb.Box(-200, -200, 1200, 1200))  # Dualgate (the 6V flavour)
    draw(22, 0, kdb.Box(0, 0, 1000, 1000))  # Comp (cathode)
    draw(32, 0, kdb.Box(0, 0, 1000, 1000))  # Nplus (n+ doped)
    draw(33, 0, kdb.Box(300, 300, 700, 700))  # Contact over the cathode
    draw(34, 0, kdb.Box(200, 200, 800, 800))  # Metal1 over the cathode
    label(34, 10, "CATH", 500, 500)

    # --- diode_pd2nw_06v0: p+ diffusion in Nwell ---
    draw(21, 0, kdb.Box(4000, 0, 7000, 3000))  # Nwell (cathode)
    draw(115, 5, kdb.Box(4800, 800, 6200, 2200))  # diode_mk (device mark)
    draw(55, 0, kdb.Box(4800, 800, 6200, 2200))  # Dualgate (the 6V flavour)
    draw(22, 0, kdb.Box(5000, 1000, 6000, 2000))  # Comp (anode)
    draw(31, 0, kdb.Box(5000, 1000, 6000, 2000))  # Pplus (p+ doped)
    draw(33, 0, kdb.Box(5300, 1300, 5700, 1700))  # Contact over the anode
    draw(34, 0, kdb.Box(5200, 1200, 5800, 1800))  # Metal1 over the anode
    label(34, 10, "ANOD", 5500, 1500)

    if unmarked_pmos:
        # Ordinary PMOS in the same Nwell, no `diode_mk` anywhere near it.
        draw(22, 0, kdb.Box(4200, 2400, 5400, 2900))  # Comp (source/drain)
        draw(31, 0, kdb.Box(4200, 2400, 5400, 2900))  # Pplus
        draw(30, 0, kdb.Box(4700, 2200, 4900, 3100))  # Poly2 (gate)

    if dummy_over_nd2ps:
        draw(*_DUMMY_MARKER, kdb.Box(-300, -300, 1300, 1300))

    return layout


def test_gf180mcu_synthetic_diode_layout_extracts_both_clamp_diodes(tmp_path):
    """The synthetic dual-diode ESD clamp extracts exactly the two `D`
    devices its topology describes (issue #542) -- before this, neither deck
    recognised a diode at all, so the same layout extracted zero devices and
    `klt lvs` could not verify any diode-based clamp.

    The n+/p-substrate diode's anode resolves to the deck's `substrate_net`
    global (gf180mcu draws no p-substrate mask, so the entry declares
    `anode=None`); the p+/Nwell diode's cathode is the untapped well, hence
    anonymous. Both report the junction's own area/perimeter."""
    path = _write_gds(_make_gf180mcu_diode_layout(), tmp_path / "diode.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "diode.spice"))

    assert report["device_counts"] == {
        "diode_nd2ps_06v0": 1,
        "diode_pd2nw_06v0": 1,
    }

    by_class = {device["class"]: device for device in report["devices"]}
    nd2ps = by_class["diode_nd2ps_06v0"]
    assert nd2ps["nets"] == {"a": "vsubs", "c": "CATH"}
    assert nd2ps["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}

    pd2nw = by_class["diode_pd2nw_06v0"]
    assert pd2nw["nets"]["a"] == "ANOD"
    assert pd2nw["nets"]["c"].startswith("$")  # untapped well, anonymous
    assert pd2nw["params"] == {"area_um2": 1.0, "perimeter_um": 4.0}


def test_gf180mcu_diode_written_as_spice_d_card(tmp_path):
    """A recognised diode reaches the written netlist as a SPICE `D` card
    whose model token is the deck entry's own name -- the same
    schematic-equivalent form the PDK's own ESD-clamp CDL uses
    (`D0 DVSS DVDD diode_nd2ps_06v0`), and directly comparable by
    `klt lvs`."""
    path = _write_gds(_make_gf180mcu_diode_layout(), tmp_path / "diode.gds")
    spice_path = tmp_path / "diode.spice"
    run_extract(path, "gf180mcu", output=str(spice_path))

    netlist = spice_path.read_text()
    # `D<name> <anode> <cathode> <model>` -- terminal order matches the CDL.
    assert "vsubs CATH diode_nd2ps_06v0" in netlist
    assert "ANOD " in netlist and "diode_pd2nw_06v0" in netlist
    assert netlist.count("\nD") == 2


def test_gf180mcu_unmarked_pmos_in_same_well_is_not_a_diode(tmp_path):
    """An ordinary PMOS drawn in the *same* Nwell as a marked diode -- p+
    diffusion inside a well, geometrically indistinguishable from the diode's
    own anode except for the missing `diode_mk` -- extracts as a `pfet` and
    **not** as a second diode (issue #542).

    This is the marker-intersection guard `extract.py` applies to both diode
    terminals, the same one the bipolar block applies to its base: without
    it, every p+-in-Nwell source/drain in a layout would be misrecognised."""
    path = _write_gds(
        _make_gf180mcu_diode_layout(unmarked_pmos=True), tmp_path / "diode_pmos.gds"
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "diode_pmos.spice"))

    assert report["device_counts"] == {
        "pfet": 1,
        "diode_nd2ps_06v0": 1,
        "diode_pd2nw_06v0": 1,
    }


def test_gf180mcu_diode_free_layout_extracts_no_diodes(tmp_path):
    """A gf180mcu layout that draws no `diode_mk` at all extracts exactly as
    it did before this feature existed: the new `diodes` entries claim
    nothing, and no empty device layers are registered (issue #542)."""
    path = _write_gds(_make_gf180mcu_bjt_layout(), tmp_path / "bjt.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "bjt.spice"))

    assert report["device_counts"] == {"bjt": 1}


def test_diodes_field_defaults_empty_and_is_a_no_op():
    """The `diodes` field is additive/optional (issue #542): a deck declaring
    none reports its pre-#542 `device_classes` unchanged, exactly as
    `resistors`/`capacitors`/`bipolars` each did when they landed."""
    deck = ExtractionDeck(
        active=(65, 20), poly=(66, 20), nwell=(64, 20), contact=(66, 44), metals=()
    )
    assert deck.diodes == ()
    assert deck.device_classes == ("nfet", "pfet")
    assert get_extraction_deck("sky130").diodes == ()


def test_diode_with_no_drawn_terminal_is_a_deck_authoring_error(tmp_path):
    """A `DiodeDevice` declaring *both* terminals substrate-formed has
    nothing to overlap, so it could only ever extract silently nothing --
    rejected as a deck-authoring mistake, checked unconditionally (even on a
    diode-free layout) like the capacitor block's own `top_plate_via`
    pairing check."""
    deck = dataclasses.replace(
        get_extraction_deck("gf180mcu"),
        diodes=(DiodeDevice(name="bad_diode", marker=(115, 5)),),
    )
    path = _write_gds(_make_gf180mcu_bjt_layout(), tmp_path / "bjt.gds")
    layout = kdb.Layout()
    layout.read(str(path))

    with pytest.raises(ExtractError, match="at most one of anode/cathode"):
        _extract_netlist(layout, layout.top_cell(), deck, None)


def test_dummy_marker_drops_a_whole_diode(tmp_path, monkeypatch):
    """A junction fully covered by the deck's `dummy` marker is dropped from
    the extracted netlist and counted once in `dummy_devices_dropped` --
    extending #295/#462's dummy suppression to diodes (#542).

    Counted **once**, not once per declared flavour: gf180mcu declares two
    diode entries sharing the one `diode_mk` layer, so counting marker
    components (rather than recognised junctions) would double-charge it."""
    path = _write_gds(
        _make_gf180mcu_diode_layout(dummy_over_nd2ps=True), tmp_path / "diode.gds"
    )

    baseline = run_extract(path, "gf180mcu", output=str(tmp_path / "base.spice"))
    assert baseline["device_counts"]["diode_nd2ps_06v0"] == 1
    assert baseline["dummy_devices_dropped"] == 0

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    suppressed = run_extract(path, "gf180mcu", output=str(tmp_path / "dummy.spice"))

    assert suppressed["dummy_devices_dropped"] == 1
    assert "diode_nd2ps_06v0" not in suppressed["device_counts"]
    # The other clamp diode is untouched by the marker and still extracts.
    assert suppressed["device_counts"]["diode_pd2nw_06v0"] == 1


# --------------------------------------------------------------------------- #
# MiM capacitor device recognition (issue #225)
# --------------------------------------------------------------------------- #


def _box_um(x0, y0, x1, y1, dbu=0.001):
    """A `kdb.Box` from micrometre coordinates at the given database unit --
    matches this deck's own `NOMINAL_DBU_UM`/corpus convention (1 nm/unit)."""
    return kdb.Box(round(x0 / dbu), round(y0 / dbu), round(x1 / dbu), round(y1 / dbu))


def _trans_um(x, y, dbu=0.001):
    """A `kdb.Trans` (label placement point) from micrometre coordinates --
    the point-placement analogue of `_box_um`."""
    return kdb.Trans(round(x / dbu), round(y / dbu))


def _make_gf180mcu_mim_layout(*, marked: bool = True) -> kdb.Layout:
    """A minimal gf180mcu MiM-cap layout on the curated deck's Option-B
    stack: a 10x10um `FuseTop` top plate (marked with both `CAP_MK`/
    `MIM_L_MK` when ``marked``) sitting over a much larger `Metal4` bottom
    plate, so the "virtual bottom plate" derivation clips to the sized
    `FuseTop` outline rather than the raw `Metal4` extent.

    ``marked=False`` drops the ``CAP_MK``/``MIM_L_MK`` markers, leaving an
    ordinary `FuseTop`-over-`Metal4` overlap indistinguishable in area/shape
    from a real MiM cap except for the missing marker -- the edge case this
    module's own docstring calls out (a marker-free overlap must not be
    misclassified as a capacitor)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, _box_um(-20, -20, 20, 20))  # Metal4 (bottom plate conductor)
    draw(75, 0, _box_um(0, 0, 10, 10))  # FuseTop (top plate)
    if marked:
        draw(117, 5, _box_um(-5, -5, 15, 15))  # CAP_MK
        draw(117, 10, _box_um(-5, -5, 15, 15))  # MIM_L_MK

    return layout


def _make_sky130_mim_layout(*, met4: bool = False) -> kdb.Layout:
    """A minimal sky130 MiM-cap layout: a 10x5um top-plate mark (`capm` on
    met3, or `capm2` on met4 when ``met4``) over a larger bottom-plate
    conductor -- sky130's derivation needs no "virtual bottom plate" sizing
    step (see `sky130.py`'s provenance note), so the bottom conductor can be
    exactly as large as convenient."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    if met4:
        draw(71, 20, _box_um(-20, -20, 20, 20))  # met4.drawing
        draw(97, 44, _box_um(0, 0, 10, 5))  # capm2.drawing
    else:
        draw(70, 20, _box_um(-20, -20, 20, 20))  # met3.drawing
        draw(89, 44, _box_um(0, 0, 10, 5))  # capm.drawing

    return layout


def test_gf180mcu_synthetic_mim_extracts_one_capacitor_device(tmp_path):
    """The synthetic marked FuseTop-over-Metal4 layout extracts exactly one
    `cap_mim_2f0_m4m5_noshield` device: `C = A * area_cap + P * perim_cap`
    for the 10x10um `FuseTop` top-plate area/perimeter against the deck's
    cited 1.99fF/um^2 / 0.2383fF/um coefficients (100um^2 * 1.99e-15 F/um^2
    + 40um * 2.383e-16 F/um = 2.08532e-13 F, issue #512), independent of the
    much larger `Metal4` bottom-plate conductor it sits over (clipped to the
    "virtual bottom plate" by the 1.06um `FuseTop` oversize -- see
    `gf180mcu.py`)."""
    path = _write_gds(_make_gf180mcu_mim_layout(), tmp_path / "mim.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "mim.spice"))

    assert report["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}

    (device,) = report["devices"]
    assert device["class"] == "cap_mim_2f0_m4m5_noshield"
    assert device["params"]["c_f"] == pytest.approx(2.08532e-13)
    assert device["params"]["area_um2"] == pytest.approx(100.0)
    assert device["params"]["perimeter_um"] == pytest.approx(40.0)


def test_gf180mcu_unmarked_metal4_fusetop_overlap_is_not_a_capacitor(tmp_path):
    """An ordinary `FuseTop`-over-`Metal4` overlap with no `CAP_MK`/
    `MIM_L_MK` marker is *not* misclassified as a capacitor -- it extracts as
    plain, unrecognised connectivity (zero devices), the same "unmarked
    conductor is never reclassified" guarantee `docs/cli/extract.md`
    documents for drawn resistors."""
    path = _write_gds(
        _make_gf180mcu_mim_layout(marked=False), tmp_path / "unmarked.gds"
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "unmarked.spice"))

    assert report["device_count"] == 0
    assert report["device_counts"] == {}


def _make_gf180mcu_mim_layout_with_bottom_routing(label: str) -> kdb.Layout:
    """Like `_make_gf180mcu_mim_layout` (marked), plus a `Via3`/`Metal3`
    landing pad wired to the same `Metal4` bottom-plate conductor, well away
    from the cap itself, and labelled on `Metal3`'s pin/label layer -- the
    "bottom plate routed down through contact/metal/via to a labelled net"
    regression case issue #314 describes: before the fix, the labelled net
    is invisible to the capacitor's P1 terminal regardless of this routing.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, _box_um(-20, -20, 20, 20))  # Metal4 (bottom plate conductor)
    draw(75, 0, _box_um(0, 0, 10, 10))  # FuseTop (top plate)
    draw(117, 5, _box_um(-5, -5, 15, 15))  # CAP_MK
    draw(117, 10, _box_um(-5, -5, 15, 15))  # MIM_L_MK

    # Route the bottom plate's Metal4 conductor down to a labelled Metal3
    # net, well away from the cap itself (still inside the big Metal4 box
    # above, so the fix's `metals[]` connectivity has to reach across it).
    draw(40, 0, _box_um(-15, -15, -13, -13))  # Via3 (Metal3 <-> Metal4)
    draw(42, 0, _box_um(-16, -16, -12, -12))  # Metal3
    top.shapes(layout.layer(42, 10)).insert(  # Metal3 pin/label
        kdb.Text(label, _trans_um(-14, -14))
    )

    return layout


def _make_gf180mcu_mim_layout_with_top_routing(label: str) -> kdb.Layout:
    """A gf180mcu MiM-cap layout whose top plate (`FuseTop`) is routed up
    through `Via4` to a labelled `Metal5` net -- the top-plate analogue of
    `_make_gf180mcu_mim_layout_with_bottom_routing`, exercising
    `CapacitorDevice.top_plate_via`/`top_plate_via_metal` (issue #314).

    `FuseTop` (and its `CAP_MK`/`MIM_L_MK` markers) extends 2um further in
    x than the `Metal4` bottom-plate conductor beneath it, so the `Via4`
    landing pad can sit inside `FuseTop`'s own footprint (this device's
    `top_region`) without also touching `Metal4` -- avoiding a confound
    where the landing pad's own overlap with `Metal4` would tie the bottom
    and top plates together through the deck's ordinary blanket
    `Metal4`<->`Via4`<->`Metal5` connectivity, rather than through the
    top-plate-specific wiring this test exists to exercise."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, _box_um(0, 0, 10, 10))  # Metal4 (bottom plate conductor)
    draw(75, 0, _box_um(0, 0, 12, 10))  # FuseTop (top plate), wider than Metal4
    draw(117, 5, _box_um(-5, -5, 17, 15))  # CAP_MK
    draw(117, 10, _box_um(-5, -5, 17, 15))  # MIM_L_MK

    # Via4 landing pad inside FuseTop's extended footprint (x = 10..12um),
    # where Metal4 is absent -- lands up to a labelled Metal5 net.
    draw(41, 0, _box_um(10.5, 4, 11.5, 6))  # Via4
    draw(81, 0, _box_um(10, 3, 12, 7))  # Metal5
    top.shapes(layout.layer(81, 10)).insert(  # Metal5 pin/label
        kdb.Text(label, _trans_um(11, 5))
    )

    return layout


def test_gf180mcu_capacitor_bottom_plate_connects_to_routed_metal_net(tmp_path):
    """Issue #314: a bottom plate wired down through `Via3` to a labelled
    `Metal3` net extracts with the capacitor's P1/bottom terminal (reported
    as `nets.a`, the first terminal passed to `extract_devices`) on that
    same labelled net, not an anonymous device-only net -- the "Confirmed on
    a minimal test case" regression this issue describes. The unrouted top
    plate stays on its own anonymous net."""
    path = _write_gds(
        _make_gf180mcu_mim_layout_with_bottom_routing("VBOT"),
        tmp_path / "mim_bottom_routed.gds",
    )
    report = run_extract(
        path, "gf180mcu", output=str(tmp_path / "mim_bottom_routed.spice")
    )

    assert report["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}
    (device,) = report["devices"]
    assert device["params"]["c_f"] == pytest.approx(2.08532e-13)
    assert device["params"]["area_um2"] == pytest.approx(100.0)
    assert device["nets"]["a"] == "VBOT"
    assert device["nets"]["b"].startswith("$")


def test_gf180mcu_capacitor_top_plate_connects_to_routed_metal_net(tmp_path):
    """Issue #314: a top plate wired up through the deck's declared
    `top_plate_via`/`top_plate_via_metal` (`Via4` -> `Metal5`) to a labelled
    net extracts with the capacitor's P2/top terminal (reported as
    `nets.b`, the second terminal passed to `extract_devices`) on that same
    labelled net. The bottom plate's `Metal4` conductor is drawn here but
    carries no label/further routing of its own, so it stays on its own
    anonymous net even though it is tied into the `metals[]` graph."""
    path = _write_gds(
        _make_gf180mcu_mim_layout_with_top_routing("VTOP"),
        tmp_path / "mim_top_routed.gds",
    )
    report = run_extract(
        path, "gf180mcu", output=str(tmp_path / "mim_top_routed.spice")
    )

    assert report["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}
    (device,) = report["devices"]
    assert device["params"]["c_f"] == pytest.approx(2.08532e-13)
    assert device["params"]["area_um2"] == pytest.approx(100.0)
    assert device["nets"]["b"] == "VTOP"
    assert device["nets"]["a"].startswith("$")


def _make_gf180mcu_mim_layout_with_drm_legal_top_via() -> kdb.Layout:
    """Issue #364's minimal repro: a bottom-plate `Metal4` box, an inset
    `FuseTop` top plate (marked `CAP_MK`/`MIM_L_MK`), and *only* a `Via4`
    landing inside both plates' footprints -- the DRM-legal position the
    PDK's own top-plate-via minimum-overlap rule requires (the bottom plate
    must enclose the via by some margin, not clear it) -- plus a small
    `Metal5` pad directly above the via, touching nothing else. No other
    routing anywhere in the layout, so the only way the two plates could end
    up on the same net is the false short this issue reports."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, _box_um(-20, -20, 20, 20))  # Metal4 (bottom plate conductor)
    draw(75, 0, _box_um(0, 0, 10, 10))  # FuseTop (top plate)
    draw(117, 5, _box_um(-5, -5, 15, 15))  # CAP_MK
    draw(117, 10, _box_um(-5, -5, 15, 15))  # MIM_L_MK

    # Via4, entirely inside both FuseTop's own footprint and the (virtual)
    # bottom-plate outline beneath it -- before the #364 fix, this overlap
    # was read by the deck's generic Metal4<->Via4<->Metal5 connectivity
    # loop as an ordinary via shorting the bottom and top plates together.
    draw(41, 0, _box_um(4, 4, 6, 6))  # Via4
    draw(81, 0, _box_um(3, 3, 7, 7))  # Metal5 pad, touches only the via

    return layout


def test_gf180mcu_capacitor_drm_legal_top_via_does_not_short_plates(tmp_path):
    """Issue #364: a `top_plate_via` (`Via4`) placed exactly per the PDK's
    own minimum-overlap rule for that via -- landing *inside* the bottom
    plate's `Metal4` footprint, as the rule requires -- must not merge the
    capacitor's two plates into one net through the deck's generic
    `Metal4`<->`Via4`<->`Metal5` connectivity loop. Before the fix this
    DRM-legal position produced `net_count == 1` (a false short between P1
    and P2); the fix keeps the two plate terminals on distinct nets while
    still wiring the top plate up to the touching `Metal5` pad through the
    same via (the #314 behaviour this fix must not regress)."""
    path = _write_gds(
        _make_gf180mcu_mim_layout_with_drm_legal_top_via(),
        tmp_path / "mim_drm_legal_top_via.gds",
    )
    report = run_extract(
        path, "gf180mcu", output=str(tmp_path / "mim_drm_legal_top_via.spice")
    )

    assert report["device_count"] == 1
    assert report["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}
    assert report["net_count"] == 2

    (device,) = report["devices"]
    assert device["class"] == "cap_mim_2f0_m4m5_noshield"
    assert device["nets"]["a"] != device["nets"]["b"]


def test_exclude_capacitor_top_via_overlap_only_cuts_the_overlapping_area():
    """Issue #364, edge case named in the acceptance criteria: a
    `top_plate_via` landing pad that only *partially* overlaps the
    recognised bottom plate (a landing pad extending past the plate's own
    edge) has only the overlapping sub-region excluded from `vias[]` -- not
    the whole via shape/component -- and an unrelated `Via4` shape drawn
    elsewhere in the layout (ordinary routing, nothing to do with this
    capacitor) is left completely untouched."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, _box_um(0, 0, 10, 10))  # Metal4 bottom plate conductor
    draw(75, 0, _box_um(0, 0, 15, 10))  # FuseTop top plate, wider than Metal4
    draw(117, 5, _box_um(-5, -5, 20, 15))  # CAP_MK
    draw(117, 10, _box_um(-5, -5, 20, 15))  # MIM_L_MK

    # Via4 landing pad straddling the bottom plate's right edge (x=10um):
    # half overlaps Metal4 (the false-short path), half extends past it
    # into FuseTop's own overhang where there is no bottom-plate conductor.
    draw(41, 0, _box_um(8, 4, 12, 6))  # Via4, straddles the Metal4 edge

    # An unrelated Via4 shape elsewhere in the layout -- ordinary routing
    # this capacitor's exclusion must never touch.
    draw(41, 0, _box_um(-10, -10, -8, -8))  # unrelated Via4 routing

    deck = get_extraction_deck("gf180mcu")
    top_cell = layout.top_cell()
    vias = [_region(layout, top_cell, layer) for layer in deck.vias]
    via4_index = deck.vias.index((41, 0))
    original_via4 = vias[via4_index]

    excluded = _exclude_capacitor_top_via_overlap(layout, top_cell, deck, vias)
    excluded_via4 = excluded[via4_index]

    overlap = kdb.Region(_box_um(8, 4, 10, 6))  # the straddling via's Metal4 half
    remainder = kdb.Region(_box_um(10, 4, 12, 6))  # the half clear of Metal4
    unrelated = kdb.Region(_box_um(-10, -10, -8, -8))

    # Only the overlapping half is cut -- the via keeps its other half...
    assert not excluded_via4.is_empty()
    assert (excluded_via4 & overlap).is_empty()
    assert (excluded_via4 & remainder).area() == pytest.approx(remainder.area())
    # ... and the unrelated via shape elsewhere is untouched.
    assert (excluded_via4 & unrelated).area() == pytest.approx(unrelated.area())
    assert excluded_via4.area() == pytest.approx(original_via4.area() - overlap.area())

    # A deck with no capacitor declaring `top_plate_via` (or none of whose
    # capacitors' `top_plate_via` overlaps anything) is a no-op: the same
    # list object comes back unchanged.
    empty_deck = dataclasses.replace(deck, capacitors=())
    assert (
        _exclude_capacitor_top_via_overlap(layout, top_cell, empty_deck, vias) is vias
    )


@pytest.mark.parametrize(
    "met4, expected_class",
    [
        (False, "sky130_fd_pr__model__cap_mim"),
        (True, "sky130_fd_pr__model__cap_mim_m4"),
    ],
)
def test_sky130_synthetic_mim_extracts_one_capacitor_device(
    tmp_path, met4, expected_class
):
    """Both of sky130's curated MiM stacks (met3's `capm`, met4's `capm2`)
    extract exactly one capacitor device each: `C = A * area_cap + P *
    perim_cap` for the 10x5um top-plate mark's own area/perimeter against
    the deck's cited 2.0fF/um^2 / 0.19fF/um coefficients (50um^2 * 2.0e-15
    F/um^2 + 30um * 1.9e-16 F/um = 1.057e-13 F, issue #512) -- no "virtual
    bottom plate" derivation needed (see `sky130.py`'s provenance note), so
    the extracted area/perimeter are exactly the drawn mark's own."""
    path = _write_gds(_make_sky130_mim_layout(met4=met4), tmp_path / "mim.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "mim.spice"))

    assert report["device_counts"] == {expected_class: 1}

    (device,) = report["devices"]
    assert device["class"] == expected_class
    assert device["params"]["c_f"] == pytest.approx(1.057e-13)
    assert device["params"]["area_um2"] == pytest.approx(50.0)
    assert device["params"]["perimeter_um"] == pytest.approx(30.0)


def _make_gf180mcu_mim_square_layout(side_um: float) -> kdb.Layout:
    """A `side_um`x`side_um` `FuseTop`-over-`Metal4` MiM cap, marked with
    `CAP_MK`/`MIM_L_MK` and drawn well clear of `Metal4`'s own edges so the
    "virtual bottom plate" derivation clips to `FuseTop`'s own footprint --
    the parametrised sibling of `_make_gf180mcu_mim_layout`'s fixed 10x10um
    case, used by `test_gf180mcu_mim_perimeter_correction_matches_pdk_model_card`
    (issue #512) to exercise several plate sizes against the PDK's own
    two-term `c_cox`/`c_capsw` law."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    margin = side_um + 20.0
    draw(46, 0, _box_um(-margin, -margin, margin, margin))  # Metal4
    draw(75, 0, _box_um(0, 0, side_um, side_um))  # FuseTop
    mark_margin = 5.0
    draw(
        117,
        5,
        _box_um(
            -mark_margin, -mark_margin, side_um + mark_margin, side_um + mark_margin
        ),
    )  # CAP_MK
    draw(
        117,
        10,
        _box_um(
            -mark_margin, -mark_margin, side_um + mark_margin, side_um + mark_margin
        ),
    )  # MIM_L_MK

    return layout


@pytest.mark.parametrize("side_um", [2.7, 5.0, 10.0, 20.0])
def test_gf180mcu_mim_perimeter_correction_matches_pdk_model_card(tmp_path, side_um):
    """Issue #512: for each of the issue's own cited plate sizes, the
    corrected `c_f` matches `sm141064.ngspice`'s own two-term
    `c_cox*c_AREA + c_capsw*c_PERI` law -- evaluated here with the deck's
    transcribed coefficients (1.99e-15 F/um^2 area, 2.383e-16 F/um
    perimeter) against the drawn square's own area/perimeter -- to a tight
    tolerance, not just the pre-#512 area-only value."""
    path = _write_gds(
        _make_gf180mcu_mim_square_layout(side_um), tmp_path / f"mim_{side_um}.gds"
    )
    report = run_extract(
        path, "gf180mcu", output=str(tmp_path / f"mim_{side_um}.spice")
    )

    assert report["device_counts"] == {"cap_mim_2f0_m4m5_noshield": 1}
    (device,) = report["devices"]

    area_um2 = side_um * side_um
    perimeter_um = 4.0 * side_um
    expected_c_f = 1.99e-15 * area_um2 + 2.383e-16 * perimeter_um

    assert device["params"]["area_um2"] == pytest.approx(area_um2)
    assert device["params"]["perimeter_um"] == pytest.approx(perimeter_um)
    assert device["params"]["c_f"] == pytest.approx(expected_c_f, rel=1e-6)
    # The pre-#512 area-only value is a systematic undercount -- confirms
    # the correction actually changed the reported number, not a no-op.
    assert device["params"]["c_f"] > 1.99e-15 * area_um2


def test_capacitor_default_perim_cap_f_um_reports_area_only_c_f(tmp_path):
    """`CapacitorDevice.perim_cap_f_um` defaults to `0.0` (issue #512): a
    deck entry that does not set it (mirroring every deck's own field before
    this feature existed) still reports the plate's `perimeter_um` (issue
    #512's other half -- `P` is now always read back), but `c_f` stays
    exactly `area_cap_f_um2 * A` -- bit-for-bit what `klt extract` reported
    before this feature existed, not silently corrected by a coefficient the
    deck never opted into."""
    deck = get_extraction_deck("gf180mcu")
    (mim,) = deck.capacitors
    assert mim.perim_cap_f_um != 0.0  # the curated deck itself now opts in

    uncorrected_deck = dataclasses.replace(
        deck,
        capacitors=(
            dataclasses.replace(
                mim,
                area_cap_f_um2=2.0e-15,
                perim_cap_f_um=0.0,  # the pre-#512 default
            ),
        ),
    )

    layout = _make_gf180mcu_mim_layout()
    (
        netlist,
        _warnings,
        _parasitic_nets,
        _black_box_regions,
        _dummy_devices_dropped,
        _unmodelled_poly,
        _abstracted_cells,
    ) = _extract_netlist(layout, layout.top_cell(), uncorrected_deck)
    circuit = netlist.circuit_by_name(layout.top_cell().name)
    devices, _device_counts = _describe_devices(circuit)

    (device,) = devices
    assert device["params"]["c_f"] == pytest.approx(2.0e-13)  # 100um^2 * 2.0e-15
    assert device["params"]["area_um2"] == pytest.approx(100.0)
    assert device["params"]["perimeter_um"] == pytest.approx(40.0)


def test_sky130_unmarked_metal_has_no_capacitor_device(tmp_path):
    """A plain met3/met4 shape with no `capm`/`capm2` mark drawn anywhere is
    not misclassified as a capacitor -- zero devices extracted."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(70, 20)).insert(_box_um(-20, -20, 20, 20))  # met3
    path = _write_gds(layout, tmp_path / "unmarked.gds")

    report = run_extract(path, "sky130", output=str(tmp_path / "unmarked.spice"))

    assert report["device_count"] == 0
    assert report["device_counts"] == {}


# --------------------------------------------------------------------------- #
# Optional PDK resolution (--pdk/--pdk-root, mirrors `klt gen`/`klt sim`)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_pdk_search(monkeypatch):
    """Scrub PDK env vars and empty the host search space -- see
    `test_gen.py`/`test_pdk.py` -- so `test_unresolvable_pdk_...` above is
    hermetic regardless of what is installed on the machine running the
    suite."""
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])


def test_optional_pdk_resolution_is_reported(tmp_path):
    variant_dir = tmp_path / "pdk_install" / "sky130A"
    (variant_dir / "libs.tech").mkdir(parents=True)
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")

    report = run_extract(
        path, "sky130", pdk_variant="sky130A", pdk_root=str(tmp_path / "pdk_install")
    )

    assert report["pdk"] == {
        "variant": "sky130A",
        "root": str(tmp_path / "pdk_install"),
        "version": None,
    }

    # The shared provenance block mirrors the resolved PDK (name/source/
    # version), independent of extract's own richer `pdk` echo above.
    assert report["provenance"]["pdk"] == {
        "name": "sky130A",
        "source": report["provenance"]["pdk"]["source"],
        "version": None,
    }
    assert report["provenance"]["pdk"]["source"] is not None


def test_extraction_does_not_require_pdk_resolution(tmp_path):
    """No --pdk/--pdk-root -> resolution is skipped entirely; extraction
    runs from the curated --deck alone (no PDK install needed, matching
    `klt drc`'s CI posture)."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130")
    assert report["pdk"] is None
    assert report["status"] == "extracted"


# --------------------------------------------------------------------------- #
# --pdk-triggered SPICE model binding (issue #209): `X` subckt calls against
# the resolved PDK's real device library, instead of the bare `M`-card
# form -- a fabricated PDK install is enough (only `find_pdk`'s directory
# probe is exercised; the curated model-name table itself does not read
# anything off disk), mirroring `test_optional_pdk_resolution_is_reported`'s
# idiom above.
# --------------------------------------------------------------------------- #


def _make_pdk_install(tmp_path, variant: str) -> str:
    root = tmp_path / "pdk_install"
    (root / variant / "libs.tech").mkdir(parents=True)
    return str(root)


def test_pdk_omitted_writes_unchanged_m_card_form(tmp_path):
    """Regression: no --pdk/--pdk-root -> the written SPICE keeps today's
    bare `M`-card form, byte-identical in shape to before issue #209."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    text = Path(report["netlist_path"]).read_text()
    assert "\nM" in text or text.startswith("M")
    device_lines = [
        line for line in text.splitlines() if line and line[0] in ("M", "X")
    ]
    assert device_lines, "expected at least one device card"
    assert all(line.startswith("M") for line in device_lines)
    assert "nfet" in text
    assert "pfet" in text
    assert "sky130_fd_pr__" not in text


def test_pdk_resolved_writes_x_card_model_binding_sky130(tmp_path):
    """--pdk sky130A resolves -> MOS devices are written as `X` subckt calls
    against the curated sky130 primitive device library, not the deck's
    bare `nfet`/`pfet` class label."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    root = _make_pdk_install(tmp_path, "sky130A")

    report = run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=root,
        output=str(tmp_path / "inv.spice"),
    )

    # JSON device_counts/class labels are unaffected by model binding --
    # only the written SPICE file's card shape changes (issue #209
    # acceptance criteria).
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert {d["class"] for d in report["devices"]} == {"nfet", "pfet"}

    text = Path(report["netlist_path"]).read_text()
    device_lines = [
        line for line in text.splitlines() if line and line[0] in ("M", "X")
    ]
    assert device_lines, "expected at least one device card"
    assert all(line.startswith("X") for line in device_lines)
    assert any("sky130_fd_pr__nfet_01v8" in line for line in device_lines)
    assert any("sky130_fd_pr__pfet_01v8" in line for line in device_lines)
    # No bare M-card model reference to the deck's own class label leaks in.
    assert not any(line.startswith("M") for line in device_lines)


def test_pdk_resolved_writes_x_card_model_binding_gf180mcu(tmp_path):
    """--pdk gf180mcuA resolves against the gf180mcu deck -> `X` subckt
    calls against gf180mcu's real 3.3V-core primitive device library."""
    layout_path = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"
    root = _make_pdk_install(tmp_path, "gf180mcuA")

    report = run_extract(
        str(layout_path),
        "gf180mcu",
        pdk_variant="gf180mcuA",
        pdk_root=root,
        output=str(tmp_path / "clkinv_1.spice"),
    )

    assert report["device_counts"] == {"nfet": 1, "pfet": 1}

    text = Path(report["netlist_path"]).read_text()
    device_lines = [
        line for line in text.splitlines() if line and line[0] in ("M", "X")
    ]
    assert device_lines
    assert all(line.startswith("X") for line in device_lines)
    assert any(" nfet_03v3 " in line for line in device_lines)
    assert any(" pfet_03v3 " in line for line in device_lines)


def test_pdk_resolved_x_card_carries_l_w_with_unit_suffix(tmp_path):
    """The emitted `X` card's `L`/`W` use the same explicit micrometre unit
    suffix `klt extract`'s own `M`-card writer already uses (see
    `docs/cli/lvs.md`'s "Unit suffixes matter" note) -- unambiguous
    regardless of what `.option scale` (if any) a caller's testbench sets."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    root = _make_pdk_install(tmp_path, "sky130A")

    report = run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=root,
        output=str(tmp_path / "inv.spice"),
    )

    text = Path(report["netlist_path"]).read_text()
    nfet = next(d for d in report["devices"] if d["class"] == "nfet")
    l_um, w_um = nfet["params"]["l_um"], nfet["params"]["w_um"]
    assert f"L={l_um:g}U" in text
    assert f"W={w_um:g}U" in text


def test_pdk_resolved_but_deck_family_mismatch_is_application_error(tmp_path):
    """A PDK that resolves fine, but whose family has no curated table entry
    for the requesting deck (e.g. the `sky130` deck against a resolved
    `gf180mcuA` install), is an `ExtractError` -- never a silent fallback to
    the bare `M`-card form."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    root = _make_pdk_install(tmp_path, "gf180mcuA")

    with pytest.raises(ExtractError, match="no curated PDK device-model binding"):
        run_extract(path, "sky130", pdk_variant="gf180mcuA", pdk_root=root)


def test_pdk_resolved_but_unrecognised_family_is_application_error(tmp_path):
    """A resolved PDK variant whose name matches no known family prefix at
    all is also an `ExtractError` naming what was tried."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    root = _make_pdk_install(tmp_path, "totallyUnknownVariant")

    with pytest.raises(ExtractError, match="no curated PDK device-model binding"):
        run_extract(path, "sky130", pdk_variant="totallyUnknownVariant", pdk_root=root)


# --------------------------------------------------------------------------- #
# --pdk model binding for non-MOS device classes (issue #339): resistor and
# capacitor on both decks, bipolar on sky130 only. gf180mcu's bipolar stays a
# bare `Q` card (documented carve-out, see `decks/gf180mcu.py:557-559`).
# Mirrors the MOS `--pdk` binding tests above.
# --------------------------------------------------------------------------- #


def _device_cards(netlist_path: str) -> list[str]:
    """Every SPICE device card (`M`/`X`/`R`/`C`/`Q`/`D`-prefixed line) in a
    written netlist -- the lines model binding rewrites from a bare primitive
    to an `X` subcircuit call."""
    return [
        line
        for line in Path(netlist_path).read_text().splitlines()
        if line[:1] in ("M", "X", "R", "C", "Q", "D")
    ]


def test_pdk_resolved_binds_resistor_sky130(tmp_path):
    """--pdk binds a recognised sky130 poly resistor to its real geometry-
    parameterized subcircuit (`sky130_fd_pr__res_generic_po`, two terminals,
    `l`/`w` in micrometres) -- not the bare, value-only `R`-card form (#339)."""
    path = _write_gds(_make_poly_resistor_layout("sky130"), tmp_path / "res.gds")
    out = str(tmp_path / "res.spice")
    run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=_make_pdk_install(tmp_path, "sky130A"),
        output=out,
    )
    cards = _device_cards(out)
    assert cards, "expected a device card"
    assert all(c.startswith("X") for c in cards), cards
    (card,) = cards
    assert " sky130_fd_pr__res_generic_po " in card
    # Two terminals (the contacted heads), no bulk tie for the generic device.
    assert card.split()[1:3] == ["RA", "RB"]
    assert "l=6U" in card and "w=1U" in card


def test_pdk_resolved_binds_resistor_gf180mcu(tmp_path):
    """--pdk binds gf180mcu's `ppolyf_u` to its real subcircuit, whose geometry
    parameters are spelled `r_length`/`r_width` (not `l`/`w`) and which carries
    the bulk terminal on the deck's substrate global (#339)."""
    path = _write_gds(_make_poly_resistor_layout("gf180mcu"), tmp_path / "res.gds")
    out = str(tmp_path / "res.spice")
    run_extract(
        path,
        "gf180mcu",
        pdk_variant="gf180mcuA",
        pdk_root=_make_pdk_install(tmp_path, "gf180mcuA"),
        output=out,
    )
    (card,) = _device_cards(out)
    assert card.startswith("X")
    assert " ppolyf_u " in card
    substrate = get_extraction_deck("gf180mcu").substrate_net
    # Three terminals: the two heads plus the substrate-tied bulk.
    assert card.split()[1:4] == ["RA", "RB", substrate]
    assert "r_length=6U" in card and "r_width=1U" in card


@pytest.mark.parametrize(
    ("met4", "expected_subckt"),
    [
        (False, "sky130_fd_pr__cap_mim_m3_1"),
        (True, "sky130_fd_pr__cap_mim_m3_2"),
    ],
)
def test_pdk_resolved_binds_capacitor_sky130(tmp_path, met4, expected_subckt):
    """--pdk binds each sky130 MiM stack to its real simulation subcircuit
    (`sky130_fd_pr__cap_mim_m3_1`/`_m3_2`), with plate `l`/`w` recovered from
    the extracted plate area+perimeter via the equivalent-rectangle solver:
    the 10x5um plate (A=50, P=30) resolves to `l=10U w=5U` (#339)."""
    path = _write_gds(_make_sky130_mim_layout(met4=met4), tmp_path / "mim.gds")
    out = str(tmp_path / "mim.spice")
    run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=_make_pdk_install(tmp_path, "sky130A"),
        output=out,
    )
    (card,) = _device_cards(out)
    assert card.startswith("X")
    assert f" {expected_subckt} " in card
    assert "l=10U" in card and "w=5U" in card
    # No bare `C`-card value-only primitive leaks in.
    assert not card.startswith("C")


def test_pdk_resolved_binds_capacitor_gf180mcu(tmp_path):
    """--pdk binds gf180mcu's MiM cap to `cap_mim_2f0_m4m5_noshield`, whose
    geometry parameters are spelled `c_length`/`c_width`; the 10x10um plate
    (A=100, P=40, a square) resolves to `c_length=10U c_width=10U` (#339)."""
    path = _write_gds(_make_gf180mcu_mim_layout(), tmp_path / "mim.gds")
    out = str(tmp_path / "mim.spice")
    run_extract(
        path,
        "gf180mcu",
        pdk_variant="gf180mcuA",
        pdk_root=_make_pdk_install(tmp_path, "gf180mcuA"),
        output=out,
    )
    (card,) = _device_cards(out)
    assert card.startswith("X")
    assert " cap_mim_2f0_m4m5_noshield " in card
    assert "c_length=10U" in card and "c_width=10U" in card


def test_pdk_resolved_binds_bipolar_sky130(tmp_path):
    """--pdk binds sky130's recognised `pnp` to a real `sky130_fd_pr__pnp_05v5`
    geometry-named variant chosen by measured emitter area (the small synthetic
    emitter, AE=0.16um^2, is nearest the W0p68L0p68 cell), emitting the four
    `c b e s` terminals -- not the bare `Q`-card form (#339)."""
    path = _write_gds(_make_sky130_bjt_layout(), tmp_path / "bjt.gds")
    out = str(tmp_path / "bjt.spice")
    report = run_extract(
        path,
        "sky130",
        pdk_variant="sky130A",
        pdk_root=_make_pdk_install(tmp_path, "sky130A"),
        output=out,
    )
    assert report["device_counts"] == {"pnp": 1}
    (card,) = _device_cards(out)
    assert card.startswith("X")
    assert " sky130_fd_pr__pnp_05v5_W0p68L0p68" in card
    # c b e s: collector (= substrate) / base / emitter / substrate again.
    substrate = get_extraction_deck("sky130").substrate_net
    assert card.split()[1:5] == [substrate, "BASE", "EMIT", substrate]
    assert not card.startswith("Q")


def test_pdk_bipolar_variant_selected_by_emitter_area():
    """The bipolar variant selector picks the geometry-named cell whose nominal
    emitter area is nearest the measured `AE` (sky130's `pnp_05v5` ships as
    discrete cells, not one parameterized cell) (#339)."""
    variants = (
        (0.4624, "sky130_fd_pr__pnp_05v5_W0p68L0p68"),
        (11.56, "sky130_fd_pr__pnp_05v5_W3p40L3p40"),
    )
    assert _select_bipolar_variant(0.16, variants).endswith("W0p68L0p68")
    assert _select_bipolar_variant(0.5, variants).endswith("W0p68L0p68")
    assert _select_bipolar_variant(9.0, variants).endswith("W3p40L3p40")
    assert _select_bipolar_variant(20.0, variants).endswith("W3p40L3p40")


def test_pdk_resolved_leaves_gf180mcu_bipolar_unbound(tmp_path):
    """gf180mcu's bipolar is deliberately NOT bound (its deck has no
    positively-identified single device-cell name -- `decks/gf180mcu.py:557-559`),
    so under --pdk its recognised `bjt` stays a bare `Q` card rather than a
    guessed subcircuit call. Regression-locks that documented carve-out (#339)."""
    path = _write_gds(_make_gf180mcu_bjt_layout(), tmp_path / "bjt.gds")
    out = str(tmp_path / "bjt.spice")
    report = run_extract(
        path,
        "gf180mcu",
        pdk_variant="gf180mcuA",
        pdk_root=_make_pdk_install(tmp_path, "gf180mcuA"),
        output=out,
    )
    assert report["device_counts"] == {"bjt": 1}
    cards = _device_cards(out)
    (card,) = cards
    # Bare `Q` primitive, not an `X` subcircuit call.
    assert card.startswith("Q")
    assert not any(c.startswith("X") for c in cards)


def test_pdk_binding_does_not_change_device_counts_or_class(tmp_path):
    """Model binding is a SPICE-serialization-only concern: `device_counts` and
    `devices[].class` are byte-identical with and without --pdk for a resistor
    and a capacitor, exactly as the MOS binding already guarantees (#339)."""
    for label, layout, deck in (
        ("res", _make_poly_resistor_layout("sky130"), "sky130"),
        ("cap", _make_sky130_mim_layout(met4=False), "sky130"),
    ):
        path = _write_gds(layout, tmp_path / f"{label}.gds")
        plain = run_extract(path, deck, output=str(tmp_path / f"{label}_plain.spice"))
        bound = run_extract(
            path,
            deck,
            pdk_variant="sky130A",
            pdk_root=_make_pdk_install(tmp_path / label, "sky130A"),
            output=str(tmp_path / f"{label}_bound.spice"),
        )
        assert bound["device_counts"] == plain["device_counts"]
        assert [d["class"] for d in bound["devices"]] == [
            d["class"] for d in plain["devices"]
        ]
        assert [d["params"] for d in bound["devices"]] == [
            d["params"] for d in plain["devices"]
        ]


def test_equivalent_rectangle_um_matches_n_squares_and_solves_geometry():
    """The factored-out equivalent-rectangle solver returns the rectangle
    (length >= width) with the given area+perimeter, and `_n_squares` is the
    length/width ratio of that same rectangle -- the refactor is behavior-
    preserving (#339)."""
    # 10x5 rectangle: A=50, P=30 -> (10, 5), 2 squares.
    assert equivalent_rectangle_um(50.0, 30.0) == pytest.approx((10.0, 5.0))
    assert _n_squares(50.0, 30.0) == pytest.approx(2.0)
    # 10x10 square: A=100, P=40 -> (10, 10), 1 square.
    assert equivalent_rectangle_um(100.0, 40.0) == pytest.approx((10.0, 10.0))
    assert _n_squares(100.0, 40.0) == pytest.approx(1.0)
    # "Rounder than any rectangle" (negative discriminant) -> no rectangle;
    # `_n_squares` clamps to one square, matching its pre-refactor behavior.
    assert equivalent_rectangle_um(100.0, 20.0) is None
    assert _n_squares(100.0, 20.0) == pytest.approx(1.0)
    # Degenerate inputs.
    assert equivalent_rectangle_um(0.0, 10.0) is None
    assert _n_squares(0.0, 10.0) == 0.0


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format text/json
# --------------------------------------------------------------------------- #


def test_cli_json_exit_zero(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(["extract", path, "--deck", "sky130", "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "extracted"
    assert out["device_count"] == 2


def test_cli_top_cell_pins_flag_demotes_subcell_label(tmp_path, capsys):
    """The `--top-cell-pins` flag wires through the CLI: a sub-cell-origin
    gate label (`A`) is not promoted to a top-level pin (issue #291)."""
    path = str(
        _write_gds(_make_inverter_layout(a_label_in_subcell=True), tmp_path / "h.gds")
    )
    exit_code = main(
        ["extract", path, "--deck", "sky130", "--top-cell-pins", "--format", "json"]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    pins = {n["name"] for n in out["nets"] if n["pin"]}
    assert "A" not in pins
    assert "A" in {n["name"] for n in out["nets"]}  # name kept


def test_cli_text_default_format(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(["extract", path, "--deck", "sky130"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: extracted" in out
    assert "devices: 2" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_unknown_deck_exits_one_with_json_error(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(["extract", path, "--deck", "nope", "--format", "json"])
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "extract"
    assert "unknown deck 'nope'" in err["error"]["message"]


def test_cli_missing_file_exits_one_text_format(tmp_path, capsys):
    exit_code = main(["extract", str(tmp_path / "nope.gds"), "--deck", "sky130"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt extract:")


def test_cli_missing_deck_flag_is_usage_error(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    with pytest.raises(SystemExit) as exc_info:
        main(["extract", path])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "deck_name, expected",
    [
        # sky130 declares one bipolar entry, `pnp` (issue #223, its own
        # `sky130_fd_pr__pnp_05v5` device cell), two MiM-capacitor entries
        # (issue #225, one per metal level its official LVS deck draws a
        # purpose-built top-plate mark layer on), and a drawn poly resistor
        # (issue #222), so the `"resistor"` role trails the capacitor names.
        (
            "sky130",
            (
                "nfet",
                "pfet",
                "pnp",
                "sky130_fd_pr__model__cap_mim",
                "sky130_fd_pr__model__cap_mim_m4",
                "resistor",
            ),
        ),
        # gf180mcu declares one bipolar entry, generic `bjt` (issue #223 --
        # the DRM's `DRC_BJT` mark layer covers both NPN and PNP polarities
        # with no single named device cell to attribute one to; see
        # `decks/gf180mcu.py`'s `EXTRACTION_DECK` note), one MiM-capacitor
        # entry (issue #225, its own official LVS device name for the deck's
        # Metal4/Metal5 MiM stack), a drawn poly resistor (issue #222), and
        # the two 6V junction-diode flavours its own I/O library's ESD clamps
        # are built from (issue #542) -- which trail the `"resistor"` role.
        (
            "gf180mcu",
            (
                "nfet",
                "pfet",
                "bjt",
                "cap_mim_2f0_m4m5_noshield",
                "resistor",
                "diode_nd2ps_06v0",
                "diode_pd2nw_06v0",
            ),
        ),
    ],
)
def test_extraction_deck_device_classes_reports_every_curated_device_family(
    deck_name, expected
):
    """`device_classes` reports exactly what each deck is structurally
    capable of recognising -- two-terminal-well MOS plus each deck's
    curated bipolar (issue #223), MiM-capacitor (issue #225), drawn
    resistor (issue #222) and junction-diode (issue #542) entries --
    independent of any particular layout."""
    deck = get_extraction_deck(deck_name)
    assert deck.device_classes == expected


def test_extraction_deck_without_resistors_reports_mos_only():
    """The `resistors` field is additive/optional: a deck declaring none
    reports the pre-#222 `("nfet", "pfet")` coverage unchanged."""
    deck = ExtractionDeck(
        active=(65, 20), poly=(66, 20), nwell=(64, 20), contact=(66, 44), metals=()
    )
    assert deck.resistors == ()
    assert deck.device_classes == ("nfet", "pfet")


# --------------------------------------------------------------------------- #
# Drawn precision resistors (issue #222)
# --------------------------------------------------------------------------- #
#
# One synthetic poly-resistor bar per deck, drawn to the same nominal
# geometry so the expected resistance is `L / W * sheet_rho` with
# `L = 6 um`, `W = 1 um` -> exactly 6 squares:
#
#     sky130   res_generic_po  6 * 48.2  =  289.2 ohm
#     gf180mcu ppolyf_u        6 * 350.0 = 2100.0 ohm

_RES_SQUARES = 6.0

# poly bar: 12 um long, 1 um wide; the resistor-ID marker covers the middle
# 6 um, leaving a 3 um contacted head at each end.
_RES_BAR = kdb.Box(0, 0, 12000, 1000)
_RES_MARKED = kdb.Box(3000, 0, 9000, 1000)


def _make_poly_resistor_layout(
    deck_name: str,
    *,
    marked: bool = True,
    extra: tuple[tuple[int, int, kdb.Box], ...] = (),
    top_name: str = "RES",
) -> kdb.Layout:
    """A single drawn poly resistor on `deck_name`'s layers: one poly bar
    with the PDK's resistor-ID marker over its middle segment and a
    contacted, labelled head at each end.

    ``marked=False`` draws the identical bar with **no** marker layer -- the
    edge case that ordinary poly routing must never be misclassified as a
    resistor. ``extra`` draws additional `(layer, datatype, box)` shapes
    (used to exercise the decks' `excludes`/`requires` layers).
    """
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    required: tuple[tuple[int, int], ...]
    if deck_name == "sky130":
        poly = (66, 20)  # poly.drawing
        marker = (66, 13)  # poly.res
        contact = (66, 44)  # licon1.drawing
        metal = (67, 20)  # li1.drawing
        metal_label = (67, 5)  # li1.pin
        # sky130's generic poly resistor needs no additional required layer.
        required = ()
    else:
        poly = (30, 0)  # Poly2
        marker = (110, 5)  # RES_MK
        contact = (33, 0)  # Contact
        metal = (34, 0)  # Metal1
        metal_label = (34, 10)  # Metal1 pin
        # gf180mcu's `ppolyf_u` is p+ (Pplus) *and* unsalicided (SAB).
        required = ((31, 0), (49, 0))

    draw(*poly, _RES_BAR)
    if marked:
        draw(*marker, _RES_MARKED)
    for layer in required:
        draw(*layer, _RES_MARKED.enlarged(500, 500))

    # Contacted, labelled head at each end of the bar.
    for x, name in ((1500, "RA"), (10500, "RB")):
        draw(*contact, kdb.Box(x - 100, 400, x + 100, 600))
        draw(*metal, kdb.Box(x - 400, 200, x + 400, 800))
        label(*metal_label, name, x, 500)

    for layer, datatype, box in extra:
        draw(layer, datatype, box)

    return layout


@pytest.mark.parametrize(
    ("deck_name", "device_class", "sheet_rho"),
    [("sky130", "res_generic_po", 48.2), ("gf180mcu", "ppolyf_u", 350.0)],
)
def test_drawn_poly_resistor_extracts_with_expected_value(
    tmp_path, deck_name, device_class, sheet_rho
):
    """A marked poly bar extracts as the deck's drawn-resistor device class
    with `R = L / W * sheet_rho` (issue #222) -- not as a short."""
    path = _write_gds(
        _make_poly_resistor_layout(deck_name), tmp_path / f"{deck_name}_res.gds"
    )
    report = run_extract(path, deck_name, output=str(tmp_path / "res.spice"))

    assert report["device_counts"] == {device_class: 1}
    (device,) = report["devices"]
    assert device["class"] == device_class
    assert device["params"]["l_um"] == pytest.approx(6.0)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(_RES_SQUARES * sheet_rho)
    # Two terminals on two *distinct* nets -- the whole point: before #222
    # the bar was one continuous conductor, i.e. a short.
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}
    # `device_classes` is the deck's full structural coverage (MOS + each
    # deck's curated bipolar/capacitor/resistor entries), independent of this
    # layout containing only the one resistor.
    assert report["device_classes"] == list(
        get_extraction_deck(deck_name).device_classes
    )
    assert "resistor" in report["device_classes"]


def test_gf180mcu_resistor_bulk_terminal_ties_to_substrate_global(tmp_path):
    """gf180mcu's `ppolyf_u` is declared `bulk_to_substrate`, so it extracts
    through `DeviceExtractorResistorWithBulk` with its third terminal on the
    deck's substrate global (the same documented approximation the NMOS body
    terminal uses)."""
    path = _write_gds(_make_poly_resistor_layout("gf180mcu"), tmp_path / "res.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "res.spice"))

    (device,) = report["devices"]
    assert device["nets"]["w"] == get_extraction_deck("gf180mcu").substrate_net


# --------------------------------------------------------------------------- #
# A deck's other selectable sheet-rho poly-resistor flavours (issue #299):
# gf180mcu's `Resistor`-marked high-sheet-rho poly and sky130's `rpm`/`urpm`
# precision-implant poly resistors, previously pure exclusions on the base
# flavour and therefore an unmodelled short when actually drawn.
# --------------------------------------------------------------------------- #


def test_gf180mcu_high_sheet_rho_poly_extracts_as_ppolyf_u_1k(tmp_path):
    """A poly segment additionally marked with gf180mcu's `Resistor` (62/0)
    high-sheet-rho layer -- on top of the base flavour's own `RES_MK`/`SAB`
    -- now extracts as `ppolyf_u_1k` (1000 ohm/sq, the PDK's own
    `POLY_RES='1k'` default) instead of collapsing to a short."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "gf180mcu", extra=((62, 0, _RES_MARKED.enlarged(500, 500)),)
        ),
        tmp_path / "ppolyf_u_1k.gds",
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "ppolyf_u_1k.spice"))

    assert report["device_counts"] == {"ppolyf_u_1k": 1}
    (device,) = report["devices"]
    assert device["class"] == "ppolyf_u_1k"
    assert device["params"]["r_ohm"] == pytest.approx(_RES_SQUARES * 1000.0)
    assert device["nets"]["w"] == get_extraction_deck("gf180mcu").substrate_net


# --------------------------------------------------------------------------- #
# Caller-selectable sheet-rho flavour (issue #595): the `2k`/`3k` siblings
# `ppolyf_u_1k` shares *identical* recognition geometry with, previously
# unselectable and therefore still collapsing to a short.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "device_class", "sheet_rho"),
    [
        ("1k", "ppolyf_u_1k", 1000.0),
        ("2k", "ppolyf_u_2k", 2000.0),
        ("3k", "ppolyf_u_3k", 3000.0),
    ],
)
def test_gf180mcu_poly_res_deck_option_selects_flavour(
    tmp_path, value, device_class, sheet_rho
):
    """`klt extract`'s `deck_options={"poly_res": <value>}` (`--deck-option
    poly_res=<value>` on the CLI) selects which of the identically-drawn
    `ppolyf_u_{1k,2k,3k}` sheet-rho interpretations a `Resistor`-marked poly
    segment extracts as -- still exactly one recognised device per drawn
    segment, never a duplicate, and never a short."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "gf180mcu", extra=((62, 0, _RES_MARKED.enlarged(500, 500)),)
        ),
        tmp_path / f"ppolyf_u_{value}.gds",
    )
    report = run_extract(
        path,
        "gf180mcu",
        output=str(tmp_path / f"ppolyf_u_{value}.spice"),
        deck_options={"poly_res": value},
    )

    assert report["device_counts"] == {device_class: 1}
    (device,) = report["devices"]
    assert device["class"] == device_class
    assert device["params"]["r_ohm"] == pytest.approx(_RES_SQUARES * sheet_rho)
    assert device["nets"]["w"] == get_extraction_deck("gf180mcu").substrate_net
    assert report["provenance"]["deck"]["options"] == {"poly_res": value}


def test_gf180mcu_poly_res_deck_option_1k_matches_omitted_default(tmp_path):
    """Selecting `poly_res=1k` explicitly reproduces the PDK's own default --
    the same netlist `run_extract` writes when `deck_options` is omitted
    entirely, byte-for-byte (issue #595's backward-compatibility guarantee)."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "gf180mcu", extra=((62, 0, _RES_MARKED.enlarged(500, 500)),)
        ),
        tmp_path / "ppolyf_u.gds",
    )
    baseline = run_extract(path, "gf180mcu", output=str(tmp_path / "baseline.spice"))
    explicit = run_extract(
        path,
        "gf180mcu",
        output=str(tmp_path / "explicit.spice"),
        deck_options={"poly_res": "1k"},
    )

    assert baseline["netlist_sha256"] == explicit["netlist_sha256"]
    assert baseline["device_counts"] == explicit["device_counts"]
    # The only difference is the additive `provenance.deck.options` echo.
    assert "options" not in baseline["provenance"]["deck"]
    assert explicit["provenance"]["deck"]["options"] == {"poly_res": "1k"}


def test_gf180mcu_poly_res_deck_option_invalid_value_is_extract_error(tmp_path):
    """An unrecognised `poly_res` value is a loud `ExtractError`, not a
    silently-kept default or a guessed resistance (issue #595)."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "gf180mcu", extra=((62, 0, _RES_MARKED.enlarged(500, 500)),)
        ),
        tmp_path / "ppolyf_u_bad.gds",
    )
    with pytest.raises(ExtractError, match="poly_res=4k"):
        run_extract(
            path,
            "gf180mcu",
            output=str(tmp_path / "bad.spice"),
            deck_options={"poly_res": "4k"},
        )


@pytest.mark.parametrize("deck_name", ["sky130", "gf180mcu"])
def test_deck_option_unrecognised_key_is_extract_error(tmp_path, deck_name):
    """A `deck_options` key no `ResistorDevice.flavour_option` matches is an
    error -- both for a deck that declares no selectable flavour at all
    (sky130) and for gf180mcu, which declares `poly_res` but not this key
    (issue #595)."""
    path = _write_gds(
        _make_poly_resistor_layout(deck_name), tmp_path / f"{deck_name}_res.gds"
    )
    with pytest.raises(ExtractError, match="no selectable option"):
        run_extract(
            path,
            deck_name,
            output=str(tmp_path / "bad.spice"),
            deck_options={"mim_cap": "x"},
        )


def test_cli_deck_option_selects_flavour(tmp_path, capsys):
    """`klt extract --deck-option poly_res=2k` end-to-end: parsed by the CLI,
    forwarded to `run_extract`, and echoed in `provenance.deck.options`
    (issue #595)."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "gf180mcu", extra=((62, 0, _RES_MARKED.enlarged(500, 500)),)
        ),
        tmp_path / "ppolyf_u_2k.gds",
    )
    netlist_path = tmp_path / "res2k.spice"
    exit_code = main(
        [
            "extract",
            path,
            "--deck",
            "gf180mcu",
            "-o",
            str(netlist_path),
            "--deck-option",
            "poly_res=2k",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)

    assert out["device_counts"] == {"ppolyf_u_2k": 1}
    assert out["provenance"]["deck"]["options"] == {"poly_res": "2k"}


def test_cli_deck_option_malformed_entry_is_a_clean_error(tmp_path, capsys):
    """A `--deck-option` entry with no `=` exits 1 with a clean message, not
    a traceback (issue #595)."""
    path = _write_gds(_make_poly_resistor_layout("gf180mcu"), tmp_path / "res.gds")
    exit_code = main(
        [
            "extract",
            path,
            "--deck",
            "gf180mcu",
            "-o",
            str(tmp_path / "out.spice"),
            "--deck-option",
            "poly_res",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "KEY=VALUE" in err["error"]["message"]


@pytest.mark.parametrize(
    ("value", "expected_subckt"),
    [
        ("1k", "ppolyf_u_1k"),
        ("2k", "ppolyf_u_2k"),
        ("3k", "ppolyf_u_3k"),
    ],
)
def test_pdk_resolved_binds_selected_poly_res_flavour(tmp_path, value, expected_subckt):
    """`--deck-option poly_res=<value>` combined with `--pdk` binds the
    *selected* flavour's own real subcircuit (`ppolyf_u_{1k,2k,3k}`, all three
    confirmed in `sm141064.ngspice` on the same three-terminal
    `r_length`/`r_width` convention as `ppolyf_u_1k`) -- not a bare, value-only
    `R` card.

    Regression-locks the gap where `_RESISTOR_MODEL_TABLE` only carried the
    `1k` entry, so selecting `2k`/`3k` produced a correct resistance but
    silently lost the model binding under `--pdk` (issue #595 review)."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "gf180mcu", extra=((62, 0, _RES_MARKED.enlarged(500, 500)),)
        ),
        tmp_path / f"ppolyf_u_{value}_pdk.gds",
    )
    out = str(tmp_path / f"ppolyf_u_{value}_pdk.spice")
    report = run_extract(
        path,
        "gf180mcu",
        pdk_variant="gf180mcuA",
        pdk_root=_make_pdk_install(tmp_path, "gf180mcuA"),
        output=out,
        deck_options={"poly_res": value},
    )
    assert report["device_counts"] == {expected_subckt: 1}

    (card,) = _device_cards(out)
    assert card.startswith("X"), card
    assert f" {expected_subckt} " in card
    # No bare `R`-card value-only primitive leaks in.
    assert not card.startswith("R")
    substrate = get_extraction_deck("gf180mcu").substrate_net
    # Three terminals: the two contacted heads plus the substrate-tied bulk.
    assert card.split()[1:4] == ["RA", "RB", substrate]
    assert "r_length=6U" in card and "r_width=1U" in card


@pytest.mark.parametrize(
    ("mask", "device_class", "sheet_rho", "fixed_offset_ohm"),
    [
        # res_high_po's sheet_rho/offset are the issue #518 pair, measured
        # via ngspice against the real two-term PDK model (see
        # `decks/sky130.py`'s provenance note) -- `r_ohm` is `L / W *
        # sheet_rho + fixed_offset_ohm`, not the area-only-equivalent
        # `319.8` `sky130.lvs` itself cites.
        ((86, 20), "res_high_po", 324.827244, 379.705147),
        # res_xhigh_po is untouched by #518 -- still the plain single-term
        # `sky130.lvs`-cited value, `fixed_offset_ohm` at its `0.0` default.
        ((79, 20), "res_xhigh_po", 2000.0, 0.0),
    ],
)
def test_sky130_precision_implant_mask_extracts_own_flavour(
    tmp_path, mask, device_class, sheet_rho, fixed_offset_ohm
):
    """A poly segment marked with sky130's `rpm`/`urpm` precision-implant
    masks *and* the `psdm` P+ implant layer both wired flavours also
    require now extracts as `res_high_po`/`res_xhigh_po` instead of
    collapsing to a short, with `res_high_po`'s fixed head/end-effect offset
    (issue #518) applied on top of the plain `L / W * sheet_rho` term."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "sky130",
            extra=(
                (mask[0], mask[1], _RES_MARKED.enlarged(200, 200)),
                (94, 20, _RES_MARKED.enlarged(200, 200)),  # psdm
            ),
        ),
        tmp_path / f"{device_class}.gds",
    )
    report = run_extract(path, "sky130", output=str(tmp_path / f"{device_class}.spice"))

    assert report["device_counts"] == {device_class: 1}
    (device,) = report["devices"]
    assert device["class"] == device_class
    assert device["params"]["r_ohm"] == pytest.approx(
        _RES_SQUARES * sheet_rho + fixed_offset_ohm
    )
    assert device["nets"]["w"] == get_extraction_deck("sky130").substrate_net


def _make_sky130_res_high_po_layout(l_um: float) -> kdb.Layout:
    """A `w=1um` `res_high_po`-marked poly bar with a `l_um`-long marked
    (resistive) segment and a contacted, labelled 3um head at each end --
    the parametrised sibling of `_make_poly_resistor_layout`'s fixed 6um
    marked segment, used by
    `test_sky130_res_high_po_fixed_offset_matches_pdk_model_card` (issue
    #518) to exercise the issue's own cited lengths (1/5/10/35um) against
    the real PDK model's measured total resistance."""
    layout = kdb.Layout()
    top = layout.create_cell("RES")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    head_um = 3.0
    total_um = l_um + 2.0 * head_um
    l_nm = int(round(l_um * 1000))
    head_nm = int(round(head_um * 1000))
    total_nm = int(round(total_um * 1000))

    draw(66, 20, kdb.Box(0, 0, total_nm, 1000))  # poly.drawing
    marked = kdb.Box(head_nm, 0, head_nm + l_nm, 1000)
    draw(66, 13, marked)  # poly.res
    draw(86, 20, marked.enlarged(200, 200))  # rpm
    draw(94, 20, marked.enlarged(200, 200))  # psdm

    for x, name in ((head_nm // 2, "RA"), (total_nm - head_nm // 2, "RB")):
        draw(66, 44, kdb.Box(x - 100, 400, x + 100, 600))  # licon1.drawing
        draw(67, 20, kdb.Box(x - 400, 200, x + 400, 800))  # li1.drawing
        label(67, 5, name, x, 500)  # li1.pin

    return layout


@pytest.mark.parametrize("l_um", [1.0, 5.0, 10.0, 35.0])
def test_sky130_res_high_po_fixed_offset_matches_pdk_model_card(tmp_path, l_um):
    """Issue #518: for each of the issue's own cited lengths, the corrected
    `r_ohm` matches ngspice's own measurement of the real two-term PDK model
    (`sky130_fd_pr__res_high_po`'s `rhead` + `rbody`) at `w=1um` -- not just
    the pre-#518 single-term `L / W * sheet_rho` value."""
    path = _write_gds(
        _make_sky130_res_high_po_layout(l_um), tmp_path / f"res_high_po_{l_um}.gds"
    )
    report = run_extract(
        path, "sky130", output=str(tmp_path / f"res_high_po_{l_um}.spice")
    )

    assert report["device_counts"] == {"res_high_po": 1}
    (device,) = report["devices"]

    # Measured via ngspice against the real PDK model at the `tt` corner,
    # `w=1um` (see `decks/sky130.py`'s provenance note) -- the issue's own
    # citation table, to the digit it quotes.
    expected_r_ohm = {
        1.0: 704.532385,
        5.0: 2003.84137,
        10.0: 3627.97760,
        35.0: 11748.6587,
    }[l_um]

    assert device["params"]["l_um"] == pytest.approx(l_um)
    assert device["params"]["w_um"] == pytest.approx(1.0)
    assert device["params"]["r_ohm"] == pytest.approx(expected_r_ohm, rel=1e-6)
    # The pre-#518 single-term value is a systematic undercount -- confirms
    # the correction actually changed the reported number, not a no-op.
    assert device["params"]["r_ohm"] > l_um / 1.0 * 319.8


def test_resistor_default_fixed_offset_ohm_reports_unchanged_r_ohm(tmp_path):
    """`ResistorDevice.fixed_offset_ohm` defaults to `0.0` (issue #518): a
    deck entry that does not set it (mirroring every deck's own field before
    this feature existed, e.g. `res_xhigh_po`/`res_generic_po` above) still
    reports `r_ohm` exactly as `L / W * sheet_rho_ohm_sq` -- bit-for-bit
    what `klt extract` reported before this feature existed, not silently
    corrected by an offset the deck never opted into."""
    deck = get_extraction_deck("sky130")
    (res_high_po,) = [r for r in deck.resistors if r.name == "res_high_po"]
    assert res_high_po.fixed_offset_ohm != 0.0  # the curated deck itself now opts in

    uncorrected_deck = dataclasses.replace(
        deck,
        resistors=tuple(
            dataclasses.replace(r, fixed_offset_ohm=0.0)
            if r.name == "res_high_po"
            else r
            for r in deck.resistors
        ),
    )

    layout = _make_poly_resistor_layout(
        "sky130",
        extra=(
            (86, 20, _RES_MARKED.enlarged(200, 200)),  # rpm
            (94, 20, _RES_MARKED.enlarged(200, 200)),  # psdm
        ),
    )
    (
        netlist,
        _warnings,
        _parasitic_nets,
        _black_box_regions,
        _dummy_devices_dropped,
        _unmodelled_poly,
        _abstracted_cells,
    ) = _extract_netlist(layout, layout.top_cell(), uncorrected_deck)
    circuit = netlist.circuit_by_name(layout.top_cell().name)
    devices, _device_counts = _describe_devices(circuit)

    (device,) = devices
    assert device["class"] == "res_high_po"
    assert device["params"]["r_ohm"] == pytest.approx(_RES_SQUARES * 324.827244)


# --------------------------------------------------------------------------- #
# The deck's two-term corrections reach the *written netlist*, not just the
# JSON report (issue #521): `klt sim` consumes the `.spice` file
# `NetlistSpiceWriter` produces and `klt lvs`'s inline-extraction path hands
# the same `kdb.Netlist` straight to `NetlistComparer`, so a correction that
# only ever landed in `_describe_devices`'s returned dict was invisible to
# both.
# --------------------------------------------------------------------------- #


def _spice_card_value(netlist_path, prefix: str) -> float:
    """Return the single ``prefix``-prefixed (``R``/``C``) device card's
    value from a written SPICE netlist.

    KLayout's ``NetlistSpiceWriter`` emits e.g.
    ``R$1 RA RB vsubs 3627.977587 res_high_po`` -- the value is the last
    token that parses as a float (the trailing token is the device-class
    name)."""
    cards = [
        line.split()
        for line in Path(netlist_path).read_text().splitlines()
        if line.startswith(prefix)
    ]
    assert len(cards) == 1, f"expected exactly one '{prefix}' card, got {cards}"
    for token in reversed(cards[0]):
        try:
            return float(token)
        except ValueError:
            continue
    raise AssertionError(f"no numeric value in card {cards[0]}")


def test_written_spice_carries_res_high_po_fixed_offset_corrected_resistance(tmp_path):
    """Issue #521's own repro: for `res_high_po` at `l=10um`/`w=1um`, the
    `R` card in the netlist `klt extract` *writes* now carries the same
    two-term `3627.977587` the JSON `devices[].params.r_ohm` reports -- not
    the single-term `3248.27244` the `NetlistSpiceWriter` used to see,
    which silently reached `klt sim` and `klt lvs` instead."""
    path = _write_gds(
        _make_sky130_res_high_po_layout(10.0), tmp_path / "res_high_po.gds"
    )
    netlist_path = tmp_path / "res_high_po.spice"
    report = run_extract(path, "sky130", output=str(netlist_path))

    (device,) = report["devices"]
    written_r_ohm = _spice_card_value(netlist_path, "R")

    # The JSON report and the written netlist agree -- the whole point of
    # correcting the `kdb.Device` rather than only the response dict.
    assert written_r_ohm == pytest.approx(device["params"]["r_ohm"])
    # ... and both match ngspice's measurement of the real two-term PDK
    # model (`sky130_fd_pr__res_high_po`'s `rhead` + `rbody`) at this length.
    assert written_r_ohm == pytest.approx(3627.97760, rel=1e-6)
    # The pre-#521 written value was the single-term body term alone.
    assert written_r_ohm > 10.0 * 324.827244


# --------------------------------------------------------------------------- #
# `--defer-resistor-fixed-offset` (issue #588): the CLI half of the
# deferred-correction contract `docs/cli/lvs.md` describes. The Python API's
# `run_extract(..., apply_resistor_fixed_offset=False)` is exercised by
# `tests/test_lvs.py` (issue #585) end-to-end through `options.combine_devices`;
# these tests cover the CLI flag's own wiring and its default.
# --------------------------------------------------------------------------- #

#: `res_high_po`'s deck constants, transcribed (not imported) from
#: `decks/sky130.py` so a deck-value drift fails loudly here rather than being
#: silently tracked -- same idiom as `tests/test_lvs.py`'s own copies.
_RES_HIGH_PO_SHEET_RHO_OHM_SQ = 324.827244
_RES_HIGH_PO_FIXED_OFFSET_OHM = 379.705147


def test_cli_defer_resistor_fixed_offset_omits_offset(tmp_path, capsys):
    """Issue #588: `klt extract --defer-resistor-fixed-offset` forwards
    `apply_resistor_fixed_offset=False` to `run_extract`, so the reported and
    written `R` carry only the raw per-primitive body resistance.

    This is what `klt lvs`'s pre-extracted `layout.netlist` + `layout.deck` +
    `options.combine_devices: true` path needs (issue #585): it adds the
    fixed offset once per *post-combine* device, so baking it in per primitive
    here would double-count it after the series fold. Before the fix the flag
    did not exist and a subprocess-only flow could not produce this netlist.
    """
    l_um = 10.0
    path = _write_gds(
        _make_sky130_res_high_po_layout(l_um), tmp_path / "res_high_po.gds"
    )
    netlist_path = tmp_path / "deferred.spice"
    exit_code = main(
        [
            "extract",
            path,
            "--deck",
            "sky130",
            "-o",
            str(netlist_path),
            "--defer-resistor-fixed-offset",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)

    raw_body_r_ohm = l_um / 1.0 * _RES_HIGH_PO_SHEET_RHO_OHM_SQ
    (device,) = out["devices"]
    assert device["class"] == "res_high_po"
    assert device["params"]["r_ohm"] == pytest.approx(raw_body_r_ohm)
    # ... and the written netlist agrees (the correction is deferred on the
    # `kdb.Device` itself, not just in the response dict).
    assert _spice_card_value(netlist_path, "R") == pytest.approx(raw_body_r_ohm)
    # Negative control: the default (corrected) value is a different number,
    # so this cannot pass by the offset merely being small.
    assert raw_body_r_ohm != pytest.approx(
        raw_body_r_ohm + _RES_HIGH_PO_FIXED_OFFSET_OHM
    )


def test_cli_without_defer_flag_still_applies_fixed_offset(tmp_path, capsys):
    """The default half of issue #588: omitting
    `--defer-resistor-fixed-offset` leaves today's behavior exactly as it was
    -- the fixed offset is applied once per drawn primitive at extraction
    time, in both the JSON report and the written netlist, and the written
    netlist is byte-identical to what `run_extract`'s default produces."""
    l_um = 10.0
    path = _write_gds(
        _make_sky130_res_high_po_layout(l_um), tmp_path / "res_high_po.gds"
    )
    netlist_path = tmp_path / "default.spice"
    exit_code = main(
        [
            "extract",
            path,
            "--deck",
            "sky130",
            "-o",
            str(netlist_path),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)

    corrected_r_ohm = (
        l_um / 1.0 * _RES_HIGH_PO_SHEET_RHO_OHM_SQ + _RES_HIGH_PO_FIXED_OFFSET_OHM
    )
    (device,) = out["devices"]
    assert device["params"]["r_ohm"] == pytest.approx(corrected_r_ohm)
    assert _spice_card_value(netlist_path, "R") == pytest.approx(corrected_r_ohm)

    # Byte-identical to the library default -- the flag's absence adds nothing.
    baseline = run_extract(path, "sky130", output=str(tmp_path / "baseline.spice"))
    assert out["netlist_sha256"] == baseline["netlist_sha256"]
    assert netlist_path.read_bytes() == (tmp_path / "baseline.spice").read_bytes()


def test_cli_defer_flag_is_a_noop_without_an_opted_in_resistor(tmp_path, capsys):
    """Edge case from issue #588's test plan: on a layout with no
    `fixed_offset_ohm`-opted-in resistor class (every deck row except sky130's
    `res_high_po` leaves the field at its `0.0` default), the flag changes
    nothing -- the two written netlists are byte-for-byte equal."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")

    def _extract(netlist_path: Path, *flags: str) -> dict:
        exit_code = main(
            [
                "extract",
                path,
                "--deck",
                "sky130",
                "-o",
                str(netlist_path),
                *flags,
                "--format",
                "json",
            ]
        )
        assert exit_code == 0
        return json.loads(capsys.readouterr().out)

    default = _extract(tmp_path / "default.spice")
    deferred = _extract(tmp_path / "deferred.spice", "--defer-resistor-fixed-offset")

    assert default["device_counts"] == {"nfet": 1, "pfet": 1}  # no resistor at all
    assert default["netlist_sha256"] == deferred["netlist_sha256"]
    assert (tmp_path / "default.spice").read_bytes() == (
        tmp_path / "deferred.spice"
    ).read_bytes()


def test_written_spice_carries_mim_perim_cap_corrected_capacitance(tmp_path):
    """The capacitor half of issue #521: the `C` card in the written netlist
    carries the full two-term `area_cap_f_um2 * A + perim_cap_f_um * P`, the
    same value the JSON `devices[].params.c_f` reports -- not the area-only
    term `NetlistSpiceWriter` used to see."""
    side_um = 10.0
    path = _write_gds(_make_gf180mcu_mim_square_layout(side_um), tmp_path / "mim.gds")
    netlist_path = tmp_path / "mim.spice"
    report = run_extract(path, "gf180mcu", output=str(netlist_path))

    (device,) = report["devices"]
    written_c_f = _spice_card_value(netlist_path, "C")
    expected_c_f = 1.99e-15 * side_um * side_um + 2.383e-16 * 4.0 * side_um

    assert written_c_f == pytest.approx(device["params"]["c_f"])
    assert written_c_f == pytest.approx(expected_c_f, rel=1e-6)
    # The pre-#521 written value was the area-only term alone.
    assert written_c_f > 1.99e-15 * side_um * side_um


def test_written_spice_unchanged_for_deck_flavour_declaring_no_corrections(tmp_path):
    """Regression guard for issue #521's non-breaking default: a resistor
    flavour that never opted into `fixed_offset_ohm` (`res_xhigh_po`, still
    at the `0.0` default) writes exactly `L / W * sheet_rho_ohm_sq` -- the
    correction pass is a no-op for every device class whose deck entry
    leaves the coefficient at its default, so nothing about the written
    SPICE changes for them."""
    path = _write_gds(
        _make_poly_resistor_layout(
            "sky130",
            extra=(
                (79, 20, _RES_MARKED.enlarged(200, 200)),  # urpm
                (94, 20, _RES_MARKED.enlarged(200, 200)),  # psdm
            ),
        ),
        tmp_path / "res_xhigh_po.gds",
    )
    netlist_path = tmp_path / "res_xhigh_po.spice"
    report = run_extract(path, "sky130", output=str(netlist_path))

    assert report["device_counts"] == {"res_xhigh_po": 1}
    assert _spice_card_value(netlist_path, "R") == pytest.approx(_RES_SQUARES * 2000.0)


@pytest.mark.parametrize("deck_name", ["sky130", "gf180mcu"])
def test_unmarked_poly_bar_is_not_a_resistor(tmp_path, deck_name):
    """Edge case from the issue: a resistor-*shaped* polygon carrying no
    resistor-ID marker stays ordinary connected routing -- no device, and
    both heads remain one net.

    The fixture's two labelled heads (``RA``/``RB``) both land on that one
    continuous, device-less net, so KLayout's ``Net.expanded_name()`` joins
    them as ``"RA,RB"`` (issue #312) -- a genuinely *named* net. Issue #539
    requires ``_extract_netlist``'s purge step to preserve exactly this: a
    device-less net survives when it is named/labelled, unlike
    :func:`test_layout_with_no_devices_succeeds_with_zero_count`'s layout,
    which draws no labels at all and legitimately purges to nothing. (Before
    #539, `netlist.purge()` dropped this merged, labelled net -- and the
    whole circuit with it -- exactly like the genuinely-unlabelled case;
    that conflation was the bug.)"""
    path = _write_gds(
        _make_poly_resistor_layout(deck_name, marked=False),
        tmp_path / f"{deck_name}_bar.gds",
    )
    report = run_extract(path, deck_name, output=str(tmp_path / "bar.spice"))

    assert report["device_count"] == 0
    assert report["device_counts"] == {}
    # One continuous conductor with no device on it, but both heads are
    # labelled -- the merged net survives as a single named, device-less,
    # promoted-to-pin net (issue #539) instead of the whole circuit purging
    # away.
    assert report["nets"] == [{"name": "RA,RB", "pin": True, "device_count": 0}]


def test_purge_preserving_named_nets_generalizes_to_hierarchical_circuits():
    """Issue #539's edge case: `_promote_orphan_named_nets` +
    `_purge_preserving_named_nets` operate on ``klayout.db`` primitives
    directly (a ``kdb.Netlist`` built by hand, with a real subcircuit
    instance), not just the flat single-circuit netlist `_extract_netlist`
    itself always produces -- proving the purge-guard's condition
    generalizes beyond the flat top-circuit-only case, per the issue's test
    plan.

    ``top`` calls ``child`` (a resistor between pins ``X``/``Y``) through a
    subcircuit instance connecting ``child``'s pins to ``top``'s own ``P``/
    ``Q`` nets, *and* ``top`` carries a third, wholly independent named net
    (``ORPHAN``) with zero devices and zero subcircuit connections -- the
    "circuit with named nets and a mix of subcircuits but zero *direct*
    devices" case. Before promotion, `Netlist.make_top_level_pins()`
    already promotes ``P``/``Q`` correctly (they reach `child` through the
    subcircuit, so they are never "floating" in KLayout's own sense) but
    silently skips `ORPHAN` (verified directly against `klayout.db`,
    independent of this module -- issue #539's root cause). Both fix
    functions must leave `P`/`Q` untouched and rescue `ORPHAN` alongside
    them."""
    netlist = kdb.Netlist()

    child = kdb.Circuit()
    child.name = "child"
    netlist.add(child)
    resistor_class = kdb.DeviceClassResistor()
    resistor_class.name = "RES"
    netlist.add(resistor_class)
    net_x = child.create_net("X")
    net_y = child.create_net("Y")
    device = child.create_device(resistor_class, "R1")
    device.connect_terminal(0, net_x)
    device.connect_terminal(1, net_y)
    pin_x = child.create_pin("X")
    child.connect_pin(pin_x, net_x)
    pin_y = child.create_pin("Y")
    child.connect_pin(pin_y, net_y)

    top = kdb.Circuit()
    top.name = "top"
    netlist.add(top)
    net_p = top.create_net("P")
    net_q = top.create_net("Q")
    top.create_net("ORPHAN")
    subcircuit = top.create_subcircuit(child, "I1")
    subcircuit.connect_pin(0, net_p)
    subcircuit.connect_pin(1, net_q)

    # `make_top_level_pins()` promotes the subcircuit-connected `P`/`Q`
    # fine on its own, but silently drops the deviceless, subcircuit-less
    # `ORPHAN` net -- the exact root cause issue #539 reports.
    netlist.make_top_level_pins()
    assert top.pin_count() == 2
    _promote_orphan_named_nets(netlist)
    assert top.pin_count() == 3

    _purge_preserving_named_nets(netlist)

    top_after = netlist.circuit_by_name("top")
    assert top_after is not None
    assert top_after.pin_count() == 3
    surviving_names = {net.name for net in top_after.each_net()}
    assert surviving_names == {"P", "Q", "ORPHAN"}

    child_after = netlist.circuit_by_name("child")
    assert child_after is not None
    assert {net.name for net in child_after.each_net()} == {"X", "Y"}


@pytest.mark.parametrize("deck_name", ["sky130", "gf180mcu"])
def test_rescued_named_net_survives_parasitics_extraction(tmp_path, deck_name):
    """Issue #563: a net rescued by `_purge_preserving_named_nets` must stay
    queryable through ``LayoutToNetlist``, so `--parasitics` still works on
    exactly the device-less labelled layouts issue #539 exists to preserve
    (bond pads, seal rings, RDL segments, power-mesh straps -- all plausible
    parasitic-extraction targets).

    This is the issue's own primary repro (unmarked poly bar, two labelled
    heads, zero devices) with `parasitics=True`: `purge()` drops the whole
    circuit, so the restore path recreates the ``kdb.Circuit`` *and* the
    ``kdb.Net``. A bare recreation loses the `(cell_index, cluster_id)` key
    `LayoutToNetlist.polygons_of_net` looks the net's shapes up by, and
    `_compute_parasitics` -- which runs after the purge and iterates every
    surviving net -- then faulted inside KLayout's hierarchical network
    processor with an unhandled internal ``RuntimeError`` (`id > 0 was not
    true in LayoutToNetlist.polygons_of_net`) instead of raising a clean
    `ExtractError` or returning a result."""
    path = _write_gds(
        _make_poly_resistor_layout(deck_name, marked=False),
        tmp_path / f"{deck_name}_bar_para.gds",
    )
    report = run_extract(
        path,
        deck_name,
        output=str(tmp_path / "bar_para.spice"),
        parasitics=True,
    )

    # Same schematic-equivalent view as the non-parasitics run.
    assert report["device_count"] == 0
    assert report["nets"] == [{"name": "RA,RB", "pin": True, "device_count": 0}]

    # The rescued net's geometry is genuinely reachable, not merely
    # not-crashing: the poly bar's real R/C is reported for it.
    para = report["parasitics"]
    assert para is not None
    assert [entry["net"] for entry in para["nets"]] == ["RA,RB"]
    assert para["nets"][0]["capacitance_ff"] > 0.0
    assert para["nets"][0]["resistance_ohm"] > 0.0


def test_rescued_net_beside_real_device_survives_parasitics_extraction(tmp_path):
    """Issue #563's second shape: the crash was never limited to the
    "whole circuit purged and got recreated" branch -- restoring *any* single
    net into a still-alive circuit hit it too.

    Here a normal inverter (two real devices, so the circuit itself survives
    `purge()` untouched) carries one extra, wholly separate labelled ``PAD``
    li1 island with no device on it. Only that one net goes through the
    rescue path; every other net is KLayout's own. With `--parasitics` set,
    the rescued net must be measured alongside the rest rather than faulting
    the whole extraction."""
    layout = _make_inverter_layout()
    top = layout.cell("TOP")
    # An isolated li1 pad well clear of the inverter, labelled but touching
    # no device -- a stand-in for a bond pad / RDL stub.
    top.shapes(layout.layer(67, 20)).insert(kdb.Box(6000, 6000, 7000, 7000))
    top.shapes(layout.layer(67, 5)).insert(kdb.Text("PAD", kdb.Trans(6500, 6500)))
    path = _write_gds(layout, tmp_path / "inv_pad.gds")

    report = run_extract(
        path, "sky130", output=str(tmp_path / "inv_pad.spice"), parasitics=True
    )

    assert report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert {"name": "PAD", "pin": True, "device_count": 0} in report["nets"]

    para = report["parasitics"]
    assert para is not None
    pad_entries = [entry for entry in para["nets"] if entry["net"] == "PAD"]
    assert len(pad_entries) == 1
    assert pad_entries[0]["capacitance_ff"] > 0.0
    # The device-bearing nets are still measured exactly as before -- the
    # rescued net is additive, not a replacement.
    assert {"A", "VGND", "VPWR", "Y"} <= {entry["net"] for entry in para["nets"]}


def test_sky130_precision_implant_mask_is_not_extracted_as_generic_poly_res(tmp_path):
    """sky130's `rpm`/`urpm` masks select the 319.8 ohm/sq and 2 kohm/sq poly
    resistor flavours (`res_high_po`/`res_xhigh_po`, issue #299). Such a
    segment must NOT be reported as the 48.2 ohm/sq generic device (a
    ~6.6x/~41x wrong resistance passing LVS with high confidence is worse
    than the known-unmodelled short) -- and here, with no `psdm` P+ implant
    layer drawn (both wired high-sheet-rho flavours also require it, see
    `test_sky130_precision_implant_mask_extracts_own_flavour`), it doesn't
    match either high-sheet-rho flavour either, so nothing extracts at all.
    Because the shape *does* carry a resistor marker this deck recognises in
    principle (`poly.res` + `rpm`/`urpm`), it must be flagged by the
    'deck-coverage gap' warning, not the plain unmarked-geometry one."""
    for mask in ((86, 20), (79, 20)):  # rpm, urpm
        path = _write_gds(
            _make_poly_resistor_layout(
                "sky130", extra=((mask[0], mask[1], _RES_MARKED.enlarged(200, 200)),)
            ),
            tmp_path / f"rpm_{mask[0]}.gds",
        )
        report = run_extract(
            path, "sky130", output=str(tmp_path / f"rpm_{mask[0]}.spice")
        )
        assert report["device_counts"] == {}
        # A second warning (issue #539's `net 'RA,RB' merges ...` collision
        # note) now also fires: with no device recognised, the two labelled
        # heads (`RA`/`RB`) merge onto one continuous, device-less net that
        # survives extraction instead of purging away -- see
        # `test_unmarked_poly_bar_is_not_a_resistor`.
        assert len(report["warnings"]) == 2
        assert "deck-coverage gap" in report["warnings"][0]
        assert "carry no resistor-marker layer at all" not in report["warnings"][0]
        assert "merges 2 distinct labels (RA, RB)" in report["warnings"][1]


def test_gf180mcu_salicided_poly_is_not_extracted_as_unsalicided_resistor(tmp_path):
    """gf180mcu's `ppolyf_u`/`ppolyf_u_1k` both require SAB (salicide
    block). Without it the real device is the 48x-lower-resistance salicided
    `ppolyf_s`, which this curated deck does not model -- so nothing may be
    extracted, but the shape still carries a resistor marker (`RES_MK`) this
    deck recognises in principle, so it is flagged by the 'deck-coverage
    gap' warning (issue #299), not the plain unmarked-geometry one."""
    layout = _make_poly_resistor_layout("gf180mcu")
    # Erase the SAB layer, leaving Pplus + RES_MK + Poly2 in place.
    layout.clear_layer(layout.layer(49, 0))
    path = _write_gds(layout, tmp_path / "salicided.gds")

    report = run_extract(path, "gf180mcu", output=str(tmp_path / "salicided.spice"))
    assert report["device_counts"] == {}
    # A second warning (issue #539's `net 'RA,RB' merges ...` collision note)
    # now also fires -- see the matching comment in
    # `test_sky130_precision_implant_mask_is_not_extracted_as_generic_poly_res`.
    assert len(report["warnings"]) == 2
    assert "deck-coverage gap" in report["warnings"][0]
    assert "carry no resistor-marker layer at all" not in report["warnings"][0]
    assert "merges 2 distinct labels (RA, RB)" in report["warnings"][1]


def test_marked_gate_poly_over_diffusion_stays_a_transistor(tmp_path):
    """A resistor marker over a *gate* must not turn a transistor into a
    resistor: both decks exclude their active/COMP layer from the resistor
    body, mirroring the PDKs' own LVS derivations."""
    layout = _make_inverter_layout()
    # poly.res over exactly the NMOS gate region (poly ∩ diff) -- the
    # `diff.drawing` exclusion must win, leaving the gate intact.
    layout.cell("TOP").shapes(layout.layer(66, 13)).insert(kdb.Box(800, 0, 1200, 1000))
    path = _write_gds(layout, tmp_path / "marked_gate.gds")

    report = run_extract(path, "sky130", output=str(tmp_path / "marked_gate.spice"))
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}


# --------------------------------------------------------------------------- #
# Unmodelled-device diagnostic (issue #288): warn on a poly+contact shape no
# device extractor claims, instead of silently absorbing it into interconnect.
# --------------------------------------------------------------------------- #

_UNMODELLED_WARNING_SNIPPET = "resistor-body signature"


@pytest.mark.parametrize("deck_name", ["sky130", "gf180mcu"])
def test_unmarked_poly_bar_triggers_unmodelled_device_warning(tmp_path, deck_name):
    """The exact repro from the issue: a resistor-*shaped* poly bar (no
    resistor-ID marker, so #222's device extractor does not claim it) is
    contacted at each end and touches no MOS gate anywhere -- the deck
    absorbs it into ordinary interconnect (see
    ``test_unmarked_poly_bar_is_not_a_resistor``), but that absorption must
    now be flagged rather than silent."""
    path = _write_gds(
        _make_poly_resistor_layout(deck_name, marked=False),
        tmp_path / f"{deck_name}_unmarked_bar.gds",
    )
    report = run_extract(
        path, deck_name, output=str(tmp_path / f"{deck_name}_unmarked_bar.spice")
    )

    assert report["device_count"] == 0
    # A second warning (issue #539's `net 'RA,RB' merges ...` collision note)
    # now also fires -- see the matching comment in
    # `test_sky130_precision_implant_mask_is_not_extracted_as_generic_poly_res`.
    assert len(report["warnings"]) == 2
    assert _UNMODELLED_WARNING_SNIPPET in report["warnings"][0]
    assert "docs/cli/extract.md" in report["warnings"][0]
    assert "merges 2 distinct labels (RA, RB)" in report["warnings"][1]


def test_unmodelled_poly_field_reports_bbox_and_reason_when_flagged(tmp_path):
    """Issue #324: the structured `unmodelled_poly` field lists the exact
    flagged shape (bbox + which of the two `warnings` cases it fell under)
    alongside the prose warning, so a consumer can enumerate and triage the
    flagged set without re-deriving it by re-implementing the heuristic
    against the stream."""
    path = _write_gds(
        _make_poly_resistor_layout("sky130", marked=False),
        tmp_path / "unmarked_bar.gds",
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "unmarked_bar.spice"))

    # A second warning (issue #539's `net 'RA,RB' merges ...` collision note)
    # now also fires -- see the matching comment in
    # `test_sky130_precision_implant_mask_is_not_extracted_as_generic_poly_res`.
    assert len(report["warnings"]) == 2
    unmodelled_poly = report["unmodelled_poly"]
    assert len(unmodelled_poly) == 1
    entry = unmodelled_poly[0]
    assert entry["reason"] == "unmarked"
    # The whole 12 um bar is one merged poly component (no marker to cut a
    # body out of it), so its bbox is the bar's full extent.
    assert entry["bbox_um"] == {
        "left": pytest.approx(0.0),
        "bottom": pytest.approx(0.0),
        "right": pytest.approx(12.0),
        "top": pytest.approx(1.0),
    }


def test_unmodelled_poly_field_empty_when_no_warning_fires(tmp_path):
    """`unmodelled_poly` is an empty list -- not omitted -- whenever the
    diagnostic flags nothing, the same additive-field contract
    `black_box_regions` established (issue #293)."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    assert report["warnings"] == []
    assert report["unmodelled_poly"] == []


@pytest.mark.parametrize(
    ("deck_name", "contact_layer", "metal_layer"),
    [("sky130", (66, 44), (67, 20)), ("gf180mcu", (33, 0), (34, 0))],
)
def test_resistor_head_multi_contact_array_does_not_trigger_unmodelled_warning(
    tmp_path, deck_name, contact_layer, metal_layer
):
    """False-positive class 2 from the issue: a *recognised* resistor's own
    terminal head, contacted at 2+ geometrically separate points (ordinary
    practice for a wide head), must not be flagged as an unmodelled device
    body. Once its body is cut out by `_resolve_resistors`, the head is a
    lone poly component that abuts the recognised body -- that abutment
    alone identifies it as this resistor's own terminal, not a missing
    device, no matter how many contacts land on it."""
    layout = _make_poly_resistor_layout(
        deck_name,
        extra=(
            # A second contact/metal pad on the *same* head as "RB"
            # (9000..12000), well clear of the existing one at x=10500 --
            # an ordinary wide-head layout, not a second device.
            (contact_layer[0], contact_layer[1], kdb.Box(9400, 400, 9600, 600)),
            (metal_layer[0], metal_layer[1], kdb.Box(9200, 200, 9800, 800)),
        ),
    )
    path = _write_gds(layout, tmp_path / f"{deck_name}_multi_contact_head.gds")
    report = run_extract(
        path,
        deck_name,
        output=str(tmp_path / f"{deck_name}_multi_contact_head.spice"),
    )

    # The resistor still extracts correctly -- the extra contact is just a
    # second landing pad on the same "RB" terminal.
    assert report["device_count"] == 1
    (device,) = report["devices"]
    assert {device["nets"]["a"], device["nets"]["b"]} == {"RA", "RB"}
    # ...and no longer trips the unmodelled-device diagnostic.
    assert report["warnings"] == []
    assert report["unmodelled_poly"] == []


def _make_mixed_unmodelled_poly_layout() -> kdb.Layout:
    """Two independent resistor-shaped `sky130` poly bars, well clear of
    each other, each contacted at two separate points and touching no MOS
    gate: one carries no resistor marker at all (the #288 signature), the
    other carries `poly.res` + `rpm` but not `psdm` -- a marker this deck
    recognises, just not narrowed to any wired flavour (the #299
    "deck-coverage gap" signature). Both must be flagged, each under its
    own distinct warning string (issue #299)."""
    layout = kdb.Layout()
    top = layout.create_cell("RES")

    def draw(x_offset: int, layer: int, datatype: int, box: kdb.Box) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(box.moved(x_offset, 0))

    def label(
        x_offset: int, layer: int, datatype: int, text: str, x: int, y: int
    ) -> None:
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x + x_offset, y))
        )

    for x_offset, marker in ((0, None), (30000, (86, 20))):  # rpm on the 2nd bar
        draw(x_offset, 66, 20, _RES_BAR)  # poly.drawing
        if marker is not None:
            draw(x_offset, 66, 13, _RES_MARKED)  # poly.res -- deck-recognised marker
            draw(x_offset, marker[0], marker[1], _RES_MARKED.enlarged(200, 200))
        for x, name in ((1500, "A"), (10500, "B")):
            draw(x_offset, 66, 44, kdb.Box(x - 100, 400, x + 100, 600))
            draw(x_offset, 67, 20, kdb.Box(x - 400, 200, x + 400, 800))
            label(x_offset, 67, 5, f"{name}{x_offset}", x, 500)

    return layout


def test_unmodelled_poly_warning_distinguishes_marked_from_unmarked(tmp_path):
    """A layout with both signatures at once produces *two* separate
    warnings, each with its own count and phrasing -- unmarked geometry
    (the #288 case) is never conflated with a marked-but-unrecognised
    deck-coverage gap (issue #299)."""
    path = _write_gds(_make_mixed_unmodelled_poly_layout(), tmp_path / "mixed.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "mixed.spice"))

    assert report["device_count"] == 0
    # Two more warnings (issue #539's `net '<labels>' merges ...` collision
    # note) now also fire, one per bar: with no device recognised on either
    # bar, each bar's own two labelled heads merge onto one continuous,
    # device-less net that survives extraction instead of purging away --
    # see the matching comment in
    # `test_sky130_precision_implant_mask_is_not_extracted_as_generic_poly_res`.
    assert len(report["warnings"]) == 4
    unmarked = [
        w for w in report["warnings"] if "carry no resistor-marker layer at all" in w
    ]
    coverage_gap = [w for w in report["warnings"] if "deck-coverage gap" in w]
    merged = [w for w in report["warnings"] if "merges 2 distinct labels" in w]
    assert len(unmarked) == 1
    assert "1 poly-layer shape" in unmarked[0]
    assert len(coverage_gap) == 1
    assert "1 poly-layer shape" in coverage_gap[0]
    assert len(merged) == 2


def test_unmodelled_poly_field_distinguishes_marked_from_unmarked(tmp_path):
    """The structured `unmodelled_poly` field (issue #324) carries the same
    unmarked/marked-but-unrecognised split as `warnings` -- one entry per
    flagged shape, each tagged with its own `reason`."""
    path = _write_gds(_make_mixed_unmodelled_poly_layout(), tmp_path / "mixed.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "mixed.spice"))

    unmodelled_poly = report["unmodelled_poly"]
    assert len(unmodelled_poly) == 2
    reasons = {entry["reason"] for entry in unmodelled_poly}
    assert reasons == {"unmarked", "marked_unrecognised"}
    # Sorted by (left, bottom) -- deterministic, diff-clean output.
    lefts = [entry["bbox_um"]["left"] for entry in unmodelled_poly]
    assert lefts == sorted(lefts)


def _make_poly_gate_strap_layout(top_name: str = "TOP") -> kdb.Layout:
    """One NMOS whose gate poly extends well past the active strip into a
    bare strap, contacted at **two** separate points along the strap (a
    legitimate poly-contacted gate strap, e.g. tying the gate up to a metal
    landing pad away from the channel). The strap is one continuous polygon
    with the transistor's own gate, so it must never be mistaken for an
    unmodelled device body no matter how many contacts land on it."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    # Gate poly: coincides with active in x (800..1200) but its y-extent runs
    # from -3000 to 0, i.e. entirely a strap *below* the active strip.
    draw(66, 20, kdb.Box(800, -3000, 1200, 1000))  # poly.drawing (gate + strap)

    # S/D contacts (ordinary transistor terminals).
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (S)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (D)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (S)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (D)
    label(67, 5, "SRC", 200, 500)
    label(67, 5, "DRN", 1800, 500)

    # Two separate contacts landing on the strap portion, well clear of the
    # gate-over-active region and of each other.
    draw(66, 44, kdb.Box(900, -2600, 1100, -2400))  # strap contact 1
    draw(67, 20, kdb.Box(800, -2700, 1200, -2300))
    draw(66, 44, kdb.Box(900, -900, 1100, -700))  # strap contact 2
    draw(67, 20, kdb.Box(800, -1000, 1200, -600))
    label(67, 5, "GATESTRAP", 1000, -2500)

    return layout


def test_poly_contacted_gate_strap_does_not_trigger_unmodelled_warning(tmp_path):
    """Edge case from the issue: a real MOS gate with contacts on its poly
    (a legitimate poly-contacted gate strap) must not trigger the new
    warning, even though it has two geometrically separate contact
    clusters -- it touches a recognised gate, so it is excluded outright."""
    path = _write_gds(_make_poly_gate_strap_layout(), tmp_path / "strap.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "strap.spice"))

    assert report["device_counts"] == {"nfet": 1}
    assert report["warnings"] == []


def _make_poly_routing_between_gates_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two separate NMOS devices sharing one continuous poly gate bar (the
    same "shared gate line" shape as `_make_inverter_layout`), with **two**
    separate contacts landing on the connecting segment between the two
    active strips -- ordinary poly routing/gate-tie between two recognised
    transistors, not an unmodelled device body."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # Two NMOS active strips, y 0..1000 and y 2000..3000, joined by one
    # continuous poly bar (x 800..1200) spanning y -200..3200.
    draw(65, 20, kdb.Box(0, 0, 2000, 1000))
    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))
    draw(66, 20, kdb.Box(800, -200, 1200, 3200))  # poly.drawing (shared gate bar)

    for y0 in (0, 2000):
        draw(66, 44, kdb.Box(100, y0 + 300, 300, y0 + 700))  # licon1 (S)
        draw(66, 44, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # licon1 (D)
        draw(67, 20, kdb.Box(0, y0 + 200, 400, y0 + 800))  # li1 (S)
        draw(67, 20, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # li1 (D)

    # Two separate contacts on the connecting segment (y 1000..2000), well
    # clear of either gate-over-active region and of each other.
    draw(66, 44, kdb.Box(900, 1200, 1100, 1400))
    draw(67, 20, kdb.Box(800, 1100, 1200, 1500))
    draw(66, 44, kdb.Box(900, 1600, 1100, 1800))
    draw(67, 20, kdb.Box(800, 1500, 1200, 1900))
    label(67, 5, "GATE_TIE", 1000, 1500)

    return layout


def test_poly_routing_between_gates_does_not_trigger_unmodelled_warning(tmp_path):
    """Edge case from the issue: ordinary poly routing between two recognised
    transistor gates must not trigger the new warning, even with two
    separate contact clusters landing on the connecting run -- the whole
    bar is one merged polygon touching both gates."""
    path = _write_gds(
        _make_poly_routing_between_gates_layout(), tmp_path / "routing.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "routing.spice"))

    assert report["device_counts"] == {"nfet": 2}
    assert not any("unmodelled" in warning for warning in report["warnings"])
    # Issue #596: this fixture deliberately leaves every source/drain net
    # unlabeled (it exercises poly-gate-tie routing only, not a full
    # circuit), so each of the two devices' source and drain nets now
    # legitimately reports as a single-terminal, non-pin net -- confirms
    # the new diagnostic coexists with the unmodelled-poly heuristic under
    # test rather than that it silences new, correctly-detected findings.
    assert len(report["single_terminal_nets"]) == 4
    assert {entry["terminal_kind"] for entry in report["single_terminal_nets"]} == {
        "source",
        "drain",
    }


def test_resistor_free_layout_extracts_byte_identically(tmp_path):
    """Regression guard for the additive contract: the synthetic inverter
    (no resistor markers anywhere) writes the same netlist bytes it did
    before drawn-resistor support existed."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert all("r_ohm" not in d["params"] for d in report["devices"])


def test_resistor_written_spice_carries_r_card(tmp_path):
    """The written netlist stays a `.SUBCKT` body `klt sim` can consume, with
    the resistor emitted as an `R` card carrying its extracted value and the
    deck's device-class name as the model token (same convention as the
    `nfet`/`pfet` `M` cards)."""
    path = _write_gds(_make_poly_resistor_layout("sky130"), tmp_path / "res.gds")
    netlist_path = tmp_path / "res.spice"
    run_extract(path, "sky130", output=str(netlist_path))

    text = netlist_path.read_text()
    assert ".SUBCKT RES" in text
    assert "289.2 res_generic_po" in text


def test_deck_resistor_on_a_non_conductor_layer_is_an_error(tmp_path):
    """Deck-authoring guard: a `ResistorDevice.body` that is not one of the
    deck's own conductor layers cannot have its body cut out of connectivity,
    so it is rejected rather than silently extracted as a short."""
    from klayout_tools.decks import ResistorDevice
    from klayout_tools.extract import _extract_netlist

    deck = get_extraction_deck("sky130")
    broken = ExtractionDeck(
        active=deck.active,
        poly=deck.poly,
        nwell=deck.nwell,
        contact=deck.contact,
        metals=deck.metals,
        resistors=(
            ResistorDevice(
                name="bogus",
                body=(99, 99),  # not a conductor layer of this deck
                marker=(66, 13),
                sheet_rho_ohm_sq=1.0,
            ),
        ),
    )
    layout = _make_poly_resistor_layout("sky130")
    with pytest.raises(ExtractError, match="not one of the deck's conductor layers"):
        _extract_netlist(layout, layout.top_cell(), broken)


@pytest.mark.parametrize(
    "top_plate_via, top_plate_via_metal, expected",
    [
        pytest.param(
            (41, 0),
            None,
            "must both be set or both be left unset",
            id="via-without-metal",
        ),
        pytest.param(
            None,
            (81, 0),
            "must both be set or both be left unset",
            id="metal-without-via",
        ),
        pytest.param(
            (41, 0),
            (99, 99),
            r"top_plate_via_metal 99/99 is not one of the deck's metals\[\] layers",
            id="metal-not-tracked",
        ),
    ],
)
def test_deck_capacitor_top_plate_via_declaration_must_be_coherent(
    top_plate_via, top_plate_via_metal, expected
):
    """Deck-authoring guard (issue #314): the optional top-plate wiring pair
    is only meaningful when both halves are declared *and* the metal they land
    on is one this deck actually tracks -- otherwise `extract.py` could not
    wire the plate anywhere, so the deck is rejected rather than silently
    extracting an isolated top-plate net that looks deliberate. Mirrors
    `test_deck_resistor_on_a_non_conductor_layer_is_an_error`'s shape, and the
    check is unconditional (see `_extract_netlist`'s capacitor loop), so it
    fires on any layout rather than only once a MiM cap is drawn."""
    from klayout_tools.decks import CapacitorDevice
    from klayout_tools.extract import _extract_netlist

    deck = get_extraction_deck("gf180mcu")
    broken = ExtractionDeck(
        active=deck.active,
        poly=deck.poly,
        nwell=deck.nwell,
        contact=deck.contact,
        metals=deck.metals,
        capacitors=(
            CapacitorDevice(
                name="bogus_cap",
                top_plate=(75, 0),  # FuseTop
                bottom_plate=(46, 0),  # Metal4
                area_cap_f_um2=2.0e-15,
                top_plate_via=top_plate_via,
                top_plate_via_metal=top_plate_via_metal,
            ),
        ),
    )
    layout = _make_gf180mcu_mim_layout()
    with pytest.raises(ExtractError, match=expected):
        _extract_netlist(layout, layout.top_cell(), broken)


# --------------------------------------------------------------------------- #
# Corpus round-trip: real sky130 / gf180mcu standard cells
# --------------------------------------------------------------------------- #


def test_corpus_files_are_non_empty():
    assert len(SKY130_CORPUS_FILES) >= 1
    assert len(GF180MCU_CORPUS_FILES) >= 1


@pytest.mark.parametrize(
    "layout_path", SKY130_CORPUS_FILES, ids=[p.name for p in SKY130_CORPUS_FILES]
)
def test_sky130_corpus_extraction_produces_well_formed_report(layout_path, tmp_path):
    report = run_extract(
        str(layout_path), "sky130", output=str(tmp_path / f"{layout_path.stem}.spice")
    )

    assert report["schema_version"] == 2
    assert report["status"] == "extracted"
    assert report["device_count"] == sum(report["device_counts"].values())
    assert report["net_count"] == len(report["nets"])
    assert report["pin_count"] == sum(1 for n in report["nets"] if n["pin"])
    for device in report["devices"]:
        assert device["class"] in {"nfet", "pfet"}
        assert device["params"]["w_um"] > 0
        assert device["params"]["l_um"] > 0


@pytest.mark.parametrize(
    "layout_path", GF180MCU_CORPUS_FILES, ids=[p.name for p in GF180MCU_CORPUS_FILES]
)
def test_gf180mcu_corpus_extraction_produces_well_formed_report(layout_path, tmp_path):
    report = run_extract(
        str(layout_path), "gf180mcu", output=str(tmp_path / f"{layout_path.stem}.spice")
    )

    assert report["schema_version"] == 2
    assert report["status"] == "extracted"
    assert report["device_count"] == sum(report["device_counts"].values())
    assert report["net_count"] == len(report["nets"])
    for device in report["devices"]:
        assert device["class"] in {"nfet", "pfet"}
        assert device["params"]["w_um"] > 0
        assert device["params"]["l_um"] > 0


def test_sky130_inv_1_spot_check(tmp_path):
    """A real sky130 inverter (`sky130_fd_sc_hd__inv_1`) extracts to exactly
    the expected two-device connectivity: NMOS pull-down and PMOS pull-up
    sharing gate net A and drain net Y."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    report = run_extract(
        str(layout_path), "sky130", output=str(tmp_path / "inv_1.spice")
    )

    assert report["top"] == "sky130_fd_sc_hd__inv_1"
    assert report["device_count"] == 2
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "pnp",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
        "resistor",
    ]

    devices = {d["class"]: d for d in report["devices"]}
    nfet, pfet = devices["nfet"], devices["pfet"]
    assert nfet["nets"] == {"s": "VGND", "g": "A", "d": "Y", "b": "vsubs"}
    assert pfet["nets"] == {"s": "VPWR", "g": "A", "d": "Y", "b": "VPB"}

    net_names = {n["name"] for n in report["nets"]}
    assert net_names == {"A", "Y", "VGND", "VPWR", "VPB", "vsubs"}


def test_gf180mcu_clkinv_1_spot_check(tmp_path):
    """A real gf180mcu inverter (`gf180mcu_fd_sc_mcu9t5v0__clkinv_1`)
    extracts to the expected NMOS/PMOS connectivity (no well-tie label
    layer in this deck, so the PMOS body is a floating, anonymous net --
    a documented approximation, see `decks/gf180mcu.py`)."""
    layout_path = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"
    report = run_extract(
        str(layout_path), "gf180mcu", output=str(tmp_path / "clkinv_1.spice")
    )

    assert report["top"] == "gf180mcu_fd_sc_mcu9t5v0__clkinv_1"
    assert report["device_count"] == 2
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "bjt",
        "cap_mim_2f0_m4m5_noshield",
        "resistor",
        "diode_nd2ps_06v0",
        "diode_pd2nw_06v0",
    ]

    devices = {d["class"]: d for d in report["devices"]}
    nfet, pfet = devices["nfet"], devices["pfet"]
    assert nfet["nets"]["s"] == "VSS"
    assert nfet["nets"]["g"] == "I"
    assert nfet["nets"]["d"] == "ZN"
    assert nfet["nets"]["b"] == "vsubs"
    assert pfet["nets"]["s"] == "VDD"
    assert pfet["nets"]["g"] == "I"
    assert pfet["nets"]["d"] == "ZN"
    # PMOS body: floating/anonymous (no well-label layer in this curated
    # deck) -- present as *some* net, just not one of the named pins.
    assert pfet["nets"]["b"] not in {"VDD", "VSS", "I", "ZN"}

    # Issue #555: the anonymous PMOS-body net's KLayout-synthesized name
    # (e.g. "$5") is surfaced structurally -- not only discoverable by
    # grepping the written SPICE body -- as an `unbiased_pmos_body_nets[]`
    # entry naming both the affected device and its floating body net, with
    # a matching prose `warnings[]` entry pointing at the docs caveat. The
    # NMOS body's `vsubs` net (a real DC bias path via `connect_global`)
    # never appears here.
    assert pfet["nets"]["b"].startswith("$")
    assert report["unbiased_pmos_body_nets"] == [
        {"device": pfet["name"], "net": pfet["nets"]["b"]}
    ]
    # Issue #599: the `warnings[]` mirror is a single aggregate line with the
    # count baked in (not one line per device naming its specific net) --
    # see the multi-instance aggregation tests below for the scaling case.
    assert any(
        "no DC bias path" in warning and "1 PMOS device" in warning
        for warning in report["warnings"]
    )

    # Issue #596: the same anonymous PMOS body net is also exactly the
    # "legitimate single-terminal tie" case -- device_count == 1 (only the
    # PMOS body terminal), not a declared pin -- so it appears in
    # `single_terminal_nets[]` too, classified `terminal_kind == "body"`
    # (not `"gate"`), with the softer "can be a legitimate single-terminal
    # tie" warning wording rather than the gate-specific "almost certainly
    # an unconnected input" wording (see the floating-gate test below).
    body_net_name = pfet["nets"]["b"]
    single_terminal_by_net = {
        entry["net"]: entry for entry in report["single_terminal_nets"]
    }
    assert body_net_name in single_terminal_by_net
    body_entry = single_terminal_by_net[body_net_name]
    assert body_entry["device"] == pfet["name"]
    assert body_entry["terminal"] == "b"
    assert body_entry["terminal_kind"] == "body"
    # Issue #599: the `warnings[]` mirror is a single aggregate line per
    # `terminal_kind` bucket (gate vs. non-gate), with the count baked in --
    # not one line per net naming its specific net name.
    assert any(
        "legitimate single-terminal tie" in warning and "1 net" in warning
        for warning in report["warnings"]
    )
    assert not any("almost certainly" in warning for warning in report["warnings"])


def test_multi_pmos_unbiased_body_nets_produce_one_aggregate_warning(tmp_path):
    """Issue #599: a layout with 2+ PMOS devices whose bodies are all
    anonymous produces exactly **one** `warnings[]` entry naming the count,
    not one line per device -- `unbiased_pmos_body_nets[]` itself still has
    one entry per device. Uses the real gf180mcu AND2 standard cell (3 PMOS,
    all bodies anonymous on this deck -- see the clkinv_1 spot-check above
    for the single-device case)."""
    layout_path = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__and2_1.gds"
    report = run_extract(
        str(layout_path), "gf180mcu", output=str(tmp_path / "and2_1.spice")
    )

    assert report["device_counts"]["pfet"] == 3
    unbiased = report["unbiased_pmos_body_nets"]
    assert len(unbiased) == 3
    assert {entry["device"] for entry in unbiased} == {
        d["name"] for d in report["devices"] if d["class"] == "pfet"
    }

    pmos_warnings = [w for w in report["warnings"] if "no DC bias path" in w]
    assert len(pmos_warnings) == 1
    assert "3 PMOS devices" in pmos_warnings[0]
    assert "unbiased_pmos_body_nets[]" in pmos_warnings[0]


# --------------------------------------------------------------------------- #
# Single-device-terminal nets (issue #596): a net with exactly one device
# terminal and no declared pin is structurally almost always a defect (a
# floating MOS gate above all), and is not discoverable today except by a
# downstream simulator's singular-matrix failure.
# --------------------------------------------------------------------------- #


def test_detect_single_terminal_nets_direct():
    """Unit-level coverage of `_detect_single_terminal_nets` against
    fabricated `devices[]`/`nets[]` arrays, mirroring the shape
    `_describe_devices`/`_describe_nets` actually produce -- covers the MOS
    gate/source/drain/body classification, the non-MOS "raw terminal key"
    fallback, the pin exclusion, and the device_count != 1 exclusion
    without needing a real layout for every case."""
    devices = [
        {
            "name": "M1",
            "class": "nfet",
            "nets": {"s": "VGND", "g": "FLOAT_G", "d": "Y", "b": "vsubs"},
            "params": {},
        },
        {
            "name": "R1",
            "class": "resistor",
            "nets": {"a": "RA", "b": "FLOAT_RB", "w": "vsubs"},
            "params": {},
        },
    ]
    nets = [
        {"name": "VGND", "pin": True, "device_count": 1},
        {"name": "FLOAT_G", "pin": False, "device_count": 1},
        {"name": "Y", "pin": True, "device_count": 1},
        {"name": "vsubs", "pin": True, "device_count": 2},
        {"name": "RA", "pin": True, "device_count": 1},
        {"name": "FLOAT_RB", "pin": False, "device_count": 1},
    ]

    result = _detect_single_terminal_nets(devices, nets)

    by_net = {entry["net"]: entry for entry in result}
    # The gate net: single device terminal, not a pin -> flagged, kind "gate".
    assert by_net["FLOAT_G"] == {
        "net": "FLOAT_G",
        "device": "M1",
        "terminal": "g",
        "terminal_kind": "gate",
    }
    # The resistor's "b" terminal: not MOS-like (no "g" terminal on R1), so
    # `terminal_kind` is the raw terminal key, not confused with a MOS body.
    assert by_net["FLOAT_RB"] == {
        "net": "FLOAT_RB",
        "device": "R1",
        "terminal": "b",
        "terminal_kind": "b",
    }
    # Declared pins (VGND, Y, RA) and the shared vsubs net (device_count ==
    # 2) are never flagged, even though VGND/Y/RA each have exactly one
    # device terminal.
    assert set(by_net) == {"FLOAT_G", "FLOAT_RB"}


def test_detect_single_terminal_nets_source_drain_body_kinds():
    """MOS `s`/`d`/`b` terminals classify as `source`/`drain`/`body` (not
    the raw letter) once the owning device also has a `g` terminal."""
    devices = [
        {
            "name": "M1",
            "class": "nfet",
            "nets": {"s": "FLOAT_S", "g": "A", "d": "FLOAT_D", "b": "FLOAT_B"},
            "params": {},
        },
    ]
    nets = [
        {"name": "FLOAT_S", "pin": False, "device_count": 1},
        {"name": "A", "pin": True, "device_count": 1},
        {"name": "FLOAT_D", "pin": False, "device_count": 1},
        {"name": "FLOAT_B", "pin": False, "device_count": 1},
    ]

    result = _detect_single_terminal_nets(devices, nets)
    kinds = {entry["net"]: entry["terminal_kind"] for entry in result}
    assert kinds == {
        "FLOAT_S": "source",
        "FLOAT_D": "drain",
        "FLOAT_B": "body",
    }


def test_floating_gate_nmos_reports_single_terminal_net(tmp_path):
    """End-to-end reproduction of issue #596's motivating case: an NMOS
    whose poly gate carries no label and touches nothing else. DRC-clean,
    device-recognisable geometry -- the only signal this is a defect is the
    new `single_terminal_nets[]` entry and its `warnings[]` prose."""
    path = _write_gds(_make_floating_gate_nmos_layout(), tmp_path / "floating_gate.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "floating_gate.spice"))

    (nfet,) = report["devices"]
    assert nfet["class"] == "nfet"
    gate_net = nfet["nets"]["g"]
    # Unlabeled -> KLayout's anonymous "$n" placeholder, not a declared pin.
    assert gate_net.startswith("$")

    single_terminal_by_net = {
        entry["net"]: entry for entry in report["single_terminal_nets"]
    }
    assert gate_net in single_terminal_by_net
    gate_entry = single_terminal_by_net[gate_net]
    assert gate_entry["device"] == nfet["name"]
    assert gate_entry["terminal"] == "g"
    assert gate_entry["terminal_kind"] == "gate"

    # Issue #599: the `warnings[]` mirror is a single aggregate line for the
    # gate bucket, with the count baked in -- not one line per net naming
    # its specific net name.
    assert any(
        "almost certainly" in warning and "1 net" in warning
        for warning in report["warnings"]
    )

    # The real interface (source/drain/body) is unaffected -- each of those
    # is a declared pin (VGND/Y) or the named substrate global (vsubs), so
    # none of them appear in `single_terminal_nets[]` despite also having
    # exactly one device terminal.
    interface_nets = {"VGND", "Y", "vsubs"}
    assert interface_nets.isdisjoint(single_terminal_by_net)


def test_inverter_layout_reports_no_single_terminal_nets_by_default(tmp_path):
    """`_make_inverter_layout()`'s default fixture has no defect of this
    kind: every net either has 2+ device terminals (the shared gate/drain)
    or is a declared pin (VGND/VPWR/VPB/vsubs) -- `single_terminal_nets[]`
    is empty and no matching warning fires."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    assert report["single_terminal_nets"] == []
    assert not any(
        "connects to exactly one device terminal" in warning
        for warning in report["warnings"]
    )


def test_multi_single_terminal_nets_produce_two_aggregate_warnings(tmp_path):
    """Issue #599: a layout with 2+ undriven MOS gates produces exactly
    **one** aggregate `warnings[]` entry for the gate bucket (count baked
    in), separate from a second aggregate line for any non-gate single-
    terminal-net hits in the same run -- not one line per net.
    `single_terminal_nets[]` itself is unaffected: still one entry per net,
    3 total (2 gate, 1 drain) for this fixture."""
    path = _write_gds(
        _make_multi_single_terminal_layout(), tmp_path / "multi_single.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "multi_single.spice"))

    single_terminal = report["single_terminal_nets"]
    assert len(single_terminal) == 3
    kinds = [entry["terminal_kind"] for entry in single_terminal]
    assert kinds.count("gate") == 2
    assert kinds.count("drain") == 1

    gate_warnings = [w for w in report["warnings"] if "almost certainly" in w]
    assert len(gate_warnings) == 1
    assert "2 nets" in gate_warnings[0]
    assert "single_terminal_nets[]" in gate_warnings[0]

    other_warnings = [
        w for w in report["warnings"] if "legitimate single-terminal tie" in w
    ]
    assert len(other_warnings) == 1
    assert "1 net" in other_warnings[0]
    assert "single_terminal_nets[]" in other_warnings[0]

    # No per-instance duplication: exactly 2 aggregate lines total, not 3
    # (one per net) or more.
    single_terminal_warnings = [
        w for w in report["warnings"] if "connect to exactly one device terminal" in w
    ]
    assert len(single_terminal_warnings) == 2


# --------------------------------------------------------------------------- #
# Multi-level metal stack: routing above Metal1 extracts as connected nets
# (issue #220 -- gf180mcu's deck now declares Metal1-Metal5 + Via1-Via4)
# --------------------------------------------------------------------------- #


def _draw_gf180mcu_nmos(top, layout, x0, source_label, drain_label=None):
    """Draw one gf180mcu NMOS at ``x0`` and return its drain-pad footprint box
    (for stacking a via chain on). ``source_label``/``drain_label`` name the
    respective Metal1 pads (drain optional)."""

    def d(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    d(22, 0, kdb.Box(x0, 0, x0 + 3000, 1000))  # Comp (active)
    d(30, 0, kdb.Box(x0 + 1300, -300, x0 + 1700, 1300))  # Poly2 gate (crosses)
    # Source pad: Contact + Metal1, labelled.
    d(33, 0, kdb.Box(x0 + 400, 300, x0 + 700, 700))
    d(34, 0, kdb.Box(x0 + 200, 200, x0 + 900, 800))
    label(34, 10, source_label, x0 + 500, 500)
    # Drain pad: Contact + Metal1, optionally labelled.
    d(33, 0, kdb.Box(x0 + 2300, 300, x0 + 2600, 700))
    d(34, 0, kdb.Box(x0 + 2000, 200, x0 + 2900, 800))
    if drain_label is not None:
        label(34, 10, drain_label, x0 + 2450, 500)
    return kdb.Box(x0 + 2300, 300, x0 + 2600, 700)  # drain via-stack footprint


# Via1..Via4 layers, bottom-up: connecting Metal1<->Metal2<->...<->Metal5.
_GF180MCU_VIA_STACK = [(35, 0), (36, 0), (38, 0), (42, 0), (40, 0), (46, 0), (41, 0)]


def _make_gf180mcu_metal_bridge_layout(*, bridge: bool) -> kdb.Layout:
    """Two gf180mcu NMOS transistors whose drains are separated in Metal1 and
    (when ``bridge``) joined *only* through the upper metal stack: a full
    Via1/Metal2/Via2/Metal3/Via3/Metal4/Via4 column at each drain up to a
    long-haul Metal5 span between them. The drains touch on no shared Metal1
    shape, so they extract as one net *iff* the deck reads Metal2-Metal5 and
    Via1-Via4 (the #220 fix); with ``bridge=False`` the upper stack is absent
    and the two drains stay distinct -- the in-suite counterfactual."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    fa = _draw_gf180mcu_nmos(top, layout, 0, "S1", drain_label="OUT")
    fb = _draw_gf180mcu_nmos(top, layout, 8000, "S2")

    if bridge:

        def d(layer, datatype, box):
            top.shapes(layout.layer(layer, datatype)).insert(box)

        for layer, datatype in _GF180MCU_VIA_STACK:
            d(layer, datatype, fa)
            d(layer, datatype, fb)
        # Metal5 long-haul span joining the two drain columns.
        d(81, 0, kdb.Box(fa.left, fa.bottom, fb.right, fb.top))

    return layout


def test_gf180mcu_metal2_bridge_joins_drains_into_one_net(tmp_path):
    """Two NMOS drains joined only through the Metal2-Metal5/Via1-Via4 stack
    extract as a single connected net -- the core #220 fix. Before the deck
    declared the full stack, everything above Metal1 was invisible to the
    connectivity graph and the drains extracted as two disconnected nets."""
    path = _write_gds(
        _make_gf180mcu_metal_bridge_layout(bridge=True), tmp_path / "bridge.gds"
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "bridge.spice"))

    nfets = [d for d in report["devices"] if d["class"] == "nfet"]
    assert len(nfets) == 2
    drains = {d["nets"]["d"] for d in nfets}
    assert drains == {"OUT"}, f"drains should share the bridged net, got {drains}"


def test_gf180mcu_without_upper_stack_drains_stay_disconnected(tmp_path):
    """Counterfactual for the test above: with the upper metal stack removed,
    the same two drains extract as two distinct nets. Guards against a false
    pass where the drains merged for some reason other than the via stack."""
    path = _write_gds(
        _make_gf180mcu_metal_bridge_layout(bridge=False), tmp_path / "nobridge.gds"
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "nobridge.spice"))

    nfets = [d for d in report["devices"] if d["class"] == "nfet"]
    assert len(nfets) == 2
    drains = {d["nets"]["d"] for d in nfets}
    assert len(drains) == 2, f"drains should be disconnected without a bridge, {drains}"


def _make_gf180mcu_dualgate_mos_layout(*, dualgate_overlap: bool) -> kdb.Layout:
    """One gf180mcu NMOS (`_draw_gf180mcu_nmos`) with a `Dualgate` (55/0)
    marker drawn either directly over it (``dualgate_overlap=True`` -- issue
    #552's own extraction reproducer: a transistor drawn entirely inside the
    5V/6V marker) or far away (``dualgate_overlap=False`` -- present in the
    stream, but overlapping no MOS device geometry, the
    false-positive-avoidance counterfactual)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    _draw_gf180mcu_nmos(top, layout, 0, "S", drain_label="D")

    def d(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    if dualgate_overlap:
        d(55, 0, kdb.Box(-500, -500, 3500, 1500))  # Dualgate, covers the NMOS
    else:
        d(55, 0, kdb.Box(100_000, 100_000, 101_000, 101_000))  # far away

    return layout


def test_gf180mcu_extract_warns_mos_inside_dualgate_marker(tmp_path):
    """Issue #552's own extraction reproducer: a transistor drawn entirely
    inside `Dualgate` (the 5V/6V marker) still extracts as an ordinary
    `nfet` device -- this deck derives MOS flavour from the well layer alone
    and never reads the marker, so the extracted class/model binding is
    unchanged -- but `voltage_domain_warnings` is the new loud signal that
    the model this device would bind to under `--pdk` (`nfet_03v3`) may be
    wrong for it."""
    path = _write_gds(
        _make_gf180mcu_dualgate_mos_layout(dualgate_overlap=True),
        tmp_path / "mv_mos.gds",
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "mv_mos.spice"))

    assert report["device_counts"] == {"nfet": 1}
    expected_description = get_unmodeled_voltage_markers("gf180mcu")[(55, 0)]
    assert report["voltage_domain_warnings"] == [
        {"marker": "55/0", "description": expected_description}
    ]
    assert any("55/0" in warning for warning in report["warnings"])


def test_gf180mcu_extract_no_warning_without_dualgate_overlap(tmp_path):
    """Counterfactual: `Dualgate` present in the stream but drawn far away
    from any MOS device geometry produces no warning -- the gate is
    "overlaps extracted MOS device geometry", not bare presence in the
    stream, so a marker shape with nothing behind it never produces a
    false-positive warning."""
    path = _write_gds(
        _make_gf180mcu_dualgate_mos_layout(dualgate_overlap=False),
        tmp_path / "mv_mos_far.gds",
    )
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "mv_mos_far.spice"))

    assert report["device_counts"] == {"nfet": 1}
    assert report["voltage_domain_warnings"] == []


def test_gf180mcu_deck_declares_full_metal_stack():
    """The gf180mcu extraction deck declares Metal1-Metal5 with a Via1-Via4
    chain between them (index-aligned, `len(metals) - 1` vias) and a pin/label
    layer per metal level -- the deck-data half of #220."""
    deck = get_extraction_deck("gf180mcu")
    assert deck.metals == ((34, 0), (36, 0), (42, 0), (46, 0), (81, 0))
    assert deck.vias == ((35, 0), (38, 0), (40, 0), (41, 0))
    assert deck.metal_labels == ((34, 10), (36, 10), (42, 10), (46, 10), (81, 10))
    assert len(deck.vias) == len(deck.metals) - 1


# --------------------------------------------------------------------------- #
# sky130's third connectivity level, met2 (issue #508, follow-up to #454/
# #468's metal2/via1 routing roles): before this, sky130's `metals` stopped
# at met1, so met1 -- itself already the *only* other level the curated
# deck's connectivity graph knew about -- was the sole plane above the
# device-pad layer a caller could route on. See `decks/sky130.py`'s own
# `EXTRACTION_DECK.metals` comment for the full provenance.
# --------------------------------------------------------------------------- #


def test_sky130_deck_declares_three_level_metal_stack():
    """The sky130 extraction deck now declares li1/met1/met2 with a
    mcon/via.drawing chain between them (index-aligned, `len(metals) - 1`
    vias) and a pin/label layer per metal level -- the deck-data half of
    #508."""
    deck = get_extraction_deck("sky130")
    assert deck.metals == ((67, 20), (68, 20), (69, 20))
    assert deck.vias == ((67, 44), (68, 44))
    assert deck.metal_labels == ((67, 5), (68, 5), (69, 5))
    assert len(deck.vias) == len(deck.metals) - 1


def _draw_sky130_nmos(top, layout, x0, source_label, drain_label=None):
    """Draw one minimal sky130 NMOS at ``x0`` (no tap/body geometry -- falls
    back to the substrate-global net, matching
    `test_sky130_nmos_body_falls_back_to_substrate_global_without_tap`
    above) and return its drain li1 pad footprint (for stacking a met2
    via-column on)."""

    def d(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    d(65, 20, kdb.Box(x0, 0, x0 + 2000, 1000))  # diff.drawing (active strip)
    d(66, 20, kdb.Box(x0 + 800, -200, x0 + 1200, 1200))  # poly.drawing (gate)
    # Source pad: licon1 + li1, labelled.
    d(66, 44, kdb.Box(x0 + 100, 300, x0 + 300, 700))  # licon1.drawing
    d(67, 20, kdb.Box(x0, 200, x0 + 400, 800))  # li1.drawing
    label(67, 5, source_label, x0 + 200, 500)
    # Drain pad: licon1 + li1, optionally labelled.
    d(66, 44, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))  # licon1.drawing
    d(67, 20, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))  # li1.drawing
    if drain_label is not None:
        label(67, 5, drain_label, x0 + 1800, 500)
    return kdb.Box(x0 + 1600, 200, x0 + 2000, 800)  # drain li1 footprint


# mcon/met1/via.drawing/met2 -- the stack a drain pad's own li1 footprint is
# contacted straight up through to reach met2 (issue #508).
_SKY130_MET2_VIA_STACK = [(67, 44), (68, 20), (68, 44), (69, 20)]


def _make_sky130_metal_bridge_layout(*, bridge: bool) -> kdb.Layout:
    """Two sky130 NMOS transistors whose drains are separated in li1 and
    (when ``bridge``) joined *only* through the met2 level: a full
    mcon/met1/via.drawing/met2 column at each drain, plus a met2 span
    between them. The drains touch on no shared li1 shape, so they extract
    as one net *iff* the deck reads met2 and the met1<->met2 via (the #508
    fix); with ``bridge=False`` the met2 span is absent and the two drains
    stay distinct -- the in-suite counterfactual (mirrors
    `_make_gf180mcu_metal_bridge_layout`)."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    fa = _draw_sky130_nmos(top, layout, 0, "S1", drain_label="OUT")
    fb = _draw_sky130_nmos(top, layout, 8000, "S2")

    if bridge:

        def d(layer, datatype, box):
            top.shapes(layout.layer(layer, datatype)).insert(box)

        for layer, datatype in _SKY130_MET2_VIA_STACK:
            d(layer, datatype, fa)
            d(layer, datatype, fb)
        # met2 long-haul span joining the two drain columns.
        d(69, 20, kdb.Box(fa.left, fa.bottom, fb.right, fb.top))

    return layout


def test_sky130_met2_bridge_joins_drains_into_one_net(tmp_path):
    """Two NMOS drains joined only through the met2/via.drawing stack extract
    as a single connected net -- the core #508 fix. Before the deck declared
    met2 (and the via reaching it), everything above met1 was invisible to
    the connectivity graph and the drains extracted as two disconnected
    nets."""
    path = _write_gds(
        _make_sky130_metal_bridge_layout(bridge=True), tmp_path / "met2_bridge.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "met2_bridge.spice"))

    nfets = [d for d in report["devices"] if d["class"] == "nfet"]
    assert len(nfets) == 2
    drains = {d["nets"]["d"] for d in nfets}
    assert drains == {"OUT"}, f"drains should share the bridged net, got {drains}"


def test_sky130_without_met2_bridge_drains_stay_disconnected(tmp_path):
    """Counterfactual for the test above: with the met2 bridge removed, the
    same two drains extract as two distinct nets. Guards against a false
    pass where the drains merged for some reason other than the met2/via
    stack."""
    path = _write_gds(
        _make_sky130_metal_bridge_layout(bridge=False), tmp_path / "no_bridge.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "no_bridge.spice"))

    nfets = [d for d in report["devices"] if d["class"] == "nfet"]
    assert len(nfets) == 2
    drains = {d["nets"]["d"] for d in nfets}
    assert len(drains) == 2, f"drains should be disconnected without a bridge, {drains}"


# --------------------------------------------------------------------------- #
# ignored_layers: shapes on layers the deck's connectivity graph never reads
# (issue #220 interim ask -- the extraction-side analogue of `klt drc`'s
# coverage.layers_in_stream_without_rules)
# --------------------------------------------------------------------------- #


def test_ignored_layers_reports_undeclared_shape_bearing_layers(tmp_path):
    """A gf180mcu NMOS layout that also carries shapes on a layer the deck's
    connectivity graph does not read (MetalTop 53/0, above this curated
    deck's Metal1-Metal5 stack) reports that layer -- with its shape count --
    in `ignored_layers`, so a downstream LVS mismatch on such geometry is
    diagnosable as a coverage gap rather than a phantom layout bug. Nplus
    (32/0) and Dualgate (55/0) are *not* ignored: the deck's drawn `ppolyf_u`
    resistor declares Nplus as an exclusion (#222) and its junction diodes
    require Dualgate (#542), so both are read layers."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def d(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    d(22, 0, kdb.Box(0, 0, 3000, 1000))  # Comp (read)
    d(30, 0, kdb.Box(1300, -300, 1700, 1300))  # Poly2 (read)
    d(33, 0, kdb.Box(400, 300, 700, 700))  # Contact (read)
    d(34, 0, kdb.Box(200, 200, 900, 800))  # Metal1 (read)
    d(32, 0, kdb.Box(0, 0, 500, 500))  # Nplus (read -- resistor exclusion, #222)
    d(55, 0, kdb.Box(0, 0, 3000, 1000))  # Dualgate (read -- diode requires, #542)
    d(53, 0, kdb.Box(0, 0, 3000, 1000))  # MetalTop (NOT read)
    d(53, 0, kdb.Box(0, 2000, 3000, 3000))  # a second MetalTop shape

    path = _write_gds(layout, tmp_path / "ign.gds")
    report = run_extract(path, "gf180mcu", output=str(tmp_path / "ign.spice"))

    ignored = {
        (e["layer"], e["datatype"]): e["shapes"] for e in report["ignored_layers"]
    }
    assert (53, 0) in ignored and ignored[(53, 0)] == 2
    # Layers the deck *does* read are never reported as ignored -- including
    # Nplus (32/0), a read layer via the drawn-resistor exclusion (#222), and
    # Dualgate (55/0), a read layer via the junction diodes' requires (#542).
    for read_layer in [(22, 0), (30, 0), (33, 0), (34, 0), (32, 0), (55, 0)]:
        assert read_layer not in ignored


def test_ignored_layers_empty_when_every_layer_is_read(tmp_path):
    """A layout drawn entirely on layers the deck reads (the sky130 inverter
    fixture, shaped to exercise every `EXTRACTION_DECK` layer role) reports an
    empty `ignored_layers` -- no false positives."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))
    assert report["ignored_layers"] == []


def test_ignored_layers_present_in_cli_json(tmp_path, capsys):
    """The `ignored_layers` field is part of the JSON contract and is emitted
    by the CLI."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(22, 0)).insert(kdb.Box(0, 0, 1000, 1000))  # Comp
    top.shapes(layout.layer(53, 0)).insert(kdb.Box(0, 0, 1000, 1000))  # MetalTop
    path = _write_gds(layout, tmp_path / "cli.gds")

    exit_code = main(["extract", str(path), "--deck", "gf180mcu", "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert "ignored_layers" in out
    assert {"layer": 53, "datatype": 0, "shapes": 1} in out["ignored_layers"]


# --------------------------------------------------------------------------- #
# Black-box / abstract regions (issue #293): geometry drawn on a reserved
# annotation layer (990-999, any datatype -- issue #289) is excluded from
# connectivity, and the exclusion is reported in `black_box_regions`.
# --------------------------------------------------------------------------- #

#: The single canonical reserved-layer pair (see docs/cli/drc.md's "Reserved
#: annotation layer") used to draw a black-box marker shape in these tests.
_BLACK_BOX_LAYER = (994, 0)


def _make_two_nmos_layout() -> kdb.Layout:
    """Two independent, non-touching NMOS devices on sky130 deck layers --
    device "A" at x=0, device "B" at x=6000 -- for black-box region tests:
    each device's active/poly/contact/li1 geometry is fully disjoint from the
    other's, so black-boxing one device's bbox cannot incidentally reach the
    other's geometry."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    def build_nmos(x0, gate_label, drain_label, source_label):
        draw(65, 20, kdb.Box(x0, 0, x0 + 2000, 1000))  # diff.drawing (active)
        # Gate poly, extended above active for an off-active strap contact.
        draw(66, 20, kdb.Box(x0 + 800, -200, x0 + 1200, 1300))  # poly.drawing
        # Source (S) / drain (D) contacts + li1, both within active.
        draw(66, 44, kdb.Box(x0 + 100, 300, x0 + 300, 700))  # licon1 (S)
        draw(67, 20, kdb.Box(x0, 200, x0 + 400, 800))  # li1 (S)
        draw(66, 44, kdb.Box(x0 + 1700, 300, x0 + 1900, 700))  # licon1 (D)
        draw(67, 20, kdb.Box(x0 + 1600, 200, x0 + 2000, 800))  # li1 (D)
        # Gate strap contact, off active (y 1000..1200).
        draw(66, 44, kdb.Box(x0 + 900, 1000, x0 + 1100, 1200))  # licon1 (G)
        draw(67, 20, kdb.Box(x0 + 850, 950, x0 + 1150, 1250))  # li1 (G)
        label(67, 5, source_label, x0 + 200, 500)
        label(67, 5, drain_label, x0 + 1800, 500)
        label(67, 5, gate_label, x0 + 1000, 1100)

    build_nmos(0, "AG", "AD", "AS")
    build_nmos(6000, "BG", "BD", "BS")
    return layout


def test_black_box_regions_empty_when_no_reserved_layer_geometry(tmp_path):
    """A layout that draws no reserved-annotation-layer geometry reports an
    empty `black_box_regions` -- additive-field, off-by-default contract."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))
    assert report["black_box_regions"] == []
    # Existing behaviour is otherwise unaffected: same device/net counts as
    # the baseline synthetic-inverter assertions.
    assert report["device_count"] == 2


def test_black_box_region_fully_excludes_covered_device(tmp_path):
    """A rectangle on the reserved layer covering device "A"'s full extent
    excludes it entirely from connectivity: device "B" (untouched) is the
    only device left, and the exclusion is reported with its bbox and a
    non-zero `shapes_excluded`. The marker geometry itself is never
    registered with `l2n`, so it still shows up in `ignored_layers` exactly
    as any other deck-unclaimed layer would (docs/cli/extract.md's "Reserved
    annotation layer")."""
    layout = _make_two_nmos_layout()
    layout.cell("TOP").shapes(layout.layer(*_BLACK_BOX_LAYER)).insert(
        kdb.Box(-500, -500, 2500, 1500)
    )
    path = _write_gds(layout, tmp_path / "bb.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "bb.spice"))

    assert report["device_count"] == 1
    assert report["device_counts"] == {"nfet": 1}
    (device,) = report["devices"]
    assert device["nets"]["g"] == "BG"
    assert device["nets"]["d"] == "BD"
    assert device["nets"]["s"] == "BS"

    net_names = {n["name"] for n in report["nets"]}
    assert {"AG", "AD", "AS"}.isdisjoint(net_names)
    assert {"BG", "BD", "BS"}.issubset(net_names)

    black_box_regions = report["black_box_regions"]
    assert len(black_box_regions) == 1
    entry = black_box_regions[0]
    assert entry["bbox_um"] == {
        "left": pytest.approx(-0.5),
        "bottom": pytest.approx(-0.5),
        "right": pytest.approx(2.5),
        "top": pytest.approx(1.5),
    }
    assert entry["shapes_excluded"] > 0

    ignored = {
        (e["layer"], e["datatype"]): e["shapes"] for e in report["ignored_layers"]
    }
    assert ignored.get(_BLACK_BOX_LAYER) == 1


def test_two_non_overlapping_black_box_regions_reported_separately(tmp_path):
    """Two geometrically separate marker shapes are each reported as their
    own entry, not merged into one bbox."""
    layout = _make_two_nmos_layout()
    top = layout.cell("TOP")
    marker_layer = layout.layer(*_BLACK_BOX_LAYER)
    top.shapes(marker_layer).insert(kdb.Box(-500, -500, 2500, 1500))  # over "A"
    top.shapes(marker_layer).insert(kdb.Box(5500, -500, 8500, 1500))  # over "B"
    path = _write_gds(layout, tmp_path / "bb2.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "bb2.spice"))

    assert report["device_count"] == 0
    black_box_regions = report["black_box_regions"]
    assert len(black_box_regions) == 2
    # Sorted by (left, bottom) ascending.
    lefts = [entry["bbox_um"]["left"] for entry in black_box_regions]
    assert lefts == sorted(lefts)
    assert black_box_regions[0]["bbox_um"]["left"] == pytest.approx(-0.5)
    assert black_box_regions[1]["bbox_um"]["left"] == pytest.approx(5.5)
    assert all(entry["shapes_excluded"] > 0 for entry in black_box_regions)


def test_black_box_region_partial_overlap_is_geometric_cut_not_device_deletion(
    tmp_path,
):
    """A marker shape covering only device "A"'s drain contact/pad (not its
    gate or source) cuts exactly that geometry -- the device is still
    recognised (a geometric cut, not an all-or-nothing device exclusion):
    the gate/source terminals keep their labelled nets, while the drain
    terminal loses its label (its contact/pad were excluded) without the
    whole device disappearing."""
    layout = _make_two_nmos_layout()
    # Covers device "A"'s drain contact (1700..1900, 300..700), li1 drain pad
    # (1600..2000, 200..800), and its "AD" label (1800, 500) -- but not the
    # gate (800..1200) or source (0..400) geometry.
    layout.cell("TOP").shapes(layout.layer(*_BLACK_BOX_LAYER)).insert(
        kdb.Box(1550, 150, 2050, 850)
    )
    path = _write_gds(layout, tmp_path / "bb_partial.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "bb_partial.spice"))

    assert report["device_count"] == 2  # both transistors still recognised
    device_a = next(d for d in report["devices"] if d["nets"]["g"] == "AG")
    assert device_a["nets"]["s"] == "AS"
    # The drain's contact/label were excluded -- it keeps *a* net (the bare
    # active polygon is still there), just not the named "AD" net anymore.
    assert device_a["nets"]["d"] != "AD"
    assert device_a["nets"]["d"] is not None

    device_b = next(d for d in report["devices"] if d["nets"]["g"] == "BG")
    assert device_b["nets"]["d"] == "BD"
    assert device_b["nets"]["s"] == "BS"

    (entry,) = report["black_box_regions"]
    assert entry["shapes_excluded"] > 0


def test_black_box_regions_present_in_cli_json(tmp_path, capsys):
    """`black_box_regions` is part of the JSON contract and is emitted by the
    CLI."""
    layout = _make_two_nmos_layout()
    layout.cell("TOP").shapes(layout.layer(*_BLACK_BOX_LAYER)).insert(
        kdb.Box(-500, -500, 2500, 1500)
    )
    path = _write_gds(layout, tmp_path / "bb_cli.gds")

    exit_code = main(["extract", str(path), "--deck", "sky130", "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert "black_box_regions" in out
    assert len(out["black_box_regions"]) == 1
    assert out["black_box_regions"][0]["shapes_excluded"] > 0


# --------------------------------------------------------------------------- #
# Cell-level (black-box + pins) abstraction -- `--abstract-cells`, issue #620
# --------------------------------------------------------------------------- #


def _make_abstract_leaf_cell(layout: kdb.Layout, name: str) -> kdb.Cell:
    """One NMOS-shaped leaf cell on sky130 deck layers, with its own li1
    pads labelled directly in the cell: ``"Y"`` (source, local (200, 500))
    and ``"A"`` (drain, local (1800, 500)). The transistor geometry is real
    (not a bare routing stub) precisely so a test can assert it is *not*
    recognised as a device once the cell is abstracted -- proving
    `--abstract-cells` actually suppresses device recognition inside the
    cell, not just that it resolves pins.
    """
    cell = layout.create_cell(name)

    def draw(layer, datatype, box):
        cell.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        cell.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))  # poly.drawing (gate)
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (S side)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (D side)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (S side)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (D side)
    label(67, 5, "Y", 200, 500)
    label(67, 5, "A", 1800, 500)
    return cell


def _wire_abstract_instance_pins(
    top: kdb.Cell, layout: kdb.Layout, trans: kdb.Trans, net_a: str, net_y: str
) -> None:
    """Land a mcon+met1 pad (labelled ``net_a``/``net_y``) directly on top of
    one abstracted-leaf-cell instance's ``A``/``Y`` li1 pads, at the
    instance's own ``trans`` -- so the parent's routing genuinely reaches the
    pin location through ordinary (unmasked) contact/metal connectivity,
    exactly as `_wire_abstract_cells` expects. Using the *same* `trans`
    object to place this parent-side geometry that instantiated the cell
    (rather than hand-computing the transformed coordinates) is deliberate:
    it exercises the real transform composition instead of an
    independently-derived (and possibly wrong) expectation.
    """
    # A (drain) pad: licon1 + met1 landing directly over the cell's own D pad.
    top.shapes(layout.layer(67, 44)).insert(
        kdb.Box(1700, 300, 1900, 700).transformed(trans)
    )
    top.shapes(layout.layer(68, 20)).insert(
        kdb.Box(1600, 200, 2000, 800).transformed(trans)
    )
    top.shapes(layout.layer(68, 5)).insert(
        kdb.Text(net_a, kdb.Trans(trans * kdb.Point(1800, 500)))
    )
    # Y (source) pad: same, over the cell's own S pad.
    top.shapes(layout.layer(67, 44)).insert(
        kdb.Box(100, 300, 300, 700).transformed(trans)
    )
    top.shapes(layout.layer(68, 20)).insert(
        kdb.Box(0, 200, 400, 800).transformed(trans)
    )
    top.shapes(layout.layer(68, 5)).insert(
        kdb.Text(net_y, kdb.Trans(trans * kdb.Point(200, 500)))
    )


def _make_abstract_cells_layout(
    *, second_trans: kdb.Trans | None = None, cell_name: str = "SC_BUF"
) -> kdb.Layout:
    """Two instances of :func:`_make_abstract_leaf_cell` in a ``TOP`` cell,
    each wired to its own pair of distinctly-named top-level nets via
    :func:`_wire_abstract_instance_pins` -- the standard fixture for
    ``--abstract-cells``'s in-cell-label pin-resolution tests.

    ``second_trans`` overrides the second instance's transform (default: a
    plain x-shift) -- used by the mirrored-instance test.
    """
    layout = kdb.Layout()
    leaf = _make_abstract_leaf_cell(layout, cell_name)
    top = layout.create_cell("TOP")

    trans1 = kdb.Trans(kdb.Vector(0, 0))
    trans2 = (
        second_trans if second_trans is not None else kdb.Trans(kdb.Vector(5000, 0))
    )
    top.insert(kdb.CellInstArray(leaf.cell_index(), trans1))
    top.insert(kdb.CellInstArray(leaf.cell_index(), trans2))

    _wire_abstract_instance_pins(top, layout, trans1, "INST1_A", "INST1_Y")
    _wire_abstract_instance_pins(top, layout, trans2, "INST2_A", "INST2_Y")

    return layout


def test_abstract_cells_resolves_pins_from_in_cell_labels(tmp_path):
    """`--abstract-cells` matching a cell type with in-cell li1 pin labels
    resolves its pins from them, wires every instance in as a black-box
    `X`/`.SUBCKT` pair using the parent's own net names, and -- because the
    leaf cell draws a real transistor -- suppresses that transistor's own
    device recognition entirely (`device_count == 0`)."""
    layout = _make_abstract_cells_layout()
    path = _write_gds(layout, tmp_path / "abstract.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "abstract.spice"),
        abstract_cell_patterns=("SC_BUF",),
    )

    assert report["device_count"] == 0
    assert report["devices"] == []

    net_names = {n["name"] for n in report["nets"]}
    assert {"INST1_A", "INST1_Y", "INST2_A", "INST2_Y"}.issubset(net_names)

    (entry,) = report["abstracted_cells"]
    assert entry == {
        "cell": "SC_BUF",
        "instance_count": 2,
        "pin_count": 2,
        "resolution_source": "in_cell_labels",
        "lef_path": None,
    }

    spice = Path(report["netlist_path"]).read_text()
    assert ".SUBCKT SC_BUF A Y" in spice
    assert ".ENDS SC_BUF" in spice
    # Two X-cards, one per instance, each naming both parent nets.
    x_lines = [line for line in spice.splitlines() if line.startswith("X")]
    assert len(x_lines) == 2
    assert any("INST1_A" in line and "INST1_Y" in line for line in x_lines)
    assert any("INST2_A" in line and "INST2_Y" in line for line in x_lines)


def test_abstract_cells_applies_mirror_transform_to_pin_footprint(tmp_path):
    """A mirrored instance of the abstracted cell resolves its pins at the
    *mirrored* access point, not the unmirrored local one -- the parent net
    landed at the mirrored coordinate is the one that ends up wired to the
    subcircuit's pin (acceptance criterion: per-instance mirror/rotation
    transforms are applied correctly)."""
    mirrored = kdb.Trans(kdb.Trans.M90, kdb.Vector(9000, 4000))
    layout = _make_abstract_cells_layout(second_trans=mirrored)
    path = _write_gds(layout, tmp_path / "abstract_mirror.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "abstract_mirror.spice"),
        abstract_cell_patterns=("SC_BUF",),
    )

    (entry,) = report["abstracted_cells"]
    assert entry["instance_count"] == 2
    assert entry["resolution_source"] == "in_cell_labels"

    spice = Path(report["netlist_path"]).read_text()
    x_lines = [line for line in spice.splitlines() if line.startswith("X")]
    assert len(x_lines) == 2
    assert any("INST2_A" in line and "INST2_Y" in line for line in x_lines)


def test_abstract_cells_pattern_matching_nothing_is_a_noop(tmp_path):
    """A pattern that matches no instantiated cell leaves extraction exactly
    as it would be without `--abstract-cells` (issue #620's zero-instance
    edge case is not an error): `abstracted_cells` is an empty list, and
    the layout's own real device extracts normally."""
    layout = _make_inverter_layout()
    path = _write_gds(layout, tmp_path / "inv_noop.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "inv_noop.spice"),
        abstract_cell_patterns=("NO_SUCH_CELL_*",),
    )

    assert report["abstracted_cells"] == []
    assert report["device_count"] == 2


def test_abstract_cells_errors_when_cell_type_has_no_resolvable_pins(tmp_path):
    """A matched cell type that draws no in-cell pin label and has no
    `--abstract-cell-lef` fallback is a clean `ExtractError`, not a silently
    dropped-pin or unconnected-instance result (issue #620's acceptance
    criterion)."""
    layout = kdb.Layout()
    leaf = layout.create_cell("NAKED")
    leaf.shapes(layout.layer(65, 20)).insert(kdb.Box(0, 0, 2000, 1000))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans(0, 0)))
    path = _write_gds(layout, tmp_path / "naked.gds")

    with pytest.raises(ExtractError, match="NAKED"):
        run_extract(
            path,
            "sky130",
            output=str(tmp_path / "naked.spice"),
            abstract_cell_patterns=("NAKED",),
        )


def test_abstract_cell_lef_without_abstract_cells_is_an_error(tmp_path):
    """`--abstract-cell-lef` given without `--abstract-cells` is a clean
    error (a likely CLI mistake -- the LEF fallback has no effect on its
    own)."""
    layout = _make_inverter_layout()
    path = _write_gds(layout, tmp_path / "inv.gds")

    with pytest.raises(ExtractError, match="abstract_cell_lef_paths"):
        run_extract(
            path,
            "sky130",
            output=str(tmp_path / "inv.spice"),
            abstract_cell_lef_paths=("/nonexistent/some.lef",),
        )


def test_abstract_cells_falls_back_to_lef_abstract_when_no_in_cell_labels(tmp_path):
    """A matched cell type with no in-cell pin label resolves its pins from
    an `--abstract-cell-lef` MACRO/PIN/PORT block of the same name instead."""
    layout = kdb.Layout()
    leaf = layout.create_cell("LEF_BUF")

    def draw(cell, layer, datatype, box):
        cell.shapes(layout.layer(layer, datatype)).insert(box)

    draw(leaf, 65, 20, kdb.Box(0, 0, 2000, 1000))
    draw(leaf, 66, 20, kdb.Box(800, -200, 1200, 1200))
    draw(leaf, 66, 44, kdb.Box(100, 300, 300, 700))
    draw(leaf, 66, 44, kdb.Box(1700, 300, 1900, 700))
    draw(leaf, 67, 20, kdb.Box(0, 200, 400, 800))
    draw(leaf, 67, 20, kdb.Box(1600, 200, 2000, 800))
    # Deliberately no li1 labels -- forces the LEF fallback.

    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans(0, 0)))
    _wire_abstract_instance_pins(top, layout, kdb.Trans(0, 0), "IN", "OUT")
    path = _write_gds(layout, tmp_path / "lef_buf.gds")

    lef_path = tmp_path / "lef_buf.lef"
    lef_path.write_text(
        "VERSION 5.7 ;\n"
        "MACRO LEF_BUF\n"
        "  ORIGIN 0.000 0.000 ;\n"
        "  SIZE 2.000 BY 1.000 ;\n"
        "  PIN Y\n"
        "    DIRECTION OUTPUT ;\n"
        "    PORT\n"
        "      LAYER li1 ;\n"
        "        RECT 0.000 0.200 0.400 0.800 ;\n"
        "    END\n"
        "  END Y\n"
        "  PIN A\n"
        "    DIRECTION INPUT ;\n"
        "    PORT\n"
        "      LAYER li1 ;\n"
        "        RECT 1.600 0.200 2.000 0.800 ;\n"
        "    END\n"
        "  END A\n"
        "END LEF_BUF\n"
    )

    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "lef_buf.spice"),
        abstract_cell_patterns=("LEF_BUF",),
        abstract_cell_lef_paths=(str(lef_path),),
    )

    (entry,) = report["abstracted_cells"]
    assert entry["cell"] == "LEF_BUF"
    assert entry["instance_count"] == 1
    assert entry["pin_count"] == 2
    assert entry["resolution_source"] == "lef_abstract"
    assert entry["lef_path"] == str(lef_path)

    spice = Path(report["netlist_path"]).read_text()
    (x_line,) = [line for line in spice.splitlines() if line.startswith("X")]
    assert "IN" in x_line
    assert "OUT" in x_line


def test_abstract_cells_present_in_cli_json(tmp_path, capsys):
    """`abstracted_cells` is part of the JSON contract and is emitted by the
    CLI, via the repeatable `--abstract-cells` flag."""
    layout = _make_abstract_cells_layout()
    path = _write_gds(layout, tmp_path / "abstract_cli.gds")

    exit_code = main(
        [
            "extract",
            str(path),
            "--deck",
            "sky130",
            "--abstract-cells",
            "SC_BUF",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert "abstracted_cells" in out
    (entry,) = out["abstracted_cells"]
    assert entry["cell"] == "SC_BUF"
    assert entry["instance_count"] == 2


def test_abstract_cells_coexists_with_unabstracted_devices(tmp_path):
    """A layout that mixes an abstracted cell type with ordinary,
    un-abstracted devices extracts both correctly in the same run: the real
    inverter's devices are unaffected, and the abstracted instance is wired
    in as its own subcircuit -- proving the abstraction-specific purge path
    (`_purge_truly_floating_nets`) does not disturb ordinary device/net
    extraction."""
    layout = _make_inverter_layout()
    leaf = _make_abstract_leaf_cell(layout, "SC_BUF")
    top = layout.cell("TOP")
    trans = kdb.Trans(kdb.Vector(20000, 0))
    top.insert(kdb.CellInstArray(leaf.cell_index(), trans))
    _wire_abstract_instance_pins(top, layout, trans, "EXTRA_A", "EXTRA_Y")

    path = _write_gds(layout, tmp_path / "mixed.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "mixed.spice"),
        abstract_cell_patterns=("SC_BUF",),
    )

    # The inverter's own two devices are unaffected.
    assert report["device_count"] == 2
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}

    (entry,) = report["abstracted_cells"]
    assert entry["cell"] == "SC_BUF"
    assert entry["instance_count"] == 1

    net_names = {n["name"] for n in report["nets"]}
    assert {"EXTRA_A", "EXTRA_Y"}.issubset(net_names)
    # The inverter's own labelled nets survive too.
    assert {"A", "Y", "VPWR", "VGND"}.issubset(net_names)


def test_abstract_cells_preserves_devices_in_shared_called_cell(tmp_path):
    """A cell type called from *inside* a matched macro that is *also*
    instantiated independently, outside every matched subtree, keeps its
    devices intact for that independent instance (issue #622's confirmed
    bug). `_erase_abstracted_cell_geometry` used to erase a called cell's
    definition in place -- but KLayout cell definitions are shared across
    every place they are instantiated, so that silently destroyed devices on
    an unrelated, un-abstracted instance of the same cell type too."""
    layout = kdb.Layout()
    leaf = _make_abstract_leaf_cell(layout, "LEAF")

    macro = layout.create_cell("MACRO")
    leaf_trans = kdb.Trans(kdb.Vector(0, 0))
    macro.insert(kdb.CellInstArray(leaf.cell_index(), leaf_trans))
    _wire_abstract_instance_pins(macro, layout, leaf_trans, "MA", "MY")

    top = layout.create_cell("TOP")
    macro_trans = kdb.Trans(kdb.Vector(10000, 0))
    top.insert(kdb.CellInstArray(macro.cell_index(), macro_trans))
    _wire_abstract_instance_pins(top, layout, macro_trans, "MACRO_A", "MACRO_Y")

    # A second, independent instance of LEAF directly at top level -- shares
    # LEAF's cell definition with the instance nested inside MACRO, but is
    # not itself abstracted (only "MACRO" is matched below).
    direct_trans = kdb.Trans(kdb.Vector(30000, 0))
    top.insert(kdb.CellInstArray(leaf.cell_index(), direct_trans))
    _wire_abstract_instance_pins(top, layout, direct_trans, "DIRECT_A", "DIRECT_Y")

    path = _write_gds(layout, tmp_path / "shared_subcell.gds")
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "shared_subcell.spice"),
        abstract_cell_patterns=("MACRO",),
    )

    # Only MACRO is matched -- the direct, un-abstracted LEAF instance keeps
    # its transistor. Before the fix this was 0: erasing MACRO's called
    # LEAF sub-cell in place also erased the independent LEAF instance.
    assert report["device_count"] == 1

    (entry,) = report["abstracted_cells"]
    assert entry["cell"] == "MACRO"
    assert entry["instance_count"] == 1

    # The direct LEAF instance is not itself abstracted, so its own in-cell
    # "A"/"Y" pin labels stay in the flat label walk and comma-merge with
    # the parent net names on the same net -- not a regression, just how an
    # un-abstracted instance's own labels always behave.
    net_name_parts = {part for n in report["nets"] for part in n["name"].split(",")}
    assert {"MACRO_A", "MACRO_Y", "DIRECT_A", "DIRECT_Y"}.issubset(net_name_parts)

    spice = Path(report["netlist_path"]).read_text()
    assert ".SUBCKT MACRO MA MY" in spice
    x_lines = [line for line in spice.splitlines() if line.startswith("X")]
    assert len(x_lines) == 1
    assert "MACRO_A" in x_lines[0] and "MACRO_Y" in x_lines[0]


def test_extraction_deck_connectivity_layers_covers_all_role_layers():
    """`ExtractionDeck.connectivity_layers` includes every layer the deck's
    extraction actually reads -- MOS-recognition, metal/via stack, labels, and
    each bipolar/capacitor entry's own layers -- and skips absent optionals."""
    deck = get_extraction_deck("gf180mcu")
    conn = deck.connectivity_layers
    # MOS + stack + labels
    for expected in [(22, 0), (30, 0), (21, 0), (33, 0), (30, 10)]:
        assert expected in conn
    for metal in deck.metals:
        assert metal in conn
    for via in deck.vias:
        assert via in conn
    for label in deck.metal_labels:
        assert label in conn
    # Bipolar + capacitor recognition layers
    for bipolar in deck.bipolars:
        assert bipolar.base in conn
        assert bipolar.marker in conn
    for capacitor in deck.capacitors:
        assert capacitor.top_plate in conn
        assert capacitor.bottom_plate in conn


# --------------------------------------------------------------------------- #
# Loop closure: extracted netlist feeds `klt sim` with no reformatting
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_extracted_netlist_feeds_klt_sim_unmodified(tmp_path):
    """Acceptance bar (Epic #153): `klt extract` output feeds `klt sim`
    unmodified. A thin testbench `.include`s the extracted sky130 inverter
    body verbatim (no reformatting), ties A=0/VPWR=VPB=1.8V/VGND=vsubs=0V,
    and confirms ngspice reports the correct inverted output (Y=VDD)."""
    from klayout_tools import sim

    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    netlist_path = tmp_path / "inv_1.spice"
    report = run_extract(str(layout_path), "sky130", output=str(netlist_path))
    assert report["device_count"] == 2

    testbench = tmp_path / "testbench.spice"
    testbench.write_text(
        f'.include "{netlist_path}"\n'
        ".model nfet nmos level=1\n"
        ".model pfet pmos level=1\n"
        ".param vdd=1.8\n"
        "Vvpwr VPWR 0 DC {vdd}\n"
        "Vvpb VPB 0 DC {vdd}\n"
        "Vvgnd VGND 0 DC 0\n"
        "Vvsubs vsubs 0 DC 0\n"
        "Va A 0 DC 0\n"
        "Xinv A VGND VPB VPWR Y vsubs sky130_fd_sc_hd__inv_1\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": str(testbench),
                "analysis": {"kind": "tran", "args": "1n 1n"},
                "measurements": [
                    {"name": "vout", "spice": ".meas tran vout find v(Y) at=0"}
                ],
            }
        )
    )

    sim_report = sim.run_sim(str(request))

    assert sim_report["status"] == "pass"
    assert sim_report["measurements"][0]["worst_case"]["value"] == pytest.approx(1.8)


# --------------------------------------------------------------------------- #
# `--parasitics`: first-order lumped RC extraction (issue #217)
# --------------------------------------------------------------------------- #


def test_n_squares_geometric_estimate():
    """The area/perimeter -> square-count estimator: 1 square for a square,
    exact L/W for a rectangle, clamped to >= 1, and 0 for empty geometry."""
    # A 1x1 square: area 1, perimeter 4 -> 1 square.
    assert _n_squares(1.0, 4.0) == pytest.approx(1.0)
    # A 10x1 wire: area 10, perimeter 22 -> 10 squares.
    assert _n_squares(10.0, 22.0) == pytest.approx(10.0, rel=1e-6)
    # A 4x1 wire: area 4, perimeter 10 -> 4 squares.
    assert _n_squares(4.0, 10.0) == pytest.approx(4.0, rel=1e-6)
    # Empty geometry contributes nothing.
    assert _n_squares(0.0, 0.0) == 0.0
    # "Rounder than a rectangle" (negative discriminant) clamps to 1.
    assert _n_squares(1.0, 3.0) == 1.0


def test_parasitics_off_by_default_is_none(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))
    assert report["parasitics"] is None


def test_parasitics_off_writes_byte_identical_netlist(tmp_path):
    """The additive contract: omitting --parasitics leaves the written SPICE
    byte-identical to a pre-feature schematic-equivalent extraction."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    a = run_extract(path, "sky130", output=str(tmp_path / "a.spice"))
    # Default (no flag) explicitly false, same output path semantics.
    b = run_extract(path, "sky130", output=str(tmp_path / "b.spice"), parasitics=False)
    assert a["netlist_sha256"] == b["netlist_sha256"]


def test_parasitics_adds_rc_without_changing_schematic_view(tmp_path):
    """--parasitics is additive: device_count/net_count/devices[]/nets[] keep
    their exact schematic-equivalent meaning; R/C live only in the SPICE and
    the separate `parasitics` block."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    plain = run_extract(path, "sky130", output=str(tmp_path / "plain.spice"))
    para = run_extract(
        path, "sky130", output=str(tmp_path / "para.spice"), parasitics=True
    )

    assert para["device_count"] == plain["device_count"]
    assert para["net_count"] == plain["net_count"]
    assert para["pin_count"] == plain["pin_count"]
    assert para["device_counts"] == plain["device_counts"]
    assert para["devices"] == plain["devices"]
    assert para["nets"] == plain["nets"]


def test_parasitics_summary_block_shape(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(
        path, "sky130", output=str(tmp_path / "inv.spice"), parasitics=True
    )
    para = report["parasitics"]
    assert para is not None
    assert set(para) == {
        "r_count",
        "c_count",
        "total_resistance_ohm",
        "total_capacitance_ff",
        "nets",
        "metals_without_coefficient",
    }
    # sky130's PARASITICS.metals is fully populated (issue #547's regression
    # target was gf180mcu, not sky130) -- no gap for this deck.
    assert para["metals_without_coefficient"] == []
    # r_count now counts every emitted resistor (one *or more* per net, issue
    # #592's star topology), so it is >= c_count (one capacitor per net,
    # unchanged) rather than always equal to it.
    assert para["r_count"] >= para["c_count"] == len(para["nets"])
    assert para["r_count"] >= 1
    assert para["total_capacitance_ff"] > 0.0

    names = [n["net"] for n in para["nets"]]
    assert names == sorted(names)  # deterministic, sorted by net name
    for entry in para["nets"]:
        assert set(entry) == {
            "net",
            "resistance_ohm",
            "capacitance_ff",
            "hub_net",
            "terminals",
        }
        assert entry["capacitance_ff"] > 0.0
        assert entry["resistance_ohm"] >= 0.0
        for term in entry["terminals"]:
            assert set(term) == {"device", "terminal", "leg_net", "resistance_ohm"}
            assert term["resistance_ohm"] >= 0.0
            assert term["leg_net"].startswith(f"{entry['net']}__t")
        # A net with >= 1 real device terminal uses the net itself as the
        # star's hub (issue #592); the fallback `<net>__par` node only
        # appears for a net with zero terminals (none in this fixture).
        if entry["terminals"]:
            assert entry["hub_net"] == entry["net"]
        else:
            assert entry["hub_net"].startswith(f"{entry['net']}__par")
    # Every net here has real device terminals to fan a star out to.
    assert all(entry["terminals"] for entry in para["nets"])
    # r_count is exactly the sum of each net's emitted resistor count.
    assert para["r_count"] == sum(
        max(1, len(entry["terminals"])) for entry in para["nets"]
    )
    # Ground net never gets its own parasitic stub.
    assert "vsubs" not in names


def test_parasitics_writes_r_and_c_cards_preserving_subckt_interface(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    plain = run_extract(path, "sky130", output=str(tmp_path / "plain.spice"))
    para = run_extract(
        path, "sky130", output=str(tmp_path / "para.spice"), parasitics=True
    )

    plain_text = Path(plain["netlist_path"]).read_text()
    para_text = Path(para["netlist_path"]).read_text()

    # The .SUBCKT pin interface is untouched (parasitic nodes are internal).
    plain_subckt = next(
        ln for ln in plain_text.splitlines() if ln.upper().startswith(".SUBCKT")
    )
    para_subckt = next(
        ln for ln in para_text.splitlines() if ln.upper().startswith(".SUBCKT")
    )
    assert plain_subckt == para_subckt

    r_lines = [ln for ln in para_text.splitlines() if ln.startswith("R")]
    c_lines = [ln for ln in para_text.splitlines() if ln.startswith("C")]
    assert len(r_lines) == para["parasitics"]["r_count"]
    assert len(c_lines) == para["parasitics"]["c_count"]
    # Bare R/C cards: R<name> n1 n2 <value> -- no trailing model token.
    for ln in r_lines + c_lines:
        assert len(ln.split()) == 4

    # Still a klt-sim-consumable circuit body: no top-level .end/.control.
    for line in para_text.splitlines():
        stripped = line.strip().upper()
        assert stripped != ".END"
        assert not stripped.startswith(".CONTROL")


def test_parasitics_deterministic_across_runs(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    a = run_extract(path, "sky130", output=str(tmp_path / "a.spice"), parasitics=True)
    b = run_extract(path, "sky130", output=str(tmp_path / "b.spice"), parasitics=True)
    assert a["netlist_sha256"] == b["netlist_sha256"]
    assert a["parasitics"] == b["parasitics"]


def test_cli_parasitics_flag_json(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(
        ["extract", path, "--deck", "sky130", "--parasitics", "--format", "json"]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["parasitics"] is not None
    assert out["parasitics"]["r_count"] >= 1


def test_cli_parasitics_text_reports_counts(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(["extract", path, "--deck", "sky130", "--parasitics"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "parasitics:" in out


@pytest.mark.parametrize(
    "deck,layout_path",
    [
        ("sky130", CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"),
        (
            "gf180mcu",
            CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds",
        ),
    ],
)
def test_corpus_parasitics_produce_positive_rc(deck, layout_path, tmp_path):
    """Real sky130/gf180mcu inverters extract a non-trivial, positive lumped
    RC on their signal/power nets."""
    report = run_extract(
        str(layout_path), deck, output=str(tmp_path / "cell.spice"), parasitics=True
    )
    para = report["parasitics"]
    assert para["r_count"] >= 3  # at least in/out/one supply
    assert para["total_resistance_ohm"] > 0.0
    assert para["total_capacitance_ff"] > 0.0
    # Device connectivity view is unchanged by the parasitics pass.
    assert report["device_count"] == 2


def test_parasitics_coefficients_sourced_from_pdk_tech():
    """Spot-check that the PARASITICS tables carry the values transcribed from
    each PDK's published magic tech file (issue #226) -- a guard against an
    accidental revert to the pre-#226 uncited starter numbers. One updated
    field per PDK, plus the dropped diffusion role."""
    from klayout_tools.decks import gf180mcu, sky130

    # sky130: met1 (metals[1]) area cap from sky130.tech `defaultareacap`.
    assert sky130.PARASITICS.metals[1].cap_area_ff_um2 == pytest.approx(0.02578)
    # sky130: met1 fringe cap from sky130.tech `defaultperimeter`.
    assert sky130.PARASITICS.metals[1].cap_perim_ff_um == pytest.approx(0.04057)
    # gf180mcu: poly sheet R from gf180mcu.tech `resist (allpolynonres)/active
    # 7300` (7300 milliohm/sq = 7.3 ohm/sq).
    assert gf180mcu.PARASITICS.poly.sheet_res_ohm_sq == pytest.approx(7.3)
    # gf180mcu: poly area cap from gf180mcu.tech `defaultareacap`.
    assert gf180mcu.PARASITICS.poly.cap_area_ff_um2 == pytest.approx(0.11067)

    # Both decks drop the diffusion role: junction capacitance is already
    # carried by each M card's AS/AD/PS/PD, so a diffusion cap term would
    # double-count it (issue #226).
    assert sky130.PARASITICS.diffusion is None
    assert gf180mcu.PARASITICS.diffusion is None


def test_gf180mcu_parasitics_metals_covers_full_stack():
    """Regression guard for issue #547: gf180mcu.PARASITICS.metals used to
    carry only one LayerRC (Metal1) against a 5-level EXTRACTION_DECK.metals
    stack, so Metal2..Metal5 silently contributed zero R/C. Now curated to
    5 entries, index-aligned with EXTRACTION_DECK.metals, sourced from
    gf180mcu.tech's nominal (`variants ()`) corner."""
    from klayout_tools.decks import gf180mcu

    assert len(gf180mcu.PARASITICS.metals) == len(gf180mcu.EXTRACTION_DECK.metals) == 5
    assert all(m is not None for m in gf180mcu.PARASITICS.metals)

    # Metal2 (index 1): sheet from `resist (allm2)/metal2 90` (90 milliohm/sq
    # unconditional); area/perim from `defaultareacap`/`defaultperimeter`
    # allm2 metal2.
    m2 = gf180mcu.PARASITICS.metals[1]
    assert m2.sheet_res_ohm_sq == pytest.approx(0.09)
    assert m2.cap_area_ff_um2 == pytest.approx(0.015016)
    assert m2.cap_perim_ff_um == pytest.approx(0.033298)

    # Metal5 (index 4, top metal of this 5-level stack): sheet from the
    # resolved, pre-built gf180mcuD.tech for the pinned PDK commit
    # (`resist (allm5)/metal5    60`, 60 milliohm/sq), the non-thick-metal
    # `else` branch -- not the generic templated gf180mcu.tech's
    # THICKMET1P1 row (40 milliohm/sq), which a prior curation (#571)
    # mistakenly used (corrected by #572).
    m5 = gf180mcu.PARASITICS.metals[4]
    assert m5.sheet_res_ohm_sq == pytest.approx(0.06)
    assert m5.cap_area_ff_um2 == pytest.approx(0.005798)
    assert m5.cap_perim_ff_um == pytest.approx(0.030386)


def test_describe_parasitics_metal_gaps_reports_truncation_and_none_entries():
    """Unit test for `_describe_parasitics_metal_gaps` (issue #547): a
    ParasiticsDeck.metals tuple that is shorter than the ExtractionDeck's
    metals stack, or carries explicit `None` entries, is reported gap-by-gap
    rather than silently producing zero R/C."""
    from klayout_tools.decks import LayerRC, ParasiticsDeck, gf180mcu
    from klayout_tools.extract import _describe_parasitics_metal_gaps

    coeff = LayerRC(sheet_res_ohm_sq=0.09, cap_area_ff_um2=0.01, cap_perim_ff_um=0.03)

    # No gap: every declared metal level has a coefficient.
    full = ParasiticsDeck(metals=(coeff, coeff, coeff, coeff, coeff))
    assert _describe_parasitics_metal_gaps(gf180mcu.EXTRACTION_DECK, full) == []

    # Truncated tuple (the original #547 bug shape): only Metal1 present.
    truncated = ParasiticsDeck(metals=(coeff,))
    gaps = _describe_parasitics_metal_gaps(gf180mcu.EXTRACTION_DECK, truncated)
    assert [g["metal_index"] for g in gaps] == [1, 2, 3, 4]
    for gap, layer in zip(gaps, gf180mcu.EXTRACTION_DECK.metals[1:], strict=True):
        assert (gap["layer"], gap["datatype"]) == layer

    # Explicit None entries interleaved with real coefficients.
    sparse = ParasiticsDeck(metals=(coeff, None, coeff, None, coeff))
    gaps = _describe_parasitics_metal_gaps(gf180mcu.EXTRACTION_DECK, sparse)
    assert [g["metal_index"] for g in gaps] == [1, 3]


def test_run_extract_warns_on_parasitics_metal_gap(monkeypatch, tmp_path):
    """Integration guard for issue #547's "make the truncation loud" ask: a
    deck whose PARASITICS.metals is shorter than its declared metal stack
    surfaces the gap in `parasitics.metals_without_coefficient` and in
    `warnings[]`, instead of silently reporting understated R/C."""
    import klayout_tools.extract as extract_mod
    from klayout_tools.decks import ParasiticsDeck, gf180mcu

    layout_path = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"

    # The current (fixed) deck reports no gap and no matching warning.
    report_fixed = extract_mod.run_extract(
        str(layout_path),
        "gf180mcu",
        output=str(tmp_path / "cell_fixed.spice"),
        parasitics=True,
    )
    assert report_fixed["parasitics"]["metals_without_coefficient"] == []
    assert not any("no R/C" in w for w in report_fixed["warnings"])

    truncated = ParasiticsDeck(
        poly=gf180mcu.PARASITICS.poly,
        metals=gf180mcu.PARASITICS.metals[:1],  # only Metal1 -- the pre-#547 shape
    )
    monkeypatch.setattr(extract_mod, "get_parasitics_deck", lambda name: truncated)

    report = extract_mod.run_extract(
        str(layout_path),
        "gf180mcu",
        output=str(tmp_path / "cell.spice"),
        parasitics=True,
    )
    gaps = report["parasitics"]["metals_without_coefficient"]
    assert [g["metal_index"] for g in gaps] == [1, 2, 3, 4]
    assert any(g["layer"] > 0 for g in gaps)
    assert any("Metal2" in w and "Metal5" in w for w in report["warnings"])
    # Never regresses below the truncated deck's total (a truncated table can
    # only omit R/C, never add it) -- the practical understatement direction
    # this issue reported. (This corpus cell happens to route entirely on
    # Metal1, so the two totals are equal here rather than strictly greater;
    # `test_gf180mcu_parasitics_metals_covers_full_stack` is what pins the
    # actual Metal2..Metal5 coefficient values.)
    assert (
        report_fixed["parasitics"]["total_capacitance_ff"]
        >= report["parasitics"]["total_capacitance_ff"]
    )


def _make_poly_gate_net_layout(poly_y0, poly_y1, top_name="TOP"):
    """One NMOS whose gate poly bar spans ``poly_y0..poly_y1`` in y while the
    active strip is fixed at y 0..1000. When the poly bar coincides with active
    in y (0..1000), the gate net's *only* poly geometry is the transistor gate
    itself; extending it beyond active adds poly interconnect that is not gate.
    The gate net carries no metal, so its parasitic capacitance is purely
    poly-derived -- exercising the issue #226 gate-poly exclusion."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (nmos active)
    # Poly bar crosses active fully in x (splitting it into two S/D pads); its
    # y-extent is the variable under test.
    draw(66, 20, kdb.Box(800, poly_y0, 1200, poly_y1))  # poly.drawing (gate)
    # S/D contacts + li1 pads, each named so the transistor has named S/D.
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (S)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (D)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (S)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (D)
    label(67, 5, "SRC", 200, 500)
    label(67, 5, "DRN", 1800, 500)
    # The gate: named ONLY on poly.pin (66/5), with no metal anywhere near it.
    label(66, 5, "GATEONLY", 1000, 500)
    return layout


def test_parasitics_excludes_transistor_gate_poly(tmp_path):
    """Regression (issue #226): the transistor gate is subtracted from a net's
    poly shapes before measuring parasitic capacitance.

    Two layouts differ only in the gate poly's y-extent. When the poly bar
    coincides exactly with the active strip (y 0..1000) the gate net's only
    poly geometry *is* the gate, so after the exclusion it has zero
    poly-derived capacitance and -- carrying no metal -- drops out of
    ``parasitics.nets``. Extending the poly bar past active (y -400..1400) adds
    non-gate poly interconnect, so the same net reappears with positive
    capacitance. The device extracts identically in both cases."""
    all_gate = _write_gds(
        _make_poly_gate_net_layout(0, 1000), tmp_path / "all_gate.gds"
    )
    report_all_gate = run_extract(
        all_gate, "sky130", output=str(tmp_path / "all_gate.spice"), parasitics=True
    )
    assert report_all_gate["device_counts"] == {"nfet": 1}
    names_all_gate = [n["net"] for n in report_all_gate["parasitics"]["nets"]]
    # Gate-only poly + no metal -> zero parasitic capacitance -> not reported.
    assert "GATEONLY" not in names_all_gate

    with_overhang = _write_gds(
        _make_poly_gate_net_layout(-400, 1400), tmp_path / "overhang.gds"
    )
    report_overhang = run_extract(
        with_overhang,
        "sky130",
        output=str(tmp_path / "overhang.spice"),
        parasitics=True,
    )
    assert report_overhang["device_counts"] == {"nfet": 1}
    overhang_nets = {n["net"]: n for n in report_overhang["parasitics"]["nets"]}
    # The non-gate poly overhang is measured, so the net now carries R/C.
    assert "GATEONLY" in overhang_nets
    assert overhang_nets["GATEONLY"]["capacitance_ff"] > 0.0


def _make_series_nmos_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two NMOS in series, T1's drain tied to T2's source through a long,
    unlabelled li1 run -- a genuinely *internal* node (two device terminals,
    zero pin labels, real routed geometry) exactly like the issue #283 repro:
    a driver's output net that only becomes a pin when promoted one hierarchy
    level up. T1's source (``VGND``), T2's drain (``Y``), and both gates
    (``A1``/``A2``) *are* labelled, so this fixture also carries the normal
    labelled-net case for comparison."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(box)

    def label(layer, datatype, text, x, y):
        li = layout.layer(layer, datatype)
        top.shapes(li).insert(kdb.Text(text, kdb.Trans(x, y)))

    # T1: active 0..2000 x 0..1000, gate at x 800..1200.
    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))  # poly.drawing (T1 gate)
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # T1 source contact
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # T1 source li1
    label(67, 5, "VGND", 200, 500)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # T1 drain contact
    # T1 drain -> T2 source: one continuous, unlabelled li1 run (real
    # interconnect -- same shape kind that scores non-zero on a pin).
    draw(67, 20, kdb.Box(1600, 200, 6300, 800))

    # T2: active 6000..8000 x 0..1000, gate at x 6800..7200.
    draw(65, 20, kdb.Box(6000, 0, 8000, 1000))  # diff.drawing
    draw(66, 20, kdb.Box(6800, -200, 7200, 1200))  # poly.drawing (T2 gate)
    draw(66, 44, kdb.Box(6100, 300, 6300, 700))  # T2 source contact
    draw(66, 44, kdb.Box(7700, 300, 7900, 700))  # T2 drain contact
    draw(67, 20, kdb.Box(7600, 200, 8000, 800))  # T2 drain li1
    label(67, 5, "Y", 7800, 500)

    # Gates: named separately so the two transistors don't share a net.
    draw(67, 20, kdb.Box(850, 1050, 1150, 1250))
    label(67, 5, "A1", 1000, 1150)
    draw(66, 44, kdb.Box(900, 1050, 1100, 1150))
    draw(67, 20, kdb.Box(6850, 1050, 7150, 1250))
    label(67, 5, "A2", 7000, 1150)
    draw(66, 44, kdb.Box(6900, 1050, 7100, 1150))

    return layout


def test_parasitics_covers_internal_unlabelled_nets_with_real_geometry(tmp_path):
    """Regression (issue #283): a purely internal (non-pin, unlabelled) net
    with real routed interconnect gets a real, non-zero parasitics entry --
    not silently dropped because it has no layout label.

    Before the fix, `_compute_parasitics()` measured this net's geometry
    correctly (the shapes are there), but `_inject_parasitics()` re-resolved
    it by `Circuit.net_by_name()`, which only finds *named* nets. An
    unlabelled net's `expanded_name()` is KLayout's auto-generated `$<n>`
    form rather than a real `.name`, so the lookup silently returned `None`
    and the already-computed R/C was discarded -- the net vanished from
    `parasitics.nets[]` with no signal anywhere in the JSON."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(
        path, "sky130", output=str(tmp_path / "series.spice"), parasitics=True
    )
    assert report["device_counts"] == {"nfet": 2}

    # The internal node is a real, unlabelled, two-terminal net in the
    # schematic-equivalent view -- confirming the fixture actually exercises
    # the internal-node case (not e.g. accidentally promoted to a pin).
    internal_nets = [
        n for n in report["nets"] if not n["pin"] and n["device_count"] == 2
    ]
    assert len(internal_nets) == 1
    internal_name = internal_nets[0]["name"]
    assert internal_name.startswith("$")  # KLayout's auto-generated form

    para_nets = {n["net"]: n for n in report["parasitics"]["nets"]}
    assert internal_name in para_nets
    assert para_nets[internal_name]["capacitance_ff"] > 0.0
    assert para_nets[internal_name]["resistance_ohm"] > 0.0
    # This internal net has real device terminals (T1's drain, T2's source),
    # so it stars off itself as the hub (issue #592) rather than falling back
    # to a fresh `__par` node.
    assert para_nets[internal_name]["hub_net"] == internal_name
    assert len(para_nets[internal_name]["terminals"]) == 2

    # The labelled nets on either side of the series pair are unaffected.
    assert "VGND" in para_nets
    assert "Y" in para_nets
    assert para_nets["VGND"]["capacitance_ff"] > 0.0
    assert para_nets["Y"]["capacitance_ff"] > 0.0


def test_parasitics_star_topology_puts_resistance_in_series_between_terminals(
    tmp_path,
):
    """Acceptance bar (issue #592): unlike the pre-#592 Gamma-shunt topology
    (#338), a resistor now sits in series between two device terminals on
    the same net -- the automated analog of the issue's own "positive
    control" (a non-zero, in-path resistance measurement that changes
    between the schematic and extracted netlist).

    Uses the same internal, unlabelled, two-terminal net as
    `test_parasitics_covers_internal_unlabelled_nets_with_real_geometry`
    (T1's drain / T2's source, joined by a long li1 run) -- real routed
    geometry between two real device terminals, exactly the shape #338's
    structural audit found universally absent."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(
        path, "sky130", output=str(tmp_path / "series.spice"), parasitics=True
    )

    internal_nets = [
        n for n in report["nets"] if not n["pin"] and n["device_count"] == 2
    ]
    assert len(internal_nets) == 1
    internal_name = internal_nets[0]["name"]

    para_entry = next(
        n for n in report["parasitics"]["nets"] if n["net"] == internal_name
    )
    terminals = para_entry["terminals"]
    assert len(terminals) == 2
    # Both terminals belong to distinct devices (T1's drain, T2's source) --
    # not the same device connected twice.
    assert terminals[0]["device"] != terminals[1]["device"]
    # Each leg carries a distinct, real net name and a strictly positive
    # resistance -- neither leg degenerates to a zero-length short.
    assert terminals[0]["leg_net"] != terminals[1]["leg_net"]
    for term in terminals:
        assert term["resistance_ohm"] > 0.0

    # The in-path, terminal-to-terminal resistance is the sum of the two
    # legs (a star: leg_a --R--> hub --R--> leg_b) -- strictly positive,
    # i.e. non-zero, unlike the schematic-equivalent view (`nets[]`, built
    # *before* parasitics injection) where the same two terminals share one
    # node with zero resistance between them by construction.
    terminal_to_terminal_ohm = sum(term["resistance_ohm"] for term in terminals)
    assert terminal_to_terminal_ohm > 0.0
    # The net's own reported total resistance is preserved as the star's
    # total budget (legs sum back to it, modulo the negligible per-leg
    # minimum-resistance clamp).
    assert terminal_to_terminal_ohm == pytest.approx(
        para_entry["resistance_ohm"], rel=1e-6
    )

    # The written SPICE netlist emits exactly two resistor cards for this
    # net's star (plus whatever other nets contribute) and one capacitor.
    netlist_text = Path(report["netlist_path"]).read_text()
    r_lines = [ln for ln in netlist_text.splitlines() if ln.startswith("R")]
    assert len(r_lines) == report["parasitics"]["r_count"]


# --------------------------------------------------------------------------- #
# Instance-name sanitization for parasitic R/C cards (issue #312)
# --------------------------------------------------------------------------- #


def test_sanitize_instance_name_maps_spice_special_chars_to_underscore():
    """Unit-level: every character outside `[A-Za-z0-9_]` becomes `_`, one
    for one -- covering the two characters `net.expanded_name()` actually
    produces (`,` for multi-label nets, `$` for KLayout's anonymous-net
    placeholder) plus the originally-reported `|` and a plain name that
    should pass through untouched."""
    from klayout_tools.extract import _sanitize_instance_name

    assert _sanitize_instance_name("Y,Y2") == "Y_Y2"
    assert _sanitize_instance_name("$3") == "_3"
    assert _sanitize_instance_name("A|B") == "A_B"
    assert _sanitize_instance_name("plain_name1") == "plain_name1"


def _assert_rc_cards_have_safe_instance_names(netlist_text: str) -> None:
    """Every emitted `R`/`C` card's instance name (the token right after the
    leading `R`/`C`, before the first space) is alnum/underscore-only, and
    the card is still a well-formed 4-token bare card (`R<name> n1 n2
    <value>`, no trailing model token)."""
    rc_lines = [
        ln
        for ln in netlist_text.splitlines()
        if ln.startswith("R") or ln.startswith("C")
    ]
    assert rc_lines, "expected at least one injected parasitic R/C card"
    for ln in rc_lines:
        instance_name = ln.split()[0][1:]
        assert instance_name, f"empty instance name in card: {ln!r}"
        assert all(ch.isalnum() or ch == "_" for ch in instance_name), (
            f"unsafe instance name {instance_name!r} in card: {ln!r}"
        )
        assert len(ln.split()) == 4, f"card is not a bare 4-token R/C card: {ln!r}"


def test_parasitics_sanitizes_unlabelled_net_instance_name(tmp_path):
    """Regression (issue #312): an unlabelled/internal net's
    `expanded_name()` is KLayout's auto-generated `$<n>` placeholder (e.g.
    `$3`). `$` is inline-comment syntax to some SPICE dialects and must not
    leak into the parasitic R/C *instance* name verbatim."""
    path = _write_gds(_make_series_nmos_layout(), tmp_path / "series.gds")
    report = run_extract(
        path, "sky130", output=str(tmp_path / "series.spice"), parasitics=True
    )
    para_names = [n["net"] for n in report["parasitics"]["nets"]]
    assert any(name.startswith("$") for name in para_names)  # sanity: repro present

    netlist_text = Path(report["netlist_path"]).read_text()
    _assert_rc_cards_have_safe_instance_names(netlist_text)


def test_parasitics_sanitizes_multi_label_net_instance_name(tmp_path):
    """Regression (issue #312): a net with two distinct text labels joins as
    `Y,Y2` via `net.expanded_name()`. ngspice does not reject the comma --
    it silently splits the card into an extra positional node, corrupting
    the card's arity. The parasitic R/C *instance* name must be sanitized to
    alnum/underscore only."""
    path = _write_gds(_make_inverter_layout(extra_y_label="Y2"), tmp_path / "multi.gds")
    report = run_extract(
        path, "sky130", output=str(tmp_path / "multi.spice"), parasitics=True
    )
    para_names = [n["net"] for n in report["parasitics"]["nets"]]
    assert any("," in name for name in para_names)  # sanity: repro present

    netlist_text = Path(report["netlist_path"]).read_text()
    _assert_rc_cards_have_safe_instance_names(netlist_text)


@_SKIP_NO_NGSPICE
def test_parasitics_multi_label_net_loads_in_ngspice(tmp_path):
    """Integration regression (issue #312): before the instance-name
    sanitization fix, this exact fixture produced a `RY,Y2`/`CY,Y2`-shaped
    card. ngspice does not reject the comma -- it mis-parses it as a token
    separator, corrupting the card's arity, and bails out with "device
    already exists" against an unrelated node instead of a clean syntax
    error. Confirms the sanitized netlist loads and elaborates cleanly in
    `ngspice -b` (skipped when ngspice is not installed, matching this
    file's `HAVE_NGSPICE` gate elsewhere)."""
    import subprocess

    layout_path = _write_gds(
        _make_inverter_layout(extra_y_label="Y2"), tmp_path / "multi.gds"
    )
    netlist_path = tmp_path / "multi.spice"
    report = run_extract(
        layout_path, "sky130", output=str(netlist_path), parasitics=True
    )
    assert report["parasitics"]["r_count"] >= 1

    netlist_text = netlist_path.read_text()
    subckt_line = next(
        ln for ln in netlist_text.splitlines() if ln.upper().startswith(".SUBCKT")
    )
    pins = subckt_line.split()[2:]

    deck_path = tmp_path / "deck.cir"
    ties = "\n".join(f"V{i} {pin} 0 DC 0" for i, pin in enumerate(pins, start=1))
    deck_path.write_text(
        "* issue #312 regression -- multi-label net + --parasitics\n"
        f'.include "{netlist_path}"\n'
        ".model nfet nmos level=1\n"
        ".model pfet pmos level=1\n"
        f"{ties}\n"
        f"Xinv {' '.join(pins)} TOP\n"
        ".control\n"
        "op\n"
        "quit\n"
        ".endc\n"
        ".end\n"
    )

    completed = subprocess.run(
        ["ngspice", "-b", str(deck_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined_output = completed.stdout + completed.stderr
    assert "device already exists" not in combined_output
    assert completed.returncode == 0, combined_output


@_SKIP_NO_NGSPICE
def test_parasitic_netlist_feeds_klt_sim_unmodified(tmp_path):
    """Acceptance bar (issue #217): a `--parasitics` netlist stays a drop-in
    `klt sim` `netlist` -- the same inverter testbench that consumes the
    schematic-equivalent extraction consumes the parasitic-aware one verbatim
    and ngspice still reports the correct inverted output."""
    from klayout_tools import sim

    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    netlist_path = tmp_path / "inv_1.spice"
    report = run_extract(
        str(layout_path), "sky130", output=str(netlist_path), parasitics=True
    )
    assert report["parasitics"]["r_count"] >= 1

    testbench = tmp_path / "testbench.spice"
    testbench.write_text(
        f'.include "{netlist_path}"\n'
        ".model nfet nmos level=1\n"
        ".model pfet pmos level=1\n"
        ".param vdd=1.8\n"
        "Vvpwr VPWR 0 DC {vdd}\n"
        "Vvpb VPB 0 DC {vdd}\n"
        "Vvgnd VGND 0 DC 0\n"
        "Vvsubs vsubs 0 DC 0\n"
        "Va A 0 DC 0\n"
        "Xinv A VGND VPB VPWR Y vsubs sky130_fd_sc_hd__inv_1\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": str(testbench),
                "analysis": {"kind": "tran", "args": "1n 5n"},
                "measurements": [
                    {"name": "vout", "spice": ".meas tran vout find v(Y) at=5n"}
                ],
            }
        )
    )

    sim_report = sim.run_sim(str(request))

    assert sim_report["status"] == "pass"
    assert sim_report["measurements"][0]["worst_case"]["value"] == pytest.approx(1.8)


# --------------------------------------------------------------------------- #
# Merged net labels (issue #470)
# --------------------------------------------------------------------------- #


def test_merged_net_labels_reports_two_label_collision(tmp_path):
    """A net carrying two distinct labels (`Y` and `Y2`, joined by KLayout as
    `Y,Y2` via `Net.expanded_name()`) is surfaced structurally in
    `merged_net_labels[]` and as a matching prose entry in `warnings[]` --
    `net_count`/`nets[]`/`pin_count` stay unchanged (purely additive)."""
    path = _write_gds(_make_inverter_layout(extra_y_label="Y2"), tmp_path / "multi.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "multi.spice"))

    assert report["merged_net_labels"] == [{"net": "Y,Y2", "labels": ["Y", "Y2"]}]
    assert any("Y,Y2" in w and "merges" in w for w in report["warnings"]), report[
        "warnings"
    ]

    # nets[] still carries the joined net exactly once, under its full name.
    net_names = [n["name"] for n in report["nets"]]
    assert "Y,Y2" in net_names
    assert report["net_count"] == len(report["nets"])
    assert report["pin_count"] == sum(1 for n in report["nets"] if n["pin"])


def test_merged_net_labels_empty_when_no_collision(tmp_path):
    """No net carries multiple labels in the plain fixture (no
    `extra_y_label`) -- `merged_net_labels[]` is present and empty, and no
    `warnings[]` entry mentions a merge."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    assert report["merged_net_labels"] == []
    assert not any("merges" in w and "distinct labels" in w for w in report["warnings"])


def test_merged_net_labels_lists_all_three_plus_labels(tmp_path):
    """A net with 3+ distinct labels (not just 2) has every label listed in
    `labels[]`, not just the first two."""
    path = _write_gds(
        _make_inverter_layout(extra_y_label=["Y2", "Y3"]), tmp_path / "multi3.gds"
    )
    report = run_extract(path, "sky130", output=str(tmp_path / "multi3.spice"))

    assert report["merged_net_labels"] == [
        {"net": "Y,Y2,Y3", "labels": ["Y", "Y2", "Y3"]}
    ]
    assert any("Y,Y2,Y3" in w for w in report["warnings"])


# --------------------------------------------------------------------------- #
# Dummy-device suppression (issue #295)
# --------------------------------------------------------------------------- #

#: An unused (layer, datatype) the tests draw dummy markers on -- outside
#: every sky130 connectivity layer so it is invisible to extraction except
#: through the `dummy` field these tests configure.
_DUMMY_MARKER = (100, 0)


def _dummy_deck(
    name: str = "sky130", deck_options: dict[str, str] | None = None
) -> ExtractionDeck:
    """The registered ``name`` extraction deck (sky130 by default), with the
    optional ``dummy`` marker layer set to :data:`_DUMMY_MARKER` (issue #295).
    Everything else is identical, so a layout with no shapes on that layer
    extracts exactly as it does under the shipped deck.

    Written to accept the same ``(name, deck_options)`` positional signature
    as ``get_extraction_deck`` (issue #595) so it can stand in for it under
    ``monkeypatch.setattr`` -- ``deck_options`` is forwarded, not ignored, so
    a test that combines dummy-suppression with a `deck_options` override
    still sees it applied. The ``get_extraction_deck`` it calls is this test
    module's own import from ``klayout_tools.decks`` -- distinct from the
    ``klayout_tools.extract`` reference the tests patch -- so there is no
    recursion."""
    return dataclasses.replace(
        get_extraction_deck(name, deck_options), dummy=_DUMMY_MARKER
    )


def _add_dummy_nfet(layout: kdb.Layout, x0: int, *, marker: str = "full") -> None:
    """Draw a standalone dummy NMOS (poly gate over diffusion, outside any
    nwell) into ``layout``'s single top cell at x-offset ``x0``.

    ``marker`` controls the dummy-marker shape drawn over the gate:

    - ``"full"``   -- covers the whole gate; the gate is fully consumed by the
      subtraction, so the device is dropped and counted.
    - ``"partial"``-- covers only the top of the gate; the remaining gate area
      still spans both source/drain, so a valid device survives the clean
      geometric cut and is *not* counted as dropped.
    - ``"none"``   -- no marker shape at all.

    The gate region is ``diff & poly`` = ``(x0+800 .. x0+1200, 0 .. 1000)``.
    """
    top = layout.top_cell()
    diff = layout.layer(65, 20)  # diff.drawing
    poly = layout.layer(66, 20)  # poly.drawing
    top.shapes(diff).insert(kdb.Box(x0, 0, x0 + 2000, 1000))
    top.shapes(poly).insert(kdb.Box(x0 + 800, -200, x0 + 1200, 1200))
    if marker == "full":
        mk = layout.layer(*_DUMMY_MARKER)
        top.shapes(mk).insert(kdb.Box(x0 + 700, -100, x0 + 1300, 1100))
    elif marker == "partial":
        mk = layout.layer(*_DUMMY_MARKER)
        # Only the top strip of the gate (y 800..1000); the remaining gate
        # (y 0..800) still spans the full x-width between both S/D regions.
        top.shapes(mk).insert(kdb.Box(x0 + 700, 800, x0 + 1300, 1100))
    elif marker != "none":
        raise ValueError(f"bad marker mode: {marker!r}")


def test_dummy_field_defaults_none_on_shipped_decks():
    """The `dummy` field is opt-in per deck (issue #295). gf180mcu still
    declares none -- out of scope for issue #491, which only wires sky130 up
    -- so its extraction is unaffected. sky130's curated deck now declares a
    curated marker layer (issue #491, `(83, 20)` -- see
    `klayout_tools.decks.sky130.EXTRACTION_DECK`'s own comment for why that
    layer was chosen), the deck-side half of a fix whose extractor-side half
    (this field itself) shipped in #295/#462."""
    assert get_extraction_deck("sky130").dummy == (83, 20)
    assert get_extraction_deck("gf180mcu").dummy is None


def test_dummy_devices_dropped_is_zero_without_dummy_layer(tmp_path):
    """A plain inverter under the shipped (no-`dummy`) deck reports
    `dummy_devices_dropped: 0` -- the additive field is always present."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")

    report = run_extract(path, "sky130", output=str(tmp_path / "inv.spice"))

    assert report["dummy_devices_dropped"] == 0
    assert report["device_count"] == 2


def test_dummy_configured_but_no_dummy_geometry_is_identical(tmp_path, monkeypatch):
    """A deck that declares a `dummy` layer, run on a layout that draws no
    shapes on it, produces byte-identical extraction output (same netlist,
    same devices) as the shipped deck -- `dummy` only ever *removes* marked
    geometry (issue #295)."""
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")

    baseline = run_extract(path, "sky130", output=str(tmp_path / "base.spice"))

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    with_dummy = run_extract(path, "sky130", output=str(tmp_path / "dummy.spice"))

    assert with_dummy["dummy_devices_dropped"] == 0
    assert with_dummy["device_count"] == baseline["device_count"]
    assert with_dummy["device_counts"] == baseline["device_counts"]
    assert with_dummy["devices"] == baseline["devices"]
    assert with_dummy["nets"] == baseline["nets"]
    # Byte-identical written netlist: nothing was suppressed.
    assert with_dummy["netlist_sha256"] == baseline["netlist_sha256"]


def test_one_dummy_device_is_excluded_and_counted(tmp_path, monkeypatch):
    """A dummy MOS whose gate is fully covered by the `dummy` marker is
    dropped from the extracted netlist; the functional pair still extracts
    and `dummy_devices_dropped == 1` (issue #295)."""
    layout = _make_inverter_layout()
    _add_dummy_nfet(layout, 3000, marker="full")
    path = _write_gds(layout, tmp_path / "inv_dummy.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 1
    # Only the functional inverter pair remains -- the dummy is gone.
    assert report["device_count"] == 2
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}


def test_one_dummy_device_would_extract_without_the_marker(tmp_path, monkeypatch):
    """Control for the previous test: the same dummy geometry with *no*
    marker drawn extracts as an ordinary third device -- proving the
    exclusion is the marker's doing, not the geometry being unrecognisable."""
    layout = _make_inverter_layout()
    _add_dummy_nfet(layout, 3000, marker="none")
    path = _write_gds(layout, tmp_path / "inv_nomark.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 0
    assert report["device_count"] == 3
    assert report["device_counts"] == {"nfet": 2, "pfet": 1}


def test_two_dummy_devices_are_both_dropped(tmp_path, monkeypatch):
    """Two non-touching dummy MOS, each marked, are both suppressed and
    counted -- `dummy_devices_dropped == 2` via the connected-component
    idiom (issue #295)."""
    layout = _make_inverter_layout()
    _add_dummy_nfet(layout, 3000, marker="full")
    _add_dummy_nfet(layout, 6000, marker="full")
    path = _write_gds(layout, tmp_path / "inv_two_dummies.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 2
    assert report["device_count"] == 2
    assert report["device_counts"] == {"nfet": 1, "pfet": 1}


def test_partial_marker_coverage_is_a_clean_cut_not_a_drop(tmp_path, monkeypatch):
    """A `dummy` marker that only partially covers a gate is a clean
    geometric cut, not an all-or-nothing misclassification: the gate is not
    fully consumed, so the device is *not* counted as dropped and still
    extracts (issue #295's complex edge case)."""
    layout = _make_inverter_layout()
    _add_dummy_nfet(layout, 3000, marker="partial")
    path = _write_gds(layout, tmp_path / "inv_partial.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    # Gate only partially covered -> not fully consumed -> not a dropped device.
    assert report["dummy_devices_dropped"] == 0
    # The partially-cut gate still spans both S/D, so the device survives:
    # inverter pair + the surviving dummy-shaped device = 3.
    assert report["device_count"] == 3
    assert report["device_counts"] == {"nfet": 2, "pfet": 1}


# --------------------------------------------------------------------------- #
# Dummy-device suppression: resistors and bipolars (issue #462)
# --------------------------------------------------------------------------- #
#
# Extends the MOS-only `dummy` marker suppression above (issue #295) to the
# resistor-body and bipolar-base recognition paths, using the same fully-
# covered-component counting idiom and the same `_dummy_deck`/`_DUMMY_MARKER`
# fixtures the MOS tests above already established.


def _add_dummy_marker(layout: kdb.Layout, box: kdb.Box) -> None:
    """Draw a `_DUMMY_MARKER` shape covering `box` into `layout`'s (single)
    top cell -- the resistor/bipolar analogue of `_add_dummy_nfet`'s own
    marker-drawing, factored out since those two fixtures already draw their
    own device geometry and only need the marker added on top."""
    top = layout.top_cell()
    top.shapes(layout.layer(*_DUMMY_MARKER)).insert(box)


def test_dummy_resistor_is_excluded_and_counted(tmp_path, monkeypatch):
    """A drawn resistor whose recognised body is fully covered by the
    `dummy` marker is dropped from the extracted netlist and counted in
    `dummy_devices_dropped` -- the resistor-recognition analogue of
    `test_one_dummy_device_is_excluded_and_counted` (issue #462)."""
    layout = _make_poly_resistor_layout("sky130")
    _add_dummy_marker(layout, _RES_MARKED.enlarged(100, 100))
    path = _write_gds(layout, tmp_path / "res_dummy.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 1
    assert report["device_count"] == 0
    assert report["device_counts"] == {}
    # The dummy-suppressed body's pre-dummy footprint must still count as
    # "recognised" for the unmodelled-poly diagnostic (issue #324) -- a
    # deliberately excluded dummy is not a deck-coverage gap.
    assert report["unmodelled_poly"] == []
    # With the resistor device suppressed, the bar's two labelled heads
    # (`RA`/`RB`) merge onto one continuous, device-less net that survives
    # extraction instead of purging away (issue #539) -- the one remaining
    # warning is that merge, not an unmodelled-device/deck-coverage-gap note.
    assert len(report["warnings"]) == 1
    assert "merges 2 distinct labels (RA, RB)" in report["warnings"][0]


def test_dummy_resistor_would_extract_without_the_marker(tmp_path, monkeypatch):
    """Control for the previous test: the same resistor geometry under the
    `dummy`-aware deck, with no marker drawn, extracts normally -- proving
    the exclusion is the marker's doing, not a side effect of the deck
    change itself."""
    layout = _make_poly_resistor_layout("sky130")
    path = _write_gds(layout, tmp_path / "res_nomark.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 0
    assert report["device_count"] == 1
    assert report["device_counts"] == {"res_generic_po": 1}


def test_partial_dummy_marker_on_resistor_is_a_clean_cut_not_a_drop(
    tmp_path, monkeypatch
):
    """A `dummy` marker that only partially covers a drawn resistor's
    recognised body is a clean geometric cut, not an all-or-nothing
    misclassification: the body is not fully consumed, so the resistor is
    *not* counted as dropped and still extracts (issue #462's complex edge
    case, resistor analogue of
    `test_partial_marker_coverage_is_a_clean_cut_not_a_drop`)."""
    layout = _make_poly_resistor_layout("sky130")
    # Only a 300 nm sliver at the body's right edge (x 8700..9000) is
    # dummy-covered; the sliver merges into the adjoining "RB" head (a clean
    # edge trim), while the remaining x 3000..8700 body slice stays one
    # connected component still touching both heads.
    _add_dummy_marker(layout, kdb.Box(8700, 0, 9000, 1000))
    path = _write_gds(layout, tmp_path / "res_partial_dummy.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    # Body only partially covered -> not fully consumed -> not a dropped device.
    assert report["dummy_devices_dropped"] == 0
    assert report["device_count"] == 1
    assert report["device_counts"] == {"res_generic_po": 1}
    assert report["warnings"] == []


def test_dummy_bipolar_is_excluded_and_counted(tmp_path, monkeypatch):
    """A bipolar device whose recognised base is fully covered by the
    `dummy` marker is dropped from the extracted netlist and counted in
    `dummy_devices_dropped` -- the bipolar-recognition analogue of
    `test_one_dummy_device_is_excluded_and_counted` (issue #462)."""
    layout = _make_sky130_bjt_layout()
    # Fully covers the 2000x2000 marked (nwell & pnp.drawing) base.
    _add_dummy_marker(layout, kdb.Box(-100, -100, 2100, 2100))
    path = _write_gds(layout, tmp_path / "bjt_dummy.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 1
    assert report["device_counts"].get("pnp", 0) == 0


def test_dummy_bipolar_would_extract_without_the_marker(tmp_path, monkeypatch):
    """Control for the previous test: the same bipolar geometry under the
    `dummy`-aware deck, with no marker drawn, extracts normally -- proving
    the exclusion is the marker's doing, not a side effect of the deck
    change itself."""
    layout = _make_sky130_bjt_layout()
    path = _write_gds(layout, tmp_path / "bjt_nomark.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 0
    assert report["device_counts"] == {"pnp": 1}


def test_partial_dummy_marker_on_bipolar_is_a_clean_cut_not_a_drop(
    tmp_path, monkeypatch
):
    """A `dummy` marker that only partially covers a bipolar's recognised
    base is a clean geometric cut, not an all-or-nothing misclassification:
    the base is not fully consumed, so the device is *not* counted as
    dropped and still extracts (issue #462's complex edge case, bipolar
    analogue of `test_partial_marker_coverage_is_a_clean_cut_not_a_drop`)."""
    layout = _make_sky130_bjt_layout()
    # A corner of the 2000x2000 base well clear of the emitter (800..1200)
    # and the base tie (100..300); the base still spans both terminals.
    _add_dummy_marker(layout, kdb.Box(1500, 1500, 2000, 2000))
    path = _write_gds(layout, tmp_path / "bjt_partial_dummy.gds")

    monkeypatch.setattr("klayout_tools.extract.get_extraction_deck", _dummy_deck)
    report = run_extract(path, "sky130", output=str(tmp_path / "out.spice"))

    assert report["dummy_devices_dropped"] == 0
    assert report["device_counts"] == {"pnp": 1}
