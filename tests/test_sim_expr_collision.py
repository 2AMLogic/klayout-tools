"""A rejected ngspice let assignment cannot supply a measured-limit result (#7794)."""

import json

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE
from klayout_tools.cli import main
from klayout_tools.sim_expr_log import discard_stale_expr_prints
from test_signoff_derived_rows import _item5


@SKIP_NO_NGSPICE
@pytest.mark.parametrize(
    ("name", "expr", "valid"),
    [
        ("out", "v(nonexistent)", False),
        ("OUT", "V(Nonexistent) +    3.25", False),
        ("out", "v(in) / 0", False),
        ("out", "v($probe)", False),
        ("sample", "v(nonexistent)", False),
        ("out", "v(out)", True),
        ("sample", "v(out)", True),
    ],
)
def test_expr_reports_only_its_evaluated_value(tmp_path, capsys, name, expr, valid):
    (tmp_path / "body.sp").write_text("Vin in 0 2\nR1 in out 1k\nR2 out 0 1k\n")
    request = {
        "netlist": "body.sp",
        "analysis": {"kind": "op", "args": ""},
        "measurements": [
            {"name": name, "expr": expr, "spec_row": "bias", "limits": {"min": 0.9}},
            {"name": "sibling", "expr": "v(in)", "limits": {"min": 1.9}},
        ],
        "options": {"ngspice_init": ["set numdgt=9", "set probe=nonexistent"]},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    code = main(["sim", str(path), "--format", "json"])
    output = capsys.readouterr().out
    report = json.loads(output)
    measured, sibling = report["corners"][0]["measurements"]
    assert (sibling["status"], sibling["value"]) == ("pass", 2.0)
    assert (code, measured["status"], measured["value"]) == (
        (0, "pass", 1.0) if valid else (4, "error", None)
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(output)
    item = _item5(
        {"5": [{"file": str(evidence), "rows": ["bias"]}]},
        [{"id": "bias"}],
        require_derived_rows=True,
    )
    assert (item["status"], item["reason"]) == (
        ("met", None) if valid else ("unmet", "check_failed")
    )


def test_unmarked_log_keeps_sibling_and_matches_only_the_rejected_assignment():
    specs = [{"name": name, "expr": "v(missing)"} for name in ("out", "sample")]
    specs.append({"name": "aliased", "expr": "v($probe)"})
    values = {"out": 1.0, "sibling": 2.0, "sample": 3.0, "aliased": 4.0}
    log = (
        'Error: RHS "v(missing)" invalid\nout = 1.0\n'
        'Error: RHS "v(gone)" invalid\naliased = 4.0\n'
        'Error: RHS "v(missing)" invalid\nWarning from checkvalid: no vector.\n'
        'sibling = 2.0\nError: RHS "different_rhs" invalid\nsample = 3.0\n'
    )
    discard_stale_expr_prints(values, log, specs)
    assert values == {"sibling": 2.0, "sample": 3.0}
