"""A ``--abstract-cell-lef`` pin missing from the cell's labels is merged in
(issue #3749).

A std cell routinely draws its substrate pin (sky130 ``VNB``) on a label layer
the deck does not read, so the in-cell labels alone lose it, and the LEF used
to be consulted only for a cell with *no* labels. The LEF-declared pins that
the labels did not supply are now appended, and a substrate-layer one with no
conductor joins the deck's substrate global net.
"""

from pathlib import Path
from types import SimpleNamespace

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_warnings import isolated_substrate_global_warning
from test_extract import _make_abstract_cells_layout, _write_gds

_LEF_WITH_VNB = """\
VERSION 5.7 ;
MACRO SC_BUF
  ORIGIN 0.000 0.000 ;
  SIZE 6.000 BY 4.000 ;
  PIN A
    PORT
      LAYER li1 ;
        RECT 0.000 0.200 0.400 0.800 ;
    END
  END A
  PIN VNB
    USE GROUND ;
    PORT
      LAYER pwell ;
        RECT 0.210 0.105 1.140 1.015 ;
    END
  END VNB
END SC_BUF
"""


def _extract(tmp_path, tag, lef=None):
    path = _write_gds(_make_abstract_cells_layout(), tmp_path / f"{tag}.gds")
    kwargs = {}
    if lef is not None:
        lef_path = tmp_path / f"{tag}.lef"
        lef_path.write_text(lef)
        kwargs["abstract_cell_lef_paths"] = (str(lef_path),)
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / f"{tag}.spice"),
        abstract_cell_patterns=("SC_BUF",),
        **kwargs,
    )
    return report, Path(report["netlist_path"]).read_text()


def test_lef_substrate_pin_is_merged_and_tied_to_the_substrate_net(tmp_path):
    report, spice = _extract(tmp_path, "with_lef", _LEF_WITH_VNB)

    (subckt,) = [x for x in spice.splitlines() if x.startswith(".SUBCKT SC_BUF")]
    pins = subckt.split()[2:]
    assert "VNB" in pins
    assert {"A", "Y"} <= set(pins)  # label pins survive; label wins on `A`
    assert pins == sorted(pins)

    (entry,) = report["abstracted_cells"]
    assert entry["pin_count"] == len(pins)
    assert entry["resolution_source"] == "in_cell_labels"
    assert entry["lef_path"] is not None

    # Connected to the substrate global, not an isolated per-instance net.
    x_lines = [x for x in spice.splitlines() if x.startswith("X")]
    assert len(x_lines) == 2
    for x_line in x_lines:
        assert x_line.split()[1 + pins.index("VNB")] == "vsubs", x_line
    assert not [w for w in report["warnings"] if "pin 'VNB'" in w]
    assert not [
        w
        for w in report["warnings"]
        if "resolve only through the deck's synthesized global net" in w
    ]


def test_without_lef_the_substrate_pin_is_still_dropped_and_warned(tmp_path):
    report, spice = _extract(tmp_path, "no_lef")

    (subckt,) = [x for x in spice.splitlines() if x.startswith(".SUBCKT SC_BUF")]
    assert "VNB" not in subckt.split()
    (entry,) = report["abstracted_cells"]
    assert entry["lef_path"] is None
    (warning,) = [
        w
        for w in report["warnings"]
        if "resolve only through the deck's synthesized global net" in w
    ]
    assert "--abstract-cell-lef" in warning
    assert "merged" in warning


def test_lef_pin_duplicating_a_label_name_keeps_the_label(tmp_path):
    lef = _LEF_WITH_VNB.replace("PIN VNB", "PIN Y").replace("END VNB", "END Y")
    report, spice = _extract(tmp_path, "dup", lef)
    (subckt,) = [x for x in spice.splitlines() if x.startswith(".SUBCKT SC_BUF")]
    assert subckt.split()[2:].count("Y") == 1
    (entry,) = report["abstracted_cells"]
    assert entry["lef_path"] is None  # nothing was merged


def test_routed_conductor_over_lef_substrate_box_does_not_capture_the_pin(tmp_path):
    """A signal conductor over the LEF ``pwell`` box centre must not steal the
    substrate pin from one instance (issue #4571)."""
    import klayout.db as kdb

    layout = _make_abstract_cells_layout()
    top = layout.cell("TOP")
    # LEF pwell box (0.210..1.140, 0.105..1.015 um) centre = (675, 560) nm.
    top.shapes(layout.layer(67, 20)).insert(kdb.Box(575, 460, 775, 660))
    top.shapes(layout.layer(67, 5)).insert(kdb.Text("SIG", kdb.Trans(675, 560)))
    path = _write_gds(layout, tmp_path / "over.gds")
    lef_path = tmp_path / "over.lef"
    lef_path.write_text(_LEF_WITH_VNB)
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "over.spice"),
        abstract_cell_patterns=("SC_BUF",),
        abstract_cell_lef_paths=(str(lef_path),),
    )
    spice = Path(report["netlist_path"]).read_text()
    (subckt,) = [x for x in spice.splitlines() if x.startswith(".SUBCKT SC_BUF")]
    idx = subckt.split()[2:].index("VNB")
    x_lines = [x for x in spice.splitlines() if x.startswith("X")]
    assert len(x_lines) == 2
    for x_line in x_lines:
        assert x_line.split()[1 + idx] == "vsubs", x_line


def _bus_bit_lef(pins: list[tuple[str, str]]) -> str:
    """A ``MAC`` LEF declaring one ``PORT`` rectangle per given
    ``(pin name, RECT arguments in um)`` pair -- issue #6843's bus-bit
    spellings (``DATA[0]`` et al.) travel through the parser verbatim."""
    blocks = []
    for name, rect in pins:
        blocks.append(
            f"  PIN {name}\n"
            "    PORT\n"
            "      LAYER li1 ;\n"
            f"        RECT {rect} ;\n"
            "    END\n"
            f"  END {name}\n"
        )
    return (
        "VERSION 5.7 ;\nMACRO MAC\n"
        "  ORIGIN 0.000 0.000 ;\n"
        "  SIZE 5.400 BY 1.000 ;\n" + "".join(blocks) + "END MAC\n"
    )


_SECOND_PAD_RECT = "1.600 0.200 2.000 0.800"

#: The supply pad of :func:`_make_bus_macro_layout`'s three-labelled-pad
#: variant (x = 3200..3600 nm): a pin wrongly appended from a rectangle here
#: resolves onto ``VPWR`` -- the emulator's 96 SRAM pins tied to power in
#: miniature. The LEF-only ``DATA[2]`` bit sits over its own parent-routed
#: pad at x = 4800..5200 nm.
_SUPPLY_PAD_RECT = "3.200 0.200 3.600 0.800"
_LEF_ONLY_PAD_RECT = "4.800 0.200 5.200 0.800"


def _make_bus_macro_layout(
    pads: list[tuple[str | None, str, int]],
) -> kdb.Layout:
    """One ``MAC`` hard-macro instance in a ``TOP`` cell, with numbered li1
    pads (issue #6843's reproduction shape).

    Each ``pads`` entry is ``(pin label drawn in ``MAC`` -- or ``None`` for a
    pad the macro itself leaves unlabelled, ``(parent met1 net name)``,
    ``(pad x offset in nm)``)``. Pad ``i`` spans ``x .. x+400`` nm at ``y =
    200..800``: inside ``MAC`` an li1 pad labelled on li1.pin (67/5); in
    ``TOP`` an mcon+met1 landing labelled on met1.pin (68/5) that routes the
    pad to its own parent net -- the same pad/wiring shape
    ``_wire_abstract_instance_pins`` uses, so the parent's routing genuinely
    reaches each pin location through ordinary connectivity.
    """
    layout = kdb.Layout()
    mac = layout.create_cell("MAC")
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(mac.cell_index(), kdb.Trans(0, 0)))
    for label, net, x in pads:
        if label is not None:
            mac.shapes(layout.layer(67, 20)).insert(kdb.Box(x, 200, x + 400, 800))
            mac.shapes(layout.layer(67, 5)).insert(
                kdb.Text(label, kdb.Trans(x + 200, 500))
            )
        top.shapes(layout.layer(67, 44)).insert(kdb.Box(x + 100, 300, x + 300, 700))
        top.shapes(layout.layer(68, 20)).insert(kdb.Box(x, 200, x + 400, 800))
        top.shapes(layout.layer(68, 5)).insert(kdb.Text(net, kdb.Trans(x + 200, 500)))
    return layout


def _run_bus_macro_extract(tmp_path, tag, layout, lef=None):
    path = _write_gds(layout, tmp_path / f"{tag}.gds")
    kwargs = {}
    if lef is not None:
        lef_path = tmp_path / f"{tag}.lef"
        lef_path.write_text(lef)
        kwargs["abstract_cell_lef_paths"] = (str(lef_path),)
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / f"{tag}.spice"),
        abstract_cell_patterns=("MAC",),
        **kwargs,
    )
    return report, Path(report["netlist_path"]).read_text()


@pytest.mark.parametrize(
    ("gds_pin", "lef_pin"),
    [("DATA<0>", "DATA[0]"), ("D2{01}(2)", "D2[1]<2>")],
)
def test_lef_bus_bit_alias_of_a_labelled_pin_is_not_appended(
    tmp_path, gds_pin, lef_pin
):
    """A LEF ``PIN DATA[0]`` and an in-cell ``DATA<0>`` label are one bus bit
    in two spellings: appending the LEF spelling as a *second* pin ties that
    copy to whatever its own PORT rectangle probes -- the supply net, in
    issue #6843's fixture -- and is the two-pin miniature of the emulator's
    96 SRAM pins duplicated onto ``VPWR``. The merge must recognise the two
    spellings as one bit and keep the label's pin and its connection."""
    layout = _make_bus_macro_layout([(gds_pin, "SIG", 0), ("VDD", "VPWR", 1600)])
    lef = _bus_bit_lef([(lef_pin, _SECOND_PAD_RECT)])

    # Control: GDS labels alone -- the extraction the LEF must not disturb.
    _, labels_only = _run_bus_macro_extract(tmp_path, "labels_only", layout)
    report, spice = _run_bus_macro_extract(tmp_path, "with_lef", layout, lef)

    for text in (labels_only, spice):
        (subckt,) = [x for x in text.splitlines() if x.startswith(".SUBCKT MAC")]
        pins = [
            pin.encode("ascii").decode("unicode_escape") for pin in subckt.split()[2:]
        ]
        assert pins == [gds_pin, "VDD"]
        (x_line,) = [x for x in text.splitlines() if x.startswith("X")]
        assert x_line.split() == ["XMAC_0", "SIG", "VPWR", "MAC"]

    (entry,) = report["abstracted_cells"]
    assert entry["pin_count"] == 2
    assert entry["resolution_source"] == "in_cell_labels"
    assert not [w for w in report["warnings"] if "tied" in w or "collapsed" in w]


def test_bus_bits_of_distinct_indices_and_lef_only_bits_stay_distinct(tmp_path):
    """The alias comparison is per *bit*, not per bus: ``DATA[1]`` must not be
    swallowed by ``DATA<0>``'s label, and ``DATA[2]`` -- a bit no label
    spells -- must still be merged in on its own PORT, exactly as #3749
    merges any LEF-only port. A key that collapsed the whole bus to one pin
    fails here in both directions at once."""
    layout = _make_bus_macro_layout(
        [
            ("DATA<0>", "SIG", 0),
            ("DATA<1>", "SIG2", 1600),
            ("VDD", "VPWR", 3200),
            (None, "SIG3", 4800),
        ]
    )
    report, spice = _run_bus_macro_extract(
        tmp_path,
        "distinct",
        layout,
        _bus_bit_lef(
            [
                ("DATA[0]", _SUPPLY_PAD_RECT),
                ("DATA[1]", _SUPPLY_PAD_RECT),
                ("DATA[2]", _LEF_ONLY_PAD_RECT),
            ]
        ),
    )

    (subckt,) = [x for x in spice.splitlines() if x.startswith(".SUBCKT MAC")]
    assert subckt.split()[2:] == ["DATA<0>", "DATA<1>", "DATA[2]", "VDD"]
    (x_line,) = [x for x in spice.splitlines() if x.startswith("X")]
    assert x_line.split() == ["XMAC_0", "SIG", "SIG2", "SIG3", "VPWR", "MAC"]

    (entry,) = report["abstracted_cells"]
    assert entry["pin_count"] == 4
    assert entry["lef_path"] is not None  # DATA[2] was merged
    assert not [w for w in report["warnings"] if "tied" in w or "collapsed" in w]


@pytest.mark.parametrize(
    ("literal", "valid"),
    [
        (f"DATA{opening}0{closing}", "DATA[0]")
        for opening, paired in ("[]", "<>", "()", "{}")
        for closing in "]>)}"
        if closing != paired
    ]
    + [("DATA[0]<1]", "DATA[0][1]"), ("DATA[0>(1)", "DATA[0][1]")],
)
def test_malformed_gds_index_keeps_its_connection_and_the_lef_pin(
    tmp_path, literal, valid
):
    layout = _make_bus_macro_layout(
        [(literal, "SIG_LITERAL", 0), (None, "SIG_LEF", 1600), ("VDD", "VPWR", 3200)]
    )
    # A single mismatched pair anywhere in the index suffix makes it literal.
    report, spice = _run_bus_macro_extract(
        tmp_path, "literal", layout, _bus_bit_lef([(valid, _SECOND_PAD_RECT)])
    )
    header = next(line for line in spice.splitlines() if line.startswith(".SUBCKT MAC"))
    instance = next(line for line in spice.splitlines() if line.startswith("XMAC_0"))
    # KLayout hex-escapes parentheses and braces in SPICE pin names.
    pins = [pin.encode("ascii").decode("unicode_escape") for pin in header.split()[2:]]
    assert dict(zip(pins, instance.split()[1:-1], strict=True)) == {
        literal: "SIG_LITERAL",
        valid: "SIG_LEF",
        "VDD": "VPWR",
    }
    assert report["abstracted_cells"][0]["pin_count"] == 3


def _add_tapless_nmos_in_top(layout):
    """Draw one plain NMOS directly in ``TOP`` (the `_make_inverter_layout`
    shape: active strip crossed by a poly bar, contacted up to li1), far
    from the abstracted-cell instances. Its body has no drawn tap, so
    extraction synthesizes the deck's substrate global for it -- a *device
    terminal* on that net (issue #5632's negative case)."""
    import klayout.db as kdb

    top = layout.cell("TOP")
    dx = 20000  # nm -- clear of the abstracted instances at 0 and 5000

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box.moved(kdb.Vector(dx, 0)))

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x + dx, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))  # poly.drawing (gate bar)
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (source side)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (source side)
    label(67, 5, "GNDB", 200, 500)
    label(67, 5, "GA", 1000, 500)


def test_isolated_substrate_global_warns(tmp_path):
    """Issue #5632: when the LEF-merged substrate pin's net carries no other
    connection, `warnings[]` names the net, the instance/cell counts and the
    two fixes -- without changing the run's outcome."""
    report, spice = _extract(tmp_path, "isolated", _LEF_WITH_VNB)
    (warning,) = [
        w
        for w in report["warnings"]
        if "substrate-role pin" in w and "isolated node" in w
    ]
    assert "'vsubs'" in warning
    assert "2 instances" in warning
    assert "1 cell type: SC_BUF" in warning
    # The fixture contains only the two buffer instances. Its LEF pins
    # establish isolation, while a physical connection needs separate evidence.
    assert "the one cell that physically ties" not in warning
    assert "verified the physical tie" in warning
    assert "hints.layout_short_nets" in warning
    # Status and exit are unchanged: a normal extracted report with the
    # substrate pin still tied to vsubs (the pin-merge assertions above).
    assert ".SUBCKT SC_BUF" in spice
    x_lines = [x for x in spice.splitlines() if x.startswith("X")]
    assert all("vsubs" in x_line for x_line in x_lines)


def test_isolated_substrate_global_warning_not_fired_with_device_on_net(tmp_path):
    """A non-abstracted NMOS body also landing on the substrate global means
    the net carries real connectivity -- no warning."""
    layout = _make_abstract_cells_layout()
    _add_tapless_nmos_in_top(layout)
    path = _write_gds(layout, tmp_path / "with_nmos.gds")
    lef_path = tmp_path / "with_nmos.lef"
    lef_path.write_text(_LEF_WITH_VNB)
    report = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "with_nmos.spice"),
        abstract_cell_patterns=("SC_BUF",),
        abstract_cell_lef_paths=(str(lef_path),),
    )
    spice = Path(report["netlist_path"]).read_text()
    # The drawn NMOS is really there, its bulk on vsubs.
    assert "vsubs nfet" in spice
    assert not [
        w
        for w in report["warnings"]
        if "substrate-role pin" in w and "isolated node" in w
    ]


def test_isolated_substrate_global_warning_not_fired_without_lef(tmp_path):
    """No LEF means no substrate pin merged at all (the pre-#3749 drop, with
    its own warning) -- the isolated-node warning must stay silent."""
    report, spice = _extract(tmp_path, "no_lef_isolated")
    assert "VNB" not in spice
    assert not [
        w
        for w in report["warnings"]
        if "substrate-role pin" in w and "isolated node" in w
    ]


def test_isolated_substrate_global_warning_tolerates_a_missing_substrate_net():
    """Unit-tested directly for the ``net is None`` guard: substrate-role
    pins recorded, but the deck's synthesized substrate global absent from
    the emitted netlist's top circuit -- the warning stays silent rather
    than raising. Unreachable through ``_wire_abstract_cells`` today (the
    binding always creates the net it binds to), so no drawn layout can
    drive it: defence in depth against a future caller, pinning the moved
    helper's own guard (issue #5632 put it under ``extract_warnings``' 100%
    floor)."""

    class _NoSubstrateNetCircuit:
        def net_by_name(self, _name):
            return None

    entries = isolated_substrate_global_warning(
        _NoSubstrateNetCircuit(),
        SimpleNamespace(substrate_net="vsubs"),
        {"SC_BUF": {"VNB"}},
        [{"cell": "SC_BUF", "instance_count": 2}],
    )
    assert entries == []
