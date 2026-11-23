"""Cell-formatting regressions for the `measurements` kind of `klt report`
(issue #5839): the Limits column's bound labelling and the no-traceback
guarantee for malformed numeric cells.

Split from tests/test_report_measurements.py, which sits close to the
500-line limit for unbaselined files (scripts/check_file_size_baseline.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.report import build_report
from klayout_tools.report_measurements import _limits

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        pytest.param(
            # A ceiling. `examples/yield/sim-report.json`'s `iq_ua` shape --
            # one-sided limits are the majority shape in the committed
            # corpus, so the cell must say which side it bounds.
            {"max": 10.0},
            "<=10",
            id="ceiling",
        ),
        pytest.param(
            # The opposite specification of the row above. Both used to
            # render the identical bare `10`; they must now differ.
            {"min": 10.0},
            ">=10",
            id="floor",
        ),
        pytest.param({"min": 30}, ">=30", id="integer-floor"),
        pytest.param(
            # A zero bound is a declared bound, not an absent one.
            {"min": 0.0},
            ">=0",
            id="zero-floor-is-declared-not-absent",
        ),
        pytest.param(
            # `baseline-yield-report.json`'s `i_mismatch_pct`: it used to
            # render `-3–3`, which reads as a typo rather than a range.
            {"min": -3.0, "max": 3.0},
            ">=-3, <=3",
            id="two-sided-negative-floor",
        ),
        pytest.param(
            {"min": 1000000.0, "max": 2000000000.0},
            ">=1e+06, <=2e+09",
            id="two-sided-g-format",
        ),
        pytest.param(
            # `exclusive_*` make the bound strict (issue #1083); `klt
            # yield`'s own limits line prints `>`/`<` for them.
            {"min": -3.0, "max": 3.0, "exclusive_min": True, "exclusive_max": True},
            ">-3, <3",
            id="both-strict",
        ),
        pytest.param(
            {"min": 0, "max": 5, "exclusive_max": True},
            ">=0, <5",
            id="strict-ceiling-only",
        ),
        pytest.param(
            # A strictness flag on a bound that is not declared names no
            # bound at all.
            {"exclusive_min": True},
            "-",
            id="exclusive-flag-without-bound",
        ),
        pytest.param(
            # `target_yield` is a yield target, not a measurement bound.
            {"target_yield": 0.9},
            "-",
            id="target-yield-only",
        ),
        pytest.param({}, "-", id="empty-block"),
        pytest.param(None, "-", id="null-limits-is-absent-not-zero"),
        pytest.param("oops", "-", id="non-dict-limits"),
    ],
)
def test_limits_cell_labels_each_bound(limits, expected):
    assert _limits(limits) == expected


def test_committed_yield_example_renders_floor_and_ceiling_distinctly():
    # The measured repro from the review: `iq_ua` is a 10 uA ceiling and
    # `vref` a two-sided window, and the table must keep them apart.
    report = build_report([str(_EXAMPLES / "yield" / "sim-report.json")])

    assert "| vref | V | >=1.15, <=1.25 |" in report["markdown"]
    assert "| iq_ua | uA | <=10 |" in report["markdown"]
    assert "<=10" in report["text"]


def _yield_envelope(**overrides):
    entry = {
        "name": "i_mismatch_pct",
        "unit": "%",
        "limits": {"min": -3.0, "max": 3.0},
        "yield": {
            "empirical": {
                "estimate": 0.40625,
                "confidence": 0.9,
                "confidence_interval": {"low": 0.25, "high": 0.5},
            },
        },
        "status": "fail",
    }
    for path, value in overrides.items():
        node = entry
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return {"schema_version": 1, "status": "fail", "measurements": [entry]}


@pytest.mark.parametrize(
    ("overrides", "quoted"),
    [
        pytest.param({"limits__min": "abc"}, ">='abc'", id="string-limits-min"),
        pytest.param({"limits__max": [1]}, "<=[1]", id="list-limits-max"),
        pytest.param(
            {"yield__empirical__estimate": "high"}, "'high'", id="string-estimate"
        ),
        pytest.param(
            {"yield__empirical__confidence_interval__low": "lo"},
            "['lo', 0.500000]",
            id="string-ci-low",
        ),
        pytest.param(
            {"yield__empirical__confidence_interval__high": "hi"},
            "[0.250000, 'hi']",
            id="string-ci-high",
        ),
        pytest.param(
            {"yield__empirical__confidence": "ninety"},
            "@ 'ninety'",
            id="string-confidence",
        ),
    ],
)
def test_malformed_numeric_cell_is_quoted_not_a_traceback(
    tmp_path, capsys, overrides, quoted
):
    # docs/cli/report.md promises "no Python traceback is printed". A
    # non-numeric value where a number belongs is a producer-contract
    # violation: it must render visibly (not as `-`, which reads as "no
    # bound declared") and the verb must still exit 0.
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(_yield_envelope(**overrides)))

    exit_code = main(["report", str(path), "--format", "github-summary"])

    out = capsys.readouterr()
    assert exit_code == 0
    assert quoted in out.out
    assert "Traceback" not in out.err
