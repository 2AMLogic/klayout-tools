"""Tests for ``measurements[].transfer`` -- AC return-ratio metrics graded
through ``klt sim`` (issue #2923).

Most tests stub ``ngspice -b`` and feed a hand-built complex rawfile (cf.
tests/test_sim_cycle_mean.py); the ``real_ngspice`` tests run the engine on a
gain-of-10 RC stage whose answer is known in closed form.
"""

from __future__ import annotations

import cmath
import json
import math
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_batch, sim_raw, sim_transfer_meas

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_BODY = {"num": "v(out)", "den": "v(in)", "metric": "phase_margin_deg"}
_DECL = {"name": "pm", "transfer": _BODY}

# Gain-of-10 single-pole stage, fc = 1 kHz: |T| = 10 / sqrt(1 + (f/fc)^2).
_GAIN = 10.0
_FC = 1e3
_UGF = _FC * math.sqrt(_GAIN**2 - 1.0)


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _write_body(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "body.spice"
    path.write_text(
        "V1 in 0 DC 0 AC 1\n"
        f"E1 e 0 in 0 {_GAIN}\n"
        "R1 e out 1k\n"
        "C1 out 0 159.1549431n\n" + extra
    )
    return path


def _request(tmp_path: Path, measurements: list[dict], **extra) -> Path:
    _write_body(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "ac", "args": "dec 50 1 1e6"},
            "measurements": measurements,
            "options": {"keep_artifacts": False},
            **extra,
        },
    )


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not-an-object", "must be an object"),
        ({**_BODY, "extra": 1}, "unknown transfer key"),
        ({"den": "v(in)", "metric": "phase_margin_deg"}, "transfer.num"),
        ({**_BODY, "num": "v(a, b)"}, "transfer.num"),
        ({**_BODY, "den": "v(a b)"}, "transfer.den"),
        ({**_BODY, "num": "v()"}, "transfer.num"),
        ({**_BODY, "num": "i(v1)"}, "transfer.num"),
        ({**_BODY, "num": "v(out)-v(in)"}, "transfer.num"),
        ({**_BODY, "den": "v(a(b))"}, "transfer.den"),
        ({**_BODY, "metric": "bandwidth"}, "transfer.metric"),
        ({"num": "v(o)", "den": "v(i)"}, "transfer.metric"),
    ],
)
def test_malformed_transfer_is_rejected(body, message):
    with pytest.raises(sim.SimError, match=message):
        sim._validate_measurement_forms([{"name": "t", "transfer": body}])


@pytest.mark.parametrize("other", ["spice", "expr", "crossings", "cycle_mean"])
def test_conflicting_forms_are_rejected(other):
    spec = {"name": "t", "transfer": _BODY, other: {"x": 1} if other[0] == "c" else "x"}
    with pytest.raises(sim.SimError, match=f"'transfer' together with '{other}'"):
        sim._validate_measurement_forms([spec])


def test_all_six_metrics_are_accepted():
    for metric in sim_transfer_meas.METRICS:
        sim._validate_measurement_forms(
            [{"name": "t", "transfer": {**_BODY, "metric": metric}}]
        )
    assert len(sim_transfer_meas.METRICS) == 6


def test_margin_metrics_are_accepted_and_the_first_six_are_unchanged():
    assert sim_transfer_meas.MARGIN_METRICS == (
        "phase_margin_min_deg",
        "gain_margin_min_db",
        "encirclement_count",
    )
    assert sim_transfer_meas.ALL_METRICS == (
        sim_transfer_meas.METRICS + sim_transfer_meas.MARGIN_METRICS
    )
    for metric in sim_transfer_meas.MARGIN_METRICS:
        sim._validate_measurement_forms(
            [{"name": "t", "transfer": {**_BODY, "metric": metric}}]
        )


def test_selector_normalisation_is_case_insensitive():
    assert sim_transfer_meas.normalize_selector("V(OUT)") == "v(out)"
    assert sim_transfer_meas.normalize_selector(" v(out)") is None
    assert sim_transfer_meas.normalize_selector(5) is None


def test_non_ac_analysis_is_rejected(tmp_path):
    request = _request(tmp_path, [_DECL])
    doc = json.loads(request.read_text())
    doc["analysis"] = {"kind": "tran", "args": "1n 1u"}
    request.write_text(json.dumps(doc))
    with pytest.raises(sim.SimError, match='requires analysis.kind "ac"'):
        sim.run_sim(str(request))


def test_duplicate_names_are_rejected(tmp_path):
    other = {"name": "PM", "transfer": {**_BODY, "metric": "unity_crossing_hz"}}
    request = _request(tmp_path, [_DECL, other])
    with pytest.raises(sim.SimError, match="duplicate measurement name"):
        sim.run_sim(str(request))


def test_refused_for_xyce(tmp_path):
    request = _request(tmp_path, [_DECL], engine="xyce")
    with pytest.raises(sim.SimError, match="transfer is not supported for engine"):
        sim.run_sim(str(request))


def _stub_offhost_backends(monkeypatch, reached: list[str]) -> None:
    """Replace every off-host entry point with a recorder, and make any
    attempt to shell out to the fleet tooling fail the test: these tests must
    never submit a job or launch an instance."""

    def _stop(name):
        def _fn(*_a, **_k):
            reached.append(name)
            raise sim.SimError("reached backend")

        return _fn

    for name in ("remote", "batch"):
        monkeypatch.setitem(sim._BACKENDS, name, _stop(name))
    monkeypatch.setattr(sim, "_run_remote_fleet", _stop("remote"))
    monkeypatch.setattr(sim, "_run_batch_fleet", _stop("batch"))
    monkeypatch.setattr(
        sim_batch,
        "_run_subprocess",
        lambda *a, **k: pytest.fail("test tried to reach the batch fleet"),
    )
    monkeypatch.delenv("KLT_BATCH_PROVISION_SCRIPT", raising=False)


@pytest.mark.parametrize("backend", ["batch", "remote"])
def test_offhost_backends_are_no_longer_refused_locally(tmp_path, monkeypatch, backend):
    """Issue #2924: the temporary local-only refusal is gone; the request
    reaches the backend (whose mandatory worker gate decides)."""
    reached: list[str] = []
    _stub_offhost_backends(monkeypatch, reached)
    request = _request(tmp_path, [_DECL], backend=backend)
    with pytest.raises(sim.SimError, match="reached backend"):
        sim.run_sim(str(request))
    assert reached == [backend]


def test_host_default_batch_multi_corner_reaches_the_batch_backend(
    tmp_path, monkeypatch
):
    reached: list[str] = []
    _stub_offhost_backends(monkeypatch, reached)
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    request = _request(tmp_path, [_DECL], corners={"temperature_c": [-40, 27, 125]})
    with pytest.raises(sim.SimError, match="reached backend"):
        sim.run_sim(str(request))
    assert reached == ["batch"]


# --------------------------------------------------------------------------- #
# Complex rawfile reader
# --------------------------------------------------------------------------- #


def _rawfile(freqs, nums, dens, *, flags="complex", points=None) -> str:
    n = len(freqs) if points is None else points
    lines = [
        "Title: stub",
        "Plotname: AC Analysis",
        f"Flags: {flags}",
        "No. Variables: 3",
        f"No. Points: {n}",
        "Variables:",
        "\t0\tfrequency\tfrequency",
        "\t1\tv(__klt_xfer_0_num)\tvoltage",
        "\t2\tv(__klt_xfer_0_den)\tvoltage",
        "Values:",
    ]
    for i, (f, a, b) in enumerate(zip(freqs, nums, dens, strict=True)):
        lines.append(f" {i}\t{f!r},0.0")
        lines.append(f"\t{a.real!r},{a.imag!r}")
        lines.append(f"\t{b.real!r},{b.imag!r}")
    return "\n".join(lines) + "\n"


def _rc_rawfile(**kwargs) -> str:
    freqs = [10 ** (k / 50) for k in range(0, 6 * 50 + 1)]
    nums = [_GAIN / (1 + 1j * f / _FC) for f in freqs]
    return _rawfile(freqs, nums, [1 + 0j] * len(freqs), **kwargs)


def test_reader_keeps_phase(tmp_path):
    path = tmp_path / "a.raw"
    path.write_text(_rawfile([1.0, 10.0], [3 - 4j, -1j], [1 + 0j, 2 + 0j]))
    raw = sim_raw.read_complex_ascii_rawfile(str(path))
    assert raw.names == ["frequency", "v(__klt_xfer_0_num)", "v(__klt_xfer_0_den)"]
    assert raw.column(1) == [3 - 4j, -1j]
    assert cmath.phase(raw.column(1)[1]) == pytest.approx(-math.pi / 2)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("Plotname:", "Notaplot:"),
        lambda t: t.replace("No. Variables: 3", "No. Variables: 4"),
        lambda t: t.replace("\t3.0,-4.0", "\tnot-a-number,1"),
        lambda t: t.rstrip("\n").rsplit("\n", 1)[0] + "\n",
        lambda t: t.replace("No. Points: 2", "No. Points: 3"),
    ],
)
def test_reader_rejects_malformed_files(tmp_path, mutate):
    path = tmp_path / "a.raw"
    path.write_text(mutate(_rawfile([1.0, 10.0], [3 - 4j, -1j], [1 + 0j, 2 + 0j])))
    with pytest.raises(sim_raw.RawFileError):
        sim_raw.read_complex_ascii_rawfile(str(path))


def test_reader_rejects_unreadable_path(tmp_path):
    with pytest.raises(sim_raw.RawFileError, match="could not read"):
        sim_raw.read_complex_ascii_rawfile(str(tmp_path / "missing.raw"))


def test_public_parser_still_returns_magnitudes(tmp_path):
    path = tmp_path / "a.raw"
    path.write_text(_rawfile([1.0], [3 - 4j], [1 + 0j]))
    assert sim.parse_ascii_rawfile(str(path))["points"] == [[1.0, 5.0, 1.0]]


# --------------------------------------------------------------------------- #
# Deck emission
# --------------------------------------------------------------------------- #


def _deck(tmp_path: Path, measurements_spec, **kwargs) -> list[str]:
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis={"kind": "ac", "args": "dec 10 1 1e6"},
        measurements_spec=measurements_spec,
        raw_path=None,
        **kwargs,
    )
    return deck_path.read_text().splitlines()


def test_deck_dumps_each_unique_pair_once_after_the_analysis(tmp_path):
    specs = [
        _DECL,
        {
            "name": "ugf",
            "transfer": {**_BODY, "num": "V(OUT)", "metric": "unity_crossing_hz"},
        },
        {"name": "g2", "transfer": {**_BODY, "den": "v(ref)"}},
    ]
    lines = _deck(tmp_path, specs)
    ac = lines.index("ac dec 10 1 1e6")
    assert lines.index("set filetype=ascii") < ac
    assert lines.count("let __klt_xfer_0_num = v(out)") == 1
    assert "let __klt_xfer_1_den = v(ref)" in lines
    write = lines.index(
        f"write {tmp_path / 'transfer_0.raw'} __klt_xfer_0_num __klt_xfer_0_den"
    )
    assert (
        ac < lines.index("let __klt_xfer_0_num = v(out)") < write < lines.index("quit")
    )
    assert not any("transfer_2" in line for line in lines)


def test_deck_without_transfer_is_unchanged(tmp_path):
    lines = _deck(tmp_path, [{"name": "v", "spice": ".meas ac v MAX vdb(out)"}])
    assert not any("xfer" in line or "filetype" in line for line in lines)


def test_save_mode_netlist_is_not_overridden(tmp_path):
    lines = _deck(tmp_path, [_DECL], save_mode="netlist")
    assert "save all" not in lines


# --------------------------------------------------------------------------- #
# run_sim with ngspice stubbed: grading and the response shape
# --------------------------------------------------------------------------- #


def _stub_ngspice(monkeypatch, *, raw: str | None, extra_raw: dict | None = None):
    real_which = shutil.which
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        lambda cmd, *a, **k: cmd if cmd == "ngspice" else real_which(cmd, *a, **k),
    )
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("No. of Data Rows : 5\n")
        for arg in cmd:
            if str(arg).endswith(".cir"):
                decks.append(Path(arg).read_text())
        if raw is not None:
            with open(os.path.join(cwd, "transfer_0.raw"), "w", encoding="utf-8") as h:
                h.write(raw)
        for name, text in (extra_raw or {}).items():
            with open(os.path.join(cwd, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


def _only_corner(report):
    (corner,) = report["corners"]
    return corner, {m["name"]: m for m in corner["measurements"]}


def test_metrics_are_computed_from_complex_samples_and_graded(tmp_path, monkeypatch):
    specs = [
        {"name": "pm", "transfer": _BODY, "limits": {"min": 80}},
        {
            "name": "ugf",
            "transfer": {**_BODY, "metric": "unity_crossing_hz"},
            "limits": {"min": 1e4},
        },
        {"name": "lf", "transfer": {**_BODY, "metric": "low_frequency_magnitude"}},
        {"name": "n", "transfer": {**_BODY, "metric": "unity_crossing_count"}},
    ]
    request = _request(tmp_path, specs)
    _stub_ngspice(monkeypatch, raw=_rc_rawfile())

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    # |T| = 1 at fc*sqrt(99) = 9949.87 Hz; PM = 180 - atan(sqrt(99)) = 95.7 deg.
    # The log-frequency interpolation error on a 50 pts/dec grid is far below
    # the 1e-3 relative tolerance used here.
    assert by_name["ugf"]["value"] == pytest.approx(_UGF, rel=1e-3)
    assert by_name["pm"]["value"] == pytest.approx(
        180.0 - math.degrees(math.atan(_UGF / _FC)), abs=0.05
    )
    assert by_name["lf"]["value"] == pytest.approx(_GAIN, rel=1e-3)
    assert by_name["n"]["value"] == 1
    assert by_name["pm"]["status"] == "pass"
    assert by_name["pm"]["margin"] == pytest.approx(by_name["pm"]["value"] - 80)
    assert by_name["ugf"]["status"] == "fail"  # 9.95 kHz < 10 kHz limit
    assert by_name["pm"]["transfer"] == _BODY
    assert report["status"] == "fail"
    assert corner["diagnostics"] == []


def test_failing_pm_limit(tmp_path, monkeypatch):
    request = _request(tmp_path, [{**_DECL, "limits": {"min": 100}}])
    _stub_ngspice(monkeypatch, raw=_rc_rawfile())
    report = sim.run_sim(str(request))
    _, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == "fail"
    assert report["status"] == "fail"


def test_unique_pair_is_dumped_and_computed_once(tmp_path, monkeypatch):
    specs = [
        _DECL,
        {
            "name": "ugf",
            "transfer": {**_BODY, "num": "V(Out)", "metric": "unity_crossing_hz"},
        },
    ]
    request = _request(tmp_path, specs)
    decks = _stub_ngspice(monkeypatch, raw=_rc_rawfile())
    calls = []
    real = sim_transfer_meas.sim_transfer.analyze_transfer
    monkeypatch.setattr(
        sim_transfer_meas.sim_transfer,
        "analyze_transfer",
        lambda *a: calls.append(1) or real(*a),
    )

    report = sim.run_sim(str(request))

    assert len(calls) == 1
    _, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == by_name["ugf"]["status"] == "pass"
    assert all(d.count("let __klt_xfer_0_num") <= 1 for d in decks)


def test_missing_vector_is_an_error_that_cannot_pass(tmp_path, monkeypatch):
    request = _request(tmp_path, [{**_DECL, "limits": {"min": 0}}])
    _stub_ngspice(monkeypatch, raw=None)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == "error"
    assert by_name["pm"]["value"] is None
    assert "transfer" not in by_name["pm"]
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["message"].startswith("[transfer_vector_missing] ")
    assert report["status"] != "pass"


def test_missing_vector_under_save_mode_netlist_is_diagnosed(tmp_path, monkeypatch):
    request = _request(tmp_path, [_DECL], options={"save_mode": "netlist"})
    _stub_ngspice(monkeypatch, raw=None)

    report = sim.run_sim(str(request))

    corner, _ = _only_corner(report)
    (diagnostic,) = corner["diagnostics"]
    assert diagnostic["message"].startswith("[transfer_vector_missing] ")
    assert "save_mode" in diagnostic["message"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("Plotname:", "Notaplot:"),
        lambda t: t.replace(" 0\t1.0,0.0", " 0\tx,0.0"),
        lambda t: "\n".join(t.split("\n")[:-4]) + "\n",
    ],
)
def test_malformed_raw_data_is_an_explicit_error(tmp_path, monkeypatch, mutate):
    request = _request(tmp_path, [_DECL])
    _stub_ngspice(monkeypatch, raw=mutate(_rc_rawfile()))

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == "error"
    assert corner["diagnostics"][0]["message"].startswith("[transfer_raw_invalid] ")


def test_zero_denominator_is_an_error(tmp_path, monkeypatch):
    freqs = [10.0, 100.0, 1000.0]
    raw = _rawfile(freqs, [1 + 0j] * 3, [0j] * 3)
    request = _request(tmp_path, [_DECL])
    _stub_ngspice(monkeypatch, raw=raw)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == "error"
    message = corner["diagnostics"][0]["message"]
    assert message.startswith("[transfer_metric_unavailable] ")
    assert "no_valid_samples" in message


def test_absent_crossing_is_an_error_but_count_is_zero(tmp_path, monkeypatch):
    freqs = [10.0, 100.0, 1000.0]
    raw = _rawfile(freqs, [5 + 0j] * 3, [1 + 0j] * 3)
    specs = [
        _DECL,
        {"name": "n", "transfer": {**_BODY, "metric": "unity_crossing_count"}},
    ]
    request = _request(tmp_path, specs)
    _stub_ngspice(monkeypatch, raw=raw)

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == "error"
    assert "no_falling_unity_crossing" in corner["diagnostics"][0]["message"]
    assert by_name["n"]["value"] == 0


def test_existing_scalar_forms_are_unaffected_by_a_transfer_neighbour(
    tmp_path, monkeypatch
):
    specs = [{"name": "peak", "spice": ".meas ac peak MAX vdb(out)"}, _DECL]
    request = _request(tmp_path, specs)
    _stub_ngspice(monkeypatch, raw=_rc_rawfile())

    report = sim.run_sim(str(request))

    _, by_name = _only_corner(report)
    assert by_name["peak"]["status"] == "error"  # stub log has no .meas value
    assert "transfer" not in by_name["peak"]
    assert by_name["pm"]["status"] == "pass"


# --------------------------------------------------------------------------- #
# Real ngspice
# --------------------------------------------------------------------------- #

real_ngspice = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice not installed"
)


@real_ngspice
@pytest.mark.parametrize("waveforms", [False, True])
def test_real_ngspice_rc_stage_matches_the_closed_form(tmp_path, waveforms):
    specs = [
        {
            "name": "ugf",
            "transfer": {**_BODY, "metric": "unity_crossing_hz"},
            "limits": {"min": 9.9e3, "max": 10e3},
        },
        {"name": "pm", "transfer": _BODY, "limits": {"min": 80}},
        {"name": "bad", "transfer": {**_BODY, "num": "v(nope)"}},
    ]
    request = _request(tmp_path, specs)
    doc = json.loads(request.read_text())
    doc["options"]["waveforms"] = waveforms
    request.write_text(json.dumps(doc))

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    # Interpolating dB and phase linearly in log10(f) between 50-pts/decade
    # samples: error well under 1e-3 relative on both.
    assert by_name["ugf"]["value"] == pytest.approx(_UGF, rel=1e-3)
    assert by_name["pm"]["value"] == pytest.approx(
        180.0 - math.degrees(math.atan(_UGF / _FC)), abs=0.05
    )
    assert by_name["ugf"]["status"] == by_name["pm"]["status"] == "pass"
    assert by_name["bad"]["status"] == "error"
    assert [d["message"][:25] for d in corner["diagnostics"]] == [
        "[transfer_vector_missing]"
    ]


@real_ngspice
def test_real_ngspice_save_mode_netlist_reports_unsaved_vector(tmp_path):
    request = _request(tmp_path, [_DECL], options={"save_mode": "netlist"})
    _write_body(tmp_path, extra=".save v(out)\n")

    report = sim.run_sim(str(request))

    corner, by_name = _only_corner(report)
    assert by_name["pm"]["status"] == "error"
    assert corner["diagnostics"][0]["message"].startswith("[transfer_vector_missing]")


# --------------------------------------------------------------------------- #
# All-crossing margin metrics (issue #3088)
# --------------------------------------------------------------------------- #

_PM_MIN = {**_BODY, "metric": "phase_margin_min_deg"}
_GM_MIN = {**_BODY, "metric": "gain_margin_min_db"}
_ENC = {**_BODY, "metric": "encirclement_count"}


def _conditionally_stable_rawfile() -> str:
    """|T| crosses 0 dB at 10 Hz (PM 80), rises again over 100 Hz and falls
    through 0 dB near 200 Hz where the phase has sagged to -170 (PM 10)."""
    freqs = [10 ** (k / 50) for k in range(0, 4 * 50 + 1)]
    nums = []
    for f in freqs:
        x = math.log10(f)
        mag_db = -20.0 * (x - 1.0) + 25.0 * math.exp(-(((x - 2.0) / 0.25) ** 2))
        phase = -100.0 - 70.0 * 0.5 * (1.0 + math.tanh((x - 1.5) / 0.1))
        nums.append(10 ** (mag_db / 20.0) * cmath.exp(1j * math.radians(phase)))
    return _rawfile(freqs, nums, [1 + 0j] * len(freqs))


def _coarse_rc_rawfile() -> str:
    """The RC stage sampled at 5 points/decade: too coarse to bracket."""
    freqs = [10 ** (k / 5) for k in range(0, 6 * 5 + 1)]
    nums = [_GAIN / (1 + 1j * f / _FC) for f in freqs]
    return _rawfile(freqs, nums, [1 + 0j] * len(freqs))


def _margin_report(tmp_path, monkeypatch, specs, raw):
    request = _request(tmp_path, specs)
    _stub_ngspice(monkeypatch, raw=raw)
    report = sim.run_sim(str(request))
    corner, by_name = _only_corner(report)
    return report, corner, by_name


def test_minimum_margin_fails_where_the_first_crossing_passes(tmp_path, monkeypatch):
    specs = [
        {"name": "pm_first", "transfer": _BODY, "limits": {"min": 45}},
        {"name": "pm_min", "transfer": _PM_MIN, "limits": {"min": 45}},
    ]
    report, corner, by_name = _margin_report(
        tmp_path, monkeypatch, specs, _conditionally_stable_rawfile()
    )

    assert by_name["pm_first"]["status"] == "pass"
    assert by_name["pm_min"]["status"] == "fail"
    assert by_name["pm_min"]["value"] == pytest.approx(10.0, abs=1.0)
    assert by_name["pm_min"]["margin"] < 0
    margins = by_name["pm_min"]["transfer"]["margins"]
    assert len(margins["phase_margins"]) == 3
    assert margins["ambiguity"] == []
    assert by_name["pm_min"]["transfer"]["verdict"] == "fail"
    assert report["status"] == "fail"
    assert corner["status"] == "fail"
    # The six existing tokens' entry shape is unchanged.
    assert set(by_name["pm_first"]["transfer"]) == {"num", "den", "metric"}


def test_clean_band_passes_and_encirclement_is_zero(tmp_path, monkeypatch):
    specs = [
        {"name": "pm", "transfer": _PM_MIN, "limits": {"min": 45}},
        {"name": "enc", "transfer": _ENC, "limits": {"max": 0}},
    ]
    report, _, by_name = _margin_report(tmp_path, monkeypatch, specs, _rc_rawfile())

    assert by_name["pm"]["status"] == "pass"
    assert by_name["pm"]["value"] == pytest.approx(95.7, abs=0.2)
    assert by_name["enc"]["status"] == "pass"
    assert by_name["enc"]["value"] == 0
    assert report["status"] == "pass"


def test_no_crossing_on_a_clean_band_is_vacuously_satisfied(tmp_path, monkeypatch):
    specs = [{"name": "gm", "transfer": _GM_MIN, "limits": {"min": 6}}]
    report, corner, by_name = _margin_report(
        tmp_path, monkeypatch, specs, _rc_rawfile()
    )

    assert by_name["gm"]["value"] is None
    assert by_name["gm"]["status"] == "pass"
    assert by_name["gm"]["transfer"]["margins"]["reasons"] == {
        "gain_margin_min_db": "no_phase_crossing"
    }
    assert corner["diagnostics"] == []
    # Nothing was actually compared with the limit, so coverage -- and with it
    # the run's own verdict -- reports "not_checked" rather than a clean pass.
    assert report["status"] == "not_checked"


def test_no_crossing_without_limits_is_an_unavailable_error(tmp_path, monkeypatch):
    specs = [{"name": "gm", "transfer": _GM_MIN}]
    _, corner, by_name = _margin_report(tmp_path, monkeypatch, specs, _rc_rawfile())

    assert by_name["gm"]["status"] == "error"
    assert "[transfer_metric_unavailable]" in corner["diagnostics"][0]["message"]


def test_ambiguous_band_cannot_pass_a_min_limit_and_is_not_fail(
    tmp_path, monkeypatch, capsys
):
    from klayout_tools.cli import main

    specs = [{"name": "pm", "transfer": _PM_MIN, "limits": {"min": 45}}]
    request = _request(tmp_path, specs)
    _stub_ngspice(monkeypatch, raw=_coarse_rc_rawfile())

    exit_code = main(["sim", str(request), "--format", "json"])

    data = json.loads(capsys.readouterr().out)
    (corner,) = data["corners"]
    (pm,) = corner["measurements"]
    assert pm["value"] == pytest.approx(95.7, abs=2.0)  # would pass 45 ...
    assert pm["status"] == "inconclusive"  # ... but is never a pass or a fail
    assert pm["transfer"]["verdict"] == "ambiguous"
    assert "crossing_not_bracketed" in pm["transfer"]["margins"]["ambiguity"]
    (diag,) = [d for d in corner["diagnostics"] if d["code"] == "margin_ambiguous"]
    assert diag["severity"] == "warning"
    assert "crossing_not_bracketed" in diag["message"]
    assert corner["status"] == "inconclusive"
    assert data["status"] == "inconclusive"
    assert data["inconclusive"] == 1
    assert {"reason": "margin_ambiguous"}.items() <= data["coverage"]["skipped"][
        0
    ].items()
    assert exit_code == 4


def test_violation_on_an_ambiguous_band_is_still_a_fail(tmp_path, monkeypatch):
    specs = [{"name": "pm", "transfer": _PM_MIN, "limits": {"min": 120}}]
    report, corner, by_name = _margin_report(
        tmp_path, monkeypatch, specs, _coarse_rc_rawfile()
    )

    assert by_name["pm"]["status"] == "fail"
    assert by_name["pm"]["transfer"]["verdict"] == "fail"
    assert corner["diagnostics"] == []
    assert report["status"] == "fail"


def test_indeterminate_encirclement_is_never_zero(tmp_path, monkeypatch):
    specs = [{"name": "enc", "transfer": _ENC, "limits": {"max": 0}}]
    report, corner, by_name = _margin_report(
        tmp_path, monkeypatch, specs, _coarse_rc_rawfile()
    )

    assert by_name["enc"]["value"] is None
    assert by_name["enc"]["status"] == "inconclusive"
    assert by_name["enc"]["transfer"]["margins"]["encirclement"] == "indeterminate"
    assert corner["diagnostics"][0]["code"] == "margin_ambiguous"
    assert report["status"] == "inconclusive"


def test_ambiguity_does_not_downgrade_a_report_only_margin(tmp_path, monkeypatch):
    specs = [{"name": "pm", "transfer": _PM_MIN}]
    report, corner, by_name = _margin_report(
        tmp_path, monkeypatch, specs, _coarse_rc_rawfile()
    )

    assert by_name["pm"]["status"] == "pass"
    assert corner["diagnostics"] == []
    assert report["status"] == "pass"


@pytest.mark.parametrize("backend", ["batch", "remote"])
def test_margin_tokens_are_refused_off_host_like_the_others(
    tmp_path, monkeypatch, backend
):
    request = _request(tmp_path, [{"name": "pm", "transfer": _PM_MIN}], backend=backend)
    monkeypatch.setattr(
        sim.subprocess, "run", lambda *a, **k: pytest.fail("must not launch")
    )
    with pytest.raises(
        sim.SimError, match=f"not yet supported for backend '{backend}'"
    ):
        sim.run_sim(str(request))
