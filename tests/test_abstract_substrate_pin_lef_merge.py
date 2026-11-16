"""A ``--abstract-cell-lef`` pin missing from the cell's labels is merged in
(issue #3749).

A std cell routinely draws its substrate pin (sky130 ``VNB``) on a label layer
the deck does not read, so the in-cell labels alone lose it, and the LEF used
to be consulted only for a cell with *no* labels. The LEF-declared pins that
the labels did not supply are now appended, and a substrate-layer one with no
conductor joins the deck's substrate global net.
"""

from pathlib import Path

from klayout_tools.extract import run_extract
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
