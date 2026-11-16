"""Tests for `analysis.op_check` (issue #5077): metastable-op detection.

Unit tests need no simulator; the integration tests run the real `ngspice -b`
and skip without it (like `tests/test_sim_nodeset.py`).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim, sim_op_check
from klayout_tools.sim_errors import SimError

OFFHOST = ("remote", "batch")

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(not HAVE_NGSPICE, reason="no ngspice")

#: A 0..1 V bistable latch: stable at (a, b) = (0, 1) or (1, 0); unstable
#: mid-rail solution at 0.5 V.
_LATCH = "Ba a 0 V=0.5+0.5*tanh(8*(v(b)-0.5))\nBb b 0 V=0.5+0.5*tanh(8*(v(a)-0.5))\n"


def _analysis(**op_check) -> dict:
    return {"kind": "op", "args": "", "op_check": op_check}


# ---- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "analysis, fragment",
    [
        ({"kind": "tran", "args": "1n 10n", "op_check": {"nodes": ["a"]}}, '"op"'),
        (_analysis(nodes=["a"], bogus=1), "unknown key"),
        (_analysis(), "non-empty"),
        (_analysis(nodes=[]), "non-empty"),
        (_analysis(nodes=["a b"]), "bare SPICE node"),
        (_analysis(nodes=["a", "A"]), "repeats"),
        (_analysis(nodes=["a"], band=0), "band"),
        (_analysis(nodes=["a"], band=0.9), "band"),
        (_analysis(nodes=["a"], band=float("nan")), "finite"),
        (_analysis(nodes=["a"], band=True), "number"),
        (_analysis(nodes=["a"], rail="vdd", supply=1.0), "mutually exclusive"),
        (_analysis(nodes=["a"], supply=-1), "positive"),
    ],
)
def test_invalid_op_check_is_an_application_error(analysis, fragment):
    with pytest.raises(SimError, match=fragment):
        sim_op_check.validate(analysis)


def test_valid_and_absent_op_check_pass_validation():
    sim_op_check.validate({"kind": "op", "args": ""})
    sim_op_check.validate(_analysis(nodes=["x1.q", "qb"], band=0.2, rail="VDD"))


def test_absent_op_check_adds_nothing_to_the_deck_or_report():
    analysis = {"kind": "op", "args": ""}
    assert sim_op_check.control_lines(analysis) == []
    assert sim_op_check.evaluate(analysis, {"vdd": 1.0}, "") == ({}, [])


# ---- evaluation ------------------------------------------------------------


def _log(*values: float) -> str:
    return "\n".join(f"__klt_opc_{k} = {v}" for k, v in enumerate(values))


def test_node_near_mid_rail_warns_with_values():
    field, diags = sim_op_check.evaluate(
        _analysis(nodes=["a", "b"], band=0.15), {"vdd": 1.0}, _log(0.52, 0.48)
    )
    assert field["op_check"]["metastable"] == ["a", "b"]
    assert field["op_check"]["nodes"] == {"a": 0.52, "b": 0.48}
    assert [d["code"] for d in diags] == ["op_metastable"] * 2
    assert {d["severity"] for d in diags} == {"warning"}


def test_nodes_at_the_rails_produce_no_diagnostic():
    field, diags = sim_op_check.evaluate(
        _analysis(nodes=["a", "b"]), {"vdd": 1.0}, _log(0.0, 1.0)
    )
    assert diags == []
    assert field["op_check"]["metastable"] == []


def test_band_scales_with_each_corners_supply():
    spec = _analysis(nodes=["a"], band=0.1)
    # 0.8 V is 0.3 V off mid-rail of a 1 V supply, but only 0.1 V off for 1.4 V.
    assert sim_op_check.evaluate(spec, {"vdd": 1.0}, _log(0.8))[1] == []
    assert sim_op_check.evaluate(spec, {"vdd": 1.4}, _log(0.8))[1] != []


def test_missing_node_value_is_an_error():
    _, diags = sim_op_check.evaluate(_analysis(nodes=["a"]), {"vdd": 1.0}, "")
    assert [(d["severity"], d["code"]) for d in diags] == [("error", "no_such_vector")]


def test_corner_without_a_supply_is_an_error_not_a_silent_skip():
    _, diags = sim_op_check.evaluate(_analysis(nodes=["a"]), {}, _log(0.5))
    assert diags[0]["severity"] == "error"
    assert "supply" in diags[0]["message"]
    field, diags = sim_op_check.evaluate(
        _analysis(nodes=["a"], supply=1.0), {}, _log(0.5)
    )
    assert diags[0]["code"] == "op_metastable"


def test_deck_lines_and_forced_save():
    analysis = _analysis(nodes=["X1.Q"])
    assert sim_op_check.control_lines(analysis) == [
        "let __klt_opc_0 = v(x1.q)",
        "print __klt_opc_0",
    ]
    lines = sim._corner_deck_control_lines(
        point=sim._expand_corners({}, [])[0],
        analysis=analysis,
        measurements_spec=[],
        raw_path=None,
        osdi_preload=(),
    )
    assert "save all" in lines
    assert lines.index("op ") < lines.index("let __klt_opc_0 = v(x1.q)")


def test_op_metastable_is_a_selectable_fail_on_diagnostic_code():
    assert "op_metastable" in sim.DIAGNOSTIC_CODES


def test_fingerprint_changes_with_op_check():
    base = {"kind": "op", "args": ""}
    kwargs = dict(
        netlist_path=__file__,
        models_lib=None,
        measurements_spec=[],
        corner_points=[],
        timeout_s=1.0,
        engine="ngspice",
    )
    a = sim._checkpoint_fingerprint(analysis=base, **kwargs)
    b = sim._checkpoint_fingerprint(analysis=_analysis(nodes=["a"]), **kwargs)
    c = sim._checkpoint_fingerprint(analysis=_analysis(nodes=["b"]), **kwargs)
    assert len({a, b, c}) == 3


# ---- off-host echo ---------------------------------------------------------


def test_offhost_worker_dropping_op_check_is_refused():
    analysis = _analysis(nodes=["a"])
    ok = [{"corner_id": "c0", "op_check": {}, "diagnostics": []}]
    sim_op_check.require_echo(analysis, ok, "batch", OFFHOST)
    with pytest.raises(SimError) as err:
        sim_op_check.require_echo(
            analysis, [{"corner_id": "c0", "diagnostics": []}], "batch", OFFHOST
        )
    assert err.value.code == "op_check_unsupported_worker"
    # Not declared, or a local backend: nothing to enforce.
    sim_op_check.require_echo({"kind": "op", "args": ""}, [{}], "batch", OFFHOST)
    sim_op_check.require_echo(analysis, [{}], "local", OFFHOST)


# ---- real engine -----------------------------------------------------------


def _run(tmp_path: Path, nodeset: str, **op_check) -> dict:
    (tmp_path / "body.spice").write_text(_LATCH + nodeset)
    request = {
        "netlist": "body.spice",
        "analysis": _analysis(nodes=["a", "b"], supply=1.0, **op_check),
        "backend": "local",
        "options": {"keep_artifacts": True},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    report = sim.run_sim(str(path), artifacts_dir=str(tmp_path / "a"))
    return report["corners"][0]


@_SKIP_NO_NGSPICE
def test_real_ngspice_mid_rail_nodeset_fires_the_diagnostic(tmp_path):
    corner = _run(tmp_path, ".nodeset v(a)=0.5 v(b)=0.5\n")
    assert corner["op_check"]["metastable"] == ["a", "b"]
    assert any(d["code"] == "op_metastable" for d in corner["diagnostics"])
    assert corner["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_real_ngspice_rail_nodeset_is_clean(tmp_path):
    corner = _run(tmp_path, ".nodeset v(a)=1 v(b)=0\n")
    assert corner["op_check"]["metastable"] == []
    assert not any(d["code"] == "op_metastable" for d in corner["diagnostics"])
    assert corner["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_real_ngspice_unknown_node_fails_the_corner(tmp_path):
    (tmp_path / "body.spice").write_text(_LATCH)
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": _analysis(nodes=["nope"], supply=1.0),
                "backend": "local",
            }
        )
    )
    report = sim.run_sim(str(path), artifacts_dir=str(tmp_path / "a"))
    assert report["corners"][0]["status"] == "error"


def test_xyce_refuses_op_check():
    with pytest.raises(SimError, match="op_check"):
        sim._enforce_xyce_support_boundary(
            engine="xyce",
            backend="local",
            corners_spec={},
            monte_carlo_declared=False,
            fail_fast_probe=False,
            op_check_declared=True,
        )
