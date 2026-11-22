"""`klt pex --resistance-graph` echo fields for the graph's terminals and
pair-resistance matrix (issue #5894).

Split out of `tests/test_pex.py` (which sits at its per-file size ceiling)
alongside the other `tests/test_pex_*.py` modules; the strapped-layout
fixtures and helpers still come from that module.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.pex import _resistance_graph_echo
from test_pex import _STRAP_GDS, _pex_strapped


def test_resistance_graph_echo_carries_terminals_and_pair_matrix():
    """Issue #5894: the echo copies the graph's `terminals` and full
    `pair_resistance_ohm` matrix verbatim from the extraction report --
    including a 3+ terminal `legacy` net (whose series-summed lumped total
    overstates any terminal-to-terminal path), a disconnected pair (`null`,
    carried as JSON `None`) and an `unsupported` net's empty lists."""
    parasitics = {
        "nets": [
            {
                "net": "TREE",
                "net_id": 3,
                "resistance_graph": {
                    "status": "ok",
                    "terminals": ["device@0,0", "device@2,0", "device@4,0"],
                    "pair_resistance_ohm": [
                        [0.0, 10.0, None],
                        [10.0, 0.0, 4.0],
                        [None, 4.0, 0.0],
                    ],
                    "legacy_resistance_ohm": 45.0,
                    "selected_resistance_ohm": 45.0,
                    "resistance_source": "legacy",
                    "resistance_source_reason": "terminal_count_not_two",
                },
            },
            {
                "net": "VIA_NET",
                "net_id": 4,
                "resistance_graph": {
                    "status": "unsupported",
                    "terminals": [],
                    "pair_resistance_ohm": [],
                    "legacy_resistance_ohm": 7.5,
                    "selected_resistance_ohm": 7.5,
                    "resistance_source": "legacy",
                    "resistance_source_reason": "graph_status_unsupported",
                },
            },
            {"net": "NO_GRAPH", "net_id": 5},
        ]
    }
    echo = _resistance_graph_echo(parasitics, 0.5)
    assert echo["r_cut_ohm"] == 0.5
    tree, via_net = echo["nets"]
    assert tree["terminals"] == ["device@0,0", "device@2,0", "device@4,0"]
    assert tree["pair_resistance_ohm"] == [
        [0.0, 10.0, None],
        [10.0, 0.0, 4.0],
        [None, 4.0, 0.0],
    ]
    assert via_net["terminals"] == []
    assert via_net["pair_resistance_ohm"] == []
    # Nets without a graph block at all stay out of the echo.
    assert [entry["net"] for entry in echo["nets"]] == ["TREE", "VIA_NET"]


def test_run_pex_resistance_graph_echo_carries_graph_terminals(tmp_path):
    """Integration (issue #5894): the strapped net's echo carries the graph's
    terminals and pair matrix verbatim, and the path R between the two
    terminals is the selected scalar the netlist used."""
    graph, _ = _pex_strapped(
        tmp_path,
        "strapped",
        "echo_graph",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
    )
    (net,) = graph["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "graph"
    assert len(net["terminals"]) == 2
    assert len(net["pair_resistance_ohm"]) == 2
    assert net["pair_resistance_ohm"][0][1] == pytest.approx(
        net["selected_resistance_ohm"]
    )
    assert net["pair_resistance_ohm"][1][0] == pytest.approx(
        net["selected_resistance_ohm"]
    )


def test_run_pex_resistance_graph_unsupported_net_echoes_empty_graph_fields(
    tmp_path,
):
    """Without a per-cut resistance the strapped net is `unsupported`: its
    echo carries empty terminals/matrix verbatim rather than omitting them."""
    graph, _ = _pex_strapped(tmp_path, "strapped", "echo_legacy", resistance_graph=True)
    (net,) = graph["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "legacy"
    assert net["terminals"] == []
    assert net["pair_resistance_ohm"] == []


def test_cli_pex_resistance_graph_json_echoes_graph_fields(tmp_path, capsys):
    """The CLI JSON report carries the echo fields too (issue #5894)."""
    out = tmp_path / "cli.spice"
    code = main(
        [
            "pex",
            str(_STRAP_GDS / "strapped.gds"),
            "--deck",
            "sky130",
            "--extract-only",
            "--resistance-graph",
            "--resistance-graph-r-cut",
            "0",
            "-o",
            str(out),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )
    assert code == 4  # extract-only: nothing checked
    report = json.loads(capsys.readouterr().out)
    (net,) = report["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "graph"
    assert len(net["terminals"]) == 2
    assert net["pair_resistance_ohm"][0][1] == net["selected_resistance_ohm"]
