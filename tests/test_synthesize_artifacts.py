"""Invocation isolation at the fake-engine boundary; no real PDK required."""

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from klayout_tools import synthesize
from klayout_tools.synthesize import SynthesizeError, run_synthesize
from test_synthesize import (
    _ABC_STIME_LINE,
    _GCD_MODULE_STATS,
    _GCD_RTL,
    _abs_path,
    _script_abc_log_path,
    _script_output_paths,
    _setup_success_env,
    _stub_yosys_success,
)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    request = _setup_success_env(tmp_path, monkeypatch)
    _stub_yosys_success(monkeypatch, yosys_log="first run\n")
    return tmp_path, request


def _evidence(directory):
    return {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("missing", ["all", "netlist", "stats", "abc"])
def test_prior_outputs_cannot_satisfy_a_successful_incomplete_rerun(
    prepared, monkeypatch, missing
):
    root, request = prepared
    first = run_synthesize(request)
    prior_dir = Path(_abs_path(first["script_path"], root)).parent
    prior = _evidence(prior_dir)
    (root / "gcd.v").write_text(_GCD_RTL.replace("result <= a;", "result <= a ^ 1'b1;"))

    def incomplete(script, *, cwd=None):
        stats, netlist = _script_output_paths(script, cwd=cwd)
        abc = _script_abc_log_path(script, cwd=cwd)
        outputs = {
            "netlist": (netlist, "module gcd(); endmodule\n"),
            "stats": (stats, json.dumps({"modules": {"\\gcd": _GCD_MODULE_STATS}})),
            "abc": (abc, _ABC_STIME_LINE),
        }
        for kind, (path, text) in outputs.items():
            if missing not in ("all", kind):
                Path(path).write_text(text)
        return "second run\n"

    monkeypatch.setattr(synthesize, "_run_yosys", incomplete)
    with pytest.raises(SynthesizeError, match="(did not produce|ABC)"):
        run_synthesize(request)
    assert _evidence(prior_dir) == prior
    scripts = list((root / ".klt/synthesize").rglob("synth_gcd.ys"))
    assert len(scripts) == 2  # Both the prior success and the failed attempt survive.


def test_concurrent_same_top_runs_keep_their_own_outputs(prepared, monkeypatch):
    root, request = prepared
    first = run_synthesize(request)
    prior_dir = Path(_abs_path(first["script_path"], root)).parent
    prior = _evidence(prior_dir)
    original = synthesize.subprocess.run
    barrier = threading.Barrier(2)
    local = threading.local()

    def interleaved(cmd, **kwargs):
        result = original(cmd, **kwargs)
        if cmd[:2] == ["yosys", "-s"]:
            stats, netlist = _script_output_paths(cmd[2], cwd=kwargs.get("cwd"))
            data = json.loads(Path(stats).read_text())
            data["modules"]["\\gcd"]["area"] = local.area
            Path(stats).write_text(json.dumps(data))
            Path(netlist).write_text(f"// area {local.area}\nmodule gcd(); endmodule\n")
            barrier.wait(timeout=10)  # Both engines write before either result is read.
        return result

    monkeypatch.setattr(synthesize.subprocess, "run", interleaved)

    def invoke(area):
        local.area = area
        return run_synthesize(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(invoke, [101, 202]))
    assert [report["area_um2"] for report in reports] == [101, 202]
    assert len({report["run_id"] for report in [first, *reports]}) == 3
    for area, report in zip([101, 202], reports, strict=True):
        assert (
            f"// area {area}"
            in Path(_abs_path(report["netlist_path"], root)).read_text()
        )
    assert _evidence(prior_dir) == prior


@pytest.mark.parametrize(
    "changed", ["rtl", "second_rtl", "liberty", "request", "restored_rtl"]
)
def test_input_changes_during_engine_execution_refuse_attribution(
    prepared, monkeypatch, changed
):
    root, request = prepared
    request_data = json.loads(Path(request).read_text())
    (root / "helper.v").write_text("module helper(); endmodule\n")
    request_data["sources"].append("helper.v")
    Path(request).write_text(json.dumps(request_data))
    paths = {
        "rtl": root / "gcd.v",
        "restored_rtl": root / "gcd.v",
        "second_rtl": root / "helper.v",
        "liberty": next((root / "install").rglob("*tt_025C_1v80.lib")),
        "request": Path(request),
    }
    original = synthesize._run_yosys

    def changing(script, **kwargs):
        log = original(script, **kwargs)
        path = paths[changed]
        before = path.read_bytes()
        path.write_bytes(before + b"\n")
        if changed == "restored_rtl":
            path.write_bytes(before)
        return log

    monkeypatch.setattr(synthesize, "_run_yosys", changing)
    with pytest.raises(SynthesizeError, match="input.*changed"):
        run_synthesize(request)
    assert list((root / ".klt/synthesize").rglob("synth_gcd.ys"))


def test_input_changes_in_late_analysis_also_refuse_attribution(prepared, monkeypatch):
    root, request = prepared

    def changing(*args):
        (root / "gcd.v").write_text("module gcd(); endmodule\n")
        return None

    monkeypatch.setattr(synthesize, "_read_sta_timing", changing)
    with pytest.raises(SynthesizeError, match="input.*changed"):
        run_synthesize(request)


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_failed_runs_retain_script_stdout_and_stderr(prepared, monkeypatch, failure):
    root, request = prepared
    first = run_synthesize(request)
    prior_dir = Path(_abs_path(first["script_path"], root)).parent
    prior = _evidence(prior_dir)
    original = synthesize.subprocess.run

    def failed(cmd, **kwargs):
        if cmd[:2] == ["yosys", "-s"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(
                    cmd, 1, output=b"partial stdout", stderr=b"partial stderr"
                )
            return subprocess.CompletedProcess(
                cmd, 1, "partial stdout", "partial stderr"
            )
        return original(cmd, **kwargs)

    monkeypatch.setattr(synthesize.subprocess, "run", failed)
    with pytest.raises(SynthesizeError):
        run_synthesize(request)
    assert _evidence(prior_dir) == prior
    (failed_script,) = [
        p
        for p in (root / ".klt/synthesize").rglob("synth_gcd.ys")
        if p.parent != prior_dir
    ]
    logs = "\n".join(p.read_text() for p in failed_script.parent.glob("*.log"))
    assert "partial stdout" in logs
    assert "partial stderr" in logs


@pytest.mark.parametrize("netlist", [b"", b"   \n", b"\xff"])
def test_success_requires_a_readable_nonempty_netlist(prepared, monkeypatch, netlist):
    _, request = prepared
    original = synthesize._run_yosys

    def empty(script, **kwargs):
        result = original(script, **kwargs)
        _, path = _script_output_paths(script, cwd=kwargs.get("cwd"))
        Path(path).write_bytes(netlist)
        return result

    monkeypatch.setattr(synthesize, "_run_yosys", empty)
    with pytest.raises(SynthesizeError, match="netlist"):
        run_synthesize(request)


def test_missing_requested_timing_is_explicit(prepared, monkeypatch):
    _, request = prepared
    data = json.loads(Path(request).read_text())
    data["constraints"] = {"clock_period_ns": 5}
    Path(request).write_text(json.dumps(data))
    _stub_yosys_success(monkeypatch, abc_log_lines="ABC: nothing to map\n")
    report = run_synthesize(request)
    assert report["timing"] is None
    assert report["warnings"]["by_category"]["timing_unavailable"] == 1


@pytest.mark.parametrize(
    "run_id", ["../old", "/tmp/old", "a/b", "a\\b", "", 1, ".", "a\n"]
)
def test_caller_run_id_must_be_a_safe_identifier(prepared, run_id):
    _, request = prepared
    data = json.loads(Path(request).read_text())
    data["run_id"] = run_id
    Path(request).write_text(json.dumps(data))
    with pytest.raises(SynthesizeError, match="run_id"):
        run_synthesize(request)


def test_caller_run_id_is_exclusive_and_returned(prepared):
    root, request = prepared
    data = json.loads(Path(request).read_text())
    data["run_id"] = "job-123"
    Path(request).write_text(json.dumps(data))
    report = run_synthesize(request)
    assert report["run_id"] == "job-123"
    netlist = Path(_abs_path(report["netlist_path"], root))
    assert netlist.parent == root / ".klt/synthesize/job-123"
    prior = _evidence(netlist.parent)
    with pytest.raises(SynthesizeError, match="(already exists|File exists)"):
        run_synthesize(request)
    assert _evidence(netlist.parent) == prior


def test_concurrent_explicit_run_id_refuses_second_engine(prepared, monkeypatch):
    _, request = prepared
    data = json.loads(Path(request).read_text())
    data["run_id"] = "shared-job"
    Path(request).write_text(json.dumps(data))
    started, release = threading.Event(), threading.Event()
    original = synthesize._run_yosys

    def held(script, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return original(script, **kwargs)

    monkeypatch.setattr(synthesize, "_run_yosys", held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(run_synthesize, request)
        try:
            assert started.wait(timeout=10)
            with pytest.raises(SynthesizeError, match="File exists"):
                run_synthesize(request)
        finally:
            release.set()
        assert first.result()["run_id"] == "shared-job"


def test_interrupted_run_preserves_prior_and_partial_evidence(prepared, monkeypatch):
    root, request = prepared
    first = run_synthesize(request)
    prior_dir = Path(_abs_path(first["script_path"], root)).parent
    prior = _evidence(prior_dir)

    def interrupted(script, **kwargs):
        Path(script).with_suffix(".log").write_text("partial engine evidence\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(synthesize, "_run_yosys", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_synthesize(request)
    assert _evidence(prior_dir) == prior
    assert len(list((root / ".klt/synthesize").glob("*/synth_gcd.ys"))) == 2
    assert any(
        "partial engine evidence" in p.read_text()
        for p in (root / ".klt/synthesize").glob("*/*.log")
    )
