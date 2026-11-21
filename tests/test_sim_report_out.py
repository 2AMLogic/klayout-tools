"""``klt sim --report-out`` (issue #2975): file copy of the report envelope."""

from __future__ import annotations

import gzip
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import _read_samples


def _report(n=4):
    return {
        "schema_version": 1,
        "netlist": "tb.spice",
        "status": "pass",
        "environment": {"monte_carlo": {"n": n, "seed": 1000}},
        "measurements": [
            {"name": "vref", "unit": "V", "limits": {"min": 0.5, "max": 3.5}}
        ],
        "corners": [
            {
                "corner_id": f"tt/1.800V/27C/mc{i}",
                "status": "pass",
                "measurements": [{"name": "vref", "value": 1.0 + i, "unit": "V"}],
                "monte_carlo": {"sample_index": i, "seed": 1000 + i},
            }
            for i in range(n)
        ],
    }


@pytest.fixture
def fake_sim(monkeypatch):
    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", lambda *a, **k: _report())


def _run(tmp_path, capsys, *extra):
    request = tmp_path / "req.json"
    request.write_text("{}")
    code = main(["sim", str(request), "--format", "json", *extra])
    return code, capsys.readouterr()


def test_gz_report_is_byte_identical_to_stdout_and_deterministic(
    tmp_path, capsys, fake_sim
):
    a, b = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    code, out = _run(tmp_path, capsys, "--report-out", str(a))
    assert code == 0
    assert gzip.decompress(a.read_bytes()).decode() == out.out
    _run(tmp_path, capsys, "--report-out", str(b))
    assert a.read_bytes() == b.read_bytes()


def test_plain_and_suffixless_paths_write_uncompressed_json(tmp_path, capsys, fake_sim):
    for name in ("r.json", "r"):
        path = tmp_path / name
        code, out = _run(tmp_path, capsys, "--report-out", str(path))
        assert code == 0
        assert path.read_text() == out.out


def test_unwritable_path_is_an_error_envelope(tmp_path, capsys, fake_sim):
    bad = tmp_path / "missing-dir" / "r.json.gz"
    code, out = _run(tmp_path, capsys, "--report-out", str(bad))
    assert code == 1
    assert out.out == ""
    assert json.loads(out.err)["error"]["command"] == "sim"


def test_yield_reads_gz_report_identically(tmp_path):
    from klayout_tools.cli.output import write_json_file

    plain, gz = tmp_path / "r.json", tmp_path / "r.json.gz"
    write_json_file(_report(), str(plain))
    write_json_file(_report(), str(gz))
    assert _read_samples(str(gz)) == _read_samples(str(plain))


def test_batch_report_with_remote_environment_is_written_verbatim(
    tmp_path, capsys, monkeypatch
):
    """Issue #3057: a batch report (``environment.remote``) lands in the file."""
    report = _report()
    report["environment"]["remote"] = {"backend": "batch", "job_id": "job-123"}
    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", lambda *a, **k: report)
    path = tmp_path / "nested" / "r.json"
    path.parent.mkdir()
    code, out = _run(tmp_path, capsys, "--backend", "batch", "--report-out", str(path))
    assert code == 0
    assert path.read_text() == out.out
    assert json.loads(path.read_text())["environment"]["remote"]["job_id"] == "job-123"


# --- Issue #5776: an explicit `-o` without `--report-out` also receives
# `<dir>/report.json`, the same filename `--batch-set` members get, so a
# completed run can no longer exit 0 leaving its evidence only on stdout.


def _completed_report():
    """``_report()`` plus the summary fields the text renderer prints."""
    report = _report()
    report["corner_count"] = 4
    report["passed"] = 4
    report["failed"] = 0
    report["errored"] = 0
    report["inconclusive"] = 0
    report["environment"]["engine"] = "ngspice"
    report["environment"]["engine_version"] = "44"
    report["environment"]["models_lib"] = {"path": "sky130.lib.spice", "scope": "repo"}
    report["netlist"] = {"path": "tb.spice", "scope": "repo"}
    for corner in report["corners"]:
        corner["runtime_s"] = 0.1
        corner["diagnostics"] = []
    for m in report["measurements"]:
        m["status"] = "pass"
        m["worst_case"] = None
    return report


def test_outdir_writes_implicit_report_json(tmp_path, capsys, fake_sim):
    outdir = tmp_path / "o"
    outdir.mkdir()
    code, _ = _run(tmp_path, capsys, "-o", str(outdir))
    assert code == 0
    assert json.loads((outdir / "report.json").read_text())["status"] == "pass"


def test_implicit_report_is_created_with_a_missing_outdir(tmp_path, capsys, fake_sim):
    outdir = tmp_path / "deep" / "nested"
    code, _ = _run(tmp_path, capsys, "-o", str(outdir))
    assert code == 0
    assert (outdir / "report.json").exists()


def test_implicit_report_matches_json_stdout(tmp_path, capsys, fake_sim):
    outdir = tmp_path / "o"
    code, out = _run(tmp_path, capsys, "-o", str(outdir))
    assert code == 0
    assert (outdir / "report.json").read_text() == out.out


def test_implicit_report_written_with_text_format(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "klayout_tools.cli.sim_cmd.run_sim", lambda *a, **k: _completed_report()
    )
    outdir = tmp_path / "o"
    code, out = _run(tmp_path, capsys, "-o", str(outdir), "--format", "text")
    assert code == 0
    assert json.loads((outdir / "report.json").read_text())["status"] == "pass"
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.out)


def test_explicit_report_out_wins_no_implicit_copy(tmp_path, capsys, fake_sim):
    outdir = tmp_path / "o"
    outdir.mkdir()
    rep = tmp_path / "elsewhere.json"
    code, out = _run(tmp_path, capsys, "-o", str(outdir), "--report-out", str(rep))
    assert code == 0
    assert rep.read_text() == out.out
    assert not (outdir / "report.json").exists()


def test_no_outdir_no_report_out_writes_nothing(tmp_path, capsys, fake_sim):
    code, out = _run(tmp_path, capsys)
    assert code == 0
    assert out.out
    assert not (tmp_path / "report.json").exists()


def test_unwritable_outdir_is_an_error_envelope(tmp_path, capsys, fake_sim):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    code, out = _run(tmp_path, capsys, "-o", str(blocker))
    assert code == 1
    assert out.out == ""
    assert json.loads(out.err)["error"]["command"] == "sim"
    assert not (blocker / "report.json").exists()


def test_dry_run_writes_no_implicit_report(tmp_path, capsys, monkeypatch):
    report = _completed_report()
    report["dry_run"] = True
    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", lambda *a, **k: report)
    outdir = tmp_path / "o"
    code, _ = _run(tmp_path, capsys, "--dry-run", "-o", str(outdir))
    assert code == 0
    assert not outdir.exists()


def test_submit_only_writes_no_implicit_report(tmp_path, capsys, monkeypatch):
    report = _report()
    report["status"] = "submitted"
    report["job_id"] = "job-1"
    report["job_uri"] = "s3://bucket/jobs/job-1"
    report["launched_at"] = "2026-11-17T00:00:00Z"
    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", lambda *a, **k: report)
    outdir = tmp_path / "o"
    code, _ = _run(tmp_path, capsys, "--submit-only", "-o", str(outdir))
    assert code == 0
    assert not outdir.exists()


def test_attach_collect_partial_writes_implicit_report(tmp_path, capsys, fake_sim):
    """The `--attach --collect-partial` path shares `_run_sweep`'s write site."""
    outdir = tmp_path / "o"
    code, out = _run(
        tmp_path,
        capsys,
        "--attach",
        "shards.json",
        "--collect-partial",
        "-o",
        str(outdir),
    )
    assert code == 0
    assert (outdir / "report.json").read_text() == out.out
