"""Tests for ``klt sim-merge`` / :mod:`klayout_tools.sim_merge` (issue #3045).

The equivalence tests drive the real :func:`klayout_tools.sim.run_sim` with a
stubbed ``ngspice`` (no binary required, as in ``tests/test_sim.py``): once as
one request over the whole grid and once as one request per process corner,
then assert the merged per-request reports equal the single-request report.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_merge
from klayout_tools.cli import main

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

#: What the stub "ngspice" measures per process section.
_VOUT = {"tt": 1.0, "ss": 0.9, "ff": 1.1}
_LIMITS = {"min": 0.95, "max": 1.2}


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        return cmd if cmd == "ngspice" else real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


@pytest.fixture
def stub_ngspice(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck = Path(cmd[cmd.index("-b") + 1]).read_text(encoding="utf-8")
        log_path = cmd[cmd.index("-o") + 1]
        process = next((p for p in _VOUT if f"{p}\n" in deck or f" {p}" in deck), "tt")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                f"vout                =  {_VOUT[process]:.5e}\n"
            )
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _write_grid_request(tmp_path: Path, name: str, processes: list[str]) -> Path:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    (tmp_path / "corner.lib").write_text(
        "".join(f".lib {p}\n.param corner_scale=1.0\n.endl {p}\n\n" for p in _VOUT)
    )
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "models": {"lib": "corner.lib"},
                "corners": {"process": processes, "temperature_c": [-40, 125]},
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [
                    {
                        "name": "vout",
                        "unit": "V",
                        "spice": ".meas tran vout FIND v(out) AT=1u",
                        "limits": _LIMITS,
                    }
                ],
            }
        )
    )
    return path


def _run(tmp_path: Path, name: str, processes: list[str]) -> dict:
    request = _write_grid_request(tmp_path, name, processes)
    return sim.run_sim(
        str(request), backend="local", artifacts_dir=str(tmp_path / name[:-5])
    )


def _write(tmp_path: Path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _plan(tmp_path: Path, reports: dict[str, dict], *, pin: bool = False) -> str:
    shards = []
    for shard_id, report in reports.items():
        shard = {"id": shard_id, "corners": [c["corner_id"] for c in report["corners"]]}
        if pin:
            request = tmp_path / f"{shard_id}.json"
            request.write_text(json.dumps({"shard": shard_id}))
            shard["request"] = request.name
            shard["request_sha256"] = hashlib.sha256(request.read_bytes()).hexdigest()
        shards.append(shard)
    return _write(tmp_path, "manifest.json", {"schema_version": 1, "shards": shards})


@pytest.fixture
def shard_reports(tmp_path, stub_ngspice):
    return {
        "tt": _run(tmp_path, "tt.json", ["tt"]),
        "ss": _run(tmp_path, "ss.json", ["ss"]),
    }


def _stable_corner(corner: dict) -> dict:
    return {
        k: corner[k]
        for k in ("corner_id", "process", "status", "measurements", "diagnostics")
    }


def test_merge_equals_single_request_over_the_same_grid(
    tmp_path, stub_ngspice, shard_reports
):
    single = _run(tmp_path, "all.json", ["tt", "ss"])
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    merged = sim_merge.merge_reports(_plan(tmp_path, shard_reports), paths)

    # The worst corner (ss, 0.9 < min 0.95) is in the second shard.
    assert single["measurements"][0]["worst_case"]["corner_id"].startswith("ss/")
    assert single["status"] == "fail"
    for key in (
        "schema_version",
        "status",
        "corner_count",
        "passed",
        "failed",
        "errored",
        "inconclusive",
        "diagnostic_counts",
        "metrics",
        "coverage",
        "measurements",
    ):
        assert merged[key] == single[key], key
    assert [_stable_corner(c) for c in merged["corners"]] == [
        _stable_corner(c) for c in single["corners"]
    ]


def test_merged_coverage_indices_are_rebased(tmp_path, shard_reports):
    # Each shard's own coverage numbers its corners from 0; the union must not.
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    merged = sim_merge.merge_reports(_plan(tmp_path, shard_reports), paths)
    ids = merged["coverage"]["checked"]
    assert (
        len(ids) == len(set(ids)) == 2 * len(shard_reports["tt"]["coverage"]["checked"])
    )
    assert merged["coverage"]["corners_simulated"] == merged["corner_count"] == 4


def test_per_report_fields_become_arrays(tmp_path, shard_reports):
    shard_reports["ss"]["environment"]["remote"] = {"job_id": "job-ss"}
    shard_reports["ss"]["environment"]["netlist_sha256"] = "f" * 64
    shard_reports["ss"]["provenance"]["input"]["content_hash"] = "e" * 64
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    merged = sim_merge.merge_reports(_plan(tmp_path, shard_reports), paths)

    env = merged["environment"]
    assert env["netlist_sha256"] == [
        shard_reports["tt"]["environment"]["netlist_sha256"],
        "f" * 64,
    ]
    assert env["remote"] == [None, {"job_id": "job-ss"}]
    assert merged["provenance"]["input"]["content_hash"][1] == "e" * 64
    assert env["engine"] == shard_reports["tt"]["environment"]["engine"]
    assert [s["id"] for s in merged["merge"]["shards"]] == ["tt", "ss"]


def test_input_order_does_not_change_the_output(tmp_path, shard_reports):
    manifest = _plan(tmp_path, shard_reports)
    forward = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    assert sim_merge.merge_reports(manifest, forward) == sim_merge.merge_reports(
        manifest, forward[::-1]
    )


def test_errored_shard_corner_poisons_the_merged_status(tmp_path, shard_reports):
    errored = copy.deepcopy(shard_reports)
    for corner in errored["ss"]["corners"]:
        corner["status"] = "error"
        for m in corner["measurements"]:
            m.update(status="error", value=None, margin=None)
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in errored.items()]
    merged = sim_merge.merge_reports(_plan(tmp_path, errored), paths)
    assert merged["status"] == "error"
    assert (merged["passed"], merged["errored"]) == (2, 2)
    # An absent margin is worst, even beside a real (failing) margin.
    assert merged["measurements"][0]["worst_case"]["margin"] is None


def _expect_refusal(manifest, paths, code, exit_code):
    with pytest.raises(sim_merge.MergeError) as excinfo:
        sim_merge.merge_reports(manifest, paths)
    assert excinfo.value.code == code
    assert excinfo.value.exit_code == exit_code


def test_missing_shard_is_refused(tmp_path, shard_reports):
    manifest = _plan(tmp_path, shard_reports)
    _expect_refusal(
        manifest,
        [_write(tmp_path, "tt.report", shard_reports["tt"])],
        "missing_shard",
        5,
    )


def test_duplicate_shard_is_refused(tmp_path, shard_reports):
    manifest = _plan(tmp_path, shard_reports)
    paths = [
        _write(tmp_path, "tt.report", shard_reports["tt"]),
        _write(tmp_path, "tt2.report", shard_reports["tt"]),
        _write(tmp_path, "ss.report", shard_reports["ss"]),
    ]
    _expect_refusal(manifest, paths, "duplicate_shard", 6)


def test_unexpected_shard_is_refused(tmp_path, shard_reports, stub_ngspice):
    manifest = _plan(tmp_path, shard_reports)
    stranger = _run(tmp_path, "ff.json", ["ff"])
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    paths.append(_write(tmp_path, "ff.report", stranger))
    _expect_refusal(manifest, paths, "unexpected_shard", 7)


def test_report_with_other_corners_is_refused(tmp_path, shard_reports):
    manifest = _plan(tmp_path, shard_reports)
    short = copy.deepcopy(shard_reports["ss"])
    short["corners"] = short["corners"][:1]
    paths = [
        _write(tmp_path, "tt.report", shard_reports["tt"]),
        _write(tmp_path, "ss.report", short),
    ]
    _expect_refusal(manifest, paths, "corner_mismatch", 8)


def test_changed_request_is_refused(tmp_path, shard_reports):
    manifest = _plan(tmp_path, shard_reports, pin=True)
    (tmp_path / "ss.json").write_text(json.dumps({"shard": "ss", "edited": True}))
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    _expect_refusal(manifest, paths, "request_mismatch", 9)


def test_pinned_requests_that_match_are_accepted(tmp_path, shard_reports):
    manifest = _plan(tmp_path, shard_reports, pin=True)
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    merged = sim_merge.merge_reports(manifest, paths)
    assert merged["merge"]["shards"][0]["request"] == "tt.json"


def test_shards_with_different_limits_are_refused(tmp_path, shard_reports):
    drifted = copy.deepcopy(shard_reports)
    drifted["ss"]["measurements"][0]["limits"] = {"min": 0.5}
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in drifted.items()]
    _expect_refusal(_plan(tmp_path, drifted), paths, "inconsistent_shards", 10)


def test_monte_carlo_report_is_not_mergeable(tmp_path, shard_reports):
    sampled = copy.deepcopy(shard_reports)
    sampled["tt"]["environment"]["monte_carlo"] = {"n": 2, "seed": 1}
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in sampled.items()]
    with pytest.raises(sim_merge.MergeError, match="Monte Carlo") as excinfo:
        sim_merge.merge_reports(_plan(tmp_path, shard_reports), paths)
    assert excinfo.value.exit_code == 1


def test_manifest_planning_the_same_corners_twice_is_invalid(tmp_path, shard_reports):
    corners = [c["corner_id"] for c in shard_reports["tt"]["corners"]]
    manifest = _write(
        tmp_path,
        "manifest.json",
        {
            "schema_version": 1,
            "shards": [
                {"id": "a", "corners": corners},
                {"id": "b", "corners": corners},
            ],
        },
    )
    with pytest.raises(sim_merge.MergeError, match="same corner list"):
        sim_merge.merge_reports(manifest, [])


def test_cli_exit_codes_and_envelope(tmp_path, shard_reports, capsys):
    manifest = _plan(tmp_path, shard_reports)
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]

    assert main(["sim-merge", manifest, *paths, "--format", "json"]) == 3
    merged = json.loads(capsys.readouterr().out)
    assert merged["status"] == "fail"
    assert merged["merge"]["shard_count"] == 2

    # A refusal is a verdict: exit >= 3 means a payload is on stdout.
    assert main(["sim-merge", manifest, paths[0], "--format", "json"]) == 5
    refused = json.loads(capsys.readouterr().out)
    assert refused["status"] == "refused"
    assert refused["refusal"]["code"] == "missing_shard"

    # Unreadable input is a plain error: exit 1, envelope on stderr.
    assert (
        main(["sim-merge", str(tmp_path / "nope.json"), *paths, "--format", "json"])
        == 1
    )
    assert json.loads(capsys.readouterr().err)["error"]["command"] == "sim-merge"

    assert main(["sim-merge", manifest, *paths]) == 3
    assert "2 shard(s), 4 corner(s): fail" in capsys.readouterr().out


def test_manifest_and_report_match_the_published_schemas(tmp_path, shard_reports):
    jsonschema = pytest.importorskip("jsonschema")
    schemas = Path(__file__).parent.parent / "docs" / "schemas"
    manifest = _plan(tmp_path, shard_reports, pin=True)
    jsonschema.validate(
        json.loads(Path(manifest).read_text()),
        json.loads((schemas / "sim-merge-manifest.schema.json").read_text()),
    )
    paths = [_write(tmp_path, f"{k}.report", r) for k, r in shard_reports.items()]
    jsonschema.validate(
        sim_merge.merge_reports(manifest, paths),
        json.loads((schemas / "sim-merge-report.schema.json").read_text()),
    )
