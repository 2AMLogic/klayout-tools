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
