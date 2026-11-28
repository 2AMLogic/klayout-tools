"""Issues #4819/#6931/#8084: ``topology.known_limitation`` disclosure on
mismatching ASAP7 cells -- single-cell top (4819) and block-level instantiated
masters (6931). The ``"series-order-port-swap"`` masters (8084) have their own
report-path tests in ``test_lvs_port_swap.py`` and corpus pins in
``test_asap7_port_swap.py``."""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.lvs import run_lvs
from klayout_tools.lvs_known_limits import (
    KNOWN_UNSUPPORTED_ASAP7_CELLS,
    count_unsupported_asap7_instances,
    note_known_limit,
)
from test_asap7_finfet import (
    _FOLDED_STACK_CELLS,
    _SERIES_ORDER_CELLS,
    DBU,
    _corpus_lvs,
    _draw_device,
    _errors,
    _needs_corpus,
    _write,
)
from test_asap7_port_swap import _PORT_SWAP_SERIES_ORDER_CELLS


def test_known_unsupported_list_matches_corpus_pins():
    assert KNOWN_UNSUPPORTED_ASAP7_CELLS == {
        **dict.fromkeys(_FOLDED_STACK_CELLS, "folded-stack"),
        **dict.fromkeys(_SERIES_ORDER_CELLS, "series-order"),
        **dict.fromkeys(_PORT_SWAP_SERIES_ORDER_CELLS, "series-order-port-swap"),
    }
    assert (len(_FOLDED_STACK_CELLS), len(_SERIES_ORDER_CELLS)) == (11, 6)
    assert len(_PORT_SWAP_SERIES_ORDER_CELLS) == 11
    assert len(KNOWN_UNSUPPORTED_ASAP7_CELLS) == 28


def _disclose(finfet, status, top):
    out: list = []
    note_known_limit(out, finfet, status, {"top": top}, {})
    return out


def test_known_limitation_disclosure_only_for_mismatching_pinned_cell():
    cell = "OAI21x1_ASAP7_75t_R"
    (entry,) = _disclose(True, "mismatch", cell)
    assert entry["category"] == "topology.known_limitation"
    assert entry["severity"] == "warning"
    assert cell in entry["description"]
    assert entry["details"] == {"cell": cell, "limitation": "folded-stack"}
    assert _disclose(True, "match", cell) == []
    assert _disclose(False, "mismatch", cell) == []
    assert _disclose(True, "mismatch", "INVx1_ASAP7_75t_R") == []


@_needs_corpus
@pytest.mark.parametrize("cell", ["OAI21x1_ASAP7_75t_R", "SDFLx1_ASAP7_75t_R"])
def test_corpus_unsupported_cell_carries_known_limitation_warning(cell):
    report = _corpus_lvs(cell)
    assert report["status"] == "mismatch" and _errors(report)
    assert report["category_counts"]["topology.known_limitation"] == 1
    (entry,) = [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
    assert entry["details"]["cell"] == cell


@_needs_corpus
def test_corpus_supported_cell_has_no_known_limitation_warning():
    report = _corpus_lvs("INVx1_ASAP7_75t_R")
    assert "topology.known_limitation" not in report["category_counts"]


# --------------------------------------------------------------------------- #
# Issue #6931: block-level census of instantiated pinned masters. The count
# tests run on plain SPICE-read netlists, so they need no pinned PDK.
# --------------------------------------------------------------------------- #


def _read_spice(text: str) -> kdb.Netlist:
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".spice", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
    netlist = kdb.Netlist()
    try:
        netlist.read(handle.name, kdb.NetlistSpiceReader())
    finally:
        Path(handle.name).unlink(missing_ok=True)
    return netlist


#: Two placements of a wrapper that instantiates one folded AOI21 leaf, one
#: direct OAI21 call (canonical spelling), a second direct call in folded
#: lower-case spelling, and an XOR2 definition no one calls: distinct masters
#: must count independently, wrapper multiplicity must multiply through, a
#: case-folded spelling must resolve to the canonical name, and the unused
#: definition must contribute nothing.
_CENSUS_SPICE = """\
.subckt aoi21x1_asap7_75t_r S G D
M0 D G S B nmos_rvt w=81n l=20n nfin=3
.ends
.subckt OAI21x1_ASAP7_75t_R S G D
M0 D G S B nmos_rvt w=81n l=20n nfin=3
.ends
.subckt xor2x1_asap7_75t_r S G D
M0 D G S B nmos_rvt w=81n l=20n nfin=3
.ends
.subckt wrapper S G D
X1 S G D aoi21x1_asap7_75t_r
.ends
.subckt TOP S G D
X1 S G D wrapper
X2 S G D wrapper
X3 S G D OAI21x1_ASAP7_75t_R
X4 S G D oai21x1_asap7_75t_r
.ends
"""


def test_census_counts_hierarchy_multiplicity_and_skips_unused_definitions():
    netlist = _read_spice(_CENSUS_SPICE)
    assert count_unsupported_asap7_instances(netlist, "TOP") == {
        "AOI21x1_ASAP7_75t_R": 2,
        "OAI21x1_ASAP7_75t_R": 2,
    }


def test_census_skips_an_unbound_instance_without_losing_resolved_siblings(monkeypatch):
    netlist = _read_spice(_CENSUS_SPICE)
    native_instances = kdb.Circuit.each_subcircuit

    def instances(circuit):
        if circuit.name == "TOP":
            yield kdb.SubCircuit()  # No referenced master.
        yield from native_instances(circuit)

    monkeypatch.setattr(kdb.Circuit, "each_subcircuit", instances)
    assert count_unsupported_asap7_instances(netlist, "TOP") == {
        "AOI21x1_ASAP7_75t_R": 2,
        "OAI21x1_ASAP7_75t_R": 2,
    }


def test_census_scopes_to_the_selected_root():
    netlist = _read_spice(_CENSUS_SPICE)
    assert count_unsupported_asap7_instances(netlist, "wrapper") == {
        "AOI21x1_ASAP7_75t_R": 1
    }
    # A leaf root has no children to count, and an unknown top name defers to
    # circuit selection's own authoritative error rather than raising here.
    assert count_unsupported_asap7_instances(netlist, "OAI21x1_ASAP7_75t_R") == {}
    assert count_unsupported_asap7_instances(netlist, "NOSUCH") == {}
    # TOP and the unused XOR are independent roots until the latter is removed.
    assert count_unsupported_asap7_instances(netlist, None) == {}
    netlist.remove(netlist.circuit_by_name("XOR2X1_ASAP7_75T_R"))
    assert count_unsupported_asap7_instances(netlist, None) == (
        count_unsupported_asap7_instances(netlist, "TOP")
    )


def test_census_ignores_pinned_master_placed_beside_the_top():
    # A second top-level circuit calling the pinned master is outside the
    # compared root and must not bleed into the block's counts.
    netlist = _read_spice(
        _CENSUS_SPICE + ".subckt OTHER S G D\nX9 S G D OAI21x1_ASAP7_75t_R\n.ends\n"
    )
    assert count_unsupported_asap7_instances(netlist, "TOP") == {
        "AOI21x1_ASAP7_75t_R": 2,
        "OAI21x1_ASAP7_75t_R": 2,
    }


def test_block_warnings_carry_independent_per_side_counts():
    out: list = []
    note_known_limit(
        out,
        True,
        "mismatch",
        {"top": "TOPBLOCK"},
        {},
        layout_counts={"AOI21x1_ASAP7_75t_R": 2, "SDFLx1_ASAP7_75t_R": 1},
        reference_counts={"AOI21x1_ASAP7_75t_R": 3, "OAI21x1_ASAP7_75t_R": 1},
    )
    assert [entry["details"]["cell"] for entry in out] == [
        "AOI21x1_ASAP7_75t_R",
        "OAI21x1_ASAP7_75t_R",
        "SDFLx1_ASAP7_75t_R",
    ]
    assert all(entry["category"] == "topology.known_limitation" for entry in out)
    assert all(entry["severity"] == "warning" for entry in out)
    assert all(entry["side"] == "both" for entry in out)
    assert out[0]["details"] == {
        "cell": "AOI21x1_ASAP7_75t_R",
        "limitation": "folded-stack",
        "layout_count": 2,
        "reference_count": 3,
    }
    assert out[1]["details"] == {
        "cell": "OAI21x1_ASAP7_75t_R",
        "limitation": "folded-stack",
        "layout_count": 0,
        "reference_count": 1,
    }
    assert out[2]["details"] == {
        "cell": "SDFLx1_ASAP7_75t_R",
        "limitation": "series-order",
        "layout_count": 1,
        "reference_count": 0,
    }


def test_block_warnings_gated_on_mismatching_finfet_and_instantiation():
    counts = {"AOI21x1_ASAP7_75t_R": 2}
    for finfet, status in ((True, "match"), (False, "mismatch")):
        out: list = []
        note_known_limit(
            out,
            finfet,
            status,
            {"top": "TOPBLOCK"},
            {},
            layout_counts=counts,
            reference_counts=counts,
        )
        assert out == []
    # A supported master and a pinned master with no placements emit nothing.
    # A single-cell warning can still coexist with block warnings below.
    out = []
    note_known_limit(
        out,
        True,
        "mismatch",
        {"top": "TOPBLOCK"},
        {},
        layout_counts={"INVx1_ASAP7_75t_R": 1, "OAI21x1_ASAP7_75t_R": 0},
        reference_counts={"OAI21x1_ASAP7_75t_R": 0},
    )
    assert out == []
    out = []
    note_known_limit(
        out,
        True,
        "mismatch",
        {"top": "SDFLx1_ASAP7_75t_R"},
        {},
        layout_counts={"OAI21x1_ASAP7_75t_R": 1},
    )
    single_cell, block = reversed(out)
    assert single_cell["details"] == {
        "cell": "SDFLx1_ASAP7_75t_R",
        "limitation": "series-order",
    }
    assert block["details"] == {
        "cell": "OAI21x1_ASAP7_75t_R",
        "limitation": "folded-stack",
        "layout_count": 1,
        "reference_count": 0,
    }


# --------------------------------------------------------------------------- #
# Issue #6931: the report path, on a synthetic hierarchical FinFET block (no
# pinned PDK needed). Masters are drawn with the public `_draw_device`
# geometry under pinned *names*: the census is driven by the master name, and
# the reference bodies carry a different nfin so the compare mismatches.
# --------------------------------------------------------------------------- #

_AOI = "AOI21x1_ASAP7_75t_R"
_OAI = "OAI21x1_ASAP7_75t_R"
_UNUSED = "XOR2x1_ASAP7_75t_R"
#: Cell footprint of `_draw_device()` in dbu: everything drawn lies within
#: x [0, 764]; placements sit side by side at this pitch.
_PITCH = 1000

_M_CARD = "M0 D G S vsubs nmos_rvt w=81n l=20n nfin={nfin}"


def _block_gds(
    tmp_path: Path,
    placements: list[tuple[str, int]],
    *,
    extra_defined: tuple[str, ...] = (),
) -> str:
    block = kdb.Layout()
    block.dbu = DBU
    for name in {name for name, _ in placements} | set(extra_defined):
        cell = _draw_device()
        cell.cell("TOP").name = name
        block.read(_write(cell, tmp_path, f"{name}.gds"))
    top = block.create_cell("TOP")
    for name, k in placements:
        top.insert(
            kdb.CellInstArray(block.cell(name).cell_index(), kdb.Trans(k * _PITCH, 0))
        )
    return _write(block, tmp_path, "block.gds")


def _block_reference(calls: list[tuple[str, str]], *, nfin: int) -> str:
    """One ``.SUBCKT`` per distinct callee plus a ``TOP`` issuing ``calls``
    (callee spelling per call, so a folded spelling is exercisable)."""
    text = "".join(
        f".SUBCKT {name} S G D\n{_M_CARD.format(nfin=nfin)}\n.ENDS\n"
        for name in sorted({name for name, _ in calls})
    )
    text += ".SUBCKT TOP S G D\n"
    text += "".join(f"X{i} S G D {callee}\n" for i, (_, callee) in enumerate(calls, 1))
    return text + ".ENDS\n"


def _block_lvs(
    tmp_path: Path,
    placements: list[tuple[str, int]],
    calls: list[tuple[str, str]],
    *,
    nfin: int,
    options: dict | None = None,
    extra_defined: tuple[str, ...] = (),
) -> dict:
    gds = _block_gds(tmp_path, placements, extra_defined=extra_defined)
    reference = tmp_path / "ref.cdl"
    reference.write_text(_block_reference(calls, nfin=nfin))
    request = {
        "layout": {"file": gds, "deck": "asap7", "top": "TOP"},
        "reference": {"netlist": str(reference), "top": "TOP"},
    }
    if options is not None:
        request["options"] = options
    return run_lvs(json.dumps(request))


def _known_limitation_entries(report: dict) -> list[dict]:
    return [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]


#: Two layout placements of the AOI master against three reference calls to
#: it (one in folded lower-case spelling) -- the acceptance criterion's
#: exactly-this-shape example -- plus one OAI placement/call on each side and
#: an XOR2 definition instantiated on neither side.
_PLACEMENTS = [(_AOI, 0), (_AOI, 1), (_OAI, 2)]
_CALLS = [(_AOI, _AOI), (_AOI, "aoi21x1_asap7_75t_r"), (_AOI, _AOI), (_OAI, _OAI)]


@pytest.mark.parametrize(
    "options",
    [
        None,  # flatten off, the shape the reported measurement used
        {"flatten_layout": True, "flatten_reference": True},
    ],
    ids=["hierarchical", "flattened"],
)
def test_block_mismatch_carries_one_warning_per_instantiated_master(tmp_path, options):
    report = _block_lvs(
        tmp_path,
        _PLACEMENTS,
        _CALLS,
        nfin=4,
        options=options,
        extra_defined=(_UNUSED,),
    )
    assert report["status"] == "mismatch"
    assert _errors(report)
    entries = _known_limitation_entries(report)
    assert report["category_counts"]["topology.known_limitation"] == len(entries) == 2
    assert all(entry["severity"] == "warning" for entry in entries)
    assert [entry["details"]["cell"] for entry in entries] == [_AOI, _OAI]
    assert entries[0]["details"] == {
        "cell": _AOI,
        "limitation": "folded-stack",
        "layout_count": 2,
        "reference_count": 3,
    }
    assert entries[1]["details"] == {
        "cell": _OAI,
        "limitation": "folded-stack",
        "layout_count": 1,
        "reference_count": 1,
    }


def test_block_of_supported_masters_mismatches_without_known_limitation(tmp_path):
    supported = ["MYCELLn1_ASAP7_75t_R", "MYCELLn2_ASAP7_75t_R"]
    report = _block_lvs(
        tmp_path,
        [(supported[0], 0), (supported[1], 1)],
        [(supported[0], supported[0]), (supported[1], supported[1])],
        nfin=4,
    )
    assert report["status"] == "mismatch"
    assert _errors(report)
    assert "topology.known_limitation" not in report["category_counts"]


def test_matching_block_of_a_pinned_master_emits_no_known_limitation(tmp_path):
    """The census runs on a matching compare too (one placement, one call);
    the mismatch gate is what keeps it silent."""
    report = _block_lvs(
        tmp_path,
        [(_AOI, 0)],
        [(_AOI, _AOI)],
        nfin=3,
        options={"flatten_layout": True, "flatten_reference": True},
    )
    assert report["status"] == "match", [
        m for m in report["mismatches"] if m["severity"] == "error"
    ]
    assert "topology.known_limitation" not in report["category_counts"]
