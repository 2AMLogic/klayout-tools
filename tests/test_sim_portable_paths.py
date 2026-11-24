"""Portable report paths refer to the files that can be moved together."""

import json
from pathlib import Path

import pytest

from klayout_tools import sim_set
from klayout_tools.cli import main
from klayout_tools.sim import SimError
from test_sim_report_out import _report


def _worker_report(path, **kwargs):
    if path == "bad.json":
        raise SimError("refused")
    report = _report(n=1)
    report["environment"] = {
        "backend": {"resolved": kwargs.get("backend") or "batch"},
        "client_ngspice_binary": "/home/example/bin/ngspice",
        "ngspice_binary": "ngspice-worker",
        "engine_build": {"binary_sha256": None},
        "client_engine_build": {"binary_sha256": "client-digest"},
    }
    report["netlist"] = path
    return report


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("backend", ["batch", "remote", "local"])
def test_single_report_mirrors_stdout(tmp_path, monkeypatch, capsys, portable, backend):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim", _worker_report)
    options = ["--portable-paths"] if portable else []
    rc = main(
        [
            "sim",
            "a.json",
            "--backend",
            backend,
            "--format",
            "json",
            "--report-out",
            "output.json",
            *options,
        ]
    )
    assert rc == 0
    assert Path("output.json").read_text() == capsys.readouterr().out
    env = json.loads(Path("output.json").read_text())["environment"]
    expected = _worker_report("a.json", backend=backend)["environment"]
    if portable and backend == "batch":
        expected.pop("client_ngspice_binary")
    assert env == expected


@pytest.mark.parametrize("aggregate", [None, "reports/aggregate.json"])
@pytest.mark.parametrize("outdir", [None, "members"])
def test_set_references_follow_bundle_on_relocation(
    tmp_path, monkeypatch, capsys, aggregate, outdir
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.chdir(bundle)
    Path("reports").mkdir()

    def run_set(paths, **kwargs):
        return sim_set.run_sim_set(paths, run=_worker_report, **kwargs)

    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim_set", run_set)
    args = [
        "sim",
        "--batch-set",
        "foo.request.json",
        "foo.json",
        "bad.json",
        "--format",
        "json",
        "--portable-paths",
    ]
    if outdir:
        args += ["-o", str(bundle / outdir)]
    if aggregate:
        args += ["--report-out", aggregate]
    assert main(args) != 0
    stdout = capsys.readouterr().out
    if aggregate:
        assert Path(aggregate).read_text() == stdout
    entries = json.loads(stdout)["requests"]
    assert entries[2]["report"] is None
    member_paths = [e["report"] for e in entries[:2]]
    assert len(set(member_paths)) == 2 and all(
        not Path(p).is_absolute() for p in member_paths
    )
    relocated = tmp_path / "relocated"
    bundle.rename(relocated)
    base = relocated / (Path(aggregate).parent if aggregate else (outdir or ".klt/sim"))
    for entry in entries[:2]:
        member = json.loads((base / entry["report"]).read_text())
        assert member["netlist"] == entry["request"]
        assert "client_ngspice_binary" not in member["environment"]
        assert member["environment"]["engine_build"] == {"binary_sha256": None}
        assert member["environment"]["client_engine_build"] == {
            "binary_sha256": "client-digest"
        }


@pytest.mark.parametrize(
    "aggregate",
    [
        "members/amp-1/report.json",
        "members/amp-1/../amp-1/report.json",
        "archive/alias.json",
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_aggregate_collision_preserves_member_before_dispatch(
    tmp_path, monkeypatch, capsys, aggregate, dry_run
):
    monkeypatch.chdir(tmp_path)
    member = Path("members/amp-1/report.json")
    member.parent.mkdir(parents=True)
    member.write_text(json.dumps({"netlist": "prior.request.json"}))
    Path("archive").mkdir()
    Path("archive/alias.json").symlink_to(Path("..") / member)
    calls = []

    def run(path, **kwargs):
        calls.append(path)
        return _worker_report(path, **kwargs)

    def run_set(paths, **kwargs):
        return sim_set.run_sim_set(paths, run=run, **kwargs)

    monkeypatch.setattr("klayout_tools.cli.sim_cmd.run_sim_set", run_set)
    rc = main(
        [
            "sim",
            "--batch-set",
            "a/amp.request.json",
            "b/amp.json",
            "--portable-paths",
            "-o",
            "members",
            "--report-out",
            aggregate,
            *(["--dry-run"] if dry_run else []),
            "--format",
            "json",
        ]
    )
    assert rc == 1
    assert "member report" in json.loads(capsys.readouterr().err)["error"]["message"]
    assert calls == []
    assert json.loads(member.read_text()) == {"netlist": "prior.request.json"}
    assert not Path("members/set-manifest.json").exists()
