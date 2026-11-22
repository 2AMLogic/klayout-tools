"""Capture-only AC needs the original, complete frequency sweep to recover."""

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE as real_ngspice
from klayout_tools import sim, sim_ac_waveform


def _rawfile(path, frequencies, plot="AC Analysis", axis="frequency"):
    header = [
        f"Plotname: {plot}",
        "Flags: complex",
        "No. Variables: 2",
        f"No. Points: {len(frequencies)}",
        "Variables:",
        f"0 {axis} frequency",
        "1 v(out) voltage",
        "Values:",
    ]
    rows = [f"{i} {frequency},0\n 0.5,0" for i, frequency in enumerate(frequencies)]
    path.write_text("\n".join(header + rows) + "\n")


@pytest.mark.parametrize(
    ("args", "frequencies"),
    [
        ("LIN 5 10 850", [10, 220, 430, 640, 850]),
        ("lin 1 10 850", [10]),
        ("lin 2 10 850", [10]),
        ("lin 3 0 1kHz", [0, 500, 1000]),
        ("dec 10 10 850", [10 * 85 ** (i / 19) for i in range(20)]),
        ("dec 10 10 90", [10 * 10 ** (i / 10) for i in range(10)]),
        ("dec 10 10 90", [10 * 9 ** (i / 9) for i in range(10)]),
        ("oct 3 10 850", [10 * 2 ** (i / 3) for i in range(20)]),
        ("oct 1000 10 20", [10 * 2 ** (i / 1000) for i in range(1002)]),
    ],
)
def test_frequency_grid(tmp_path, args, frequencies):
    path = tmp_path / "waveform.raw"
    _rawfile(path, frequencies)
    length = len(frequencies)
    scale = f"    frequency : frequency, complex, {length} long, grid = xlog"
    rows = f"No. of Data Rows : {length}"
    for log in (scale, rows, rows + "\n" + scale):
        assert sim_ac_waveform.complete(str(path), args, log)
    _rawfile(path, frequencies[:-1])
    for log in (scale, rows, rows + "\n" + scale):
        assert not sim_ac_waveform.complete(str(path), args, log)


@pytest.mark.parametrize("legacy", [True, False])
def test_subdecade_grid_requires_the_same_spacing_throughout(tmp_path, legacy):
    path = tmp_path / "waveform.raw"
    # Both versions produce 306 points at reltol=0.01, with distinct spacing.
    older = [10 * 2 ** (i / 301) for i in range(306)]
    newer = [10 * 10 ** (i / 1000) for i in range(306)]
    grid, other = (older, newer) if legacy else (newer, older)
    args = "dec 1000 10 20"
    scale = "frequency : frequency, complex, 306 long"
    _rawfile(path, grid)
    assert sim_ac_waveform.complete(str(path), args, scale)
    grid[150] = other[150]
    _rawfile(path, grid)
    assert not sim_ac_waveform.complete(str(path), args, scale)


@pytest.mark.parametrize(
    "args",
    [
        "op",
        "list 3 10 20",
        "lin 0 10 20",
        "lin x 10 20",
        "lin 1 x 20",
        "dec 1 10 1",
        "oct 1 0 20",
        "lin 1 10 1e999",
        "dec 3 1e-300 1e300",
    ],
)
def test_unknown_extent(tmp_path, args):
    path = tmp_path / "waveform.raw"
    _rawfile(path, [10])
    assert not sim_ac_waveform.complete(str(path), args, "")


def test_requires_readable_ac_plot_and_intact_axis(tmp_path):
    path = tmp_path / "waveform.raw"
    args = "lin 3 1 3"
    assert not sim_ac_waveform.complete(str(path), args, "")
    path.write_bytes(b"invalid")
    assert not sim_ac_waveform.complete(str(path), args, "")
    for frequencies, plot, axis in (
        ([1, 2, 3], "Noise Spectral Density Curves", "frequency"),
        ([1, 2, 3], "AC Analysis", "time"),
        ([1, 1, 3], "AC Analysis", "frequency"),
        ([1, math.nan, 3], "AC Analysis", "frequency"),
        ([1, 2, 3, 4], "AC Analysis", "frequency"),
    ):
        _rawfile(path, frequencies, plot, axis)
        assert not sim_ac_waveform.complete(
            str(path), args, f"No. of Data Rows : {len(frequencies)}"
        )


_CAPACITIVE = "Vdrive in 0 DC 0 AC 1\nC1 in out 1n\nC2 out 0 1n\n"
#: With both stepping aids disabled and one iteration per step, ngspice aborts
#: this AC sweep before it creates the `frequency` vector (issue #7293).
_ABORTING = (
    "Vin in 0 DC 1 AC 1\n"
    "B1 out 0 V = 1e6*tanh(1e9*v(out)) + 1e6*v(in)\n"
    "R1 out 0 1e-12\n"
)
_UNRECOVERABLE = {"itl1": 1, "itl2": 1, "gminsteps": 0, "srcsteps": 0}


def _run(tmp_path, options, args="dec 10 10 100k", body=_CAPACITIVE):
    (tmp_path / "body.spice").write_text(body)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "ac", "args": args},
                "options": {
                    "waveforms": True,
                    "capture_only": True,
                    "ngspice_init": [],
                    **options,
                },
            }
        )
    )
    return sim.run_sim(str(path), backend="local")


@real_ngspice
@pytest.mark.parametrize(
    ("options", "status"),
    [
        ({}, "pass"),
        ({"complex_vectors": ["v(out)"]}, "pass"),
        ({"fail_on_diagnostic": ["singular_matrix"]}, "inconclusive"),
        ({"complex_vectors": ["v(missing)"]}, "error"),
    ],
)
def test_real_capture_only_corner(tmp_path, options, status):
    report = _run(tmp_path, options)
    (corner,) = report["corners"]
    assert corner["status"] == status
    if status == "pass":
        assert report["status"] == "pass"
    warnings = {
        d["code"]
        for d in corner["diagnostics"]
        if d["severity"] == "warning" and "completed the AC sweep" in d["message"]
    }
    assert warnings == {"singular_matrix", "nonconvergence"}
    if options.get("complex_vectors") == ["v(out)"]:
        assert corner["complex_vectors"]["points"] == 41
        sidecar = json.loads(Path(corner["artifacts"]["complex_vectors"]).read_text())
        assert sidecar["data"]["v(out)"]["real"] == pytest.approx([0.5] * 41)


@real_ngspice
@pytest.mark.parametrize("fault", ["abort", "sidecar_only"])
def test_complete_vector_alone_cannot_rescue_corner(tmp_path, monkeypatch, fault):
    original_run = sim.subprocess.run

    def inject_fault(cmd, *args, **kwargs):
        result = original_run(cmd, *args, **kwargs)
        if "-o" in cmd and "-b" in cmd:
            log = Path(cmd[cmd.index("-o") + 1])
            if fault == "abort":
                with log.open("a") as handle:
                    handle.write("ac simulation(s) aborted\n")
            else:
                (log.parent / "waveform.raw").unlink()
        return result

    monkeypatch.setattr(sim.subprocess, "run", inject_fault)
    report = _run(tmp_path, {"complex_vectors": ["v(out)"]})
    (corner,) = report["corners"]
    assert report["status"] == corner["status"] == "error"
    assert corner["complex_vectors"]["points"] == 41
    assert {d["code"] for d in corner["diagnostics"] if d["severity"] == "error"} >= {
        "singular_matrix",
        "nonconvergence",
    }


@real_ngspice
@pytest.mark.parametrize(
    ("mode", "shortened", "row_reports", "status"),
    [
        ("oct", False, 1, "pass"),
        ("dec", False, 1, "pass"),
        ("oct", False, 0, "pass"),
        ("dec", False, 2, "pass"),
        ("oct", True, 0, "error"),
        ("dec", True, 2, "error"),
        ("oct", True, 1, "error"),
    ],
)
def test_nondefault_logarithmic_endpoint(
    tmp_path, monkeypatch, mode, shortened, row_reports, status
):
    original_run = sim.subprocess.run

    def adjust_engine_artifacts(cmd, *args, **kwargs):
        result = original_run(cmd, *args, **kwargs)
        if "-o" in cmd and "-b" in cmd:
            log = Path(cmd[cmd.index("-o") + 1])
            text = log.read_text()
            (row_report,) = re.findall(r"No\. of Data Rows[^\n]*", text)
            log.write_text(
                text.replace(row_report, "\n".join([row_report] * row_reports))
            )
            if shortened:
                raw = log.parent / "waveform.raw"
                keep = 1002 if mode == "oct" else 302
                head, tail = raw.read_text().split("Values:\n")
                head = re.sub(r"No\. Points: \d+", f"No. Points: {keep}", head)
                tail = re.split(rf"^ *{keep}\t", tail, flags=re.MULTILINE)[0]
                raw.write_text(head + "Values:\n" + tail)
        return result

    monkeypatch.setattr(sim.subprocess, "run", adjust_engine_artifacts)
    report = _run(tmp_path, {"ngspice_options": {"reltol": 0.01}}, f"{mode} 1000 10 20")
    (corner,) = report["corners"]
    assert report["status"] == corner["status"] == status
    assert {
        d["code"]
        for d in corner["diagnostics"]
        if d["severity"] == ("warning" if status == "pass" else "error")
    } >= {"singular_matrix", "nonconvergence"}


@pytest.mark.parametrize(
    "log",
    [
        "",
        None,
        "No. of Data Rows : 1002\nNo. of Data Rows : 1002",
        "frequency : frequency, complex, 1015 long",
    ],
)
def test_logarithmic_endpoint_needs_the_original_length(tmp_path, log):
    path = tmp_path / "waveform.raw"
    _rawfile(path, [10 * 2 ** (i / 1000) for i in range(1002)])
    assert not sim_ac_waveform.complete(str(path), "oct 1000 10 20", log)


#: After `write <raw>`, a capture-only AC deck records the original point
#: count for the logarithmic endpoint check (issue #7120). The `if` keeps an
#: aborted sweep -- which leaves no `frequency` vector -- from turning the
#: probe into a klt-caused `no_such_vector` diagnostic (issue #7293).
_FREQUENCY_PROBE = ["if length(frequency) > 0", "display frequency", "end"]


def _control(analysis, measurements_spec=(), raw_path="/x/waveform.raw"):
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=list(measurements_spec),
        raw_path=raw_path,
        osdi_preload=(),
    )


def test_frequency_probe_is_guarded_and_follows_the_rawfile_write():
    lines = _control({"kind": "ac", "args": "dec 10 10 100k"})
    write = lines.index("write /x/waveform.raw")
    assert lines[write + 1 : write + 4] == _FREQUENCY_PROBE
    assert "display frequency" not in lines[: write + 1] + lines[write + 4 :]


@pytest.mark.parametrize(
    ("analysis", "measurements_spec", "raw_path"),
    [
        (
            {"kind": "ac", "args": "dec 10 10 100k"},
            [{"name": "g", "expr": "v(out)"}],
            "/x/waveform.raw",
        ),
        ({"kind": "tran", "args": "1u 1m"}, (), "/x/waveform.raw"),
        ({"kind": "ac", "args": "dec 10 10 100k"}, (), None),
    ],
)
def test_frequency_probe_needs_a_capture_only_ac_rawfile(
    analysis, measurements_spec, raw_path
):
    assert "display frequency" not in _control(analysis, measurements_spec, raw_path)


def _capture_logs(monkeypatch):
    """Collect each ngspice log's text while the corner directory still exists."""
    logs = []
    original_run = sim.subprocess.run

    def read_log(cmd, *args, **kwargs):
        result = original_run(cmd, *args, **kwargs)
        if "-o" in cmd and "-b" in cmd:
            logs.append(Path(cmd[cmd.index("-o") + 1]).read_text())
        return result

    monkeypatch.setattr(sim.subprocess, "run", read_log)
    return logs


@real_ngspice
def test_aborted_sweep_does_not_blame_the_frequency_probe(tmp_path, monkeypatch):
    logs = _capture_logs(monkeypatch)
    report = _run(tmp_path, {"ngspice_options": _UNRECOVERABLE}, body=_ABORTING)
    (corner,) = report["corners"]
    assert report["status"] == corner["status"] == "error"
    assert {d["code"] for d in corner["diagnostics"]} == {
        "analysis_aborted",
        "timestep_too_small",
    }
    assert report["diagnostic_counts"]["by_severity"] == {"error": 2}
    log = "\n".join(logs)
    assert "ac simulation(s) aborted" in log
    assert "no such vector as frequency" not in log


@real_ngspice
def test_completed_sweep_still_records_the_frequency_vector_length(
    tmp_path, monkeypatch
):
    logs = _capture_logs(monkeypatch)
    report = _run(tmp_path, {})
    assert report["status"] == "pass"
    assert re.search(r"frequency\s*:\s*frequency, complex, 41 long", "\n".join(logs))
