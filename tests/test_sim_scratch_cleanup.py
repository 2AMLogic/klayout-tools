"""The corner and probe runners own only the temporary directories they allocate."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from klayout_tools import sim


def _observe_runner(monkeypatch, runner, outcome):
    observed = []

    if outcome == "prepare":
        write_init = sim._write_spiceinit

        def stop_preparation(directory, lines):
            write_init(directory, lines)
            observed.append(Path(directory))
            raise RuntimeError("deck preparation interrupted")

        monkeypatch.setattr(sim, "_write_spiceinit", stop_preparation)
    elif outcome == "read_log":
        read_log = sim._read_log_file

        def stop_processing(path):
            if Path(path).suffix == ".log":
                assert Path(path).is_file()
                raise RuntimeError("result processing interrupted")
            return read_log(path)

        monkeypatch.setattr(sim, "_read_log_file", stop_processing)

    def launch(command, *, cwd, **kwargs):
        directory = Path(cwd)
        observed.append(directory)
        assert (directory / f"{runner}.cir").is_file()
        assert (directory / ".spiceinit").read_text() == "set numdgt=7\n"
        if outcome == "launch_failure":
            raise PermissionError("cannot launch simulator")
        if outcome == "interrupt":
            raise KeyboardInterrupt("interrupted simulation")
        Path(command[command.index("-o") + 1]).write_text("No. of Data Rows : 1\n")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(sim.subprocess, "run", launch)
    return observed


@pytest.mark.parametrize("runner", ["corner", "probe"])
@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success", "prepare", "launch_failure", "interrupt", "read_log"]
)
def test_runner_work_directory_lifetime(tmp_path, monkeypatch, runner, keep, outcome):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    point = sim.CornerPoint(None, {}, 27.0)
    artifacts = tmp_path / "artifacts"
    kept_dir = artifacts / (
        point.slug if runner == "corner" else f"{point.slug}__probe"
    )
    kept_dir.mkdir(parents=True)
    sentinel = kept_dir / "caller.txt"
    sentinel.write_text("already here")
    body = tmp_path / "body.spice"
    body.write_text("V1 in 0 1\nR1 in 0 1k\n")
    observed = _observe_runner(monkeypatch, runner, outcome)
    common = dict(
        point=point,
        netlist_path=str(body),
        models_lib=None,
        artifacts_dir=str(artifacts),
        keep_artifacts=keep,
        ngspice_init=("set numdgt=7",),
    )
    if runner == "corner":
        run = sim._run_corner
        args = dict(
            analysis={"kind": "tran", "args": "1n 1u"},
            measurements_spec=[],
            timeout_s=1,
            want_waveforms=False,
        )
    else:
        run = sim._run_calibration_probe
        args = dict(step_token="1n", probe_window_s=1e-6, probe_timeout_s=1)

    if outcome == "success":
        result = run(**common, **args)
        if runner == "corner":
            assert result[0]["status"] == "pass"
        else:
            assert result["error"] is None and not result["timed_out"]
    else:
        expected = {
            "prepare": RuntimeError,
            "launch_failure": PermissionError,
            "interrupt": KeyboardInterrupt,
            "read_log": RuntimeError,
        }[outcome]
        with pytest.raises(expected):
            run(**common, **args)

    (directory,) = observed
    assert sentinel.read_text() == "already here"
    assert list(scratch.iterdir()) == []
    if keep:
        assert directory == kept_dir
        assert (directory / f"{runner}.cir").is_file()
        assert (directory / ".spiceinit").read_text() == "set numdgt=7\n"
    else:
        assert directory.parent == scratch
        assert not directory.exists()
