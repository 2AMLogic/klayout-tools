"""Corner batch reports must belong to the submitted netlist (#7180)."""

import hashlib
import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_batch, sim_set
from test_sim_batch import (
    _batch_request,
    _FakeRunner,
    _install_fake_batch_transport,
    _receipt_job_kwargs,
    _sim_report,
    _status,
    job_id_of,
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_member_dirs_handle_index_collisions_and_symlinks(tmp_path):
    (tmp_path / "alias").symlink_to(tmp_path / "tb", target_is_directory=True)
    paths = ["a/tb.json", "b/tb-2.json", "c/tb.json", "d/alias.json"]
    dirs = sim_set._request_dirs(paths, str(tmp_path))
    assert len({Path(d).resolve() for d in dirs}) == len(paths)


class _EchoNetlist(_FakeRunner):
    """Return a separate report for the actual bytes uploaded under each job ID."""

    def __init__(self):
        super().__init__()
        self.uploads = {}
        self.default = fake_completed(_status("done"))

    def __call__(self, argv, timeout_s):
        if "cp" in argv and "--recursive" in argv:
            source, dest = argv[argv.index("cp") + 1 : argv.index("cp") + 3]
            if dest.endswith("/inputs"):
                job = job_id_of(dest)
                files = {
                    path.name: path.read_bytes() for path in Path(source).iterdir()
                }
                self.uploads[job] = files
                deck = files["netlist.cir"]
                # Return member 0's digest for member 3, keeping all other pairs intact.
                echoed = _sha(deck.replace(b"variant=3", b"variant=0"))
                report = json.loads(_sim_report(netlist_sha256=echoed))
                report["corners"][0]["measurements"] = [
                    {
                        "name": "peak",
                        "margin": 0.8,
                        "value": 1.8,
                        "status": "pass",
                        "limits": {"min": 1.0},
                    }
                ]
                self.outputs_by_job[job] = {"report.json": json.dumps(report)}
        return super().__call__(argv, timeout_s)


def _netlist_member(tmp_path, i):
    root = tmp_path / f"req{i}"
    request_dir = root / "tt"
    request_dir.mkdir(parents=True)
    (root / "dut.spice").write_text(f"R1 out 0 {i + 1}k\n")
    deck = request_dir / "tb_c00.spice"
    deck.write_text(f'* variant={i}\n.include "../dut.spice"\nV1 out 0 1.8\n')
    path = request_dir / "tb_c00.json"
    request = _batch_request(
        request_dir,
        netlist=deck.name,
        measurements=[
            {"name": "peak", "expr": "v(out)", "reduce": "max", "limits": {"min": 1.0}}
        ],
    )
    path.write_text(json.dumps(request))
    return path


def test_concurrent_members_check_their_uploaded_decks(tmp_path, monkeypatch):
    runner = _EchoNetlist()
    _install_fake_batch_transport(monkeypatch, runner=runner)
    requests = [_netlist_member(tmp_path, i) for i in range(6)]
    result = sim_set.run_sim_set(
        list(map(str, requests)), outdir=str(tmp_path / "out"), max_in_flight=4
    )
    assert len(runner.uploads) == len(requests)
    for i, (path, entry) in enumerate(zip(requests, result["requests"], strict=True)):
        raw = path.with_suffix(".spice").read_bytes()
        if i == 3:
            assert (entry["status"], entry["exit_code"]) == ("error", 4)
            assert entry["report"] is None
            assert "netlist" in entry["error"]["message"]
            continue
        assert entry["status"] == "pass", entry["error"]
        report = json.loads(Path(entry["report"]).read_text())
        job = report["environment"]["remote"]["job_id"]
        uploaded = runner.uploads[job]
        assert report["environment"]["netlist_sha256"] == _sha(raw)
        assert f"variant={i}".encode() in uploaded["netlist.cir"]
        assert b"../dut.spice" not in uploaded["netlist.cir"]
        assert uploaded["dut.spice"] == (path.parent.parent / "dut.spice").read_bytes()
        assert _sha(uploaded["netlist.cir"]) != _sha(raw)


@pytest.mark.parametrize("echo", ["staged", "wrong", "absent", "null"])
def test_attach_verifies_the_original_staging_rules(tmp_path, monkeypatch, echo):
    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    path = _netlist_member(tmp_path, 0)
    kwargs = _receipt_job_kwargs(
        tmp_path, runner, netlist_path=str(path.with_suffix(".spice"))
    )
    raw = path.with_suffix(".spice").read_bytes()
    staged = _sha(raw.replace(b"../dut.spice", b"dut.spice"))
    env = (
        {}
        if echo == "absent"
        else {
            "netlist_sha256": {
                "staged": staged.upper(),
                "wrong": _sha(raw),
                "null": None,
            }[echo]
        }
    )
    runner.outputs = {"report.json": _sim_report(**env)}
    if echo == "wrong":
        with pytest.raises(sim.SimError) as caught:
            sim_batch._run_batch_job(**kwargs, attach=True)
        assert all(s in str(caught.value) for s in ("job-1", staged, _sha(raw)))
    else:
        corners, _, _ = sim_batch._run_batch_job(**kwargs, attach=True)
        assert [corner["status"] for corner in corners] == ["pass"]
    assert not runner.argvs_matching("launch", "--apply")


def test_resume_rejects_a_swapped_report_without_launching_again(tmp_path, monkeypatch):
    from test_sim_batch_resume_interrupted import _one_job_resume_fixture

    runner, run, receipt = _one_job_resume_fixture(tmp_path, monkeypatch)
    run()
    job = json.loads(receipt.read_text())["job_id"]
    launches = runner.argvs_matching("launch", "--apply")
    wrong = "f" * 64
    runner.outputs = {"report.json": _sim_report(netlist_sha256=wrong)}
    with pytest.raises(sim.SimError) as caught:
        run(resume=True)
    assert all(
        s in str(caught.value)
        for s in (job, _sha((tmp_path / "body.spice").read_bytes()), wrong)
    )
    assert runner.argvs_matching("launch", "--apply") == launches


def test_shard_with_a_wrong_digest_discards_only_its_corners(tmp_path, monkeypatch):
    from test_sim_batch import _run_capped_fleet, _write_body

    runner = _FakeRunner()
    runner.default = fake_completed(_status("done"))
    _install_fake_batch_transport(monkeypatch, runner=runner)
    jobs = iter(("job-a", "job-b"))
    monkeypatch.setattr(sim_batch, "new_job_id", lambda: next(jobs))
    expected = _sha(_write_body(tmp_path).read_bytes())
    wrong = "0" * 64
    runner.outputs_by_job = {
        "job-a": {"report.json": _sim_report(["first"], netlist_sha256=expected)},
        "job-b": {"report.json": _sim_report(["swapped"], netlist_sha256=wrong)},
    }
    corners, _, _ = _run_capped_fleet(tmp_path, units=2, hosts=2)
    kept, lost = sorted(corners, key=lambda corner: corner["status"] == "error")
    assert (kept["status"], kept["corner_id"]) == ("pass", "first")
    assert lost["status"] == "error" and lost["corner_id"] != "swapped"
    message = lost["diagnostics"][0]["message"]
    assert all(s in message for s in ("job-b", wrong, expected))
