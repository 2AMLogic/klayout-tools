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

import json
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import pdk
from klayout_tools.cli import main
from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.parasitics import _squares

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


def _make_inverter_layout(top_name: str = "TOP") -> kdb.Layout:
    """A minimal inverter: one NMOS (active outside nwell) and one PMOS
    (active inside nwell) sharing a poly gate, contacted up through li1 to
    met1, with li1/met1 pin labels naming every net -- shaped to exercise
    every layer role in `decks.sky130.EXTRACTION_DECK`."""
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
    label(67, 5, "A", 1000, 1500)

    # NMOS body: no drawn tap in this synthetic fixture -- exercises the
    # substrate-global fallback. PMOS body: an nwell tap (tap.drawing inside
    # the nwell) contacted to li1, labelled VPB.
    draw(65, 44, kdb.Box(-400, 2400, -200, 2600))  # tap.drawing (nwell tap)
    draw(66, 44, kdb.Box(-380, 2450, -220, 2550))  # licon1 over the tap
    draw(67, 20, kdb.Box(-450, 2400, -150, 2600))  # li1 over the tap
    label(64, 5, "VPB", -300, 2500)

    return layout


def _write_gds(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


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
    with pytest.raises(ExtractError, match="no open_pdks-layout PDK install"):
        run_extract(
            path,
            "sky130",
            pdk_variant="sky130A",
            pdk_root=str(tmp_path / "missing_pdk_root"),
        )


def test_output_directory_missing_is_application_error(tmp_path):
    path = _write_gds(_make_inverter_layout(), tmp_path / "inv.gds")
    with pytest.raises(ExtractError, match="output directory does not exist"):
        run_extract(path, "sky130", output=str(tmp_path / "no_such_dir" / "out.spice"))


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

    assert report["schema_version"] == 1
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
    assert report["pdk"] is None
    assert report["warnings"] == []

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
# CLI: exit codes, --format text/json
# --------------------------------------------------------------------------- #


def test_cli_json_exit_zero(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    exit_code = main(["extract", path, "--deck", "sky130", "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "extracted"
    assert out["device_count"] == 2


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

    assert report["schema_version"] == 1
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

    assert report["schema_version"] == 1
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


# --------------------------------------------------------------------------- #
# Loop closure: extracted netlist feeds `klt sim` with no reformatting
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# --parasitics: first-order lumped RC extraction (issue #217)
# --------------------------------------------------------------------------- #


def test_parasitics_omitted_leaves_output_byte_identical(tmp_path):
    """The flag is strictly additive: without it, both the written SPICE and
    the JSON response are exactly what they were before #217."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    report = run_extract(
        str(layout_path), "sky130", output=str(tmp_path / "plain.spice")
    )

    assert report["parasitics"] is None
    text = Path(report["netlist_path"]).read_text()
    assert "\nR" not in text
    assert "\nC" not in text
    for net in report["nets"]:
        assert "resistance_ohm" not in net
        assert "capacitance_ff" not in net


def test_parasitics_adds_rc_elements_to_the_same_netlist(tmp_path):
    """One SPICE file, still a `.SUBCKT` body: parasitic R/C elements are
    additional primitive cards alongside the existing device cards, never a
    second file or a different netlist shape."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    report = run_extract(
        str(layout_path),
        "sky130",
        output=str(tmp_path / "inv_1.spice"),
        parasitics=True,
    )

    parasitics = report["parasitics"]
    assert parasitics["model"] == "lumped_rc_load"
    assert parasitics["ground_net"] == "vsubs"
    assert parasitics["net_count"] > 0
    assert parasitics["resistor_count"] == parasitics["net_count"]
    assert parasitics["capacitor_count"] == parasitics["net_count"]
    assert parasitics["total_resistance_ohm"] > 0
    assert parasitics["total_capacitance_ff"] > 0

    text = Path(report["netlist_path"]).read_text()
    lines = [line for line in text.splitlines() if line and not line.startswith("*")]
    resistors = [line for line in lines if line.startswith("R")]
    capacitors = [line for line in lines if line.startswith("C")]
    assert len(resistors) == parasitics["resistor_count"]
    assert len(capacitors) == parasitics["capacitor_count"]
    # Still exactly one circuit body, with the device cards untouched.
    assert text.count(".SUBCKT") == 1
    assert text.count(".ENDS") == 1
    assert len([line for line in lines if line.startswith("M")]) == 2


def test_parasitic_cards_carry_no_model_name(tmp_path):
    """Each generated element is a *primitive* R/C card -- `R<name> a b
    <ohms>` -- with no trailing model name. KLayout's default device writer
    would append the device-class name, which SPICE reads as a semiconductor
    resistor's model and ngspice rejects outright."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    report = run_extract(
        str(layout_path),
        "sky130",
        output=str(tmp_path / "inv_1.spice"),
        parasitics=True,
    )

    text = Path(report["netlist_path"]).read_text()
    cards = [line for line in text.splitlines() if line and not line.startswith("*")]
    assert not any("parasitic_" in line for line in cards)
    for line in cards:
        if line.startswith(("R", "C")):
            fields = line.split()
            assert len(fields) == 4, f"expected `<name> a b value`, got: {line}"
            float(fields[3])  # the value field parses as a plain number


def test_parasitics_does_not_change_the_device_or_net_view(tmp_path):
    """`devices[]`/`device_counts`/`nets[]` keep their exact documented
    meaning with the flag on: the generated R/C elements are not devices, and
    the internal RC nodes are not nets."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    plain = run_extract(
        str(layout_path), "sky130", output=str(tmp_path / "plain.spice")
    )
    annotated = run_extract(
        str(layout_path),
        "sky130",
        output=str(tmp_path / "rc.spice"),
        parasitics=True,
    )

    assert annotated["device_count"] == plain["device_count"]
    assert annotated["device_counts"] == plain["device_counts"]
    assert annotated["devices"] == plain["devices"]
    assert annotated["net_count"] == plain["net_count"]
    assert annotated["pin_count"] == plain["pin_count"]

    plain_nets = {net["name"]: net for net in plain["nets"]}
    for net in annotated["nets"]:
        assert net["name"] in plain_nets
        assert not net["name"].endswith("_par")
        assert net["pin"] == plain_nets[net["name"]]["pin"]
        assert net["device_count"] == plain_nets[net["name"]]["device_count"]
        assert net["resistance_ohm"] >= 0.0
        assert net["capacitance_ff"] >= 0.0


def test_parasitics_per_net_values_sum_to_the_reported_totals(tmp_path):
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    report = run_extract(
        str(layout_path),
        "sky130",
        output=str(tmp_path / "inv_1.spice"),
        parasitics=True,
    )

    parasitics = report["parasitics"]
    assert sum(net["resistance_ohm"] for net in report["nets"]) == pytest.approx(
        parasitics["total_resistance_ohm"]
    )
    assert sum(net["capacitance_ff"] for net in report["nets"]) == pytest.approx(
        parasitics["total_capacitance_ff"]
    )
    assert (
        sum(1 for net in report["nets"] if net["capacitance_ff"] > 0)
        == parasitics["net_count"]
    )


def test_parasitics_values_are_physically_plausible_for_a_sky130_inverter(tmp_path):
    """Order-of-magnitude bar for a real sky130 standard cell: a single
    inverter's per-net wire capacitance belongs in the sub-femtofarad range
    and its wire resistance in the ohms-to-kilohm range. Deliberately a
    plausibility check, not a golden number -- #216 rules calibration
    against silicon out of scope."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    report = run_extract(
        str(layout_path),
        "sky130",
        output=str(tmp_path / "inv_1.spice"),
        parasitics=True,
    )

    output_net = next(net for net in report["nets"] if net["name"] == "Y")
    assert 0.01 < output_net["capacitance_ff"] < 10.0
    assert 0.1 < output_net["resistance_ohm"] < 10000.0
    # The gate net runs on poly (48.2 ohm/sq) and picks up real resistance,
    # unlike a metal-only net.
    gate_net = next(net for net in report["nets"] if net["name"] == "A")
    assert gate_net["resistance_ohm"] > output_net["resistance_ohm"]


def test_parasitics_is_deterministic(tmp_path):
    """Same layout in, byte-identical netlist out (the guarantee the rest of
    `klt extract`'s output already makes)."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__nand2_2.gds"
    first = run_extract(
        str(layout_path), "sky130", output=str(tmp_path / "a.spice"), parasitics=True
    )
    second = run_extract(
        str(layout_path), "sky130", output=str(tmp_path / "b.spice"), parasitics=True
    )

    assert first["netlist_sha256"] == second["netlist_sha256"]
    assert first["parasitics"] == second["parasitics"]


@pytest.mark.parametrize(
    "layout_path", GF180MCU_CORPUS_FILES, ids=[p.name for p in GF180MCU_CORPUS_FILES]
)
def test_gf180mcu_corpus_parasitics(layout_path, tmp_path):
    """The second curated PDK: same flag, same resolution path, no new
    PDK-selection mechanism (`docs/design/lvs-extraction-spike.md` ->
    "Addendum (#216)" -> "PDK coverage")."""
    report = run_extract(
        str(layout_path),
        "gf180mcu",
        output=str(tmp_path / f"{layout_path.stem}.spice"),
        parasitics=True,
    )

    parasitics = report["parasitics"]
    assert parasitics["net_count"] > 0
    assert parasitics["total_capacitance_ff"] > 0
    assert parasitics["total_resistance_ohm"] > 0
    text = Path(report["netlist_path"]).read_text()
    assert any(line.startswith("R") for line in text.splitlines())
    assert any(line.startswith("C") for line in text.splitlines())


def test_parasitics_with_pdk_model_binding_keeps_both_card_rewrites(tmp_path):
    """`--parasitics` and `--pdk` compose: MOS devices still become `X`
    subcircuit calls and the parasitic elements are still bare R/C cards
    (KLayout allows exactly one writer delegate, so this is a real
    interaction, not an independent pair of code paths)."""
    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    root = _make_pdk_install(tmp_path, "sky130A")

    report = run_extract(
        str(layout_path),
        "sky130",
        pdk_variant="sky130A",
        pdk_root=root,
        output=str(tmp_path / "inv_1.spice"),
        parasitics=True,
    )

    text = Path(report["netlist_path"]).read_text()
    lines = [line for line in text.splitlines() if line and not line.startswith("*")]
    assert any(line.startswith("X") and "sky130_fd_pr__" in line for line in lines)
    assert not any(line.startswith("M") for line in lines)
    assert any(len(line.split()) == 4 for line in lines if line.startswith("R"))


def test_parasitics_on_a_layout_with_no_wiring_is_a_no_op(tmp_path):
    """A layout with no shapes on any curated parasitic layer still succeeds
    and simply reports an empty model -- never a crash or an invented value."""
    layout = kdb.Layout()
    top = layout.create_cell("EMPTY")
    top.shapes(layout.layer(64, 20)).insert(kdb.Box(0, 0, 1000, 1000))  # nwell only
    path = _write_gds(layout, tmp_path / "empty.gds")

    report = run_extract(
        path, "sky130", output=str(tmp_path / "empty.spice"), parasitics=True
    )

    assert report["status"] == "extracted"
    assert report["parasitics"]["resistor_count"] == 0
    assert report["parasitics"]["capacitor_count"] == 0
    assert report["parasitics"]["total_capacitance_ff"] == 0.0


def test_internal_node_name_avoids_a_collision(tmp_path):
    """A layout that already labels a net `<name>_par` cannot be shorted to
    the internal RC node generated for `<name>`."""
    layout = _make_inverter_layout()
    top = layout.top_cell()
    # Rename the NMOS source pin to "Y_par" -- a real, device-connected net
    # (so it survives `purge()`) that collides with the internal node name
    # the pass would otherwise generate for net "Y".
    li1_pin = layout.layer(67, 5)
    texts = [shape.text for shape in top.shapes(li1_pin).each()]
    top.shapes(li1_pin).clear()
    for text in texts:
        if text.string == "VGND":
            text.string = "Y_par"
        top.shapes(li1_pin).insert(text)
    path = _write_gds(layout, tmp_path / "collide.gds")

    report = run_extract(
        path, "sky130", output=str(tmp_path / "collide.spice"), parasitics=True
    )

    text = Path(report["netlist_path"]).read_text()
    net_names = {net["name"] for net in report["nets"]}
    assert "Y_par" in net_names  # the real, labelled net survives as itself
    assert "Y_par2" in text  # the generated node took the uniquified name


def test_parasitics_promotes_an_untied_substrate_net_with_a_warning(tmp_path):
    """A layout that never creates the substrate global (no NMOS) would leave
    every generated capacitor on an unreachable internal node. The pass
    promotes that net to a pin instead -- and says so in `warnings[]`, never
    silently."""
    layout = kdb.Layout()
    top = layout.create_cell("PMOS_ONLY")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))  # diff inside the well
    draw(64, 20, kdb.Box(-500, 1500, 2500, 3500))  # nwell
    draw(66, 20, kdb.Box(800, 1800, 1200, 3200))  # poly gate
    draw(66, 44, kdb.Box(100, 2300, 300, 2700))  # licon1 (S)
    draw(66, 44, kdb.Box(1700, 2300, 1900, 2700))  # licon1 (D)
    draw(67, 20, kdb.Box(0, 2200, 400, 2800))  # li1 (S)
    draw(67, 20, kdb.Box(1600, 2200, 2000, 2800))  # li1 (D)
    top.shapes(layout.layer(67, 5)).insert(kdb.Text("S", kdb.Trans(200, 2500)))
    top.shapes(layout.layer(67, 5)).insert(kdb.Text("D", kdb.Trans(1800, 2500)))
    path = _write_gds(layout, tmp_path / "pmos_only.gds")

    report = run_extract(
        path, "sky130", output=str(tmp_path / "pmos_only.spice"), parasitics=True
    )

    assert any("substrate net 'vsubs'" in warning for warning in report["warnings"])
    text = Path(report["netlist_path"]).read_text()
    subckt = next(line for line in text.splitlines() if line.startswith(".SUBCKT"))
    assert subckt.split()[-1] == "vsubs"


def test_cli_parasitics_flag_json_and_text(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))

    exit_code = main(["extract", path, "--deck", "sky130", "--parasitics"])
    assert exit_code == 0
    assert "parasitics: lumped_rc_load" in capsys.readouterr().out

    exit_code = main(
        ["extract", path, "--deck", "sky130", "--parasitics", "--format", "json"]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["parasitics"]["model"] == "lumped_rc_load"
    assert out["parasitics"]["resistor_count"] > 0


def test_cli_without_parasitics_reports_null(tmp_path, capsys):
    path = str(_write_gds(_make_inverter_layout(), tmp_path / "inv.gds"))
    assert main(["extract", path, "--deck", "sky130", "--format", "json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["parasitics"] is None


def test_equivalent_rectangle_square_count():
    """The square-count reduction behind the lumped R (see
    `klayout_tools.parasitics`'s "The model"): a 10x1 wire is 10 squares, a
    square pad is 1, and a shape too compact for any rectangle falls back to
    1 rather than producing a nonsense value."""
    dbu = 0.001

    def region(*boxes):
        return kdb.Region([kdb.Box(*box) for box in boxes]).merged()

    assert _squares(region((0, 0, 10000, 1000)), dbu) == pytest.approx(10.0)
    assert _squares(region((0, 0, 1000, 1000)), dbu) == pytest.approx(1.0)
    # Two disjoint 5-square segments on one layer sum in series.
    assert _squares(
        region((0, 0, 5000, 1000), (20000, 0, 25000, 1000)), dbu
    ) == pytest.approx(10.0)
    # An octagon (P^2 < 16A) has no real equivalent rectangle -> one square.
    octagon = kdb.Region(
        kdb.Polygon(
            [
                kdb.Point(300, 0),
                kdb.Point(700, 0),
                kdb.Point(1000, 300),
                kdb.Point(1000, 700),
                kdb.Point(700, 1000),
                kdb.Point(300, 1000),
                kdb.Point(0, 700),
                kdb.Point(0, 300),
            ]
        )
    )
    assert _squares(octagon, dbu) == pytest.approx(1.0)


@_SKIP_NO_NGSPICE
def test_parasitic_netlist_feeds_klt_sim_unmodified(tmp_path):
    """The acceptance bar of #217, mirroring
    `test_extracted_netlist_feeds_klt_sim_unmodified` below: a
    parasitics-annotated netlist is still directly consumable by `klt sim`
    with no reformatting, and still produces the correct DC answer."""
    from klayout_tools import sim

    layout_path = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
    netlist_path = tmp_path / "inv_1_rc.spice"
    report = run_extract(
        str(layout_path), "sky130", output=str(netlist_path), parasitics=True
    )
    assert report["device_count"] == 2
    assert report["parasitics"]["resistor_count"] > 0

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
