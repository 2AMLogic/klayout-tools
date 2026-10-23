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
