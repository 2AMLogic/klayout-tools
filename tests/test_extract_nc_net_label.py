"""`klt extract` keeps a net label that starts with lowercase ``nc_`` (issue #6732).

KLayout's stock ``NetlistSpiceWriter`` (``use_net_names`` on) throws
``Unexpected text after numeric value`` from inside ``Netlist.write`` for a net
named ``nc_<letters>``, so ``klt extract`` used to exit 1 with no SPICE and no
report. ``extract_netlist_write`` now writes such a net under a placeholder
prefix and restores the real label in the written SPICE.

The end-to-end cases reuse the drawn SG13G2 ``rppd`` fixture from
``test_extract.py`` (that file is size-capped, so the cases live here) and only
change the two Metal1 text labels: one pad carries the name under test, the
other ``vss``.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.extract_netlist_write import (
    _restore_nc_prefix,
    write_spice_netlist,
)
from test_extract import _make_sg13g2_poly_resistor_layout, _write_gds

#: label -> the node token ``NetlistSpiceWriter`` spells it as. ``-`` is the
#: writer's own ``\x2d`` escape, so a restored label must keep that spelling.
_FAILING_LABELS = {
    "nc_a": "nc_a",
    "nc_ab": "nc_ab",
    "nc_A": "nc_A",
    "nc_a_b": "nc_a_b",
    "nc_a-b": "nc_a\\x2db",
}

#: The original reproducer's seven controls: they extracted before the fix and
#: must keep their spelling and connectivity.
_CONTROL_LABELS = {
    "nca": "nca",
    "n_a": "n_a",
    "xx_a": "xx_a",
    "a_nc": "a_nc",
    "nc_1": "nc_1",
    "nc-a": "nc\\x2da",
    "NC_a": "NC_a",
}


def _relabelled_resistor_gds(tmp_path, label: str) -> str:
    """The drawn ``rppd`` fixture with its pad labels set to ``label``/``vss``."""
    layout = _make_sg13g2_poly_resistor_layout("rppd", 1.0, 5.0)
    layer = layout.layer(8, 25)  # the fixture's Metal1.pin text layer
    shapes = layout.top_cell().shapes(layer)
    texts = sorted(
        (shape.text for shape in shapes.each() if shape.is_text()),
        key=lambda text: text.x,
    )
    assert len(texts) == 2
    shapes.clear()
    for text, name in zip(texts, (label, "vss"), strict=True):
        shapes.insert(kdb.Text(name, kdb.Trans(text.x, text.y)))
    return _write_gds(layout, tmp_path / "relabelled.gds")


def _extract(tmp_path, capsys, label: str):
    """Run ``klt extract`` on the relabelled fixture; return exit code, parsed
    JSON (or ``None`` on failure), the failure text, and the SPICE lines."""
    gds = _relabelled_resistor_gds(tmp_path, label)
    # Neutral output name: a path must never be able to satisfy a label assert.
    out = tmp_path / "extracted.spice"
    code = main(
        ["extract", gds, "--deck", "sg13g2", "--format", "json", "-o", str(out)]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out) if code == 0 else None
    return code, report, captured.err, out.read_text().splitlines()


def _tokens(line: str) -> list[str]:
    return line.split()


@pytest.mark.parametrize(
    ("label", "token"),
    [*_FAILING_LABELS.items(), *_CONTROL_LABELS.items()],
)
def test_label_extracts_with_its_spelling_in_spice_and_report(
    tmp_path, capsys, label, token
):
    code, report, err, lines = _extract(tmp_path, capsys, label)

    assert code == 0, err
    # JSON report: the same net, one rppd, terminals tied to the label / vss.
    assert report["device_counts"] == {"rppd": 1}
    assert label in {net["name"] for net in report["nets"]}
    (device,) = report["devices"]
    assert device["nets"]["a"] == label
    assert device["nets"]["b"] == "vss"

    # SPICE: header pin and device terminal both carry the label's spelling. A
    # header-only file also exists after a failure, so inspect the tokens. The
    # header's pin order follows the names' sort order, so compare it sorted;
    # the card's terminal order (a, b, w) is fixed.
    (header,) = [line for line in lines if line.startswith(".SUBCKT RES ")]
    assert sorted(_tokens(header)[2:]) == sorted([token, "vss", "vsubs"])
    (card,) = [line for line in lines if line.startswith("X$1 ")]
    assert _tokens(card)[1:5] == [token, "vss", "vsubs", "rppd"]
    assert f"* pin {label}" in lines


def _top_circuit_with_nets(*names: str):
    """A bare netlist whose ``TOP`` circuit has one net and pin per name."""
    netlist = kdb.Netlist()
    circuit = kdb.Circuit()
    circuit.name = "TOP"
    netlist.add(circuit)
    for name in names:
        circuit.connect_pin(circuit.create_pin(name), circuit.create_net(name))
    return netlist, circuit


def _write_bare(netlist, path) -> list[str]:
    writer = kdb.NetlistSpiceWriter()
    writer.use_net_names = True
    write_spice_netlist(netlist, str(path), writer, "d")
    return path.read_text().splitlines()


def test_merged_label_and_numeric_suffix_keep_spelling_and_net_names_restored(
    tmp_path,
):
    # `nc_x,vss` is a merged-label net name (the writer spells the comma `|`);
    # it fails exactly like `nc_x` because only the start of the name counts.
    names = ("nc_ab", "nc_1", "nc_x,vss", "vss,nc_y")
    netlist, circuit = _top_circuit_with_nets(*names)

    lines = _write_bare(netlist, tmp_path / "bare.spice")

    assert ".SUBCKT TOP nc_ab nc_1 nc_x|vss vss|nc_y" in lines
    # The real nets keep their own names for the report and later passes.
    assert sorted(net.name for net in circuit.each_net()) == sorted(names)


def test_placeholder_avoids_names_already_in_the_netlist(tmp_path):
    # Existing names (matched case-insensitively) that start like the first two
    # placeholder candidates must come back untouched, not rewritten to `nc_`.
    names = ("nc_a", "klt_nc0_a", "KLT_NC1_b")
    netlist, _circuit = _top_circuit_with_nets(*names)

    lines = _write_bare(netlist, tmp_path / "collide.spice")

    assert ".SUBCKT TOP nc_a klt_nc0_a KLT_NC1_b" in lines


def test_unrelated_write_error_keeps_its_own_cause(tmp_path):
    netlist, circuit = _top_circuit_with_nets("nc_a")
    writer = kdb.NetlistSpiceWriter()
    writer.use_net_names = True

    with pytest.raises(Exception) as excinfo:
        write_spice_netlist(
            netlist, str(tmp_path / "missing_dir" / "x.spice"), writer, "d"
        )

    assert "Unexpected text after numeric value" not in str(excinfo.value)
    assert "klt_nc" not in str(excinfo.value)
    assert [net.name for net in circuit.each_net()] == ["nc_a"]


def test_restore_refuses_a_placeholder_it_cannot_rewrite(tmp_path):
    path = tmp_path / "deck.spice"
    path.write_text(".SUBCKT TOP klt_nc0_a\n* v(klt_nc0_a)\n.ENDS TOP\n")

    with pytest.raises(RuntimeError, match="klt_nc0_"):
        _restore_nc_prefix(str(path), "klt_nc0_")

    # Refused before writing: the file is not left half-restored.
    assert path.read_text().startswith(".SUBCKT TOP klt_nc0_a\n")
