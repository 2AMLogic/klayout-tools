"""Tests for ``analysis.kind: "twoport"`` -- first-class S-parameters from
``klt sim`` (issue #3338)."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_batch, sim_twoport, touchstone

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

real_ngspice = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice not installed"
)

_PORTS = [
    {"p": "in", "n": "0", "z0": 50},
    {"p": "out", "n": "0", "z0": 50},
]
_FREQ = {"sweep": "dec", "points": 5, "start": 1e6, "stop": 1e8}


def _analysis(**overrides):
    return {"kind": "twoport", "ports": _PORTS, "freq": _FREQ, **overrides}


def _request(tmp_path: Path, body: str, analysis=None, **extra) -> Path:
    (tmp_path / "body.spice").write_text(body)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {"netlist": "body.spice", "analysis": analysis or _analysis(), **extra}
        )
    )
    return path


def _run(tmp_path: Path, body: str, analysis=None, **extra):
    return sim.run_sim(
        str(_request(tmp_path, body, analysis, **extra)), backend="local"
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_request_form_resolves_to_an_ac_analysis():
    resolved = sim_twoport.resolve_analysis(_analysis())
    assert resolved["kind"] == "ac"
    assert resolved["args"] == "dec 5 1000000.0 100000000.0"
    assert sim_twoport.is_twoport(resolved)
    assert resolved["twoport"]["ports"][0] == {"p": "in", "n": "0", "z0": 50.0}


def test_other_analyses_pass_through_untouched():
    ac = {"kind": "ac", "args": "dec 10 1 1e6"}
    assert sim_twoport.resolve_analysis(ac) is ac
    assert not sim_twoport.is_twoport(ac)


@pytest.mark.parametrize(
    ("analysis", "match"),
    [
        ({"kind": "twoport", "freq": _FREQ}, "exactly two"),
        ({"kind": "twoport", "ports": _PORTS[:1], "freq": _FREQ}, "exactly two"),
        ({"kind": "twoport", "ports": _PORTS}, "analysis.freq"),
        (_analysis(args="dec 1 1 10"), "unknown key"),
        (
            _analysis(ports=[{**_PORTS[0], "z0": 0}, {**_PORTS[1], "z0": 0}]),
            "z0 must be positive",
        ),
        (
            _analysis(ports=[{**_PORTS[0], "z0": -5}, {**_PORTS[1], "z0": -5}]),
            "z0 must be positive",
        ),
        (
            _analysis(ports=[{"p": "in", "n": "0"}, _PORTS[1]]),
            "z0 is required",
        ),
        (
            _analysis(ports=[{**_PORTS[0], "z0": "50"}, _PORTS[1]]),
            "z0 must be a number",
        ),
        (
            _analysis(ports=[{**_PORTS[0], "z0": 75}, _PORTS[1]]),
            "differing z0",
        ),
        (
            _analysis(ports=[{"p": "in", "n": "in", "z0": 50}, _PORTS[1]]),
            "same node",
        ),
        (
            _analysis(ports=[{"p": "0", "n": "in", "z0": 50}, _PORTS[1]]),
            "must not be ground",
        ),
        (
            _analysis(ports=[{"p": "a b", "n": "0", "z0": 50}, _PORTS[1]]),
            "node name",
        ),
        (
            _analysis(ports=[_PORTS[0], {**_PORTS[1], "p": "IN"}]),
            "share the signal node",
        ),
        (_analysis(freq={**_FREQ, "sweep": "log"}), "sweep must be one of"),
        (_analysis(freq={**_FREQ, "points": 0}), "positive integer"),
        (_analysis(freq={**_FREQ, "start": 0}), "start must be positive"),
        (_analysis(freq={**_FREQ, "stop": 1e5}), "stop must exceed start"),
        (
            _analysis(freq={"sweep": "lin", "points": 1, "start": 1e6, "stop": 2e6}),
            "single-point lin sweep",
        ),
        (_analysis(freq={**_FREQ, "extra": 1}), "unknown key"),
    ],
)
def test_malformed_forms_are_rejected_before_launch(analysis, match):
    with pytest.raises(sim.SimError, match=match):
        sim_twoport.resolve_analysis(analysis)


def test_mixed_with_analysis_steps_is_rejected(tmp_path):
    request = _request(
        tmp_path,
        "R1 in out 100\n",
        analysis_steps=[{"name": "s", "kind": "ac", "args": "dec 1 1 10"}],
    )
    with pytest.raises(sim.SimError, match="both 'analysis' and 'analysis_steps'"):
        sim.run_sim(str(request), backend="local", dry_run=True)


def test_measurements_are_rejected(tmp_path):
    request = _request(
        tmp_path,
        "R1 in out 100\n",
        measurements=[{"name": "m", "expr": "v(out)"}],
    )
    with pytest.raises(sim.SimError, match="takes no measurements"):
        sim.run_sim(str(request), backend="local")


def test_waveforms_are_rejected(tmp_path):
    request = _request(tmp_path, "R1 in out 100\n", options={"waveforms": True})
    with pytest.raises(sim.SimError, match="not supported with analysis.kind"):
        sim.run_sim(str(request), backend="local")


def test_xyce_is_rejected():
    resolved = sim_twoport.resolve_analysis(_analysis())
    with pytest.raises(sim.SimError, match="xyce"):
        sim_twoport.refuse_xyce(resolved, "xyce")
    sim_twoport.refuse_xyce(resolved, "ngspice")


# --------------------------------------------------------------------------- #
# Deck and reduction
# --------------------------------------------------------------------------- #


def test_deck_and_control_lines():
    resolved = sim_twoport.resolve_analysis(
        _analysis(
            ports=[
                {"p": "in", "n": "0", "z0": 50},
                {"p": "out", "n": "ref", "z0": 50},
            ]
        )
    )
    assert sim_twoport.deck_lines(resolved) == [
        "Rkltp1 in klt_tp1_x 50.0",
        "Vkltp1 klt_tp1_x 0 DC 0 AC 0",
        "Rkltp2 out klt_tp2_x 50.0",
        "Vkltp2 klt_tp2_x ref DC 0 AC 0",
    ]
    control = sim_twoport.control_lines(resolved, "/c")
    assert control == [
        "set filetype=ascii",
        "alter Vkltp1 ac = 1",
        "alter Vkltp2 ac = 0",
        "setplot new",
        "ac dec 5 1000000.0 100000000.0",
        "let __klt_tp_0 = v(in)",
        "let __klt_tp_1 = v(out) - v(ref)",
        "write /c/twoport_1.raw __klt_tp_0 __klt_tp_1",
        "alter Vkltp1 ac = 0",
        "alter Vkltp2 ac = 1",
        "setplot new",
        "ac dec 5 1000000.0 100000000.0",
        "let __klt_tp_0 = v(in)",
        "let __klt_tp_1 = v(out) - v(ref)",
        "write /c/twoport_2.raw __klt_tp_0 __klt_tp_1",
    ]
    assert sim_twoport.deck_lines({"kind": "ac", "args": "dec 1 1 10"}) == []


def test_reduce_s_of_a_matched_through():
    # A lossless 0-ohm through between matched ports: V1 = V2 = 0.5 per drive.
    half = [0.5 + 0j]
    s = sim_twoport.reduce_s((half, half), (half, half))
    assert s == {"s11": [0j], "s21": [1 + 0j], "s12": [1 + 0j], "s22": [0j]}


# --------------------------------------------------------------------------- #
# Extraction failure semantics (rawfiles written by hand)
# --------------------------------------------------------------------------- #


def _rawfile(plot: str, rows, names=("frequency", "v(__klt_tp_0)", "v(__klt_tp_1)")):
    head = [
        "Title: t",
        "Date: d",
        f"Plotname: {plot}",
        "Flags: complex",
        f"No. Variables: {len(names)}",
        f"No. Points: {len(rows)}",
        "Variables:",
        *(f"\t{i}\t{name}\tfrequency" for i, name in enumerate(names)),
        "Values:",
    ]
    body = []
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            prefix = f" {i}\t" if j == 0 else "\t"
            body.append(f"{prefix}{value.real!r},{value.imag!r}")
    return "\n".join(head + body) + "\n"


def _write_drives(corner_dir: Path, first: str | None, second: str | None):
    for index, text in enumerate((first, second)):
        if text is not None:
            (corner_dir / f"twoport_{index + 1}.raw").write_text(text)


def _resolved():
    return sim_twoport.resolve_analysis(_analysis())


_GOOD_ROWS = [(1e6 + 0j, 0.5 + 0j, 0.25 + 0j), (2e6 + 0j, 0.5 + 0j, 0.25 + 0j)]


def test_good_rawfiles_reduce_and_write_the_s2p(tmp_path):
    good = _rawfile("AC Analysis", _GOOD_ROWS)
    _write_drives(tmp_path, good, good)
    outcome = sim_twoport.extract_corner(_resolved(), str(tmp_path))
    assert outcome.diagnostics == []
    assert outcome.s["s11"] == [0j, 0j]
    assert outcome.s["s21"] == [0.5 + 0j, 0.5 + 0j]
    text = Path(outcome.s2p_path).read_text()
    assert "# HZ S RI R 50.0" in text
    assert len(text.splitlines()) == 4 + 1 + 2
    fields = sim_twoport.corner_fields(_resolved(), outcome)["twoport"]
    assert fields["points"] == 2
    assert fields["s_parameters"]["s21"]["real"] == [0.5, 0.5]


@pytest.mark.parametrize(
    ("first", "second", "code"),
    [
        (None, "ok", "twoport_missing"),
        ("ok", None, "twoport_missing"),
        ("ok", "garbage", "twoport_raw_invalid"),
        ("ok", "constants", "twoport_raw_invalid"),
        ("ok", "novector", "twoport_missing"),
        ("ok", "nan", "twoport_non_finite"),
        ("ok", "shortaxis", "twoport_axis_mismatch"),
    ],
)
def test_a_dead_or_damaged_excitation_is_an_error(tmp_path, first, second, code):
    ok = _rawfile("AC Analysis", _GOOD_ROWS)
    texts = {
        "ok": ok,
        None: None,
        "garbage": "not a rawfile\n",
        "constants": _rawfile("Constants", _GOOD_ROWS),
        "novector": _rawfile(
            "AC Analysis", _GOOD_ROWS, names=("frequency", "v(__klt_tp_0)", "v(x)")
        ),
        "nan": _rawfile(
            "AC Analysis",
            [(1e6 + 0j, 0.5 + 0j, complex(math.nan, 0)), _GOOD_ROWS[1]],
        ),
        "shortaxis": _rawfile("AC Analysis", _GOOD_ROWS[:1]),
    }
    _write_drives(tmp_path, texts[first], texts[second])
    outcome = sim_twoport.extract_corner(_resolved(), str(tmp_path))
    (diag,) = outcome.diagnostics
    assert diag["severity"] == "error"
    assert f"[{code}]" in diag["message"]
    assert outcome.s2p_path is None
    assert not (tmp_path / "twoport.s2p").exists()
    # A failed corner still reports a (empty) shape, never stale arrays.
    fields = sim_twoport.corner_fields(_resolved(), outcome)["twoport"]
    assert fields["points"] == 0
    assert fields["s_parameters"]["s11"] == {"real": [], "imag": []}


def test_extraction_is_inert_for_other_analyses(tmp_path):
    ac = {"kind": "ac", "args": "dec 1 1 10"}
    outcome = sim_twoport.extract_corner(ac, str(tmp_path))
    assert outcome.diagnostics == []
    assert sim_twoport.corner_fields(ac, outcome) == {}
    assert sim_twoport.artifact_fields(ac, outcome) == {}


# --------------------------------------------------------------------------- #
# Real ngspice, local backend
# --------------------------------------------------------------------------- #

_Z0 = 50.0


def _s(corner, name):
    block = corner["twoport"]["s_parameters"][name]
    return [complex(r, i) for r, i in zip(block["real"], block["imag"], strict=True)]


@real_ngspice
def test_series_r_matches_the_closed_form(tmp_path):
    r = 100.0
    report = _run(tmp_path, f"R1 in out {r}\n")
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert corner["twoport"]["points"] == 5 * 2 + 1
    expected_s11 = r / (r + 2 * _Z0)
    expected_s21 = 2 * _Z0 / (r + 2 * _Z0)
    for name, expected in (
        ("s11", expected_s11),
        ("s22", expected_s11),
        ("s21", expected_s21),
        ("s12", expected_s21),
    ):
        assert _s(corner, name) == pytest.approx(
            [expected] * corner["twoport"]["points"], abs=1e-9
        )
    s2p = Path(corner["artifacts"]["twoport_s2p"])
    assert s2p.is_file()
    data = [
        line.split()
        for line in s2p.read_text().splitlines()
        if not line.startswith(("!", "#"))
    ]
    assert len(data) == corner["twoport"]["points"]
    assert float(data[0][1]) == pytest.approx(expected_s11)
    assert float(data[0][3]) == pytest.approx(expected_s21)


@real_ngspice
def test_shunt_r_matches_the_closed_form(tmp_path):
    r = 25.0
    report = _run(tmp_path, f"R1 in 0 {r}\nR2 in out 0.001\n")
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    # Shunt R to ground across a (near-)through: y = z0 / R.
    y = _Z0 / r
    s11 = -y / (2 + y)
    s21 = 2 / (2 + y)
    assert _s(corner, "s11")[0] == pytest.approx(s11, abs=1e-4)
    assert _s(corner, "s21")[0] == pytest.approx(s21, abs=1e-4)


@real_ngspice
def test_complex_rc_lowpass_matches_the_closed_form(tmp_path):
    # Series R then shunt C: ABCD -> S with z0 = 50, tolerance 1e-6 absolute.
    r, c = 50.0, 1e-9
    report = _run(tmp_path, f"R1 in out {r}\nC1 out 0 {c}\n")
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    freqs = corner["twoport"]["frequency_hz"]
    for index, f in enumerate(freqs):
        w = 2 * math.pi * f
        a, b, cc, d = 1 + 1j * w * c * r, r, 1j * w * c, 1
        den = a + b / _Z0 + cc * _Z0 + d
        assert _s(corner, "s21")[index] == pytest.approx(2 / den, abs=1e-6)
        assert _s(corner, "s11")[index] == pytest.approx(
            (a + b / _Z0 - cc * _Z0 - d) / den, abs=1e-6
        )
        assert _s(corner, "s12")[index] == pytest.approx(
            2 * (a * d - b * cc) / den, abs=1e-6
        )


@real_ngspice
def test_single_lin_point(tmp_path):
    freq = {"sweep": "lin", "points": 1, "start": 1e6, "stop": 1e6}
    report = _run(tmp_path, "R1 in out 100\n", _analysis(freq=freq))
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert corner["twoport"]["frequency_hz"] == [1e6]


@real_ngspice
def test_lin_sweep_axis(tmp_path):
    freq = {"sweep": "lin", "points": 4, "start": 1e6, "stop": 4e6}
    report = _run(tmp_path, "R1 in out 100\n", _analysis(freq=freq))
    (corner,) = report["corners"]
    assert corner["twoport"]["frequency_hz"] == pytest.approx([1e6, 2e6, 3e6, 4e6])


@real_ngspice
def test_killed_second_excitation_is_an_error(tmp_path, monkeypatch):
    original = sim_twoport.control_lines

    def drop_second(analysis, corner_dir):
        lines = original(analysis, corner_dir)
        # Cut the deck after the first excitation's `write`.
        first_write = next(
            i for i, line in enumerate(lines) if line.startswith("write")
        )
        return lines[: first_write + 1]

    monkeypatch.setattr(sim_twoport, "control_lines", drop_second)
    report = _run(tmp_path, "R1 in out 100\n")
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    assert any("twoport_missing" in d["message"] for d in corner["diagnostics"])
    assert corner["artifacts"]["twoport_s2p"] is None


@real_ngspice
def test_existing_ac_requests_are_unchanged(tmp_path):
    (tmp_path / "body.spice").write_text(
        "V1 in 0 DC 0 AC 1\nR1 in out 1k\nC1 out 0 1n\n"
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "ac", "args": "dec 5 10 1e5"},
            }
        )
    )
    report = sim.run_sim(str(request), backend="local")
    (corner,) = report["corners"]
    assert "twoport" not in corner
    assert "twoport_s2p" not in corner["artifacts"]


# --------------------------------------------------------------------------- #
# Batch: runner gate and artifact install
# --------------------------------------------------------------------------- #


def test_twoport_is_a_registered_mandatory_runner_feature():
    request = {"analysis": _analysis()}
    assert sim_batch._request_features(request) == ["twoport"]
    assert "twoport" in sim_batch._MANDATORY_RUNNER_FEATURES
    assert sim_batch._request_features({"analysis": {"kind": "ac", "args": ""}}) == []
    assert sim_batch._missing_runner_features(["twoport"], "0.7.0") == ["twoport"]
    assert sim_batch._missing_runner_features(["twoport"], "0.7.1") == []


@real_ngspice
def test_batch_artifact_install_returns_the_same_s2p(tmp_path):
    """Mocked batch round trip: the worker's artifacts tree is collected and
    installed like a fleet job's; the sidecar reads back identical."""
    (tmp_path / "local").mkdir()
    local = _run(tmp_path / "local", "R1 in out 100\n")["corners"][0]
    local_path = Path(local["artifacts"]["twoport_s2p"])

    collected = tmp_path / "collected"
    worker_dir = collected / "artifacts" / "c0"
    worker_dir.mkdir(parents=True)
    shutil.copy(local_path, worker_dir / "twoport.s2p")
    corner = {
        "artifacts": {
            "log": None,
            "twoport_s2p": "/job/outputs/artifacts/c0/twoport.s2p",
        }
    }
    installed = tmp_path / "installed"
    sim_batch._install_batch_artifacts(
        [corner], collected_dir=str(collected), artifacts_dir=str(installed)
    )
    rewritten = Path(corner["artifacts"]["twoport_s2p"])
    assert rewritten == installed / "c0" / "twoport.s2p"
    assert rewritten.read_text() == local_path.read_text()


# --------------------------------------------------------------------------- #
# Shared Touchstone formatter
# --------------------------------------------------------------------------- #


def test_format_s2p_orders_columns_s11_s21_s12_s22():
    text = touchstone.format_s2p(
        [1e9], [1 + 2j], [3 + 4j], [5 + 6j], [7 + 8j], 50.0, comments=["hello"]
    )
    assert text.splitlines() == [
        "! hello",
        "# HZ S RI R 50.0",
        "1000000000.0 1.0 2.0 3.0 4.0 5.0 6.0 7.0 8.0",
    ]
