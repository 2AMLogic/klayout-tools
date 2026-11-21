"""`klt pex`'s resistance-graph echo: the per-net `fallback_reason` branch
table (issue #5747), the `--resistance-graph-r-cut` request forwarding
(issue #4037), and the `--resistance-graph-max-*` cap passthrough with its
`extraction.resistance_graph.caps` echo (issue #6964).

`tests/test_pex.py` sits at its file-size ceiling, so these live here rather
than beside the other resistance-graph tests; the run-level helpers they
exercise stay importable from `test_pex` alongside the integration tests
that share them. The dry-run cases import `tests/test_pex_dry_run.py`'s
`bench` fixture and message-parity helper for the same reason."""

import json

import pytest

import test_pex_dry_run
from klayout_tools.cli import main
from klayout_tools.extract_resistance_graph_caps import (
    RESISTANCE_GRAPH_CAP_DEFAULTS,
    RESISTANCE_GRAPH_CAP_FLAGS,
)
from klayout_tools.pex import PexError
from klayout_tools.pex_dry_run import run_pex_dry_run
from klayout_tools.pex_resistance_graph import _resistance_graph_fallback_reason
from test_extract_resistance_graph_caps_cli import (
    _perforated_layout,
    _t_strip_layout,
)
from test_pex import _capture_run_extract_kwargs, _pex_strapped
from test_pex_dry_run import _assert_same_message

bench = test_pex_dry_run.bench  # pytest collects the fixture by module-level name


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


# ---------------------------------------------------------------------------
# --resistance-graph-max-* (issue #6964): the caps reach `klt extract` and
# the echo records the effective values
# ---------------------------------------------------------------------------


def test_run_pex_forwards_the_caps_and_echoes_the_effective_values(
    tmp_path, monkeypatch
):
    """A single override reaches `run_extract` alone (an absent cap keeps the
    builder's default) and the echo records all four effective values."""
    calls = _capture_run_extract_kwargs(monkeypatch)
    report, _ = _pex_strapped(
        tmp_path,
        "strapped",
        "caps",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
        resistance_graph_caps={"max_terminals": 65},
    )
    assert calls[0]["resistance_graph_caps"] == {"max_terminals": 65}
    echo = report["extraction"]["resistance_graph"]
    assert echo["caps"] == {**RESISTANCE_GRAPH_CAP_DEFAULTS, "max_terminals": 65}
    assert list(echo) == ["r_cut_ohm", "caps", "nets"]
    assert list(echo["caps"]) == list(RESISTANCE_GRAPH_CAP_FLAGS)
    (net,) = echo["nets"]
    assert net["resistance_source"] == "graph"


def test_run_pex_omits_the_caps_echo_when_no_cap_flag_was_given(tmp_path):
    """With and without the graph flag, a run that names no cap carries no
    `caps` member -- the echo is byte-identical to the pre-#6964 one."""
    off, _ = _pex_strapped(tmp_path, "strapped", "off")
    graph, _ = _pex_strapped(
        tmp_path,
        "strapped",
        "graph",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
    )
    assert "resistance_graph" not in off["extraction"]
    assert '"caps"' not in json.dumps(off)
    assert set(graph["extraction"]["resistance_graph"]) == {"r_cut_ohm", "nets"}


def test_run_pex_caps_at_the_defaults_leave_the_netlist_and_nets_unchanged(
    tmp_path,
):
    """Spelling out every default, or raising a cap a net never reaches,
    changes neither the written netlist nor the per-net echo."""
    base, _ = _pex_strapped(
        tmp_path,
        "strapped",
        "base",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
    )
    for label, caps in (
        ("defaults", RESISTANCE_GRAPH_CAP_DEFAULTS),
        ("surplus", {cap: 100 for cap in RESISTANCE_GRAPH_CAP_FLAGS}),
    ):
        capped, _ = _pex_strapped(
            tmp_path,
            "strapped",
            label,
            resistance_graph=True,
            resistance_graph_r_cut_ohm=0.0,
            resistance_graph_caps=caps,
        )
        extraction = capped["extraction"]
        assert extraction["netlist_sha256"] == base["extraction"]["netlist_sha256"]
        assert (
            extraction["resistance_graph"]["nets"]
            == (base["extraction"]["resistance_graph"]["nets"])
        )
        assert extraction["resistance_graph"]["caps"] == {
            **RESISTANCE_GRAPH_CAP_DEFAULTS,
            **caps,
        }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"resistance_graph_caps": {"max_cells": 8}}, "requires --resistance-graph"),
        (
            {"resistance_graph": True, "resistance_graph_caps": {"max_cells": 0}},
            "(got 0)",
        ),
        (
            {"resistance_graph": True, "resistance_graph_caps": {"max_cells": -8}},
            "(got -8)",
        ),
        (
            {"resistance_graph": True, "resistance_graph_caps": {"max_cells": 2.5}},
            "(got 2.5)",
        ),
        (
            {"resistance_graph": True, "resistance_graph_caps": {"max_cells": True}},
            "(got True)",
        ),
        (
            {"resistance_graph": True, "resistance_graph_caps": {"cells": 8}},
            "unknown resistance graph cap 'cells'",
        ),
    ],
)
def test_run_pex_rejects_invalid_caps(tmp_path, kwargs, message):
    with pytest.raises(PexError, match="extraction failed: .*" + message):
        _pex_strapped(tmp_path, "strapped", "x", **kwargs)


def _cli_pex(capsys, tmp_path, layout, cap_flags) -> dict:
    """`klt pex --extract-only --resistance-graph` JSON report of ``layout``."""
    gds = tmp_path / "pex.gds"
    layout.write(str(gds))
    argv = [
        "pex",
        str(gds),
        "--deck",
        "sky130",
        "--extract-only",
        "--resistance-graph",
        "--resistance-graph-r-cut",
        "0",
        *cap_flags,
        "-o",
        str(tmp_path / "pex.spice"),
        "--outdir",
        str(tmp_path / "art"),
        "--format",
        "json",
    ]
    code = main(argv)  # extract-only: nothing checked, so exit 4
    assert code == 4
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("key", "value"),
    [("max_terminals", 1), ("max_cells", 5), ("max_nodes", 3), ("max_edges", 4)],
)
def test_cli_pex_lowers_each_cap_alone_and_echoes_it(tmp_path, capsys, key, value):
    """One cap per run, one below what the T-strip needs: the net falls back
    to the legacy R, and the echo holds that value beside the other three
    defaults."""
    report = _cli_pex(
        capsys,
        tmp_path,
        _t_strip_layout(),
        [RESISTANCE_GRAPH_CAP_FLAGS[key], str(value)],
    )
    echo = report["extraction"]["resistance_graph"]
    assert echo["caps"] == {**RESISTANCE_GRAPH_CAP_DEFAULTS, key: value}
    (net,) = echo["nets"]
    assert net["resistance_source"] == "legacy"
    assert net["fallback_reason"] == "graph_too_large"


def test_cli_pex_restoring_capacity_selects_the_graph_value(tmp_path, capsys):
    report = _cli_pex(
        capsys,
        tmp_path,
        _t_strip_layout(),
        ["--resistance-graph-max-edges", "5", "--resistance-graph-max-cells", "6"],
    )
    echo = report["extraction"]["resistance_graph"]
    assert echo["caps"] == {
        **RESISTANCE_GRAPH_CAP_DEFAULTS,
        "max_edges": 5,
        "max_cells": 6,
    }
    (net,) = echo["nets"]
    assert net["resistance_source"] == "graph"
    assert net["fallback_reason"] is None


def test_cli_pex_cell_cap_lets_a_poured_rail_take_the_graph_resistance(
    tmp_path, capsys
):
    """The issue's case B through `klt pex`: over the default cell cap the
    rail keeps the legacy series sum; with only that cap raised its lumped R
    is the graph value."""
    default = _cli_pex(capsys, tmp_path, _perforated_layout(), [])
    (legacy,) = default["extraction"]["resistance_graph"]["nets"]
    assert "caps" not in default["extraction"]["resistance_graph"]
    assert (legacy["resistance_source"], legacy["fallback_reason"]) == (
        "legacy",
        "graph_too_large",
    )
    raised = _cli_pex(
        capsys,
        tmp_path,
        _perforated_layout(),
        ["--resistance-graph-max-cells", "65536"],
    )
    (net,) = raised["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "graph"
    assert net["fallback_reason"] is None
    assert net["selected_resistance_ohm"] is not None
    assert raised["extraction"]["resistance_graph"]["caps"] == {
        **RESISTANCE_GRAPH_CAP_DEFAULTS,
        "max_cells": 65536,
    }
    assert (
        raised["extraction"]["netlist_sha256"]
        != default["extraction"]["netlist_sha256"]
    )


def test_cli_pex_rejects_a_cap_without_the_graph_flag(tmp_path, capsys):
    gds = tmp_path / "pex.gds"
    _t_strip_layout().write(str(gds))
    code = main(
        [
            "pex",
            str(gds),
            "--deck",
            "sky130",
            "--extract-only",
            "--resistance-graph-max-cells",
            "8",
            "-o",
            str(tmp_path / "pex.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )
    assert code == 1
    error = json.loads(capsys.readouterr().err)["error"]["message"]
    assert error == (
        "extraction failed: --resistance-graph-max-cells requires --resistance-graph"
    )


def test_pex_dry_run_agrees_with_the_real_run_on_caps(bench):
    """The dry run runs the same `run_extract` argument rules, so every
    invalid cap gets the real run's message, and a valid one passes."""
    for caps in (
        {"max_cells": 0},
        {"max_edges": -1},
        {"max_nodes": 2.5},
        {"max_cells": True},
        {"cells": 8},
    ):
        message = _assert_same_message(
            bench, [bench["request"]], resistance_graph=True, resistance_graph_caps=caps
        )
        assert message.startswith("extraction failed: ")
    message = _assert_same_message(
        bench, [bench["request"]], resistance_graph_caps={"max_cells": 8}
    )
    assert (
        message
        == "extraction failed: --resistance-graph-max-cells requires --resistance-graph"
    )
    report = run_pex_dry_run(
        bench["layout"],
        [bench["request"]],
        "sky130",
        resistance_graph=True,
        resistance_graph_caps={"max_cells": 8},
    )
    assert report["status"] == "valid"
