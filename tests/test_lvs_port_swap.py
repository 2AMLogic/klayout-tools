"""Issue #8084: ``topology.known_limitation`` for the ASAP7 RVT masters whose
series-order difference from the CDL sits across two commutative inputs
(``"series-order-port-swap"``).

PDK-free, always run: ``note_known_limit`` on the new limitation kind, and the
public ``klt lvs`` report path on a synthetic two-device series stack drawn
under a pinned port-swap master name -- the devices are the public
``_draw_device`` geometry. Every output goes to ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.lvs import run_lvs
from klayout_tools.lvs_known_limits import note_known_limit
from test_asap7_finfet import (
    DBU,
    GATE,
    GATE_PIN,
    M1,
    M1_PIN,
    _draw_device,
    _errors,
    _write,
)


def _disclose(finfet, status, top):
    out: list = []
    note_known_limit(out, finfet, status, {"top": top}, {})
    return out


def _known_limitation_entries(report: dict) -> list[dict]:
    return [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]


def test_port_swap_disclosure_names_the_swap_and_stays_gated_on_mismatch():
    """Disclose the port-swap limitation on any mismatching FinFET compare."""
    cell = "AO21x1_ASAP7_75t_R"
    (entry,) = _disclose(True, "mismatch", cell)
    assert entry["category"] == "topology.known_limitation"
    assert entry["severity"] == "warning"
    assert entry["side"] == "layout"
    assert entry["details"] == {"cell": cell, "limitation": "series-order-port-swap"}
    assert (
        f"cell '{cell}' is a known ASAP7 RVT library limitation: its drawn "
        "series-stack order matches the library CDL only under a commutative "
        "swap of symmetric input ports"
    ) in entry["description"]
    assert "can contribute to a mismatch when" in entry["description"]
    assert "expected for the cell" not in entry["description"]
    assert _disclose(True, "match", cell) == []
    assert _disclose(False, "mismatch", cell) == []


def test_port_swap_block_warning_states_the_port_swap_with_per_side_counts():
    cell = "FAx1_ASAP7_75t_R"
    out: list = []
    note_known_limit(
        out,
        True,
        "mismatch",
        {"top": "ALU8"},
        {},
        layout_counts={cell: 2, "OAI31xp33_ASAP7_75t_R": 3},
        reference_counts={cell: 2},
    )
    assert [entry["details"]["cell"] for entry in out] == [
        cell,
        "OAI31xp33_ASAP7_75t_R",
    ]
    assert all(entry["side"] == "both" for entry in out)
    assert all(entry["severity"] == "warning" for entry in out)
    assert all(
        entry["details"]["limitation"] == "series-order-port-swap" for entry in out
    )
    assert out[0]["details"] == {
        "cell": cell,
        "limitation": "series-order-port-swap",
        "layout_count": 2,
        "reference_count": 2,
    }
    assert out[1]["details"]["layout_count"] == 3
    assert out[1]["details"]["reference_count"] == 0
    assert (
        f"block instantiates 2 layout and 2 reference instance(s) of cell '{cell}', "
        "a known ASAP7 RVT library limitation: its drawn series-stack order "
        "matches the library CDL only under a commutative swap of symmetric "
        "input ports"
    ) in out[0]["description"]
    assert "can contribute to a mismatch when" in out[0]["description"]
    assert "expected to mismatch" not in out[0]["description"]


# --------------------------------------------------------------------------- #
# Issue #8084: a series stack whose order differs from the reference only
# across two commutative inputs, drawn under a pinned port-swap master name.
# No PDK needed -- the devices are the public `_draw_device` geometry.
# --------------------------------------------------------------------------- #

_PORT_SWAP = "AO21x1_ASAP7_75t_R"
_LOAD = "LOADx1_ASAP7_75t_R"
#: Lands the upper device's source pad (y -160..-90) on the lower device's
#: drain bar (y 762..832), so the stack needs no wire between them.
_STACK_PITCH = 922
#: x offset of the load device's cell from the master's.
_LOAD_X = 1500
_STACK_CARD = "w=81n l=20n nfin=3"
#: The stack drawn the other way round from the layout: VSS -[A2]- net29
#: -[A1]- Y. ``vsubs`` is a port so the bulk net is one net on both sides of a
#: flattened reference, as it is in the extracted layout.
_STACK_CDL_REVERSED = (
    f".SUBCKT {_PORT_SWAP} A1 A2 Y VSS vsubs\n"
    f"M0 net29 A2 VSS vsubs nmos_rvt {_STACK_CARD}\n"
    f"M1 Y A1 net29 vsubs nmos_rvt {_STACK_CARD}\n"
    ".ENDS\n"
)
#: The stack in the order the cell is drawn -- the negative control.
_STACK_CDL_DRAWN = (
    f".SUBCKT {_PORT_SWAP} A1 A2 Y VSS vsubs\n"
    f"M0 net29 A1 VSS vsubs nmos_rvt {_STACK_CARD}\n"
    f"M1 Y A2 net29 vsubs nmos_rvt {_STACK_CARD}\n"
    ".ENDS\n"
)
_GLV_LIBRARY = (
    f".SUBCKT {_PORT_SWAP} A1 A2 Y VSS\n"
    f"M0 net29 A2 VSS VSS nmos_rvt {_STACK_CARD}\n"
    f"M1 Y A1 net29 VSS nmos_rvt {_STACK_CARD}\n"
    ".ENDS\n"
    f".SUBCKT {_LOAD} IN2 OUT2 VSS\n"
    f"M0 OUT2 IN2 VSS VSS nmos_rvt {_STACK_CARD}\n"
    ".ENDS\n"
)
_FLATTENED = {"flatten_layout": True, "flatten_reference": True}
#: The issue's block shape: both sides flattened. The ``gate-level-verilog``
#: library ties every nFET bulk to ``VSS``, so the layout's synthesized
#: substrate needs the disclosed bulk policy to meet it.
_GLV_OPTIONS = {**_FLATTENED, "finfet_bulk": {"nmos": "VSS", "pmos": "VDD"}}


def _renamed_device(
    layout: kdb.Layout, name: str, labels: dict[str, str], trans: kdb.Trans
) -> kdb.Cell:
    """One ``_draw_device()`` nFET in its own cell ``name``, its ``S``/``D``/``G``
    pin texts renamed through ``labels``, then moved by ``trans``."""
    cell = layout.create_cell(name)
    device = _draw_device()
    cell.copy_tree(device.cell("TOP"))
    for layer in (GATE_PIN, M1_PIN):
        shapes = cell.shapes(layout.layer(*layer))
        texts = [(s.text.string, s.text_trans) for s in shapes.each() if s.is_text()]
        shapes.clear()
        for text, text_trans in texts:
            shapes.insert(kdb.Text(labels[text], text_trans))
    cell.transform(trans)
    return cell


def _stack_cell(layout: kdb.Layout, name: str, gates: tuple[str, str]) -> kdb.Cell:
    """Two nFETs in series in one flattened cell ``name``: ``VSS`` -[gates[0]]-
    ``MID`` -[gates[1]]- ``Y``, the upper device ``_STACK_PITCH`` above the
    lower one."""
    top = layout.create_cell(name)
    devices = (
        _renamed_device(
            layout, f"{name}_0", {"S": "VSS", "D": "MID", "G": gates[0]}, kdb.Trans()
        ),
        _renamed_device(
            layout,
            f"{name}_1",
            {"S": "MID", "D": "Y", "G": gates[1]},
            kdb.Trans(0, _STACK_PITCH),
        ),
    )
    for cell in devices:
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans()))
    top.flatten(-1, True)
    return top


def _stack_lvs(
    tmp_path: Path,
    *,
    cdl: str,
    options: dict | None = None,
    hints: dict | None = None,
) -> dict:
    """``klt lvs`` of the standalone ``_PORT_SWAP`` master, drawn ``VSS`` -[A1]-
    ``MID`` -[A2]- ``Y``, against ``cdl``."""
    layout = kdb.Layout()
    layout.dbu = DBU
    _stack_cell(layout, _PORT_SWAP, ("A1", "A2"))
    reference = tmp_path / "ref.cdl"
    reference.write_text(cdl)
    request = {
        "layout": {
            "file": _write(layout, tmp_path, "stack.gds"),
            "deck": "asap7",
            "top": _PORT_SWAP,
        },
        "reference": {"netlist": str(reference), "top": _PORT_SWAP},
    }
    request |= {
        key: value for key, value in (("options", options), ("hints", hints)) if value
    }
    return run_lvs(json.dumps(request))


def _input_pairing(report: dict) -> dict[str, str]:
    """``{layout port: reference port}`` for the report's input pins."""
    return {
        pair["layout"]: pair["reference"]
        for pair in report["net_correspondence"]
        if pair["reference"] in ("A1", "A2")
    }


def test_port_swap_master_matches_unanchored_through_a_swapped_pin_pair(tmp_path):
    """The standalone, unanchored compare absorbs the stack-order difference by
    pairing the two commutative ports with each other: the verdict is ``match``
    and the mismatch gate keeps the named limit silent."""
    report = _stack_lvs(tmp_path, cdl=_STACK_CDL_REVERSED)
    assert report["status"] == "match", [
        m for m in report["mismatches"] if m["severity"] == "error"
    ][:5]
    assert _input_pairing(report) == {"A1": "A2", "A2": "A1"}
    assert "topology.known_limitation" not in report["category_counts"]


@pytest.mark.parametrize(
    ("options", "hints"),
    [
        ({"anchor_top_level_pins": True}, None),
        (None, {"same_nets": [["A1", "A1"], ["A2", "A2"]]}),
    ],
    ids=["anchored-pins", "same_nets"],
)
def test_port_swap_master_mismatches_with_the_named_limit_once_its_ports_cannot_swap(
    tmp_path, options, hints
):
    """Pinning each port to itself leaves the two stack devices without a
    counterpart; the single-cell entry names the limit and says the CDL matches
    only under a commutative port swap."""
    report = _stack_lvs(tmp_path, cdl=_STACK_CDL_REVERSED, options=options, hints=hints)
    assert report["status"] == "mismatch"
    errors = _errors(report)
    assert len([m for m in errors if m["category"] == "device.unmatched"]) == 2
    assert _input_pairing(report) == {"A1": "A1", "A2": "A2"}
    entries = _known_limitation_entries(report)
    assert report["category_counts"]["topology.known_limitation"] == len(entries) == 1
    (entry,) = entries
    assert entry["severity"] == "warning"
    assert entry["side"] == "layout"
    assert entry["details"] == {
        "cell": _PORT_SWAP,
        "limitation": "series-order-port-swap",
    }
    assert (
        f"cell '{_PORT_SWAP}' is a known ASAP7 RVT library limitation: its drawn "
        "series-stack order matches the library CDL only under a commutative swap "
        "of symmetric input ports"
    ) in entry["description"]


def test_port_swap_master_in_drawn_order_stays_silent_with_anchored_pins(tmp_path):
    """Negative control: with the CDL stack ordered the way the cell draws it,
    the anchored compare matches, so the anchored mismatch above is the stack
    order and not the anchoring."""
    report = _stack_lvs(
        tmp_path, cdl=_STACK_CDL_DRAWN, options={"anchor_top_level_pins": True}
    )
    assert report["status"] == "match", [
        m for m in report["mismatches"] if m["severity"] == "error"
    ][:5]
    assert _input_pairing(report) == {"A1": "A1", "A2": "A2"}
    assert "topology.known_limitation" not in report["category_counts"]


def _ports(*, tie_inputs: bool, load: bool) -> list[str]:
    """``TOP``'s ports: ``IN`` (which also carries the second gate when
    ``tie_inputs``, else ``IN`` and ``IN2``), ``OUT``, ``OUT2`` with ``load``,
    and ``VSS``."""
    inputs = ["IN"] if tie_inputs else ["IN", "IN2"]
    return [*inputs, "OUT", *(["OUT2"] if load else []), "VSS"]


def _port_swap_block_gds(tmp_path: Path, *, tie_inputs: bool, load: bool) -> str:
    """``TOP`` instantiating one ``_PORT_SWAP`` master with ``IN`` wired to
    ``A1`` and ``IN2`` to ``A2`` (both gates on ``IN`` when ``tie_inputs``),
    plus -- with ``load`` -- a plain nFET whose gate hangs on ``A2``'s net so
    the two input nets are topologically distinct. Wires follow
    ``_draw_device``'s pin geometry: gate bars at y 512..592 and (one
    ``_STACK_PITCH`` up) 1434..1514, the source pad at y -160..-90 and the top
    drain bar at y 1684..1754."""
    layout = kdb.Layout()
    layout.dbu = DBU
    _stack_cell(layout, _PORT_SWAP, ("A1", "A2"))
    top = layout.create_cell("TOP")
    top.insert(kdb.CellInstArray(layout.cell(_PORT_SWAP).cell_index(), kdb.Trans()))
    gate = top.shapes(layout.layer(*GATE))
    gate_pin = top.shapes(layout.layer(*GATE_PIN))
    metal = top.shapes(layout.layer(*M1))
    metal_pin = top.shapes(layout.layer(*M1_PIN))
    in2 = "IN" if tie_inputs else "IN2"
    gate_pin.insert(kdb.Text("IN", kdb.Trans(-300, 552)))
    gate_pin.insert(kdb.Text(in2, kdb.Trans(600, 1454)))
    metal_pin.insert(kdb.Text("VSS", kdb.Trans(-300, -120)))
    metal_pin.insert(kdb.Text("OUT", kdb.Trans(330, 1850)))
    gate.insert(kdb.Box(-400, 512, 285, 592))
    gate.insert(kdb.Box(364, 1434, 764, 1514))
    metal.insert(kdb.Box(-400, -160, 171, -90))
    metal.insert(kdb.Box(300, 1754, 380, 1900))
    if tie_inputs:
        gate.insert(kdb.Box(-400, 1434, 284, 1514))
        gate.insert(kdb.Box(-460, 512, -400, 1514))
    elif load:
        cell = _renamed_device(
            layout, _LOAD, {"S": "VSS", "D": "OUT2", "G": in2}, kdb.Trans(_LOAD_X, 0)
        )
        top.insert(kdb.CellInstArray(cell.cell_index(), kdb.Trans()))
        gate.insert(kdb.Box(764, 1434, _LOAD_X + 284, 1514))
        gate.insert(kdb.Box(_LOAD_X + 284, 592, _LOAD_X + 364, 1514))
        metal.insert(kdb.Box(170, -160, _LOAD_X + 170, -90))
    return _write(layout, tmp_path, "port_swap_block.gds")


def _port_swap_block_reference(*, tie_inputs: bool, load: bool) -> str:
    in2 = "IN" if tie_inputs else "IN2"
    text = _STACK_CDL_REVERSED + (
        f".SUBCKT TOP {' '.join(_ports(tie_inputs=tie_inputs, load=load))}\n"
        f"X1 IN {in2} OUT VSS vsubs {_PORT_SWAP}\n"
    )
    if load:
        text += f"M2 OUT2 {in2} VSS vsubs nmos_rvt {_STACK_CARD}\n"
    return text + ".ENDS\n"


def _glv_top(*, tie_inputs: bool, load: bool) -> str:
    in2 = "IN" if tie_inputs else "IN2"
    inputs = ", ".join(["IN"] if tie_inputs else ["IN", "IN2"])
    return (
        f"module TOP ({', '.join(_ports(tie_inputs=tie_inputs, load=load))});\n"
        f"  input {inputs}, VSS;\n"
        f"  output {'OUT, OUT2' if load else 'OUT'};\n"
        f"  {_PORT_SWAP} u0 (.A1(IN), .A2({in2}), .Y(OUT), .VSS(VSS));\n"
        + (f"  {_LOAD} u1 (.IN2(IN2), .OUT2(OUT2), .VSS(VSS));\n" if load else "")
        + "endmodule\n"
    )


def _port_swap_block_lvs(
    tmp_path: Path,
    *,
    tie_inputs: bool,
    load: bool,
    form: str = "spice",
    options: dict | None = None,
) -> dict:
    """``klt lvs`` of the block against a SPICE reference, or against a
    ``gate-level-verilog`` top expanded from ``_GLV_LIBRARY``."""
    request = {
        "layout": {
            "file": _port_swap_block_gds(tmp_path, tie_inputs=tie_inputs, load=load),
            "deck": "asap7",
            "top": "TOP",
        }
    }
    if form == "gate-level-verilog":
        library = tmp_path / "lib.cdl"
        library.write_text(_GLV_LIBRARY)
        top = tmp_path / "top.v"
        top.write_text(_glv_top(tie_inputs=tie_inputs, load=load))
        request["reference"] = {
            "netlist": str(top),
            "top": "TOP",
            "form": form,
            "pin_order_files": [str(library)],
        }
    else:
        reference = tmp_path / "ref.cdl"
        reference.write_text(
            _port_swap_block_reference(tie_inputs=tie_inputs, load=load)
        )
        request["reference"] = {"netlist": str(reference), "top": "TOP"}
    if options is not None:
        request["options"] = options
    return run_lvs(json.dumps(request))


@pytest.mark.parametrize(
    ("form", "options"),
    [
        ("spice", None),
        ("spice", _FLATTENED),
        ("gate-level-verilog", _GLV_OPTIONS),
    ],
    ids=["hierarchical", "flattened", "gate-level-verilog"],
)
def test_port_swap_block_mismatch_carries_the_named_limit_with_per_side_counts(
    tmp_path, form, options
):
    """The issue's block shape: ``A2``'s net also drives a second gate, so the
    comparer cannot swap the two inputs and the block mismatches. The block-level
    entry names the master, counts it on both sides, and states the port swap."""
    report = _port_swap_block_lvs(
        tmp_path, tie_inputs=False, load=True, form=form, options=options
    )
    assert report["status"] == "mismatch"
    assert _errors(report)
    entries = _known_limitation_entries(report)
    assert report["category_counts"]["topology.known_limitation"] == len(entries) == 1
    (entry,) = entries
    assert entry["severity"] == "warning"
    assert entry["side"] == "both"
    assert entry["details"] == {
        "cell": _PORT_SWAP,
        "limitation": "series-order-port-swap",
        "layout_count": 1,
        "reference_count": 1,
    }
    assert (
        f"block instantiates 1 layout and 1 reference instance(s) of cell "
        f"'{_PORT_SWAP}', a known ASAP7 RVT library limitation: its drawn "
        "series-stack order matches the library CDL only under a commutative swap "
        "of symmetric input ports"
    ) in entry["description"]


@pytest.mark.parametrize(
    ("form", "options"),
    [("spice", _FLATTENED), ("gate-level-verilog", _GLV_OPTIONS)],
    ids=["flattened", "gate-level-verilog"],
)
def test_port_swap_block_fails_on_exactly_the_two_stack_devices(
    tmp_path, form, options
):
    """With both sides flattened the two stack devices are the only unmatched
    devices -- the load nFET sharing ``A2``'s net matches -- so what fails is
    the stack order the named limit discloses, not the block's wiring."""
    report = _port_swap_block_lvs(
        tmp_path, tie_inputs=False, load=True, form=form, options=options
    )
    assert report["status"] == "mismatch"
    errors = _errors(report)
    assert len([m for m in errors if m["category"] == "device.unmatched"]) == 2
    assert {m["category"] for m in errors} <= {
        "device.unmatched",
        "hints.rejected",
        "topology",
    }


@pytest.mark.parametrize(
    ("tie_inputs", "form", "options"),
    [
        (True, "spice", None),
        (True, "spice", _FLATTENED),
        (True, "gate-level-verilog", _GLV_OPTIONS),
        (False, "spice", None),
    ],
    ids=["tied-hierarchical", "tied-flattened", "tied-gate-level-verilog", "symmetric"],
)
def test_port_swap_block_that_still_matches_stays_silent(
    tmp_path, tie_inputs, form, options
):
    """The mismatch gate keeps the named limit silent: with both gates on one
    net the stack order cannot matter (any compare shape), and a hierarchical
    compare still swaps the two inputs of an instance whose nets are
    interchangeable."""
    report = _port_swap_block_lvs(
        tmp_path, tie_inputs=tie_inputs, load=False, form=form, options=options
    )
    assert report["status"] == "match", [
        m for m in report["mismatches"] if m["severity"] == "error"
    ][:5]
    assert "topology.known_limitation" not in report["category_counts"]


def test_hierarchical_block_still_swaps_interchangeable_instance_inputs(tmp_path):
    """The hierarchical compare pairs interchangeable instance inputs crosswise."""
    report = _port_swap_block_lvs(tmp_path, tie_inputs=False, load=False)
    assert report["status"] == "match"
    assert {
        pair["layout"]: pair["reference"]
        for pair in report["net_correspondence"]
        if pair["reference"] in ("IN", "IN2")
    } == {"IN": "IN2", "IN2": "IN"}
