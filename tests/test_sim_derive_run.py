"""``measurements[].derive`` through the real ``klt sim`` pipeline (issue #7184).

The corner-grid validation, then decks, harvest, rollup, coverage, report
``status``, exit codes and ``analysis_steps[]`` integration. The
stubbed-``ngspice`` tests print each corner's value from its own deck, the
way a real engine would; the real ``ngspice`` test runs a resistive divider
whose values are known analytically.
Known-answer arithmetic lives in ``tests/test_sim_derive.py``.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from helpers.subprocess_fakes import fake_completed
from helpers.tool_probes import SKIP_NO_NGSPICE as _SKIP_NO_NGSPICE
from klayout_tools import _paths as paths_module
from klayout_tools import evidence_lint, sim, sim_derive, sim_derive_rollup, sim_remote
from klayout_tools.cli import main
from klayout_tools.coverage import work_id
from klayout_tools.sim_errors import SimError
from test_sim_derive import _CODE, CORNERS, GROUPS, derive, with_source

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

SOURCE = {"name": "vout", "expr": "v(out)"}
#: The issue's row, reduced per supply rail over the code axis.
DERIVE = {
    "name": "code_range_rel",
    "derive": {
        "from": "vout",
        "axis": "params.code",
        "reduce": "range_rel",
        "reference": {"params.code": 128},
        "group_by": ["supply_v"],
    },
    "unit": "ratio",
    "limits": {"min": 0.30},
    "spec_row": "trim-range",
}


def _stub_ngspice(monkeypatch) -> None:
    """``ngspice`` printing ``vout`` from the corner's own deck, exactly as a
    real engine would report the issue's values for that ``(vdd, code)``."""
    real_which = shutil.which
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        lambda cmd, *a, **k: cmd if cmd == "ngspice" else real_which(cmd, *a, **k),
    )
    table = {
        (supply, code): value
        for (_, supply), row in GROUPS.items()
        for code, value in zip((0, 128, 255), row, strict=True)
    }

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        deck = Path(cmd[cmd.index("-b") + 1]).read_text()
        vdd = float(re.search(r"^alter vdd=([\d.]+)", deck, re.M).group(1))
        code = int(re.search(r"^alterparam code=(\d+)", deck, re.M).group(1))
        value = table[(vdd, code)]
        # The step markers let the `analysis_steps[]` grader pair the plot.
        log = (
            f"Circuit: * klt\nklt_step_begin 0 unknown1\nklt_step_end 0 op1\n"
            f"vout = {value:e}\nop1__vout = {value:e}\n"
        )
        Path(cmd[cmd.index("-o") + 1]).write_text(log)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _request(tmp_path: Path, measurements: list[dict], **extra) -> Path:
    (tmp_path / "body.spice").write_text(".param code=0\nVDD vdd 0 1\nR1 out 0 1k\n")
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "op", "args": ""},
                "corners": {
                    "supply_v": {"vdd": [1.0, 2.0]},
                    "params": {"code": [0, 128, 255]},
                },
                "measurements": measurements,
                **extra,
            }
        )
    )
    return path


def _run(tmp_path, monkeypatch, capsys, measurements, **extra) -> tuple[int, dict]:
    _stub_ngspice(monkeypatch)
    request = _request(tmp_path, measurements, **extra)
    code = main(["sim", str(request), "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def _derived(report: dict) -> dict:
    return next(m for m in report["measurements"] if m["name"] == "code_range_rel")


# --------------------------------------------------------------------------- #
# Request validation: the corner grid
# --------------------------------------------------------------------------- #


def _refuses(spec: dict, corners: dict, message: str, **request) -> None:
    with pytest.raises(SimError, match=message):
        sim_derive.validate_grid(
            with_source(spec),
            sim.expand_corners(corners, []),
            {"corners": corners, **request},
        )


def test_grid_refuses_an_axis_with_fewer_than_two_declared_values():
    _refuses(derive(), {"params": {"code": [0]}}, "fewer than two declared values")


def test_grid_refuses_two_corners_at_one_group_axis_coordinate():
    # A swept axis is missing from group_by, so two corners share (all, code=0).
    _refuses(
        derive(),
        {"process": ["a", "b"], "params": {"code": [0, 1]}},
        r"share one \(group, params.code\) coordinate",
    )


def test_grid_refuses_an_undeclared_group_axis_or_reference_point():
    corners = {"params": {"code": [0, 128, 255]}}
    _refuses(
        derive(group_by=["params.w"]), corners, "'params.w' is not on the corner grid"
    )
    _refuses(
        derive(reduce="range_rel", reference={"params.code": 64}),
        corners,
        "which the request does not declare",
    )
    _refuses(derive(), corners, "not supported with monte_carlo", monte_carlo={"n": 2})


def test_bare_supply_axis_needs_a_single_rail():
    corners = {
        "supply_v": {"vdd": [1.0, 2.0], "vss": [0, 0]},
        "params": {"code": [0, 1]},
    }
    _refuses(derive(axis="supply_v"), corners, "several supply rails")
    rail = derive(axis="supply_v.vdd", group_by=["params.code"])
    sim_derive.validate_grid(
        with_source(rail), sim.expand_corners(corners, []), {"corners": corners}
    )


@pytest.mark.parametrize("source", ["nope", "d", "other", "VOUT"])
def test_from_must_name_a_per_corner_measurement(source):
    corners = {"params": {"code": [0, 1]}}
    specs = [
        {"name": "vout", "expr": "v(out)"},
        {"name": "other", "derive": dict(_CODE, **{"from": "vout"})},
        {"name": "d", "derive": {**_CODE, "from": source}},
    ]
    with pytest.raises(SimError, match=f"derive.from names {source!r}"):
        sim_derive.validate_grid(
            specs, sim.expand_corners(corners, []), {"corners": corners}
        )
    specs[2]["derive"]["from"] = "vout"
    sim_derive.validate_grid(
        specs, sim.expand_corners(corners, []), {"corners": corners}
    )


def test_a_request_without_derive_is_never_gated():
    assert sim_derive.validate_grid([], [], {}) is None
    assert sim_derive.validate_grid([{"name": "v", "expr": "v(out)"}], [], {}) is None


def test_expected_axis_values_come_from_the_grid_before_exclude():
    spec = derive()
    corners = {"params": {"code": [0, 128, 255]}}
    points = sim.expand_corners(corners, [{"params": {"code": 128}}])
    assert [p.params["code"] for p in points] == [0, 255]
    sim_derive.validate_grid(with_source(spec), points, {"corners": corners})
    assert spec[sim_derive.EXPECTED_KEY] == [0, 128, 255]


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


def test_a_derived_limit_fails_the_envelope_with_unbounded_sources(
    tmp_path, monkeypatch, capsys
):
    with patch.object(
        sim_derive_rollup, "_reduce_group", wraps=sim_derive_rollup._reduce_group
    ) as reduce_group:
        code, report = _run(tmp_path, monkeypatch, capsys, [SOURCE, DERIVE])
    assert reduce_group.call_count == 2  # One reduction per supply group.
    assert code == 3
    assert report["status"] == "fail"
    assert (report["passed"], report["failed"], report["errored"]) == (6, 0, 0)
    assert all(c["status"] == "pass" for c in report["corners"])
    assert all(
        "vout" in [m["name"] for m in c["measurements"]] for c in report["corners"]
    )
    assert not any(
        "code_range_rel" in [m["name"] for m in c["measurements"]]
        for c in report["corners"]
    )
    entry = _derived(report)
    assert entry["status"] == "fail"
    assert entry["worst_case"]["group_id"] == "supply_v=2.000V"
    assert entry["spec_row"] == "trim-range"
    coverage = report["coverage"]
    assert (
        coverage["measurements_declared"],
        coverage["measurements_with_limits"],
    ) == (2, 1)
    assert coverage["skipped"] == []
    assert (
        work_id("derived_limit", "code_range_rel", "supply_v=2.000V", "min")
        in coverage["checked"]
    )
    assert work_id("derived_measurement", "vout") not in "".join(coverage["checked"])
    assert evidence_lint._rederive_status(report) == "fail"


def test_a_derived_error_is_reported_as_error(tmp_path, monkeypatch, capsys):
    exclude = [{"supply_v": {"vdd": 1.0}, "params": {"code": 128}}]
    code, report = _run(
        tmp_path, monkeypatch, capsys, [SOURCE, DERIVE], exclude=exclude
    )
    assert (
        code == 4
    )  # `klt sim` reports any `status: "error"` at exit 4 (docs: Exit codes)
    assert report["status"] == "error"
    assert (report["errored"], report["failed"]) == (0, 0)
    entry = _derived(report)
    assert [(g["group_id"], g["reason"]) for g in entry["derive"]["groups"]] == [
        ("supply_v=1.000V", "missing_axis_points"),
        ("supply_v=2.000V", None),
    ]
    assert {s["reason"] for s in report["coverage"]["skipped"]} == {
        "unavailable_measurement"
    }
    assert evidence_lint._rederive_status(report) == "error"


def test_a_derived_inconclusive_is_reported_as_inconclusive(
    tmp_path, monkeypatch, capsys
):
    plausible = {**DERIVE, "plausible_range": {"min": 0, "max": 0.2}}
    code, report = _run(tmp_path, monkeypatch, capsys, [SOURCE, plausible])
    assert code == 4
    assert report["status"] == "inconclusive"
    assert all(c["status"] == "pass" for c in report["corners"])
    assert {g["reason"] for g in _derived(report)["derive"]["groups"]} == {
        "implausible_value"
    }
    reasons = {s["reason"] for s in report["coverage"]["skipped"]}
    assert reasons == {"implausible_solution"}
    assert evidence_lint._rederive_status(report) == "inconclusive"


def test_a_passing_derived_limit_passes_and_prints_its_group(
    tmp_path, monkeypatch, capsys
):
    code, report = _run(
        tmp_path, monkeypatch, capsys, [SOURCE, {**DERIVE, "limits": {"min": 0.20}}]
    )
    assert code == 0
    assert report["status"] == "pass"
    assert _derived(report)["worst_case"]["value"] == pytest.approx(0.25)
    _stub_ngspice(monkeypatch)
    request = _request(tmp_path, [SOURCE, {**DERIVE, "limits": {"min": 0.20}}])
    capsys.readouterr()
    assert main(["sim", str(request)]) == 0
    out = capsys.readouterr().out
    # The binding group is named where a per-corner entry names its corner, and
    # no per-corner value rows follow (no corner carries the derived entry).
    worst = _derived(report)["worst_case"]
    head = next(line for line in out.splitlines() if "code_range_rel" in line)
    assert head.strip() == (
        f"code_range_rel [pass]  worst={worst['value']!r} @ supply_v=2.000V"
        f" (margin={worst['margin']!r})"
    )
    assert out.count("code_range_rel") == 1


def test_the_worker_request_carries_no_derive_entry():
    request = {
        "netlist": "x.spice",
        "measurements": [SOURCE, DERIVE],
        "corners": CORNERS,
    }
    remote = sim_remote._build_remote_request(
        request, timeout_s=30, keep_artifacts=False, want_waveforms=False
    )
    assert [m["name"] for m in remote["measurements"]] == ["vout"]
    untouched = {"netlist": "x", "measurements": [SOURCE]}
    assert sim_derive.without_derive(untouched) is untouched


def test_dry_run_validates_the_derive_grid(tmp_path, monkeypatch, capsys):
    _stub_ngspice(monkeypatch)
    request = _request(tmp_path, [SOURCE, DERIVE])
    assert main(["sim", str(request), "--dry-run", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["unit_count"] == 6
    request = _request(
        tmp_path, [SOURCE, {**DERIVE, "derive": {**DERIVE["derive"], "from": "nope"}}]
    )
    assert main(["sim", str(request), "--dry-run", "--format", "json"]) == 1
    assert "derive.from names 'nope'" in capsys.readouterr().err


def test_dry_run_accepts_a_derive_entry_for_xyce(tmp_path, capsys):
    """Xyce has no parameter axis, so the reduced axis there is a PVT one; the
    entry touches no deck (the live Xyce run is not exercised here)."""
    (tmp_path / "body.spice").write_text("VIN in 0 3\nR1 in out 1k\nR2 out 0 1k\n")
    derive_spec = {
        "name": "tc",
        "derive": {
            "from": "vout",
            "axis": "temperature_c",
            "reduce": "box_spread",
            "reference": {"temperature_c": 27},
            "group_by": [],
        },
        "limits": {"max": 50e-6},
    }
    request = tmp_path / "xyce.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "engine": "xyce",
                "analysis": {"kind": "dc", "args": "VIN 3 3 1"},
                "corners": {"temperature_c": [-40, 27, 125]},
                "measurements": [
                    {"name": "vout", "spice": ".meas dc vout FIND v(out) AT=3"},
                    derive_spec,
                ],
            }
        )
    )
    assert main(["sim", str(request), "--dry-run", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["status"], report["engine"], report["unit_count"]) == (
        "valid",
        "xyce",
        3,
    )


def test_a_derive_entry_leaves_the_checkpoint_fingerprint_unchanged():
    base = dict(
        netlist_path="x.spice",
        models_lib=None,
        analysis={"kind": "op", "args": ""},
        corner_points=sim.expand_corners(CORNERS, []),
        timeout_s=30,
        engine="ngspice",
    )
    plain = sim._checkpoint_fingerprint(measurements_spec=[SOURCE], **base)
    with_derive = sim._checkpoint_fingerprint(
        measurements_spec=sim_derive.per_corner_measurements([SOURCE, DERIVE]), **base
    )
    assert with_derive == plain


# --------------------------------------------------------------------------- #
# analysis_steps[]
# --------------------------------------------------------------------------- #


def test_analysis_steps_accept_a_top_level_derive_and_refuse_a_step_scoped_one():
    body = {
        "from": "op1.vout",
        "axis": "params.code",
        "reduce": "range",
        "group_by": [],
    }
    request = {
        "analysis_steps": [
            {
                "name": "op1",
                "analysis": {"kind": "op", "args": ""},
                "measurements": [{"name": "vout", "expr": "v(out)"}],
            }
        ],
        "corners": {"params": {"code": [0, 1]}},
        "measurements": [{"name": "rng", "derive": body}],
    }
    _, specs = sim._resolve_analysis_and_measurements(request)
    assert [s["name"] for s in specs] == ["op1.vout", "rng"]
    assert "log_key" not in specs[1] and "deck_lines" not in specs[1]
    assert sim_derive.per_corner_measurements(specs) == specs[:1]

    request["analysis_steps"][0]["measurements"].append(derive())
    with pytest.raises(SimError, match="step 'op1': measurement 'd': 'derive' reduces"):
        sim._resolve_analysis_and_measurements(request)

    request["analysis_steps"][0]["measurements"].pop()
    request["measurements"] = [
        {"name": "x", "expr": "abs(op1.vout) + rng"},
        {"name": "rng", "derive": body},
    ]
    with pytest.raises(SimError, match="references derive measurement 'rng'"):
        sim._resolve_analysis_and_measurements(request)


def test_analysis_steps_derive_runs_through_the_pipeline(tmp_path, monkeypatch, capsys):
    step = {
        "name": "op1",
        "analysis": {"kind": "op", "args": ""},
        "measurements": [{"name": "vout", "expr": "v(out)"}],
    }
    derive_spec = {
        "name": "rng",
        "derive": {
            "from": "op1.vout",
            "axis": "params.code",
            "reduce": "range",
            "group_by": ["supply_v"],
        },
        "limits": {"min": 0.3},
    }
    request = _request(tmp_path, [derive_spec], analysis_steps=[step])
    request = json.loads(request.read_text())
    del request["analysis"]
    path = tmp_path / "steps.json"
    path.write_text(json.dumps(request))
    _stub_ngspice(monkeypatch)
    code = main(["sim", str(path), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["measurements"][1]["name"] == "rng"
    assert report["measurements"][1]["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_real_ngspice_divider_matches_the_analytic_reductions(tmp_path):
    (tmp_path / "body.spice").write_text(
        ".param rload=1000\nVIN in 0 3\nR1 in out {rload}\nR2 out 0 1k\n"
    )
    reductions = {
        "span": {"reduce": "range"},
        "step": {"reduce": "max_abs_step"},
        "rel": {"reduce": "range_rel", "reference": {"params.rload": 2000}},
    }
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "op", "args": ""},
                "corners": {"params": {"rload": [1000, 2000, 3000]}},
                "measurements": [
                    {"name": "vout", "expr": "v(out)", "unit": "V"},
                    *(
                        {
                            "name": name,
                            "derive": {
                                "from": "vout",
                                "axis": "params.rload",
                                "group_by": [],
                                **body,
                            },
                            "unit": "V",
                        }
                        for name, body in reductions.items()
                    ),
                ],
                "options": {"keep_artifacts": False},
            }
        )
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "a"))
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    by_name = {m["name"]: m for m in report["measurements"]}
    assert by_name["span"]["worst_case"]["value"] == pytest.approx(0.75, abs=1e-6)
    assert by_name["step"]["worst_case"]["value"] == pytest.approx(0.5, abs=1e-6)
    assert by_name["rel"]["worst_case"]["value"] == pytest.approx(0.75, abs=1e-6)
