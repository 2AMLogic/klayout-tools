"""``klt extract --resistance-graph-max-*`` end to end (issue #6964).

Both CLI forms (argv flags and a request document) on synthetic sky130
layouts: each cap lowered on its own forces its ``graph_too_large``
diagnostic naming the value applied, restoring capacity solves the unchanged
graph, and the issue's two over-cap geometries solve once their cap is
raised. The builder, adapter and argument-rule cases live in
``tests/test_extract_resistance_graph_caps.py``; the ``klt pex`` half in
``tests/test_pex_resistance_graph.py``.
"""

from __future__ import annotations

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.extract import run_extract
from klayout_tools.extract_resistance_graph_caps import (
    RESISTANCE_GRAPH_CAP_DEFAULTS,
    RESISTANCE_GRAPH_CAP_FLAGS,
)
from test_extract_resistance_graph_caps import (
    LI1,
    LI1_LABEL,
    LICON1,
    UM,
    _perforated_plate,
)

#: A T-shaped 5 x 0.17 um li1 strip with a 0.5 um stub and a licon1 square on
#: each end edge: two terminals, four pieces in a 3 x 2 grid (six cells), six
#: nodes and five edges.
_T_STRIP = ((0, 0, 5 * UM, 170), (2400, 0, 2570, 500))
_T_STRIP_CUTS = ((0, 0, 170, 170), (5 * UM - 170, 0, 5 * UM, 170))


def _layout(conductor, cuts, label_at) -> kdb.Layout:
    """A labelled li1 conductor with one licon1 square per ``cuts`` entry --
    each a terminal, since no modelled layer sits under it."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    li1 = layout.layer(*LI1)
    for shape in conductor:
        top.shapes(li1).insert(kdb.Box(*shape) if isinstance(shape, tuple) else shape)
    top.shapes(layout.layer(*LI1_LABEL)).insert(kdb.Text("S", kdb.Trans(*label_at)))
    for box in cuts:
        top.shapes(layout.layer(*LICON1)).insert(kdb.Box(*box))
    return layout


def _t_strip_layout() -> kdb.Layout:
    return _layout(_T_STRIP, _T_STRIP_CUTS, (2500, 85))


def _plate_layout() -> kdb.Layout:
    """A 20 x 4 um li1 plate with 65 licon1 squares along its lower edge."""
    cuts = tuple((i * 300, 0, i * 300 + 200, 170) for i in range(65))
    return _layout(((0, 0, 20 * UM, 4 * UM),), cuts, (10 * UM, 2 * UM))


def _perforated_layout() -> kdb.Layout:
    """A plate with a 100 x 10 lattice of 1 x 1 um holes (a strapped/poured
    rail) and a licon1 square in two corners."""
    plate, near, far = _perforated_plate()
    return _layout((plate,), (near, far), (UM, far[3] - UM))


def _write(layout: kdb.Layout, path) -> str:
    layout.write(str(path))
    return str(path)


def _cli_extract(capsys, path: str, cap_flags: list[str]) -> dict:
    argv = ["extract", path, "--deck", "sky130", "--parasitics", "--resistance-graph"]
    assert main([*argv, *cap_flags, "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def _graphs(report: dict) -> list[dict]:
    return [entry["resistance_graph"] for entry in report["parasitics"]["nets"]]


def _strip_path(tmp_path) -> str:
    return _write(_t_strip_layout(), tmp_path / "strip.gds")


@pytest.mark.parametrize(
    ("flag", "value", "named"),
    [
        ("--resistance-graph-max-terminals", "1", "2 terminals exceed the 1 cap"),
        ("--resistance-graph-max-cells", "5", "over this build's 5-mesh-cell cap"),
        ("--resistance-graph-max-nodes", "3", "4 piece nodes exceed the 3 cap"),
        (
            "--resistance-graph-max-edges",
            "4",
            "6 nodes / 5 edges exceed the 5000 / 4 caps",
        ),
    ],
)
def test_cli_lowers_each_cap_alone_and_names_the_applied_value(
    tmp_path, capsys, flag, value, named
):
    """One cap per run, one below what the strip needs: the flag reaches the
    builder, which refuses naming the value that was applied."""
    (graph,) = _graphs(_cli_extract(capsys, _strip_path(tmp_path), [flag, value]))
    assert graph["status"] == "unsupported"
    (diagnostic,) = graph["diagnostics"]
    assert diagnostic["code"] == "graph_too_large"
    assert named in diagnostic["message"]
    assert graph["resistance_source"] == "legacy"


@pytest.mark.parametrize(
    "cap_flags",
    [
        ["--resistance-graph-max-edges", "5"],
        ["--resistance-graph-max-cells", "6", "--resistance-graph-max-nodes", "6"],
        [
            item
            for key, default in RESISTANCE_GRAPH_CAP_DEFAULTS.items()
            for item in (RESISTANCE_GRAPH_CAP_FLAGS[key], str(default))
        ],
        [
            item
            for flag in RESISTANCE_GRAPH_CAP_FLAGS.values()
            for item in (flag, "100000")
        ],
    ],
)
def test_cli_restoring_capacity_solves_the_unchanged_graph(tmp_path, capsys, cap_flags):
    """Exactly enough capacity, the defaults spelled out and a large surplus
    all leave the default graph and netlist untouched."""
    path = _strip_path(tmp_path)
    default = _cli_extract(capsys, path, [])
    (graph,) = _graphs(default)
    assert (graph["status"], graph["nodes"], graph["edges"]) == ("ok", 6, 5)
    restored = _cli_extract(capsys, path, cap_flags)
    assert restored["parasitics"] == default["parasitics"]
    assert restored["netlist_sha256"] == default["netlist_sha256"]


def test_cli_omitted_caps_are_byte_identical_to_a_call_without_them(tmp_path, capsys):
    """No cap flag: the report is ``run_extract``'s with no ``caps``
    argument -- the call every site that predates the flags makes -- and
    names no cap anywhere."""
    path = _strip_path(tmp_path)
    cli = _cli_extract(capsys, path, [])
    library = run_extract(
        path,
        "sky130",
        output=str(tmp_path / "lib.spice"),
        parasitics=True,
        resistance_graph=True,
    )
    # `-o` vs the default path, and wall-clock stage timings, are the only
    # run-to-run differences.
    for report in (cli, library):
        del report["netlist_path"], report["timing"]
    assert cli == library
    assert "resistance_graph_caps" not in json.dumps(cli)
    assert '"caps"' not in json.dumps(cli)


def test_cli_terminal_cap_lets_a_65_terminal_plate_solve(tmp_path, capsys):
    """The issue's case A as a layout: the 65th terminal is refused at the
    default 64 and solves, as a full 65 x 65 matrix, with the cap at 65."""
    path = _write(_plate_layout(), tmp_path / "plate.gds")
    (over,) = _graphs(_cli_extract(capsys, path, []))
    assert over["status"] == "unsupported"
    assert over["diagnostics"] == [
        {
            "code": "graph_too_large",
            "message": "65 terminals exceed the 64 cap",
            "detail": {"terminals": 65, "max_terminals": 64},
        }
    ]
    (ok,) = _graphs(
        _cli_extract(capsys, path, ["--resistance-graph-max-terminals", "65"])
    )
    assert ok["status"] == "ok"
    assert [len(row) for row in ok["pair_resistance_ohm"]] == [65] * 65
    assert ok["resistance_source"] == "legacy"
    assert ok["resistance_source_reason"] == "terminal_count_not_two"


def test_cli_cell_cap_lets_the_perforated_plate_solve(tmp_path, capsys):
    """The issue's case B as a layout: over the default 4096 mesh cells, and
    solved with only the cell cap raised."""
    path = _write(_perforated_layout(), tmp_path / "perforated.gds")
    (over,) = _graphs(_cli_extract(capsys, path, []))
    assert over["status"] == "unsupported"
    (diagnostic,) = over["diagnostics"]
    assert diagnostic["code"] == "graph_too_large"
    assert "mesh cells, over this build's 4096-mesh-cell cap" in diagnostic["message"]
    (ok,) = _graphs(
        _cli_extract(capsys, path, ["--resistance-graph-max-cells", "65536"])
    )
    assert ok["status"] == "ok"
    assert ok["nodes"] > 3000
    assert ok["resistance_source"] == "graph"


@pytest.mark.parametrize(
    ("value", "message"),
    [("0", "(got 0)"), ("-8", "(got -8)")],
)
def test_cli_rejects_a_non_positive_cap(tmp_path, capsys, value, message):
    path = _strip_path(tmp_path)
    code = main(
        [
            "extract",
            path,
            "--deck",
            "sky130",
            "--parasitics",
            "--resistance-graph",
            "--resistance-graph-max-cells",
            value,
            "--format",
            "json",
        ]
    )
    assert code == 1
    error = json.loads(capsys.readouterr().err)["error"]["message"]
    assert error == f"--resistance-graph-max-cells must be a positive integer {message}"


def test_cli_rejects_a_non_integer_cap_as_a_usage_error(tmp_path, capsys):
    path = _strip_path(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "extract",
                path,
                "--deck",
                "sky130",
                "--parasitics",
                "--resistance-graph",
                "--resistance-graph-max-cells",
                "2.5",
            ]
        )
    assert excinfo.value.code == 2
    assert "--resistance-graph-max-cells: invalid" in capsys.readouterr().err


def test_cli_rejects_a_cap_without_the_graph_flag(tmp_path, capsys):
    path = _strip_path(tmp_path)
    code = main(
        [
            "extract",
            path,
            "--deck",
            "sky130",
            "--parasitics",
            "--resistance-graph-max-cells",
            "8",
            "--format",
            "json",
        ]
    )
    assert code == 1
    error = json.loads(capsys.readouterr().err)["error"]["message"]
    assert error == "--resistance-graph-max-cells requires --resistance-graph"


def test_cli_request_document_carries_the_caps(tmp_path, capsys):
    _strip_path(tmp_path)
    base = {
        "schema": "klt.extract.request/1",
        "file": "strip.gds",
        "deck": "sky130",
        "parasitics": True,
        "resistance_graph": True,
    }

    def run(document: dict) -> dict:
        request = tmp_path / "extract.json"
        request.write_text(json.dumps(document))
        assert main(["extract", str(request), "--format", "json"]) == 0
        return json.loads(capsys.readouterr().out)

    def error(document: dict) -> str:
        request = tmp_path / "extract.json"
        request.write_text(json.dumps(document))
        assert main(["extract", str(request), "--format", "json"]) == 1
        return json.loads(capsys.readouterr().err)["error"]["message"]

    (lowered,) = _graphs(run({**base, "resistance_graph_max_edges": 4}))
    assert (
        lowered["diagnostics"][0]["message"]
        == "6 nodes / 5 edges exceed the 5000 / 4 caps"
    )
    at_defaults = {
        f"resistance_graph_{key}": value
        for key, value in RESISTANCE_GRAPH_CAP_DEFAULTS.items()
    }
    assert run({**base, **at_defaults})["parasitics"] == run(base)["parasitics"]
    assert error({**base, "resistance_graph_max_cells": 0}) == (
        "--resistance-graph-max-cells must be a positive integer (got 0)"
    )
    without_graph = {
        key: value for key, value in base.items() if key != "resistance_graph"
    }
    assert error({**without_graph, "resistance_graph_max_cells": 8}) == (
        "--resistance-graph-max-cells requires --resistance-graph"
    )
    assert "resistance_graph_max_cells" in error(
        {**base, "resistance_graph_max_cells": 2.5}
    )
    assert "must be an integer" in error({**base, "resistance_graph_max_cells": 2.5})
