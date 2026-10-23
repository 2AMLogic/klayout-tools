"""Tests for ``analysis.linearize`` on a scalar ``tran`` (issue #4014).

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

from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)

_RC = "V1 in 0 SIN(0 1 1k)\nR1 in out 1k\nC1 out 0 159.155n\n"
_EXPR = "mean(v(out)*cos(2*pi*1k*time))*2"


def _control(analysis: dict, raw_path: str | None = None) -> list[str]:
    return sim._corner_deck_control_lines(
        point=SimpleNamespace(supply_v={}, params={}),
        analysis=analysis,
        measurements_spec=[{"name": "a", "expr": _EXPR, "unit": "V"}],
        raw_path=raw_path,
        osdi_preload=(),
    )


def _request(tmp_path: Path, analysis: dict, **extra) -> Path:
    (tmp_path / "body.spice").write_text(_RC)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": analysis,
                "measurements": [{"name": "a", "expr": _EXPR, "unit": "V"}],
                "backend": "local",
                **extra,
            }
        )
    )
    return path


def test_linearize_follows_write_and_precedes_expr_lets():
    lines = _control(
        {"kind": "tran", "args": "5u 5m", "linearize": True}, raw_path="/x/w.raw"
    )
    assert lines.index("tran 5u 5m") < lines.index("write /x/w.raw")
    assert lines.index("write /x/w.raw") < lines.index("linearize")
    assert lines.index("linearize") < lines.index(f"let a = {_EXPR}")


def test_deck_is_unchanged_without_linearize():
    base = {"kind": "tran", "args": "5u 5m"}
    assert "linearize" not in _control(base)
    assert _control({**base, "linearize": False}) == _control(base)


@pytest.mark.parametrize(
    "analysis, match",
    [
        ({"kind": "ac", "args": "dec 5 1 1k", "linearize": True}, "requires.*tran"),
        ({"kind": "tran", "args": "5u 5m", "linearize": "yes"}, "boolean"),
    ],
)
def test_bad_linearize_is_refused(tmp_path, analysis, match):
    with pytest.raises(sim.SimError, match=match):
        sim.run_sim(str(_request(tmp_path, analysis)))


def test_linearize_refused_with_analysis_steps(tmp_path):
    request = _request(
        tmp_path,
        {"kind": "tran", "args": "5u 5m"},
        analysis_steps=[{"kind": "tran", "args": "5u 5m", "linearize": True}],
    )
    with pytest.raises(sim.SimError, match="analysis_steps.*linearize"):
        sim.run_sim(str(request))


def test_linearize_refused_for_xyce(tmp_path):
    request = _request(
        tmp_path,
        {"kind": "tran", "args": "5u 5m", "linearize": True},
        engine="xyce",
    )
    with pytest.raises(sim.SimError, match="linearize.*xyce"):
        sim.run_sim(str(request))


@pytest.mark.skipif(not HAVE_NGSPICE, reason="ngspice is not installed")
def test_real_ngspice_fourier_sum_after_linearize(tmp_path):
    values = {}
    for flag in (False, True):
        sub = tmp_path / str(flag)
        sub.mkdir()
        analysis = {"kind": "tran", "args": "5u 5m", "linearize": flag}
        report = sim.run_sim(
            str(_request(sub, analysis, options={"keep_artifacts": True})),
            artifacts_dir=str(sub / "a"),
        )
        assert report["status"] == "pass", [c["diagnostics"] for c in report["corners"]]
        (corner,) = report["corners"]
        values[flag] = corner["measurements"][0]["value"]
        deck = Path(corner["artifacts"]["deck"]).read_text()
        assert ("linearize" in deck.splitlines()) is flag
    assert values[True] != pytest.approx(values[False], abs=1e-6)
