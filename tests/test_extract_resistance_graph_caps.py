"""The caller-settable resistance-graph build caps (issue #6964).

``klt extract --resistance-graph`` builds each net's graph under four caps
(``MAX_GRAPH_TERMINALS``, ``DEFAULT_MAX_DECOMPOSITION_CELLS``,
``MAX_GRAPH_NODES``, ``MAX_GRAPH_EDGES``) that no flag could raise, so a
power rail with 65 or more terminals, or a poured polygon needing more than
4096 mesh cells, stayed ``graph_too_large`` on the legacy series-sum scalar.
The four ``--resistance-graph-max-*`` flags expose the builder's own keyword
arguments.

The builder-level cases are the issue's two synthetic geometries. The adapter
and CLI cases lower each cap independently, check the diagnostic names the
value actually applied, and check that restoring capacity solves again --
a flag that parses but never reaches the builder fails here.

`tests/test_extract.py` and `tests/test_extract_resistance_graph.py` both sit
at their file-size ceilings, so these live in their own module; the
``klt pex`` half is in ``tests/test_pex_resistance_graph.py``.
"""

from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.extract import ExtractError, run_extract
from klayout_tools.extract_graph_ports import resistance_graph_report
from klayout_tools.extract_resistance_graph import (
    PortInput,
    RoleInput,
    build_resistance_graph,
    terminal_resistance_matrix,
)
from klayout_tools.extract_resistance_graph_caps import (
    RESISTANCE_GRAPH_CAP_DEFAULTS,
    RESISTANCE_GRAPH_CAP_FLAGS,
    effective_resistance_graph_caps,
    validate_resistance_graph_caps,
)
from klayout_tools.extract_runset_flags import REFUSED_FLAG_BY_PARAM, refused_flags

DBU = 0.001
UM = 1000
LI1, LI1_LABEL, LICON1 = (67, 20), (67, 5), (66, 44)  # sky130 layers


def _region(*boxes: tuple[int, int, int, int]) -> kdb.Region:
    return kdb.Region([kdb.Box(*box) for box in boxes])


# ---------------------------------------------------------------------------
# Builder level: the issue's cases A (terminal cap) and B (cell cap)
# ---------------------------------------------------------------------------


def _plate_ports(n_ports: int):
    """Case A: one 20 x 4 um plate with ``n_ports`` 1 um terminal ports,
    0.3 um apart."""
    plate = kdb.Polygon(kdb.Box(0, 0, 20 * UM, 4 * UM))
    ports = [
        ("metal1", kdb.Box(i * 300, 0, i * 300 + UM, 4 * UM)) for i in range(n_ports)
    ]
    return plate, ports


def _build_plate(n_ports: int, **caps):
    plate, patches = _plate_ports(n_ports)
    roles = [RoleInput("metal1", 0.125, (plate,))]
    ports = [PortInput(f"t{i:03d}", (patch,)) for i, patch in enumerate(patches)]
    return build_resistance_graph(roles, ports, [], DBU, **caps)


def test_terminal_cap_control_solves_and_one_more_terminal_is_too_large():
    control = _build_plate(64)
    assert control.status == "ok"
    matrix = terminal_resistance_matrix(control, dbu=DBU)
    assert matrix.status == "ok"
    assert matrix.solves == 63
    over = _build_plate(65)
    assert over.status == "unsupported"
    assert over.nodes == () and over.edges == ()
    assert [(d.code, d.detail) for d in over.diagnostics] == [
        ("graph_too_large", {"terminals": 65, "max_terminals": 64})
    ]


@pytest.mark.parametrize("max_terminals", [65, 128])
def test_terminal_cap_raised_to_at_least_the_count_gives_the_full_matrix(
    max_terminals,
):
    graph = _build_plate(65, max_terminals=max_terminals)
    assert graph.status == "ok"
    matrix = terminal_resistance_matrix(graph, dbu=DBU)
    assert matrix.status == "ok"
    assert len(matrix.terminals) == 65
    assert [len(row) for row in matrix.pair_resistance_ohm] == [65] * 65
    assert matrix.solves == 64
    assert all(r is not None for row in matrix.pair_resistance_ohm for r in row)


def _perforated_plate(cols: int = 100, rows: int = 10):
    """Case B: a plate with a ``cols x rows`` lattice of 1 x 1 um holes (a
    strapped/poured rail) and two corner terminals."""
    region = kdb.Region(kdb.Box(0, 0, (cols + 1) * 2 * UM, (rows + 1) * 2 * UM))
    for i in range(cols):
        for j in range(rows):
            region -= kdb.Region(
                kdb.Box(
                    i * 2 * UM + UM // 2,
                    j * 2 * UM + UM // 2,
                    i * 2 * UM + 3 * UM // 2,
                    j * 2 * UM + 3 * UM // 2,
                )
            )
    far = (cols + 1) * 2 * UM, (rows + 1) * 2 * UM
    return (
        next(region.merged().each()),
        (0, 0, UM, UM),
        (far[0] - UM, far[1] - UM, *far),
    )


def _build_perforated(**caps):
    plate, near, far = _perforated_plate()
    roles = [RoleInput("metal1", 0.125, (plate,))]
    ports = [
        PortInput("A", (("metal1", kdb.Box(*near)),)),
        PortInput("B", (("metal1", kdb.Box(*far)),)),
    ]
    return build_resistance_graph(roles, ports, [], DBU, **caps)


def test_cell_cap_default_refuses_the_perforated_plate_and_raising_it_solves():
    over = _build_perforated()
    assert over.status == "unsupported"
    (diagnostic,) = over.diagnostics
    assert diagnostic.code == "graph_too_large"
    assert "4669 mesh cells" in diagnostic.message
    assert f"{RESISTANCE_GRAPH_CAP_DEFAULTS['max_cells']}-mesh-cell cap" in (
        diagnostic.message
    )
    graph = _build_perforated(max_cells=65536)
    assert graph.status == "ok"
    assert (len(graph.nodes), len(graph.edges)) == (3560, 4784)
    matrix = terminal_resistance_matrix(graph, dbu=DBU)
    assert matrix.status == "ok"
    assert [len(row) for row in matrix.pair_resistance_ohm] == [2, 2]


# ---------------------------------------------------------------------------
# The extract adapter: each cap reaches the builder on its own
# ---------------------------------------------------------------------------

#: A T-shaped 5 x 0.17 um metal0 strip (Rs = 10 ohm/sq) with a 0.5 um stub
#: and a contact on each end edge: two terminals, four pieces in a 3 x 2
#: grid (six cells), six nodes and five edges at the default caps.
_T_STRIP = (("metal0", 10.0, _region((0, 0, 5 * UM, 170), (2400, 0, 2570, 500)), None),)
_T_STRIP_CONTACTS = ((0, 0, 170, 170), (5 * UM - 170, 0, 5 * UM, 170))


def _t_strip_report(caps=None):
    return resistance_graph_report(
        list(_T_STRIP), _region(*_T_STRIP_CONTACTS), [], DBU, 400.0, 0.0, caps=caps
    )


@pytest.mark.parametrize(
    ("cap", "value", "named"),
    [
        ("max_terminals", 1, "2 terminals exceed the 1 cap"),
        ("max_cells", 5, "over this build's 5-mesh-cell cap"),
        ("max_nodes", 3, "4 piece nodes exceed the 3 cap"),
        ("max_edges", 4, "6 nodes / 5 edges exceed the 5000 / 4 caps"),
    ],
)
def test_adapter_lowers_each_cap_alone_and_names_the_applied_value(cap, value, named):
    """Each cap on its own, one below what the strip needs: the builder
    refuses, the diagnostic names the value that was applied, and the net
    stays on the legacy scalar."""
    report = _t_strip_report({cap: value})
    assert report["status"] == "unsupported"
    (diagnostic,) = report["diagnostics"]
    assert diagnostic["code"] == "graph_too_large"
    assert named in diagnostic["message"]
    assert report["resistance_source"] == "legacy"
    assert report["selected_resistance_ohm"] == 400.0


def test_adapter_restored_capacity_solves_the_unchanged_graph():
    """Exactly enough capacity, any surplus, the defaults spelled out and no
    caps at all all give the same solved graph."""
    default = _t_strip_report()
    assert default["status"] == "ok"
    assert (default["nodes"], default["edges"]) == (6, 5)
    assert default["resistance_source"] == "graph"
    exactly = {"max_terminals": 2, "max_cells": 6, "max_nodes": 6, "max_edges": 5}
    surplus = {cap: 100 for cap in RESISTANCE_GRAPH_CAP_FLAGS}
    for caps in (exactly, surplus, RESISTANCE_GRAPH_CAP_DEFAULTS, {}):
        assert _t_strip_report(caps) == default
    for key in RESISTANCE_GRAPH_CAP_FLAGS:
        assert _t_strip_report({key: exactly[key]}) == default


def _plate_report(caps=None):
    """Case A through the adapter: 65 disjoint 0.2 x 0.17 um contacts along
    the plate's lower edge, each a terminal on the modelled conductor."""
    plate = [("metal0", 0.125, _region((0, 0, 20 * UM, 4 * UM)), None)]
    cuts = tuple((i * 300, 0, i * 300 + 200, 170) for i in range(65))
    return resistance_graph_report(plate, _region(*cuts), [], DBU, 9.0, 0.0, caps=caps)


def test_adapter_terminal_cap_lets_the_65_terminal_plate_through():
    over = _plate_report()
    assert over["status"] == "unsupported"
    assert over["diagnostics"][0]["message"] == "65 terminals exceed the 64 cap"
    ok = _plate_report({"max_terminals": 65})
    assert ok["status"] == "ok"
    assert len(ok["terminals"]) == 65
    assert [len(row) for row in ok["pair_resistance_ohm"]] == [65] * 65
    assert ok["resistance_source_reason"] == "terminal_count_not_two"


def test_adapter_cell_cap_lets_the_perforated_plate_through():
    plate, near, far = _perforated_plate()
    roles = [("metal0", 0.125, kdb.Region(plate), None)]
    contacts = _region(near, far)
    over = resistance_graph_report(roles, contacts, [], DBU, 9.0, 0.0)
    assert over["status"] == "unsupported"
    assert "graph_too_large" == over["diagnostics"][0]["code"]
    ok = resistance_graph_report(
        roles, contacts, [], DBU, 9.0, 0.0, caps={"max_cells": 65536}
    )
    assert ok["status"] == "ok"
    assert ok["resistance_source"] == "graph"


# ---------------------------------------------------------------------------
# The caps module: validation, defaults and the runset-engine refusal table
# ---------------------------------------------------------------------------


def test_cap_flags_and_defaults_are_the_builders_own():
    from klayout_tools import extract_resistance_graph as erg

    assert RESISTANCE_GRAPH_CAP_DEFAULTS == {
        "max_terminals": erg.MAX_GRAPH_TERMINALS,
        "max_cells": erg.DEFAULT_MAX_DECOMPOSITION_CELLS,
        "max_nodes": erg.MAX_GRAPH_NODES,
        "max_edges": erg.MAX_GRAPH_EDGES,
    }
    assert list(RESISTANCE_GRAPH_CAP_DEFAULTS) == list(RESISTANCE_GRAPH_CAP_FLAGS)
    assert [
        f"max_{flag.rsplit('-', 1)[1]}" for flag in RESISTANCE_GRAPH_CAP_FLAGS.values()
    ] == list(RESISTANCE_GRAPH_CAP_DEFAULTS)


@pytest.mark.parametrize(
    "caps", [None, {}, {"max_cells": 1}, RESISTANCE_GRAPH_CAP_DEFAULTS]
)
def test_valid_caps_pass(caps):
    assert validate_resistance_graph_caps(True, caps) is None


@pytest.mark.parametrize("caps", [None, {}])
def test_no_cap_needs_the_graph_flag(caps):
    validate_resistance_graph_caps(False, caps)


@pytest.mark.parametrize(
    ("caps", "message"),
    [
        ({"max_cells": 0}, "requires --resistance-graph"),
        (
            {"max_cells": 3, "max_edges": 9},
            "--resistance-graph-max-cells, "
            "--resistance-graph-max-edges require --resistance-graph",
        ),
    ],
)
def test_a_cap_without_the_graph_flag_is_rejected(caps, message):
    with pytest.raises(ExtractError, match=message):
        validate_resistance_graph_caps(False, caps)


@pytest.mark.parametrize("value", [0, -1, 2.5, "8", True, None, float("nan")])
def test_a_cap_that_is_not_a_positive_integer_is_rejected(value):
    with pytest.raises(ExtractError) as excinfo:
        validate_resistance_graph_caps(True, {"max_nodes": value})
    assert str(excinfo.value).startswith("--resistance-graph-max-nodes must be a")
    assert f"(got {value!r})" in str(excinfo.value)


@pytest.mark.parametrize("bad", [{"cells": 8}, {42: 8}, "max_cells=8"])
def test_an_unknown_cap_or_non_mapping_is_rejected(bad):
    with pytest.raises(ExtractError, match="cap"):
        validate_resistance_graph_caps(True, bad)


def test_effective_caps_fill_in_the_defaults_in_flag_order():
    assert effective_resistance_graph_caps(None) == RESISTANCE_GRAPH_CAP_DEFAULTS
    assert effective_resistance_graph_caps({}) == RESISTANCE_GRAPH_CAP_DEFAULTS
    effective = effective_resistance_graph_caps({"max_cells": 65536})
    assert effective == {**RESISTANCE_GRAPH_CAP_DEFAULTS, "max_cells": 65536}
    assert list(effective) == list(RESISTANCE_GRAPH_CAP_FLAGS)


def test_runset_engine_refuses_exactly_the_caps_given():
    assert "resistance_graph_caps" in REFUSED_FLAG_BY_PARAM
    assert refused_flags({"resistance_graph_caps": None}) == []
    assert refused_flags({"resistance_graph_caps": {}}) == []
    assert refused_flags({"resistance_graph_caps": {"max_cells": 8}}) == [
        "--resistance-graph-max-cells"
    ]
    assert refused_flags(
        {
            "resistance_graph": True,
            "resistance_graph_caps": {"max_edges": 9, "max_cells": 8},
        }
    ) == [
        "--resistance-graph",
        "--resistance-graph-max-cells",
        "--resistance-graph-max-edges",
    ]


# ---------------------------------------------------------------------------
# `run_extract` argument validation, before any file is read
# ---------------------------------------------------------------------------


def test_run_extract_rejects_caps_without_the_graph_flag():
    with pytest.raises(ExtractError, match="--resistance-graph-max-cells requires"):
        run_extract(
            "/nonexistent.gds",
            "sky130",
            parasitics=True,
            resistance_graph_caps={"max_cells": 8},
        )


@pytest.mark.parametrize("value", [0, -5, 2.5, True])
def test_run_extract_rejects_a_cap_that_is_not_a_positive_integer(value):
    with pytest.raises(ExtractError, match="must be a positive integer"):
        run_extract(
            "/nonexistent.gds",
            "sky130",
            parasitics=True,
            resistance_graph=True,
            resistance_graph_caps={"max_cells": value},
        )


def test_run_extract_rejects_an_unknown_cap():
    with pytest.raises(ExtractError, match="unknown resistance graph cap 'cells'"):
        run_extract(
            "/nonexistent.gds",
            "sky130",
            parasitics=True,
            resistance_graph=True,
            resistance_graph_caps={"cells": 8},
        )


def test_run_extract_rejects_emit_selected_resistance_without_the_graph():
    """`emit_selected_resistance` (issue #4037) reads the graph result, so it is
    refused up front without `resistance_graph`. This validator moved into
    `extract_validate.py` with this PR's size-ratchet displacement, where the
    module's 100% coverage floor leaves its `raise` arm no slack."""
    with pytest.raises(
        ExtractError, match="emit_selected_resistance requires resistance_graph"
    ):
        run_extract(
            "/nonexistent.gds",
            "sky130",
            parasitics=True,
            emit_selected_resistance=True,
        )


def test_caps_reach_the_finfet_extraction_path(tmp_path):
    """The FinFET route forwards only the parasitics options it names to the
    shared pass, so a cap missing there would parse, validate and echo but
    never apply. Each of its nets builds under the default caps and is
    refused under a lowered one."""
    from test_asap7_finfet import _draw_device, _write

    path = _write(_draw_device(fins=2), tmp_path)
    kwargs = {"parasitics": True, "resistance_graph": True}
    default = run_extract(path, "asap7", output=str(tmp_path / "d.spice"), **kwargs)
    capped = run_extract(
        path,
        "asap7",
        output=str(tmp_path / "c.spice"),
        resistance_graph_caps={"max_edges": 1},
        **kwargs,
    )
    assert [e["resistance_graph"]["status"] for e in default["parasitics"]["nets"]] == [
        "skipped",
        "skipped",
    ]
    for entry in capped["parasitics"]["nets"]:
        graph = entry["resistance_graph"]
        assert graph["status"] == "unsupported"
        assert graph["diagnostics"][0]["message"].endswith(" / 1 caps")


def test_help_text_states_the_builders_defaults():
    import argparse

    from klayout_tools.cli.parser import create_parser

    subparsers = next(
        action
        for action in create_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    actions = {action.dest: action for action in subparsers.choices["extract"]._actions}
    for key, default in RESISTANCE_GRAPH_CAP_DEFAULTS.items():
        text = actions[f"resistance_graph_{key}"].help
        assert f"cap (positive integer, default {default})" in text, (key, text)
        assert text.startswith("--resistance-graph ") and "Requires" in text
        assert actions[f"resistance_graph_{key}"].option_strings == [
            RESISTANCE_GRAPH_CAP_FLAGS[key]
        ]
