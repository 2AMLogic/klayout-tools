"""The four remaining SPICE writers tolerate an ``nc_<letters>`` net (#6735).

Follow-up to #6732's flat-write fix: the ``--subcircuit`` slice write, the
runset-SPICE write, ``klt lvs``'s retained extracted deck and both netgen
input decks each built the same stock ``NetlistSpiceWriter``
(``use_net_names`` on) and called ``Netlist.write`` directly, so a net named
``nc_a``/``nc_ab`` aborted each one with ``Unexpected text after numeric
value``. Every case here was reproduced against ``origin/main`` before the
fix (all four sites raise that exact error) and now routes through
``extract_netlist_write.write_spice_netlist``.

The end-to-end cases reuse the drawn SG13G2 ``rppd`` fixture and the label
tables from ``test_extract_nc_net_label.py`` (both files stay out of the
size-capped ``test_extract.py``/``test_lvs.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools._provenance import sha256_file
from klayout_tools.cli import main
from klayout_tools.extract import run_extract
from klayout_tools.extract_runset_spice import _write_netlist
from klayout_tools.lvs import run_lvs
from test_extract import _make_sg13g2_poly_resistor_layout, _write_gds
from test_extract_nc_net_label import (
    _CONTROL_LABELS,
    _FAILING_LABELS,
    _top_circuit_with_nets,
)


def _two_level_gds(tmp_path, label: str) -> str:
    """``TOP`` instantiating the drawn SG13G2 ``rppd`` cell ``RES``, pads
    labelled ``label``/``vss`` -- issue #6735's reproducer layout."""
    import klayout.db as kdb

    layout = _make_sg13g2_poly_resistor_layout("rppd", 1.0, 5.0)
    res = layout.top_cell()
    shapes = res.shapes(layout.layer(8, 25))  # the fixture's Metal1.pin layer
    texts = sorted(
        (shape.text for shape in shapes.each() if shape.is_text()),
        key=lambda text: text.x,
    )
    assert len(texts) == 2
    shapes.clear()
    for text, name in zip(texts, (label, "vss"), strict=True):
        shapes.insert(kdb.Text(name, kdb.Trans(text.x, text.y)))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(res.cell_index(), kdb.Trans(0, 0)))
    return _write_gds(layout, tmp_path / f"two_level_{label}.gds")


# `--subcircuit` (the slice write in extract.py) --------------------------------


def _slice(tmp_path, capsys, label: str):
    """Run ``klt extract --subcircuit RES`` on the two-level fixture; return
    the parsed report and the slice deck's lines."""
    gds = _two_level_gds(tmp_path, label)
    out = tmp_path / "sliced.spice"
    code = main(
        [
            "extract",
            gds,
            "--deck",
            "sg13g2",
            "--top",
            "TOP",
            "--subcircuit",
            "RES",
            "--format",
            "json",
            "-o",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    report = json.loads(captured.out)
    lines = Path(report["subcircuit"]["path"]).read_text().splitlines()
    return report["subcircuit"], lines


@pytest.mark.parametrize(
    ("label", "token"),
    [*_FAILING_LABELS.items(), *_CONTROL_LABELS.items()],
)
def test_subcircuit_slice_writes_the_label_at_pins_and_terminals(
    tmp_path, capsys, label, token
):
    sub, lines = _slice(tmp_path, capsys, label)

    # The `.SUBCKT RES` header pin and the `X$1` resistor card's terminal
    # both carry the label's own spelling (`-` stays `\x2d`-escaped).
    (header,) = [line for line in lines if line.startswith(".SUBCKT RES ")]
    assert sorted(header.split()[2:]) == sorted([token, "vss", "vsubs"])
    (card,) = [line for line in lines if line.startswith("X$1 ")]
    assert card.split()[1:5] == [token, "vss", "vsubs", "rppd"]
    assert f"* pin {label}" in lines
    # The slice report's hash matches the deck on disk.
    assert sub["sha256"] == sha256_file(sub["path"])


def test_subcircuit_slice_report_is_unchanged_for_the_control(tmp_path, capsys):
    """The `nca` control's sub-circuit report keeps every field's value from
    before the fix (issue #6735's first acceptance criterion)."""
    sub, _lines = _slice(tmp_path, capsys, "nca")

    assert sub["cell"] == "RES"
    assert [pin["name"] for pin in sub["pins"]] == ["nca", "vss", "vsubs"]
    assert {pin["role"] for pin in sub["pins"]} == {"boundary"}
    assert sub["device_count"] == 1
    assert sub["net_count"] == 3
    assert sub["instance_line"] == "XDUT nca vss vsubs RES"


# The runset-SPICE write (extract_runset_spice._write_netlist) ------------------


@pytest.mark.parametrize("label", ["nc_a", "nc_ab", "nca"])
def test_runset_spice_write_tolerates_the_label(tmp_path, label):
    netlist, _circuit = _top_circuit_with_nets(label, "vss")

    reported_path, digest = _write_netlist(
        netlist=netlist,
        path=str(tmp_path / "runset.spice"),
        output=None,
        no_netlist=False,
        deck_basename="deck.lyr",
        models={},
        device_params={},
        uncarried={},
    )

    lines = Path(reported_path).read_text().splitlines()
    (header,) = [line for line in lines if line.startswith(".SUBCKT TOP ")]
    assert sorted(header.split()[2:]) == sorted([label, "vss"])
    assert digest == sha256_file(reported_path)


# The retained extracted deck (`klt lvs` + options.keep_extracted) --------------


def test_lvs_keep_extracted_writes_the_retained_deck_with_the_label(tmp_path):
    gds = _two_level_gds(tmp_path, "nc_a")
    # The reference is the flat extraction of the same layout -- itself
    # written through the fixed flat writer, so `nc_a` survives both sides.
    run_extract(gds, "sg13g2", output=str(tmp_path / "reference.spice"), top="TOP")
    request = {
        "layout": {"file": gds, "deck": "sg13g2", "top": "TOP"},
        "reference": {"netlist": str(tmp_path / "reference.spice"), "top": "TOP"},
        "options": {"keep_extracted": True},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))

    report = run_lvs(str(request_path))

    # The comparison verdict is not this issue's concern (the flat-written
    # reference's X-card resistor is not remappable -- a pre-existing
    # read-back quirk, reported as mismatches); the retained deck's tokens
    # are: the write itself must have carried the label through.
    retained = Path(report["environment"]["extracted_netlist"])
    assert retained == tmp_path / ".klt" / "lvs" / "TOP.spice"
    lines = retained.read_text().splitlines()
    (header,) = [line for line in lines if line.startswith(".SUBCKT TOP ")]
    assert sorted(header.split()[2:]) == sorted(["nc_a", "vss", "vsubs"])
    (card,) = [line for line in lines if line.startswith("R$1 ")]
    assert card.split()[1:4] == ["nc_a", "vss", "vsubs"]


# Both netgen input decks (lvs_netgen._run_netgen_lvs) --------------------------


def _run_netgen_with_stubbed_subprocess(layout_label: str, reference_label: str):
    """`_run_netgen_lvs` with netgen stubbed out; returns the two written
    decks' text, captured at the subprocess boundary (the work dir is gone
    by the time the call returns)."""
    from klayout_tools import lvs_netgen

    decks: dict[str, str] = {}

    class _Completed:
        stdout = "Netgen 1.5.323 compiled on today"

    def fake_run(cmd, capture_output, text, timeout):
        # cmd[3]/cmd[4] are the "<file> <cell>" pair per side.
        for pair in (cmd[3], cmd[4]):
            path = pair.rsplit(" ", 1)[0]
            decks[path] = Path(path).read_text()
        with open(cmd[-1], "w", encoding="utf-8") as handle:
            handle.write("Final result: Circuits match uniquely.\n")
        return _Completed()

    original_run = lvs_netgen.subprocess.run
    lvs_netgen.subprocess.run = fake_run
    try:
        layout_netlist, layout_circuit = _top_circuit_with_nets(layout_label, "vss")
        reference_netlist, reference_circuit = _top_circuit_with_nets(
            reference_label, "vss"
        )
        status, mismatches, _version = lvs_netgen._run_netgen_lvs(
            layout_netlist=layout_netlist,
            layout_circuit=layout_circuit,
            reference_netlist=reference_netlist,
            reference_circuit=reference_circuit,
            setup_file=None,
            timeout_s=10.0,
            binary="/bin/true",
        )
    finally:
        lvs_netgen.subprocess.run = original_run

    assert status == "match", mismatches
    (layout_deck,) = [text for path, text in decks.items() if "layout" in path]
    (reference_deck,) = [text for path, text in decks.items() if "reference" in path]
    return layout_deck, reference_deck


@pytest.mark.parametrize("label", ["nc_a", "nc_ab"])
def test_netgen_layout_deck_tolerates_the_label(label):
    layout_deck, reference_deck = _run_netgen_with_stubbed_subprocess(label, "x")

    assert f".SUBCKT TOP {label} vss" in layout_deck
    assert f"* pin {label}" in layout_deck
    # The untouched side keeps its own names.
    assert ".SUBCKT TOP x vss" in reference_deck


@pytest.mark.parametrize("label", ["nc_a", "nc_ab"])
def test_netgen_reference_deck_tolerates_the_label(label):
    layout_deck, reference_deck = _run_netgen_with_stubbed_subprocess("x", label)

    assert f".SUBCKT TOP {label} vss" in reference_deck
    assert f"* pin {label}" in reference_deck
    assert ".SUBCKT TOP x vss" in layout_deck
