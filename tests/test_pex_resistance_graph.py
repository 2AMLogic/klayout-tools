"""`klt pex`'s resistance-graph echo: the per-net `fallback_reason` branch
table (issue #5747) and the `--resistance-graph-r-cut` request forwarding
(issue #4037).

`tests/test_pex.py` sits at its file-size ceiling, so these live here rather
than beside the other resistance-graph tests; the run-level helpers they
exercise stay importable from `test_pex` alongside the integration tests
that share them."""

import pytest

from klayout_tools.pex import run_pex
from klayout_tools.pex_resistance_graph import _resistance_graph_fallback_reason
from test_pex import _capture_run_extract_kwargs, _pex_strapped


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ({"resistance_source": "graph", "resistance_source_reason": "x"}, None),
        (
            {
                "resistance_source": "legacy",
                "status": "unsupported",
                "diagnostics": [
                    {"code": "graph_too_large"},
                    {"code": "unsupported_geometry"},
                ],
                "resistance_source_reason": "graph_status_unsupported",
            },
            "graph_too_large",
        ),
        (
            {
                "resistance_source": "legacy",
                "status": "unsupported",
                "diagnostics": [],
                "resistance_source_reason": "graph_status_unsupported",
            },
            "graph_status_unsupported",
        ),
        (
            {
                "resistance_source": "legacy",
                "status": "ok",
                "diagnostics": [{"code": "ignored"}],
                "resistance_source_reason": "terminal_count_not_two",
            },
            "terminal_count_not_two",
        ),
        (
            {
                "resistance_source": "legacy",
                "status": "skipped",
                "resistance_source_reason": "graph_status_skipped",
            },
            "graph_status_skipped",
        ),
        (
            {
                "resistance_source": "legacy",
                "status": "ok",
                "resistance_source_reason": "pair_resistance_unavailable",
            },
            "pair_resistance_unavailable",
        ),
    ],
)
def test_resistance_graph_fallback_reason_branches(graph, expected):
    assert _resistance_graph_fallback_reason(graph) == expected


def test_run_pex_resistance_graph_r_cut_zero_is_forwarded(tmp_path, monkeypatch):
    calls = _capture_run_extract_kwargs(monkeypatch)
    _pex_strapped(
        tmp_path,
        "single_level",
        "g",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0,
    )
    assert calls[0]["resistance_graph"] is True
    assert calls[0]["resistance_graph_r_cut_ohm"] == 0
    assert calls[0]["emit_selected_resistance"] is True


def test_run_pex_resistance_graph_legacy_net_keeps_legacy_value(tmp_path):
    """Without `--resistance-graph-r-cut`, and on a deck that curates no
    value for the landing type (gf180mcu's poly contact), the two-level net
    is `unsupported`: the source stays `legacy` and the written netlist is
    unchanged (the graph request alone writes nothing new)."""
    from helpers.resistance_graph_fixtures import make_gf180mcu_poly_contact_layout
    from test_pex import _write_gds

    layout = _write_gds(make_gf180mcu_poly_contact_layout(), tmp_path / "pc.gds")
    base = run_pex(
        layout,
        [],
        "gf180mcu",
        output=str(tmp_path / "b.spice"),
        artifacts_dir=str(tmp_path / "art_b"),
        extract_only=True,
    )
    graph = run_pex(
        layout,
        [],
        "gf180mcu",
        output=str(tmp_path / "g.spice"),
        artifacts_dir=str(tmp_path / "art_g"),
        extract_only=True,
        resistance_graph=True,
    )
    (net,) = graph["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "legacy"
    assert net["fallback_reason"] == "via_resistance_unknown"
    assert net["terminals"] == []
    assert net["pair_resistance_ohm"] == []
    assert net["selected_resistance_ohm"] == net["legacy_resistance_ohm"]
    assert graph["extraction"]["netlist_sha256"] == base["extraction"]["netlist_sha256"]
