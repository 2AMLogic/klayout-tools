"""Grade the requested two-stimulus PVT grid, including incomplete populations."""

import json
import shutil

import pytest

from klayout_tools import sim, sim_probit_sigma, yield_analysis
from klayout_tools.cli import main
from klayout_tools.cli.sim_measurement_text import print_measurement_head
from klayout_tools.coverage import work_id
from klayout_tools.sim_corner_point import CornerPoint
from test_signoff_derived_rows import _item5

SPEC = {
    "name": "noise_sigma",
    "sources": [
        {"measurement": "lo", "stimulus": -1},
        {"measurement": "hi", "stimulus": 1},
    ],
    "unit": "mV",
    "limits": {"max": 2.5},
    "grid": "mean",
    "spec_row": "noise",
}


def _samples():
    points = []
    corners = []
    for base, n, low, high in [("A", 10, 1, 9), ("B", 20, 8, 12)]:
        for index in range(n):
            point = CornerPoint(base, {}, 27, sample_index=index)
            points.append(point)
            corners.append(
                {
                    "corner_id": point.corner_id,
                    "status": "pass",
                    "measurements": [
                        {"name": "lo", "value": int(index < low), "status": "pass"},
                        {"name": "hi", "value": int(index < high), "status": "pass"},
                    ],
                }
            )
    return points, corners


def _grade(points, corners, **overrides):
    [entry] = sim_probit_sigma.rollup(
        {"probit_sigma": {**SPEC, **overrides}}, points, corners
    )
    return entry


def test_text_reports_each_probit_fit_or_its_failure_reason(capsys):
    points, corners = _samples()
    for corner in corners[:10]:
        corner["measurements"][0]["value"] = 0
    entry = _grade(points, corners)

    print_measurement_head(entry, corners)

    assert capsys.readouterr().out.splitlines() == [
        "  noise_sigma [inconclusive]  mean=None mV (max=2.5, source=grid)",
        "    A/novdd/27C: saturated_fraction",
        f"    B/novdd/27C: {entry['by_corner'][1]['sigma']}",
    ]


def test_equal_corner_weight_and_inclusive_limit():
    points, corners = _samples()
    mean = _grade(points[::-1], corners[::-1])
    assert mean["value"] == pytest.approx(2.3637290108)
    assert mean["status"] == "pass" and mean["source"] == "grid"
    assert [c["sigma"] for c in mean["by_corner"]] == pytest.approx(
        [0.7803041461, 3.9471538755]
    )
    assert [c["expected"] for c in mean["by_corner"]] == [10, 20]
    assert _grade(points, corners) == mean
    worst = _grade(points, corners, grid="worst")
    assert worst["status"] == "fail"
    assert worst["source"] == "B/novdd/27C"
    assert worst["value"] == pytest.approx(3.9471538755)
    exact = _grade(
        points[:10], corners[:10], limits={"max": mean["by_corner"][0]["sigma"]}
    )
    assert exact["status"] == "pass"


def test_duplicate_declared_draw_and_empty_grid_cannot_pass():
    points, corners = _samples()
    duplicate = _grade([*points, points[0]], corners)
    assert duplicate["status"] == "inconclusive"
    assert duplicate["by_corner"][0]["reason"] == "duplicate_requested_sample"
    empty = _grade([], [])
    assert empty["value"] is None and empty["reason"] == "empty_corner_grid"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing_corner", "missing_or_duplicate_sample"),
        ("duplicate", "missing_or_duplicate_sample"),
        ("missing_measurement", "missing_or_duplicate_measurement"),
        ("nonbinary", "nonbinary_measurement"),
        ("diagnostic", "untrusted_sample"),
        ("error", "untrusted_sample"),
        ("implausible", "untrusted_measurement"),
        ("saturated", "saturated_fraction"),
        ("flat", "non_increasing_slope"),
        ("reversed", "non_increasing_slope"),
    ],
)
def test_an_unevaluable_corner_prevents_grid_credit(change, reason):
    points, corners = _samples()
    if change == "missing_corner":
        corners = corners[10:]
    elif change == "duplicate":
        corners.append(corners[0])
    elif change == "missing_measurement":
        corners[0]["measurements"].pop()
    elif change == "nonbinary":
        corners[0]["measurements"][0]["value"] = 0.1
    elif change in ("diagnostic", "error"):
        corners[0]["status"] = "error" if change == "error" else "inconclusive"
    elif change == "implausible":
        corners[0]["measurements"][1]["status"] = "inconclusive"
    else:
        for c in corners[:10]:
            lo, hi = c["measurements"]
            if change == "saturated":
                lo["value"] = 0
            elif change == "flat":
                lo["value"] = hi["value"]
            else:
                lo["value"], hi["value"] = hi["value"], lo["value"]
    entry = _grade(points, corners)
    assert entry["value"] is None and entry["status"] == "inconclusive"
    assert entry["by_corner"][0]["corner_id"] == "A/novdd/27C"
    assert entry["by_corner"][0]["reason"] == reason
    coverage = sim._build_coverage(
        [{"name": "lo"}, {"name": "hi"}], corners, grid_stats=[entry]
    )
    graded = sim.grade_corners(corners, [entry], coverage)
    assert graded["status"] == ("error" if change == "error" else "inconclusive")
    assert graded["inconclusive"] == int(change == "diagnostic")
    assert {
        "id": work_id("grid_limit", "noise_sigma", "max"),
        "reason": "undefined_grid_statistic",
    } in coverage["skipped"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "lo"),
        ("grid", "median"),
        ("unit", ""),
        ("spec_row", " "),
        ("limits", {"max": float("nan")}),
        ("limits", {"min": 0}),
        ("sources", []),
        (
            "sources",
            [
                {"measurement": "lo", "stimulus": 0},
                {"measurement": "lo", "stimulus": 1},
            ],
        ),
        (
            "sources",
            [
                {"measurement": "lo", "stimulus": 0},
                {"measurement": "unknown", "stimulus": 1},
            ],
        ),
        (
            "sources",
            [
                {"measurement": "lo", "stimulus": 0},
                {"measurement": "hi", "stimulus": 0},
            ],
        ),
        (
            "sources",
            [
                {"measurement": "lo", "stimulus": True},
                {"measurement": "hi", "stimulus": 1},
            ],
        ),
    ],
)
def test_request_validation(field, value):
    with pytest.raises(sim.SimError, match="monte_carlo.probit_sigma"):
        sim_probit_sigma.validate(
            {"probit_sigma": {**SPEC, field: value}}, [{"name": "lo"}, {"name": "hi"}]
        )


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice is required")
@pytest.mark.parametrize(
    ("grid", "high_threshold", "code", "item_status"),
    [
        ("mean", 9, 0, "met"),
        ("worst", 9, 3, "unmet"),
        ("mean", 1, 4, "unmet"),
    ],
)
def test_ngspice_grid_only_bound_and_strict_signoff(
    tmp_path, capsys, grid, high_threshold, code, item_status
):
    (tmp_path / "binary.spice").write_text(
        "* Binary comparator populations\n"
        "Blo lo 0 V={mc_sample_index < (temper < 50 ? 1 : 4)}\n"
        f"Bhi hi 0 V={{mc_sample_index < (temper < 50 ? {high_threshold} : 6)}}\n"
        "Rlo lo 0 1k\nRhi hi 0 1k\n"
    )
    req = {
        "netlist": "binary.spice",
        "backend": "local",
        "monte_carlo": {
            "n": 10,
            "seed": 37,
            "vary": "mismatch",
            "probit_sigma": {**SPEC, "grid": grid},
        },
        "corners": {"temperature_c": [27, 85]},
        "analysis": {"kind": "tran", "args": "0.1n 2n"},
        "measurements": [
            {"name": n, "spice": f".meas tran {n} FIND v({n}) AT=1n"}
            for n in ("lo", "hi")
        ],
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(req))
    assert main(["sim", str(request), "--format", "json"]) == code
    text = capsys.readouterr().out
    envelope = tmp_path / "report.json"
    envelope.write_text(text)
    report = json.loads(text)
    draws = yield_analysis._measurements_from_sim_report(report)
    assert [(row["name"], len(row["samples"])) for row in draws] == [
        ("lo", 20),
        ("hi", 20),
    ]
    assert report["coverage"]["measurements_with_limits"] == 1
    assert report["coverage"]["measurements_declared"] == 3
    assert report["inconclusive"] == 0 and report["passed"] == 20
    assert report["coverage"]["checked"].count(
        work_id("grid_limit", "noise_sigma", "max")
    ) == int(code != 4)
    item = _item5(
        {"5": [{"file": str(envelope), "rows": ["noise"]}]},
        [{"id": "noise"}],
        require_derived_rows=True,
    )
    assert item["status"] == item_status
    if code == 0:
        assert item["row_coverage"]["row_sources"] == {"noise": "derived"}
