"""Capture-only AC needs the original, complete frequency sweep to recover."""

import json
import math
from pathlib import Path

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
        ("oct 3 10 850", [10 * 2 ** (i / 3) for i in range(20)]),
        ("oct 1000 10 20", [10 * 2 ** (i / 1000) for i in range(1002)]),
    ],
)
def test_frequency_grid(tmp_path, args, frequencies):
    path = tmp_path / "waveform.raw"
    _rawfile(path, frequencies)
    assert sim_ac_waveform.complete(str(path), args, None)
    assert sim_ac_waveform.complete(str(path), args, str(len(frequencies)))
    _rawfile(path, frequencies[:-1])
    assert not sim_ac_waveform.complete(str(path), args, None)
    assert not sim_ac_waveform.complete(str(path), args, str(len(frequencies)))


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
    assert not sim_ac_waveform.complete(str(path), args, None)


def test_requires_readable_ac_plot_and_intact_axis(tmp_path):
    path = tmp_path / "waveform.raw"
    args = "lin 3 1 3"
    assert not sim_ac_waveform.complete(str(path), args, None)
    path.write_bytes(b"invalid")
    assert not sim_ac_waveform.complete(str(path), args, None)
    for frequencies, plot, axis in (
        ([1, 2, 3], "Noise Spectral Density Curves", "frequency"),
        ([1, 2, 3], "AC Analysis", "time"),
        ([1, 1, 3], "AC Analysis", "frequency"),
        ([1, math.nan, 3], "AC Analysis", "frequency"),
        ([1, 2, 3, 4], "AC Analysis", "frequency"),
    ):
        _rawfile(path, frequencies, plot, axis)
        assert not sim_ac_waveform.complete(str(path), args, str(len(frequencies)))


def _run(tmp_path, options):
    (tmp_path / "body.spice").write_text(
        "Vdrive in 0 DC 0 AC 1\nC1 in out 1n\nC2 out 0 1n\n"
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "ac", "args": "dec 10 10 100k"},
                "options": {"waveforms": True, "capture_only": True, **options},
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
