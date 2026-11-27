"""``artifacts.waveform_spectrum``: a noise run's per-frequency spectrum (#7866).

``options.waveforms`` on a ``noise`` analysis used to export only ngspice's
current plot after the run, the integrated one. The spectrum plot now comes
home as a sibling ``waveform_spectrum.raw.json``; ``waveform.raw*`` is untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers.tool_probes import SKIP_NO_NGSPICE as _SKIP_NO_NGSPICE
from klayout_tools import sim, sim_batch_contract_spec, sim_noise, sim_noise_spectrum

ARGS = "v(out) V1 dec 20 10 1e7"
NOISE = {"kind": "noise", "args": ARGS}
RAW = "/corner/waveform.raw"
SPECTRUM_RAW = "/corner/waveform_spectrum.raw"
GROUP = [
    "set __klt_nzw_plot = $curplot",
    "setplot noise1",
    f"write {SPECTRUM_RAW}",
    "setplot $__klt_nzw_plot",
]


# ------------------------------------------------------------------ the deck


def test_noise_with_a_rawfile_dumps_noise1_beside_the_main_rawfile():
    assert sim_noise_spectrum.control_lines(NOISE, RAW) == GROUP


@pytest.mark.parametrize(
    ("analysis", "raw_path"),
    [
        (NOISE, None),  # noise without options.waveforms
        ({"kind": "ac", "args": "dec 5 1 1e6"}, RAW),
        ({"kind": "tran", "args": "1n 1u"}, RAW),
        ({"kind": "dc", "args": "V1 0 1 0.1"}, RAW),
        ({}, RAW),
    ],
)
def test_every_other_deck_gets_no_spectrum_lines(analysis, raw_path):
    assert sim_noise_spectrum.control_lines(analysis, raw_path) == []
    assert not sim_noise_spectrum.applies(analysis, raw_path)


def _control(analysis, measurements, raw_path):
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=sim_noise.lower_measurements(measurements, analysis),
        raw_path=raw_path,
        osdi_preload=(),
        corner_dir="/corner",
    )


def test_group_follows_the_main_write_and_precedes_every_expr_line():
    lines = _control(NOISE, [{"name": "total", "expr": "onoise_total"}], RAW)
    write = lines.index(f"write {RAW}")
    assert lines[write + 1 : write + 1 + len(GROUP)] == GROUP
    after = lines[write + 1 + len(GROUP) :]
    # the totals plot is current again before any later line reads it
    assert after[0] == "let inoise_spectrum = noise1.inoise_spectrum"
    assert "let total = onoise_total" in after


def test_mechanism_dump_and_expr_lines_still_follow_the_restore():
    analysis = {"kind": "noise", "args": ARGS + " 1"}
    measurements = [
        {"name": "total", "expr": "onoise_total"},
        {
            "name": "thermal",
            "noise": {
                "quantity": "onoise",
                "band_hz": [1e2, 1e5],
                "mechanism": "thermal",
            },
        },
    ]
    lines = _control(analysis, measurements, RAW)
    restore = lines.index("setplot $__klt_nzw_plot")
    expr = lines.index("let total = onoise_total")
    mechanism = lines.index("set __klt_nzm_plot = $curplot")
    assert restore < expr < mechanism
    assert lines.count("setplot noise1") == 2  # the spectrum dump and the mechanism one


@pytest.mark.parametrize(
    ("analysis", "raw_path"),
    [(NOISE, None), ({"kind": "ac", "args": "dec 5 1 1e6"}, RAW)],
)
def test_decks_without_a_noise_waveform_request_are_unchanged(analysis, raw_path):
    text = "\n".join(_control(analysis, [], raw_path))
    assert "waveform_spectrum" not in text
    assert "__klt_nzw_plot" not in text
    assert "noise1" not in text


# ------------------------------------------------------------- persistence


def _spectrum_rawfile(raw_dir: Path, points: int = 4, body: str | None = None) -> Path:
    rows = "".join(
        f" {i}\t{10.0 * (i + 1):.15e}\n\t{1e-8:.15e}\n\t{2e-8 / (i + 1):.15e}\n\n"
        for i in range(points)
    )
    path = raw_dir / "waveform_spectrum.raw"
    path.write_text(
        "Title: t\nPlotname: Noise Spectral Density Curves\nFlags: real\n"
        f"No. Variables: 3\nNo. Points: {points}\nVariables:\n"
        "\t0\tfrequency\tfrequency grid=3\n"
        "\t1\tinoise_spectrum\tvoltage-density\n"
        "\t2\tonoise_spectrum\tvoltage-density\n"
        f"Values:\n{rows if body is None else body}"
    )
    return path


def _persist(tmp_path, *, analysis=NOISE, max_bytes=None, decimate=None):
    diagnostics: list[dict] = []
    fields = sim_noise_spectrum.persist(
        analysis, str(tmp_path / "waveform.raw"), max_bytes, decimate, diagnostics
    )
    return fields, diagnostics


def test_persist_converts_the_full_sweep_to_the_sibling_json(tmp_path):
    _spectrum_rawfile(tmp_path, points=4)
    fields, diagnostics = _persist(tmp_path)
    artifact = tmp_path / "waveform_spectrum.raw.json"
    assert fields == {"waveform_spectrum": str(artifact)}
    assert diagnostics == []
    body = json.loads(artifact.read_text())
    assert body["plotname"] == "Noise Spectral Density Curves"
    assert [v["name"] for v in body["variables"]] == [
        "frequency",
        "inoise_spectrum",
        "onoise_spectrum",
    ]
    assert len(body["points"]) == 4
    assert body["points"][0][0] == 10.0


def test_persist_honors_waveform_decimate_keeping_first_and_last(tmp_path):
    _spectrum_rawfile(tmp_path, points=8)
    fields, _ = _persist(tmp_path, decimate=3)
    points = json.loads(Path(fields["waveform_spectrum"]).read_text())["points"]
    # indices 0, 3, 6 by stride, plus the final point (7) which is off-stride
    assert [p[0] for p in points] == [10.0, 40.0, 70.0, 80.0]


def test_persist_is_empty_when_the_corner_dumps_no_spectrum(tmp_path):
    _spectrum_rawfile(tmp_path)
    ac = {"kind": "ac", "args": "dec 5 1 1e6"}
    assert _persist(tmp_path, analysis=ac) == ({}, [])
    diagnostics: list[dict] = []
    assert sim_noise_spectrum.persist(NOISE, None, None, None, diagnostics) == {}
    assert diagnostics == []


def test_oversized_spectrum_warns_and_leaves_the_key_null(tmp_path):
    raw = _spectrum_rawfile(tmp_path, points=40)
    fields, diagnostics = _persist(tmp_path, max_bytes=raw.stat().st_size - 1)
    assert fields == {"waveform_spectrum": None}
    assert not (tmp_path / "waveform_spectrum.raw.json").exists()
    (diagnostic,) = diagnostics
    assert diagnostic["severity"] == "warning"
    assert diagnostic["code"] == sim.WAVEFORM_TOO_LARGE_CODE
    assert "waveform_spectrum.raw" in diagnostic["message"]


def test_spectrum_at_exactly_the_cap_is_converted(tmp_path):
    raw = _spectrum_rawfile(tmp_path)
    fields, diagnostics = _persist(tmp_path, max_bytes=raw.stat().st_size)
    assert fields["waveform_spectrum"] is not None
    assert diagnostics == []


def test_unparseable_spectrum_warns_and_leaves_no_truncated_json(tmp_path):
    good = _spectrum_rawfile(tmp_path, points=4).read_text()
    (tmp_path / "waveform_spectrum.raw").write_text(good.replace("2.0", "bogus", 1))
    fields, diagnostics = _persist(tmp_path)
    assert fields == {"waveform_spectrum": None}
    assert not (tmp_path / "waveform_spectrum.raw.json").exists()
    (diagnostic,) = diagnostics
    assert (diagnostic["severity"], diagnostic["code"]) == ("warning", "unknown")
    assert "waveform_spectrum.raw" in diagnostic["message"]


def test_missing_spectrum_rawfile_warns(tmp_path):
    fields, diagnostics = _persist(tmp_path)
    assert fields == {"waveform_spectrum": None}
    (diagnostic,) = diagnostics
    assert (diagnostic["severity"], diagnostic["code"]) == ("warning", "unknown")


def test_size_guard_code_stays_the_main_artifacts_code():
    assert sim_noise_spectrum.TOO_LARGE_CODE == sim.WAVEFORM_TOO_LARGE_CODE
    assert sim.WAVEFORM_TOO_LARGE_CODE in sim.DIAGNOSTIC_CODES


# ------------------------------------------------------- batch: not a gate


def test_noise_waveforms_is_deliberately_not_a_runner_gated_feature():
    """A registry row would refuse (``runner_version_check`` defaults to
    ``enforce``) every batch request that merely sets ``waveforms`` on a noise
    analysis and only needs the integrated plot, on any runner image older than
    the row's release -- and nothing else in the request says the spectrum is
    wanted. An older runner just writes no ``waveform_spectrum``; the docs say
    how to detect that (issue #7866)."""
    request = {
        "netlist": "rc.spice",
        "analysis": NOISE,
        "options": {"waveforms": True, "keep_artifacts": True},
    }
    assert sim_batch_contract_spec._request_features(request) == []


def test_batch_install_brings_the_spectrum_home_and_rewrites_its_path(tmp_path):
    """The batch job writes the corner dir under ``$EDA_OUTPUT_DIR/artifacts``;
    the client copies that whole tree into its own artifacts dir and rewrites
    every path-valued ``artifacts.*`` entry, so the new sibling needs no
    collection change. Runs the real install on a fabricated collected tree."""
    from klayout_tools import sim_batch

    collected = tmp_path / "collected"
    corner = collected / "artifacts" / "tt_27C"
    corner.mkdir(parents=True)
    for name in ("waveform.raw.json", "waveform_spectrum.raw.json"):
        (corner / name).write_text("{}")
    remote = "/mnt/job/outputs/artifacts/tt_27C"
    corners = [
        {
            "artifacts": {
                "waveform": f"{remote}/waveform.raw.json",
                "waveform_spectrum": f"{remote}/waveform_spectrum.raw.json",
                "raw_bytes": 335,
            }
        }
    ]
    local = tmp_path / "local"
    sim_batch._install_batch_artifacts(
        corners, collected_dir=str(collected), artifacts_dir=str(local)
    )
    artifacts = corners[0]["artifacts"]
    assert artifacts["waveform_spectrum"] == str(
        local / "tt_27C" / "waveform_spectrum.raw.json"
    )
    assert Path(artifacts["waveform_spectrum"]).is_file()
    assert artifacts["raw_bytes"] == 335  # non-path metadata passes through


# ------------------------------------------------------------- real ngspice


def _run(tmp_path, *, analysis=NOISE, measurements=(), options=None):
    (tmp_path / "rc.spice").write_text(
        "V1 in 0 DC 0 AC 1\nR1 in out 10k\nC1 out 0 1n\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "rc.spice",
                "analysis": analysis,
                "measurements": list(measurements),
                "options": {
                    "waveforms": True,
                    "keep_artifacts": True,
                    **(options or {}),
                },
            }
        )
    )
    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "out"))
    (corner,) = report["corners"]
    return corner


def _first_data_rows(corner) -> int:
    log = Path(corner["artifacts"]["log"]).read_text()
    return int(re.search(r"No\. of Data Rows\s*:\s*(\d+)", log).group(1))


@_SKIP_NO_NGSPICE
def test_real_noise_run_keeps_the_whole_spectrum_and_the_integrated_plot(tmp_path):
    corner = _run(tmp_path)
    assert corner["diagnostics"] == [], corner["diagnostics"]
    spectrum = json.loads(Path(corner["artifacts"]["waveform_spectrum"]).read_text())
    assert spectrum["plotname"] != "Integrated Noise"
    names = [v["name"] for v in spectrum["variables"]]
    assert {"frequency", "inoise_spectrum", "onoise_spectrum"} <= set(names)
    assert len(spectrum["points"]) == _first_data_rows(corner) == 121
    # the main artifact is exactly what it was: one integrated point
    main = json.loads(Path(corner["artifacts"]["waveform"]).read_text())
    assert main["plotname"] == "Integrated Noise"
    assert len(main["points"]) == 1
    assert corner["noise_representation"] == "amplitude"


@_SKIP_NO_NGSPICE
def test_real_expr_and_mechanism_still_resolve_after_the_extra_write(tmp_path):
    analysis = {"kind": "noise", "args": ARGS + " 1"}
    measurements = [
        {"name": "total", "expr": "onoise_total"},
        {"name": "spot", "noise": {"quantity": "onoise", "at_hz": 1e3}},
        {
            "name": "thermal",
            "noise": {
                "quantity": "onoise",
                "band_hz": [1e2, 1e5],
                "mechanism": "thermal",
            },
        },
    ]
    corner = _run(tmp_path, analysis=analysis, measurements=measurements)
    assert corner["diagnostics"] == [], corner["diagnostics"]
    assert [m["status"] for m in corner["measurements"]] == ["pass"] * 3
    assert all(m["value"] > 0 for m in corner["measurements"])
    spectrum = json.loads(Path(corner["artifacts"]["waveform_spectrum"]).read_text())
    assert len(spectrum["points"]) == _first_data_rows(corner)


@_SKIP_NO_NGSPICE
def test_real_decimate_and_size_cap_apply_to_the_spectrum(tmp_path):
    (tmp_path / "dec").mkdir()
    decimated = _run(tmp_path / "dec", options={"waveform_decimate": 5})
    spectrum = json.loads(Path(decimated["artifacts"]["waveform_spectrum"]).read_text())
    assert len(spectrum["points"]) == 25  # every 5th of 121 (0..120), last included
    (tmp_path / "cap").mkdir()
    capped = _run(tmp_path / "cap", options={"waveform_max_bytes": 1000})
    assert capped["status"] == "pass"
    assert capped["artifacts"]["waveform_spectrum"] is None
    assert capped["artifacts"]["waveform"] is not None  # the 335-byte plot fits
    assert [d["code"] for d in capped["diagnostics"]] == ["waveform_too_large"]


@_SKIP_NO_NGSPICE
def test_real_key_is_absent_without_a_noise_waveform_request(tmp_path):
    ac = _run(tmp_path, analysis={"kind": "ac", "args": "dec 5 1 1e6"})
    assert "waveform_spectrum" not in ac["artifacts"]
    (tmp_path / "off").mkdir()
    off = _run(tmp_path / "off", options={"waveforms": False, "keep_artifacts": True})
    assert "waveform_spectrum" not in off["artifacts"]
    assert not list((tmp_path / "off").rglob("waveform_spectrum.raw*"))
