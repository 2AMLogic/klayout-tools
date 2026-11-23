"""Tests for ``analysis.post_commands`` on a scalar ``tran`` (issue #6080).

Unit tests need no simulator; the integration test runs the real
``ngspice -b`` (one corner, local) and skips without it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from klayout_tools import sim, sim_post_commands
from klayout_tools import sim_batch_contract_spec as spec

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)

_TWO_TONE = "Va a 0 SIN(0 1 10k)\nVb b 0 SIN(0 1 12k)\nRa a 0 1k\nRb b 0 1k\n"
_POST = ["linearize v(a) v(b)", "fft v(a) v(b)"]
_TRAN = {"kind": "tran", "args": "1u 2m"}
_EXPR = {"name": "m", "expr": "mag(a[20])", "unit": "V"}


def _control(analysis: dict, raw_path: str | None = None) -> list[str]:
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=[_EXPR],
        raw_path=raw_path,
        osdi_preload=(),
    )


def _request(tmp_path: Path, analysis: dict, measurements=None, **extra) -> Path:
    (tmp_path / "body.spice").write_text(_TWO_TONE)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": analysis,
                "measurements": measurements if measurements is not None else [_EXPR],
                "backend": "local",
                **extra,
            }
        )
    )
    return path


def test_post_commands_follow_linearize_and_precede_expr_lets():
    lines = _control(
        {**_TRAN, "linearize": True, "post_commands": _POST}, raw_path="/x/w.raw"
    )
    order = [
        "tran 1u 2m",
        "write /x/w.raw",
        "linearize",
        *_POST,
        f"let m = {_EXPR['expr']}",
    ]
    assert [lines.index(item) for item in order] == sorted(
        lines.index(item) for item in order
    )
    assert lines[lines.index("linearize") + 1 : lines.index("linearize") + 3] == _POST


def test_deck_is_unchanged_without_post_commands():
    assert _control(_TRAN) == [
        line
        for line in _control({**_TRAN, "post_commands": _POST})
        if line not in _POST
    ]


@pytest.mark.parametrize(
    "post, match",
    [
        ("fft v(a)", "must be a list"),
        ([], "must not be empty"),
        ([7], r"post_commands\[0\] must be a string"),
        (["fft v(a)", "unknown"], r"post_commands\[1\] must start with"),
        (["fft v(a)\n"], r"post_commands\[0\] must be a single"),
        (["fft v(a);"], r"post_commands\[0\] must be a single"),
        ([""], r"post_commands\[0\] must start with"),
        (["fftx v(a)"], r"post_commands\[0\] must start with"),
    ],
)
def test_bad_post_commands_are_refused(post, match):
    analysis = {**_TRAN, "post_commands": post}
    with pytest.raises(sim.SimError, match=match):
        sim_post_commands.validate(analysis, [_EXPR])


@pytest.mark.parametrize("verb", ["linearize", "fft"])
@pytest.mark.parametrize(
    "args", ["v(a) >", "v(a) <", "v(a) ?", "v()", "v(a)\v", "v(a)\x00"]
)
def test_invalid_waveform_arguments_name_the_entry(verb, args):
    analysis = {**_TRAN, "post_commands": ["linearize", f"{verb} {args}"]}
    with pytest.raises(sim.SimError, match=r"analysis\.post_commands\[1\].*waveform"):
        sim_post_commands.validate(analysis, [_EXPR])


@pytest.mark.parametrize(
    "command",
    [
        "fft",
        "fft (v(a)",
        "fft v(a) +",
        "fft v(a))",
        "fft mag()",
        "fft v(a)v(b)",
        "fft unknown(v(a))",
        "fft unknown (v(a))",
        "linearize v(a,b)",
    ],
)
def test_malformed_or_unsupported_expressions_are_refused(command):
    with pytest.raises(sim.SimError, match=r"post_commands\[0\].*waveform"):
        sim_post_commands.validate({**_TRAN, "post_commands": [command]}, [_EXPR])


@pytest.mark.parametrize(
    "command",
    [
        "linearize",
        "linearize V(x1.out) i(V1) 3 time v1#branch @m1[id]",
        "fft V(a, b) mag(v(a) - v(b))",
        "fft (v(a) + i(V1)) / 2",
        "fft -v(a) * 2e-3 + .5m",
        "fft REAL(MAG(v(x1.out)))",
        "fft @m1[id] va#branch",
    ],
)
def test_supported_waveform_arguments_are_preserved(command):
    analysis = {**_TRAN, "post_commands": [f" \t{command}  "]}
    sim_post_commands.validate(analysis, [_EXPR])
    assert sim_post_commands.control_lines(analysis) == [command]


def test_post_commands_refused_on_non_tran(tmp_path):
    analysis = {"kind": "ac", "args": "dec 5 1 1k", "post_commands": _POST}
    with pytest.raises(sim.SimError, match='requires analysis.kind "tran"'):
        sim.run_sim(str(_request(tmp_path, analysis)))


def test_post_commands_refused_with_analysis_steps(tmp_path):
    request = _request(
        tmp_path,
        _TRAN,
        analysis_steps=[{**_TRAN, "post_commands": _POST}],
    )
    with pytest.raises(sim.SimError, match=r"analysis_steps\[0\]\.post_commands"):
        sim.run_sim(str(request))


def test_post_commands_refused_for_xyce(tmp_path):
    request = _request(tmp_path, {**_TRAN, "post_commands": _POST}, engine="xyce")
    with pytest.raises(sim.SimError, match="post_commands.*xyce"):
        sim.run_sim(str(request))


def test_post_commands_refused_with_transient_plot_measurements(tmp_path):
    crossings = {"name": "c", "crossings": {"signal": "v(a)", "level": 0.0}}
    request = _request(
        tmp_path, {**_TRAN, "post_commands": _POST}, measurements=[_EXPR, crossings]
    )
    with pytest.raises(sim.SimError, match="'c' cannot be combined"):
        sim.run_sim(str(request))


def test_post_commands_gate_requires_runner_0_9_3():
    assert "post_commands" in spec._MANDATORY_RUNNER_FEATURES
    declared = {"analysis": {**_TRAN, "post_commands": _POST}}
    assert spec._request_features(declared) == ["post_commands"]
    assert spec._request_features({"analysis": _TRAN}) == []
    assert spec._missing_runner_features(["post_commands"], "0.9.0") == [
        "post_commands"
    ]
    assert spec._missing_runner_features(["post_commands"], "0.9.3") == []


@pytest.mark.skipif(not HAVE_NGSPICE, reason="ngspice is not installed")
@pytest.mark.parametrize(
    ("post", "readout"),
    [
        (_POST, "mag(a[20])"),
        (
            ["linearize", "fft v(a) v(b) mag(v(a) - v(b))"],
            'mag("mag(v(a) - v(b))"[20])',
        ),
    ],
)
def test_real_ngspice_reads_spectrum_bin_after_fft(tmp_path, post, readout):
    analysis = {**_TRAN, "post_commands": post}
    measurements = [
        {"name": name, "expr": f"mag({node}[{index}])", "unit": "V"}
        for node, index, name in [
            ("a", 20, "a10k"),
            ("a", 24, "a12k"),
            ("b", 20, "b10k"),
            ("b", 24, "b12k"),
        ]
    ]
    measurements.append({"name": "readout", "expr": readout, "unit": "V"})
    report = sim.run_sim(
        str(
            _request(tmp_path, analysis, measurements, options={"keep_artifacts": True})
        ),
        artifacts_dir=str(tmp_path / "a"),
    )
    assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
    (corner,) = report["corners"]
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values["readout"] > 0
    assert values["a10k"] > 10 * values["a12k"] > 0
    assert values["b12k"] > 10 * values["b10k"] > 0
    deck = Path(corner["artifacts"]["deck"]).read_text().splitlines()
    assert deck.index(post[-1]) < deck.index("let a10k = mag(a[20])")
