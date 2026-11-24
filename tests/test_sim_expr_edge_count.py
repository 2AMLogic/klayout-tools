"""The documented ``expr`` edge-count recipe, run through ``klt sim`` (issue #4290).

ngspice's control language has no ``sgn`` or ``sum``, so edge counts are
spelled with a comparison, index slices and ``mean(x)*length(x)``.
docs/cli/sim.md ("Counting edges with expr") documents the recipe; this keeps
it honest against a synthetic square wave with a known edge count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE as real_ngspice
from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


# 10 us period, 50% duty, over 100 us: 10 rising and 10 falling edges.
_STATE = "v(in) gt 0.5"
_STEP = "(v(in) gt 0.5)[1,length(time)-1] - (v(in) gt 0.5)[0,length(time)-2]"

#: The recipe as documented: a signed per-sample step, then count by direction.
EDGE_EXPRS = {
    "rises": f"mean(({_STEP}) gt 0.5)*(length(time)-1)",
    "falls": f"mean(({_STEP}) lt -0.5)*(length(time)-1)",
    "edges": f"mean(abs({_STEP}))*(length(time)-1)",
}


def _request(tmp_path: Path) -> Path:
    (tmp_path / "body.spice").write_text(
        "V1 in 0 PULSE(0 1 0 1n 1n 5u 10u)\nR1 in 0 1k\n"
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "100n 100u"},
                "measurements": [
                    {"name": name, "expr": expr} for name, expr in EDGE_EXPRS.items()
                ],
            }
        )
    )
    return path


@real_ngspice
def test_documented_edge_count_recipe_counts_a_square_wave(tmp_path):
    report = sim.run_sim(str(_request(tmp_path)), backend="local")

    (corner,) = report["corners"]
    assert corner["status"] == "pass", corner
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["rises"] == pytest.approx(10)
    assert values["falls"] == pytest.approx(10)
    assert values["edges"] == pytest.approx(20)


@pytest.mark.parametrize(
    ("expr", "named"),
    [
        ("sum(v(out))", "sum() -> mean(x)*length(x)"),
        ("sgn(v(out))", "sgn() -> "),
        ("max(v(out))", "max() -> vecmax(x)"),
    ],
)
def test_no_value_error_names_the_replacement(expr, named):
    message = sim._no_value_message({"name": "m", "expr": expr})
    assert named in message


def test_no_value_error_has_no_hint_for_supported_functions():
    message = sim._no_value_message({"name": "m", "expr": "vecmax(v(out))"})
    assert "no such function" not in message


@pytest.mark.parametrize(
    ("expr", "hints"),
    [
        ("max(max(a,b),c)-min(min(a,b),c)", set()),
        ("max(min(x),y)", {"min"}),
        ("min(a,max(b,c))", set()),
        ("vmax(x) + ns.max(y)", set()),
        ("max(s[1,2])", {"max"}),
        ("min(v(in,out))", {"min"}),
        ("max()", set()),
    ],
)
def test_extrema_diagnostic_follows_call_arity(expr, hints):
    message = sim._no_value_message({"name": "m", "expr": expr})
    actual = {name for name in ("max", "min") if f"{name}() ->" in message}
    assert actual == hints


@real_ngspice
@pytest.mark.parametrize(
    ("source", "derived", "expected"),
    [("expr", "expr", 4.0), ("spice", "spice", 4.0), ("spice", "expr", None)],
)
def test_three_scalar_spread(tmp_path, source, derived, expected):
    path = _request(tmp_path)
    (tmp_path / "body.spice").write_text("Va va 0 2\nVb vb 0 5\nVc vc 0 1\n")
    request = json.loads(path.read_text())
    formula = "max(max(a,b),c)-min(min(a,b),c)"
    sources = [
        {
            "name": name,
            source: f"v(v{name})[0]"
            if source == "expr"
            else f".meas tran {name} FIND v(v{name}) AT=1u",
        }
        for name in ("a", "b", "c")
    ]
    spread = formula if derived == "expr" else f".meas tran spread param='{formula}'"
    request["measurements"] = [*sources, {"name": "spread", derived: spread}]
    request["options"] = {"ngspice_init": ["set numdgt=10"]}
    path.write_text(json.dumps(request))
    report = sim.run_sim(str(path), backend="local")
    rows = report["corners"][0]["measurements"]
    assert [row["value"] for row in rows] == [2.0, 5.0, 1.0, expected]
    if expected is None:
        assert report["status"] == "error"
        diagnostics = report["corners"][0]["diagnostics"]
        assert any("produced no value" in item["message"] for item in diagnostics)
        assert all("max() ->" not in item["message"] for item in diagnostics)
