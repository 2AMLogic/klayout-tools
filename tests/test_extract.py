"""Tests for `klt extract` and the `run_extract` library function.

Two fixture sources, mirroring `tests/test_drc.py`: layouts built
programmatically with `klayout.db` for the edge cases (no devices, unknown
layers, several top cells), and the real standard-cell corpus checked in under
`tests/corpus/` for the round-trip cases — one sky130 cell and one gf180mcu
cell, so both decks are exercised headless in CI.

The netlist-shape assertions are the load-bearing ones: the emitted file must
be a circuit body `klt sim` can consume unmodified (see `docs/cli/sim.md` →
"Netlist convention" and `docs/cli/extract.md` → "Netlist convention").
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.extract import (
    SCHEMA_VERSION,
    ExtractError,
    _strip_deck_cards,
    run_extract,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
SKY130_INV = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"
GF180MCU_CLKINV = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"

#: (corpus layout, deck, expected top cell, expected nfet/pfet class names).
#: Both corpus cells are single-stage inverters, so each has exactly one
#: n-channel and one p-channel device — a fact independent of deck curation
#: detail, which makes it a safe cross-PDK assertion.
INVERTER_CASES = [
    pytest.param(
        SKY130_INV,
        "sky130",
        "sky130_fd_sc_hd__inv_1",
        ("nfet_01v8", "pfet_01v8"),
        id="sky130",
    ),
    pytest.param(
        GF180MCU_CLKINV,
        "gf180mcu",
        "gf180mcu_fd_sc_mcu9t5v0__clkinv_1",
        ("nfet_03v3", "pfet_03v3"),
        id="gf180mcu",
    ),
]

TOP_LEVEL_FIELDS = {
    "schema_version",
    "file",
    "deck",
    "top",
    "dbu_um",
    "pdk",
    "netlist_path",
    "netlist_sha256",
    "status",
    "device_count",
    "net_count",
    "pin_count",
    "device_counts",
    "devices",
    "nets",
    "warnings",
}


def _make_empty_layout() -> kdb.Layout:
    """A layout with geometry on a deck layer but no device-forming overlap.

    A lone poly rectangle with no diffusion under it forms no channel, so
    extraction succeeds and finds zero devices — the "no extractable devices"
    edge case, distinct from a failed run.
    """
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    top.shapes(poly).insert(kdb.Box(0, 0, 1000, 200))
    return layout


def _make_unknown_layer_layout() -> kdb.Layout:
    """A layout whose only geometry sits on a layer no deck knows about."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    unknown = layout.layer(1234, 7)
    top.shapes(unknown).insert(kdb.Box(0, 0, 1000, 1000))
    return layout


def _make_two_top_cells() -> kdb.Layout:
    layout = kdb.Layout()
    poly = layout.layer(66, 20)
    for name in ("ALPHA", "BETA"):
        cell = layout.create_cell(name)
        cell.shapes(poly).insert(kdb.Box(0, 0, 1000, 200))
    return layout


# ---------------------------------------------------------------------------
# Round trip on the real corpus, both decks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout_path,deck,top,classes", INVERTER_CASES)
def test_corpus_round_trip(tmp_path, layout_path, deck, top, classes):
    netlist = tmp_path / "out.spice"
    report = run_extract(str(layout_path), deck_name=deck, output=str(netlist))

    assert report["status"] == "extracted"
    assert report["top"] == top
    assert report["deck"] == deck
    assert report["netlist_path"] == str(netlist)
    assert netlist.is_file()

    nfet, pfet = classes
    assert report["device_counts"] == {nfet: 1, pfet: 1}
    assert report["device_count"] == 2
    # An inverter's pins: in, out, and the supply/bulk rails.
    assert report["pin_count"] >= 4
    assert report["net_count"] >= report["pin_count"]


@pytest.mark.parametrize("layout_path,deck,top,classes", INVERTER_CASES)
def test_json_contract(tmp_path, capsys, layout_path, deck, top, classes):
    netlist = tmp_path / "out.spice"
    exit_code = main(
        [
            "extract",
            str(layout_path),
            "--deck",
            deck,
            "-o",
            str(netlist),
            "--format",
            "json",
        ]
    )
    assert exit_code == 0

    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == TOP_LEVEL_FIELDS
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["file"] == str(layout_path)
    assert data["deck"] == deck
    assert data["top"] == top
    assert data["dbu_um"] == pytest.approx(0.001)
    assert data["pdk"] is None
    assert data["status"] == "extracted"
    assert isinstance(data["warnings"], list)
    assert re.fullmatch(r"[0-9a-f]{64}", data["netlist_sha256"])

    for device in data["devices"]:
        assert set(device.keys()) == {"circuit", "name", "class", "nets", "params"}
        assert device["circuit"] == top
        assert device["class"] in classes
        # A four-terminal MOS: the engine's own terminal ids, kept verbatim.
        assert set(device["nets"]) == {"S", "G", "D", "B"}
        assert set(device["params"]) == {
            "ad_um2",
            "as_um2",
            "l_um",
            "pd_um",
            "ps_um",
            "w_um",
        }
        assert device["params"]["w_um"] > 0
        assert device["params"]["l_um"] > 0

    for net in data["nets"]:
        assert set(net.keys()) == {"circuit", "name", "pin", "device_count"}
        assert isinstance(net["pin"], bool)
        assert net["device_count"] >= 0
        assert net["name"]


@pytest.mark.parametrize("layout_path,deck,top,classes", INVERTER_CASES)
def test_topology_is_an_inverter(tmp_path, layout_path, deck, top, classes):
    """The extracted topology, not just the counts: both devices share a gate
    net and a drain net, which is what makes the cell an inverter."""
    netlist = tmp_path / "out.spice"
    report = run_extract(str(layout_path), deck_name=deck, output=str(netlist))

    by_class = {device["class"]: device for device in report["devices"]}
    nfet, pfet = classes
    assert by_class[nfet]["nets"]["G"] == by_class[pfet]["nets"]["G"]
    assert by_class[nfet]["nets"]["D"] == by_class[pfet]["nets"]["D"]
    # Sources go to opposite rails.
    assert by_class[nfet]["nets"]["S"] != by_class[pfet]["nets"]["S"]


@pytest.mark.parametrize("layout_path,deck,top,classes", INVERTER_CASES)
def test_output_is_deterministic(tmp_path, layout_path, deck, top, classes):
    """Same input, same bytes — the canonical-ordering guarantee."""
    first = tmp_path / "first.spice"
    second = tmp_path / "second.spice"
    a = run_extract(str(layout_path), deck_name=deck, output=str(first))
    b = run_extract(str(layout_path), deck_name=deck, output=str(second))

    assert first.read_text() == second.read_text()
    assert a["netlist_sha256"] == b["netlist_sha256"]
    assert a["devices"] == b["devices"]
    assert a["nets"] == b["nets"]


# ---------------------------------------------------------------------------
# The netlist is a `klt sim`-consumable circuit body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout_path,deck,top,classes", INVERTER_CASES)
def test_netlist_is_a_circuit_body(tmp_path, layout_path, deck, top, classes):
    """docs/cli/sim.md's netlist convention, asserted line by line.

    `klt sim` wraps the netlist it is given in a generated corner deck, so a
    file carrying its own `.end` or `.control` is explicitly unsupported
    there. This is the acceptance bar Epic #153 states as "`klt extract`
    output feeds `klt sim` unmodified".
    """
    netlist = tmp_path / "out.spice"
    run_extract(str(layout_path), deck_name=deck, output=str(netlist))
    text = netlist.read_text()

    cards = [
        line.strip().lower()
        for line in text.splitlines()
        if line.strip().startswith(".")
    ]
    assert ".end" not in cards
    assert not any(card.startswith(".control") for card in cards)
    assert not any(card.startswith(".endc") for card in cards)

    # The top cell is written as a subcircuit definition, closed by .ENDS.
    assert re.search(rf"(?im)^\.subckt\s+{re.escape(top)}\s", text)
    assert re.search(rf"(?im)^\.ends\s+{re.escape(top)}\s*$", text)

    # Net names, not KLayout's escaped `\$<id>` internal node syntax.
    assert "\\$" not in text


@pytest.mark.parametrize("layout_path,deck,top,classes", INVERTER_CASES)
def test_subckt_ports_match_pin_count(tmp_path, layout_path, deck, top, classes):
    """The `.SUBCKT` port list a caller instantiates is exactly `pin_count`
    ports wide, and every port is a reported pin net."""
    netlist = tmp_path / "out.spice"
    report = run_extract(str(layout_path), deck_name=deck, output=str(netlist))

    match = re.search(
        rf"(?im)^\.subckt\s+{re.escape(top)}\s+(.*)$", netlist.read_text()
    )
    assert match is not None
    ports = match.group(1).split()
    assert len(ports) == report["pin_count"]

    pin_nets = {
        net["name"] for net in report["nets"] if net["pin"] and net["circuit"] == top
    }
    assert set(ports) == pin_nets


def test_strip_deck_cards_removes_end_and_control():
    """The boundary guarantee, exercised directly.

    KLayout's writer emits neither card today; this keeps the contract fixed
    at the `klt extract` boundary regardless of what a future release writes.
    """
    text = (
        "* header\n"
        ".SUBCKT TOP a b\n"
        "M1 a b 0 0 nfet_01v8 L=1U W=1U\n"
        ".ENDS TOP\n"
        ".control\n"
        "op\n"
        ".endc\n"
        ".END\n"
    )
    stripped, removed = _strip_deck_cards(text)

    assert removed == 4
    assert ".ENDS TOP" in stripped  # the subcircuit terminator survives
    assert ".control" not in stripped
    assert ".endc" not in stripped
    assert not any(line.strip().lower() == ".end" for line in stripped.splitlines())


def test_strip_deck_cards_is_a_noop_on_a_clean_body():
    text = ".SUBCKT TOP a b\nM1 a b 0 0 nfet_01v8\n.ENDS TOP\n"
    assert _strip_deck_cards(text) == (text, 0)


# ---------------------------------------------------------------------------
# Defaults, flags, and text output
# ---------------------------------------------------------------------------


def test_default_output_path_replaces_the_extension(tmp_path):
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))

    report = run_extract(str(layout), deck_name="sky130")

    assert report["netlist_path"] == str(tmp_path / "design.spice")
    assert (tmp_path / "design.spice").is_file()


def test_output_directory_is_created(tmp_path):
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))
    netlist = tmp_path / "nested" / "dir" / "out.spice"

    run_extract(str(layout), deck_name="sky130", output=str(netlist))

    assert netlist.is_file()


def test_sha256_matches_the_written_file(tmp_path):
    import hashlib

    netlist = tmp_path / "out.spice"
    report = run_extract(str(SKY130_INV), deck_name="sky130", output=str(netlist))

    expected = hashlib.sha256(netlist.read_bytes()).hexdigest()
    assert report["netlist_sha256"] == expected


def test_default_format_is_text(tmp_path, capsys):
    netlist = tmp_path / "out.spice"
    assert (
        main(["extract", str(SKY130_INV), "--deck", "sky130", "-o", str(netlist)]) == 0
    )

    out = capsys.readouterr().out
    assert out.startswith("file: ")
    assert "deck: sky130" in out
    assert "status: extracted" in out
    assert "nfet_01v8" in out
    assert not out.lstrip().startswith("{")


def test_explicit_top_cell(tmp_path):
    layout = tmp_path / "two.gds"
    _make_two_top_cells().write(str(layout))

    report = run_extract(
        str(layout), deck_name="sky130", output=str(tmp_path / "o.spice"), top="BETA"
    )
    assert report["top"] == "BETA"


# ---------------------------------------------------------------------------
# Edge cases and error paths
# ---------------------------------------------------------------------------


def test_layout_with_no_devices_still_succeeds(tmp_path):
    """ "No extractable devices" is a successful run with a warning, not an
    error — the distinction an agent needs to tell "the layout has nothing"
    from "the tool broke"."""
    layout = tmp_path / "empty.gds"
    _make_empty_layout().write(str(layout))
    netlist = tmp_path / "out.spice"

    report = run_extract(str(layout), deck_name="sky130", output=str(netlist))

    assert report["status"] == "extracted"
    assert report["device_count"] == 0
    assert report["device_counts"] == {}
    assert netlist.is_file()
    assert any("no devices extracted" in w for w in report["warnings"])


def test_unrecognized_layer_is_not_an_error(tmp_path):
    """A layout whose geometry sits on layers no deck knows about extracts to
    an empty netlist rather than failing — the same "layer absent -> nothing to
    report" posture `klt drc` takes."""
    layout = tmp_path / "unknown.gds"
    _make_unknown_layer_layout().write(str(layout))
    netlist = tmp_path / "out.spice"

    report = run_extract(str(layout), deck_name="gf180mcu", output=str(netlist))

    assert report["status"] == "extracted"
    assert report["device_count"] == 0
    assert netlist.is_file()


def test_missing_file():
    with pytest.raises(ExtractError, match="file not found"):
        run_extract("does-not-exist.gds", deck_name="sky130")


def test_directory_is_not_a_file(tmp_path):
    with pytest.raises(ExtractError, match="not a file"):
        run_extract(str(tmp_path), deck_name="sky130")


def test_unknown_deck(tmp_path, capsys):
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))

    assert main(["extract", str(layout), "--deck", "not-a-real-deck"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "klt extract" in captured.err
    assert "unknown deck" in captured.err


def test_unknown_deck_json_format(tmp_path, capsys):
    """An unknown deck is an application error, so it uses the shared envelope."""
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))

    assert main(["extract", str(layout), "--deck", "nope", "--format", "json"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "extract"
    assert "unknown deck" in error["error"]["message"]


def test_deck_is_required_without_a_pdk(tmp_path, capsys):
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))

    assert main(["extract", str(layout)]) == 1
    assert "--deck is required" in capsys.readouterr().err


def test_several_top_cells_is_an_error(tmp_path):
    layout = tmp_path / "two.gds"
    _make_two_top_cells().write(str(layout))

    with pytest.raises(ExtractError, match="top cells"):
        run_extract(str(layout), deck_name="sky130")


def test_unknown_top_cell_is_an_error(tmp_path):
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))

    with pytest.raises(ExtractError, match="not found"):
        run_extract(str(layout), deck_name="sky130", top="NOPE")


def test_unresolvable_pdk_is_an_error(tmp_path):
    """An explicitly requested PDK is never silently downgraded."""
    layout = tmp_path / "design.gds"
    _make_empty_layout().write(str(layout))

    with pytest.raises(ExtractError):
        run_extract(
            str(layout),
            deck_name="sky130",
            output=str(tmp_path / "o.spice"),
            pdk="sky130A",
            pdk_root=str(tmp_path / "no-such-pdk-root"),
        )


def test_exit_code_2_is_reserved_for_argparse():
    """`klt extract` has no exit 3; usage errors still come from argparse."""
    with pytest.raises(SystemExit) as excinfo:
        main(["extract", "design.gds", "--deck", "sky130", "--format", "bogus"])
    assert excinfo.value.code == 2
