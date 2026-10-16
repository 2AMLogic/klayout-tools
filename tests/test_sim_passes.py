"""Tests for ``analysis.passes`` -- bounded multi-pass AC acquisition through
``klt sim`` (issue #3159).

Validation and deck-shape tests need no engine; the ``real_ngspice`` tests run
a two-source resistive/capacitive summing node whose complex answer is known in
closed form.
"""

from __future__ import annotations

import cmath
import json
import math
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_passes, sim_raw

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

real_ngspice = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice not installed"
)

# v(o) = (Vac_a / R1 + Vac_b / R2) / (1/R1 + 1/R2 + 1/R3 + j w C)
_R1, _R2, _R3, _C = 1e3, 2e3, 1e3, 1e-7
_ARGS = "dec 5 10 1e5"


def _alter(a: float, b: float) -> list[dict]:
    return [
        {"target": "VA", "value": a},
        {"target": "VB", "value": b},
    ]


_PASS_A = {"name": "inj_a", "alter": _alter(1, 0)}
_PASS_B = {"name": "inj_b", "alter": _alter(0, 1)}


def _expected(freq: float, a: float, b: float) -> complex:
    y = 1 / _R1 + 1 / _R2 + 1 / _R3 + 2j * math.pi * freq * _C
    return (a / _R1 + b / _R2) / y


def _write_body(tmp_path: Path) -> None:
    (tmp_path / "body.spice").write_text(
        "VA a 0 DC 0 AC 0\n"
        "VB b 0 DC 0 AC 0\n"
        f"R1 a o {_R1}\n"
        f"R2 b o {_R2}\n"
        f"R3 o 0 {_R3}\n"
        f"C1 o 0 {_C}\n"
    )


def _request(tmp_path: Path, passes, *, corners=None, **extra) -> Path:
    _write_body(tmp_path)
    doc = {
        "netlist": "body.spice",
        "analysis": {"kind": "ac", "args": _ARGS, "passes": passes},
        "options": {"keep_artifacts": True},
        **extra,
    }
    if corners is not None:
        doc["corners"] = corners
    path = tmp_path / "request.json"
    path.write_text(json.dumps(doc))
    return path


def _deck(tmp_path: Path, passes) -> list[str]:
    analysis = sim_passes.resolve_analysis(
        {"kind": "ac", "args": _ARGS, "passes": passes}
    )
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis=analysis,
        measurements_spec=[],
        raw_path=None,
    )
    return deck_path.read_text().splitlines()


# --------------------------------------------------------------------------- #
# Request validation (nothing launches)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("passes", "message"),
    [
        ([], "non-empty array"),
        ("nope", "non-empty array"),
        (["x"], "must be an object"),
        ([{"alter": _alter(1, 0)}], "name must match"),
        ([{"name": "1bad", "alter": _alter(1, 0)}], "name must match"),
        ([{"name": "a"}], "non-empty array"),
        ([{"name": "a", "alter": []}], "non-empty array"),
        ([{"name": "a", "alter": ["VA"]}], "must be an object"),
        ([{"name": "a", "alter": [{"target": "x1.v", "value": 1}]}], "not a top-level"),
        ([{"name": "a", "alter": [{"target": "VA", "value": "1"}]}], "finite number"),
        ([{"name": "a", "alter": [{"target": "VA", "value": True}]}], "finite number"),
        (
            [{"name": "a", "alter": [{"target": "VA", "value": float("inf")}]}],
            "finite number",
        ),
        ([{"name": "a", "alter": [{"target": "VA", "value": 1, "x": 1}]}], "unknown"),
        (
            [{"name": "a", "alter": [{"target": "VA", "value": 1, "field": "q"}]}],
            "field",
        ),
        (
            [{"name": "a", "alter": [{"target": "R1", "value": 1}]}],
            "no AC magnitude",
        ),
        (
            [{"name": "a", "alter": [*_alter(1, 0), {"target": "va", "value": 2}]}],
            "repeated",
        ),
        ([_PASS_A, {**_PASS_B, "name": "INJ_A"}], "unique"),
        ([{**_PASS_A, "extra": 1}], "unknown key"),
    ],
)
def test_malformed_passes_are_rejected_before_launch(
    tmp_path, monkeypatch, passes, message
):
    monkeypatch.setattr(
        sim.subprocess, "run", lambda *a, **k: pytest.fail("engine must not launch")
    )
    request = _request(tmp_path, passes)
    with pytest.raises(sim.SimError, match=message):
        sim.run_sim(str(request))


def test_incomplete_alter_sets_are_rejected_not_inherited(tmp_path):
    passes = [_PASS_A, {"name": "inj_b", "alter": [{"target": "VB", "value": 1}]}]
    with pytest.raises(sim.SimError, match="does not alter VA .*pass order"):
        sim.run_sim(str(_request(tmp_path, passes)))


def test_non_ac_analysis_is_rejected(tmp_path):
    request = _request(tmp_path, [_PASS_A, _PASS_B])
    doc = json.loads(request.read_text())
    doc["analysis"]["kind"] = "tran"
    doc["analysis"]["args"] = "1u 10u"
    request.write_text(json.dumps(doc))
    with pytest.raises(sim.SimError, match='requires analysis.kind "ac"'):
        sim.run_sim(str(request))


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"measurements": [{"name": "m", "expr": "v(o)"}]}, "measurements"),
        ({"engine": "xyce"}, "xyce"),
        ({"options": {"waveforms": True}}, "waveforms"),
    ],
)
def test_unsupported_combinations_are_rejected(tmp_path, monkeypatch, patch, message):
    monkeypatch.setattr(
        sim.subprocess, "run", lambda *a, **k: pytest.fail("engine must not launch")
    )
    request = _request(tmp_path, [_PASS_A, _PASS_B])
    doc = json.loads(request.read_text())
    for key, value in patch.items():
        doc[key] = {**doc.get(key, {}), **value} if isinstance(value, dict) else value
    request.write_text(json.dumps(doc))
    with pytest.raises(sim.SimError, match=message):
        sim.run_sim(str(request))


@pytest.mark.parametrize("backend", ["remote", "batch"])
def test_offhost_backends_are_rejected_before_provisioning(tmp_path, backend):
    request = _request(tmp_path, [_PASS_A, _PASS_B])
    with pytest.raises(sim.SimError, match="analysis.passes is not supported"):
        sim.run_sim(str(request), backend=backend)


def test_alter_target_on_a_supply_axis_is_rejected(tmp_path):
    request = _request(
        tmp_path, [_PASS_A, _PASS_B], corners={"supply_v": {"VA": [1.0, 2.0]}}
    )
    with pytest.raises(sim.SimError, match="corners.supply_v"):
        sim.run_sim(str(request))


def test_dc_alter_of_a_param_is_rejected(tmp_path):
    passes = [
        {"name": "a", "alter": [{"target": "RX", "value": 1, "field": "dc"}]},
        {"name": "b", "alter": [{"target": "RX", "value": 2, "field": "dc"}]},
    ]
    request = _request(tmp_path, passes)
    (tmp_path / "body.spice").write_text(
        (tmp_path / "body.spice").read_text() + ".param RX=1k\n"
    )
    with pytest.raises(sim.SimError, match=".param"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Deck shape
# --------------------------------------------------------------------------- #


def test_deck_applies_each_complete_alter_set_in_a_fresh_plot_per_pass(tmp_path):
    _write_body(tmp_path)
    lines = _deck(tmp_path, [_PASS_A, _PASS_B])
    ac_at = [i for i, line in enumerate(lines) if line == f"ac {_ARGS}"]
    assert len(ac_at) == 2
    first, second = ac_at
    assert lines[first - 3 : first] == [
        "alter VA ac = 1",
        "alter VB ac = 0",
        "setplot new",
    ]
    assert lines[first + 1] == f"write {tmp_path / 'ac_pass_0.raw'}"
    assert lines[second - 3 : second] == [
        "alter VA ac = 0",
        "alter VB ac = 1",
        "setplot new",
    ]
    assert lines[second + 1] == f"write {tmp_path / 'ac_pass_1.raw'}"
    assert lines.index("set filetype=ascii") < first
    assert lines[-3:-1] == ["quit", ".endc"]


def test_dc_field_emits_a_plain_alter(tmp_path):
    passes = [
        {"name": "a", "alter": [{"target": "R3", "value": 1000, "field": "dc"}]},
        {"name": "b", "alter": [{"target": "R3", "value": 2000.5, "field": "dc"}]},
    ]
    lines = _deck(tmp_path, passes)
    assert "alter R3 = 1000" in lines
    assert "alter R3 = 2000.5" in lines


def test_legacy_ac_deck_is_unchanged(tmp_path):
    deck_path = tmp_path / "corner.cir"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=sim.CornerPoint(None, {}, 27),
        analysis={"kind": "ac", "args": _ARGS},
        measurements_spec=[],
        raw_path=None,
    )
    text = deck_path.read_text()
    assert "setplot" not in text
    assert "ac_pass" not in text


# --------------------------------------------------------------------------- #
# Real ngspice
# --------------------------------------------------------------------------- #


def _decoded(corner: dict) -> dict[str, sim_raw.ComplexRawfile]:
    return {
        p["name"]: sim_raw.read_complex_ascii_rawfile(p["raw"])
        for p in corner["passes"]
    }


def _check_pass(raw: sim_raw.ComplexRawfile, a: float, b: float) -> None:
    index = raw.names.index("v(o)")
    freqs = [z.real for z in raw.column(0)]
    assert len(freqs) == 21  # 4 decades x 5 pts + 1
    for freq, value in zip(freqs, raw.column(index), strict=True):
        assert value == pytest.approx(_expected(freq, a, b), rel=1e-6)


@real_ngspice
def test_two_passes_run_at_every_corner_and_stay_distinguishable(tmp_path):
    request = _request(
        tmp_path, [_PASS_A, _PASS_B], corners={"temperature_c": [27, 85]}
    )
    report = sim.run_sim(str(request))

    assert len(report["corners"]) == 2
    for corner in report["corners"]:
        assert corner["status"] == "pass", corner["diagnostics"]
        assert [p["name"] for p in corner["passes"]] == ["inj_a", "inj_b"]
        assert all(p["status"] == "ok" and p["points"] == 21 for p in corner["passes"])
        decoded = _decoded(corner)
        _check_pass(decoded["inj_a"], 1, 0)
        _check_pass(decoded["inj_b"], 0, 1)
        # Genuinely different, complex (phase carried), not magnitude-only.
        col_a = decoded["inj_a"].column(decoded["inj_a"].names.index("v(o)"))
        col_b = decoded["inj_b"].column(decoded["inj_b"].names.index("v(o)"))
        assert col_a != col_b
        assert any(abs(cmath.phase(z)) > 1e-3 for z in col_a)


@real_ngspice
def test_reversing_pass_order_leaves_each_named_result_unchanged(tmp_path):
    forward = sim.run_sim(str(_request(tmp_path, [_PASS_A, _PASS_B])))
    other = tmp_path / "rev"
    other.mkdir()
    reverse = sim.run_sim(str(_request(other, [_PASS_B, _PASS_A])))

    fwd = _decoded(forward["corners"][0])
    rev = _decoded(reverse["corners"][0])
    assert [p["name"] for p in reverse["corners"][0]["passes"]] == ["inj_b", "inj_a"]
    for name in ("inj_a", "inj_b"):
        assert rev[name].points == fwd[name].points


@real_ngspice
def test_public_report_has_no_complex_data_and_legacy_requests_are_unaffected(tmp_path):
    report = sim.run_sim(str(_request(tmp_path, [_PASS_A, _PASS_B])))
    corner = report["corners"][0]
    assert set(corner["passes"][0]) == {"name", "status", "points", "raw"}
    json.dumps(report)  # serialisable: nothing complex leaked into it

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    request = _request(legacy_dir, [_PASS_A, _PASS_B])
    doc = json.loads(request.read_text())
    del doc["analysis"]["passes"]
    (legacy_dir / "body.spice").write_text(
        (legacy_dir / "body.spice").read_text().replace("DC 0 AC 0", "DC 0 AC 1", 1)
    )
    doc["measurements"] = [{"name": "peak", "spice": ".meas ac peak MAX vdb(o)"}]
    request.write_text(json.dumps(doc))
    legacy = sim.run_sim(str(request))
    assert "passes" not in legacy["corners"][0]
    assert legacy["corners"][0]["measurements"][0]["status"] != "error", legacy[
        "corners"
    ][0]["diagnostics"]


@real_ngspice
def test_a_failed_pass_is_reported_not_inherited(tmp_path):
    # `.ac` args ngspice rejects: no rawfile for any pass.
    request = _request(tmp_path, [_PASS_A, _PASS_B])
    doc = json.loads(request.read_text())
    doc["analysis"]["args"] = "foo 5 10 1e5"
    request.write_text(json.dumps(doc))
    report = sim.run_sim(str(request))
    corner = report["corners"][0]
    assert corner["status"] == "error"
    assert [p["status"] for p in corner["passes"]] == ["error", "error"]
    assert {d["code"] for d in corner["diagnostics"]} >= {"pass_failed"}
