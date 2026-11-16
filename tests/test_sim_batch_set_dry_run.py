"""``klt sim --batch-set --dry-run`` never contacts the fleet (issue #5012)."""

import json

from klayout_tools.cli import main
from test_sim_batch import _write_set
from test_sim_batch_set_forwarding import _patch_set_run


def test_batch_set_dry_run_submits_nothing(tmp_path, monkeypatch, capsys):
    calls = []
    _patch_set_run(monkeypatch, lambda path, **kw: calls.append(path))
    paths = _write_set(tmp_path, 2)
    out = tmp_path / "out"

    code = main(
        ["sim", "--batch-set", *paths, "--dry-run", "-o", str(out), "--format", "json"]
    )

    assert code == 0
    assert calls == []
    assert not (out / "set-manifest.json").exists()
    payload = json.loads(capsys.readouterr().out)
    report = payload.get("data", payload)
    assert report["dry_run"] is True
    assert report["submitted"] == 0
    assert [r["status"] for r in report["requests"]] == ["dry_run", "dry_run"]


def test_batch_set_dry_run_runs_preflight(tmp_path, monkeypatch, capsys):
    from klayout_tools import sim_set

    seen = []
    paths = _write_set(tmp_path, 2)
    report = sim_set.run_sim_set(
        paths,
        dry_run=True,
        outdir=str(tmp_path / "out"),
        preflight=lambda path, **kw: seen.append(path),
    )

    assert seen == paths
    assert report["dry_run"] is True
