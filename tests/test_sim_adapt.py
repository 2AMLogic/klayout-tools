"""``request.adapt`` validation and refusals (issue #3873); no toolchain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import sim, sim_adapt
from klayout_tools._paths import _resolve_relative

_FINFET = "Mn1 d g s b nmos_rvt L=20n W=0.1u NFIN=3\n"
_CARD = ".model nmos_rvt nmos level = 72 version = 107 tfin = 6n\n"


def _req(tmp_path: Path, adapt=None, **extra) -> Path:
    (tmp_path / "n.spice").write_text(_FINFET)
    (tmp_path / "tt.pm").write_text(_CARD)
    request = {"netlist": "n.spice", "analysis": {"kind": "op"}, **extra}
    if adapt is not None:
        request["adapt"] = adapt
    path = tmp_path / "req.json"
    path.write_text(json.dumps(request))
    return path


def _dry(path: Path) -> dict:
    return sim.run_sim(str(path), dry_run=True, backend="local")


def test_dry_run_accepts_adapt_without_warning(tmp_path):
    report = _dry(
        _req(tmp_path, {"kind": "asap7_bsimcmg", "model_cards": {"tt": "tt.pm"}})
    )
    assert report["warnings"] == []
    assert report["netlist"].endswith("n.spice")
    assert Path(report["netlist"]).read_text().startswith("Nn1 d g s b nmos_rvt")


@pytest.mark.parametrize(
    "adapt, extra, match",
    [
        ({"kind": "nope"}, {}, "adapt.kind 'nope' is not supported"),
        ("x", {}, "adapt must be an object"),
        (
            {"kind": "asap7_bsimcmg", "model_cards": {"tt": "tt.pm"}},
            {"models": {"lib": "tt.pm"}},
            "both set",
        ),
        (
            {"kind": "asap7_bsimcmg"},
            {"engine": "xyce"},
            "OSDI/ngspice only",
        ),
        (
            {"kind": "asap7_bsimcmg", "model_cards": {"tt": "missing.pm"}},
            {},
            "model card not found",
        ),
    ],
)
def test_refusals(tmp_path, adapt, extra, match):
    with pytest.raises(sim.SimError, match=match):
        _dry(_req(tmp_path, adapt, **extra))


def test_adapter_value_errors_surface(tmp_path):
    path = _req(tmp_path, {"kind": "asap7_bsimcmg"})
    (tmp_path / "n.spice").write_text("Mn1 d g s b nmos_rvt L=20n W=0.1u\n")
    with pytest.raises(sim.SimError, match="no NFIN"):
        _dry(path)
    path = _req(tmp_path, {"kind": "asap7_bsimcmg", "model_cards": {"tt": "tt.pm"}})
    (tmp_path / "tt.pm").write_text(".model m nmos level = 1\n")
    with pytest.raises(sim.SimError, match="not a BSIM-CMG"):
        _dry(path)


def test_batch_backend_refused(tmp_path):
    with pytest.raises(sim.SimError, match="adapt is not supported for backend"):
        sim.run_sim(
            str(_req(tmp_path, {"kind": "asap7_bsimcmg"})),
            dry_run=True,
            backend="batch",
        )


def test_environment_entry_only_when_adapt(tmp_path):
    request = json.loads(_req(tmp_path).read_text())
    same, env = sim_adapt.apply_adapt(
        request,
        str(tmp_path),
        engine="ngspice",
        work_dir=str(tmp_path / "w"),
        resolve=_resolve_relative,
    )
    assert same is request and env is None
    request["adapt"] = {"kind": "asap7_bsimcmg", "model_cards": {"tt": "tt.pm"}}
    out, env = sim_adapt.apply_adapt(
        request,
        str(tmp_path),
        engine="ngspice",
        work_dir=str(tmp_path / "w"),
        resolve=_resolve_relative,
    )
    assert env["kind"] == "asap7_bsimcmg"
    assert env["model_cards"][0]["corner"] == "tt"
    assert len(env["netlist_sha256_out"]) == 64
    assert ".lib tt" in Path(out["models"]["lib"]).read_text()


def test_offhost_backends_mirror_sim():
    assert sim_adapt.OFFHOST_BACKENDS == sim._OFFHOST_BACKENDS
