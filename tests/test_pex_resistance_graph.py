"""`klt pex`'s resistance-graph echo: the per-net `fallback_reason` branch
table (issue #5747) and the `--resistance-graph-r-cut` request forwarding
(issue #4037).

`tests/test_pex.py` sits at its file-size ceiling, so these live here rather
than beside the other resistance-graph tests; the run-level helpers they
exercise stay importable from `test_pex` alongside the integration tests
that share them."""

import pytest

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
