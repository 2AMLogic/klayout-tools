"""Tests for `klt sim`'s `options.ngspice_options` (issue #3111).

A request-level map of ngspice solver/convergence `.options` (`gminsteps`,
`itl1`, `noopiter`, ...) rendered as one `.options` card in each corner deck
and echoed verbatim as `environment.ngspice_options`. Modelled on
`tests/test_sim_save_mode.py`: unit tests stub `subprocess.run`, so no real
ngspice (and no SPICE grid, local or fleet) is needed.
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_batch

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_CARD = {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ngspice":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


def _write_body(tmp_path: Path) -> None:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )


def _write_request(tmp_path: Path, **overrides) -> Path:
    _write_body(tmp_path)
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [_CARD],
    }
    request.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _capture_decks(monkeypatch) -> list[str]:
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        decks.append(Path(cmd[cmd.index("-b") + 1]).read_text())
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text("")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


def _deck_lines(tmp_path: Path, ngspice_options, point=None) -> list[str]:
    deck_path = tmp_path / "deck.spice"
    sim._write_corner_deck(
        deck_path=str(deck_path),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=point or sim.CornerPoint("tt", {}, 27),
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=[],
        raw_path=None,
        ngspice_options=ngspice_options,
    )
    return deck_path.read_text().splitlines()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [None, {}])
def test_validate_unset_is_empty(value):
    assert sim._validate_ngspice_options(value) == {}


def test_validate_accepts_scalars():
    value = {"gminsteps": 0, "reltol": 1e-4, "method": "gear", "noopiter": True}
    assert sim._validate_ngspice_options(value) == value


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (["gminsteps"], "must be an object"),
        ("gminsteps=0", "must be an object"),
        ({"gminstep": 0}, '"gminstep" is not a supported'),
        ({"gminsteps": [0]}, r"gminsteps must be a number"),
        ({"gminsteps": {"a": 1}}, r"gminsteps must be a number"),
        ({"method": "gear\n.end"}, r"method must be a number"),
        ({"method": "two words"}, r"method must be a number"),
        ({"reltol": float("inf")}, r"reltol must be finite"),
        ({"itl1": None}, r"itl1 must be a number"),
    ],
)
def test_validate_rejects_naming_the_offender(value, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_ngspice_options(value)


def test_run_sim_rejects_invalid_before_any_launch(tmp_path, monkeypatch):
    request = _write_request(tmp_path, options={"ngspice_options": {"bogus": 1}})
    decks = _capture_decks(monkeypatch)
    with pytest.raises(sim.SimError, match='"bogus"'):
        sim.run_sim(str(request))
    assert decks == []


def test_run_sim_xyce_refuses_the_field(tmp_path, monkeypatch):
    request = _write_request(
        tmp_path, engine="xyce", options={"ngspice_options": {"itl1": 400}}
    )
    calls: list = []
    monkeypatch.setattr(sim.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(sim.SimError, match="options.ngspice_options is not supported"):
        sim.run_sim(str(request))
    assert calls == []


# --------------------------------------------------------------------------- #
# Deck rendering
# --------------------------------------------------------------------------- #


def test_deck_renders_options_card_after_temp(tmp_path):
    lines = _deck_lines(tmp_path, {"gminsteps": 0})
    assert ".options gminsteps=0" in lines
    assert lines.index(".options gminsteps=0") == lines.index(".temp 27") + 1


def test_deck_value_types(tmp_path):
    lines = _deck_lines(
        tmp_path,
        {"reltol": 0.0001, "method": "gear", "noopiter": True, "srcsteps": False},
    )
    assert ".options reltol=0.0001 method=gear noopiter" in lines


def test_deck_unset_is_byte_identical(tmp_path):
    assert _deck_lines(tmp_path, None) == _deck_lines(tmp_path, {})
    assert not any(line.startswith(".options") for line in _deck_lines(tmp_path, None))


def test_deck_coexists_with_monte_carlo_seed(tmp_path):
    point = sim.CornerPoint(
        "tt",
        {},
        27,
        mc_seed={"rndseed": 7, "process_seed": 1, "mismatch_seed": 2},
        sample_index=0,
    )
    lines = _deck_lines(tmp_path, {"itl1": 400}, point=point)
    assert ".options seed=7" in lines
    assert ".options itl1=400" in lines


# --------------------------------------------------------------------------- #
# Propagation, report echo, identity
# --------------------------------------------------------------------------- #


def test_run_sim_renders_and_echoes(tmp_path, monkeypatch):
    request = _write_request(tmp_path, options={"ngspice_options": {"gminsteps": 0}})
    decks = _capture_decks(monkeypatch)

    report = sim.run_sim(str(request))

    assert len(decks) == 1
    assert ".options gminsteps=0" in decks[0].splitlines()
    assert report["environment"]["ngspice_options"] == {"gminsteps": 0}


def test_run_sim_unset_omits_echo_and_card(tmp_path, monkeypatch):
    request = _write_request(tmp_path)
    decks = _capture_decks(monkeypatch)

    report = sim.run_sim(str(request))

    assert "ngspice_options" not in report["environment"]
    assert ".options" not in decks[0]


def test_checkpoint_fingerprint_changes_only_when_set():
    kwargs = {
        "netlist_path": __file__,
        "models_lib": None,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "corner_points": [sim.CornerPoint(None, {}, 27)],
        "timeout_s": 5.0,
        "engine": "ngspice",
    }
    default = sim._checkpoint_fingerprint(**kwargs)
    assert sim._checkpoint_fingerprint(**kwargs, ngspice_options={}) == default
    assert (
        sim._checkpoint_fingerprint(**kwargs, ngspice_options={"gminsteps": 0})
        != default
    )


# --------------------------------------------------------------------------- #
# Off-host echo check
# --------------------------------------------------------------------------- #

_DECLARED = {"options": {"ngspice_options": {"gminsteps": 0}}}


def test_echo_check_passes_on_match_and_when_undeclared():
    sim.check_ngspice_options_echo(
        _DECLARED, {"environment": {"ngspice_options": {"gminsteps": 0}}}
    )
    sim.check_ngspice_options_echo({"options": {}}, {"environment": {}})


@pytest.mark.parametrize(
    "report_env",
    [{}, {"ngspice_options": {}}, {"ngspice_options": {"gminsteps": 1}}],
)
def test_echo_check_fails_on_missing_or_different_echo(report_env):
    with pytest.raises(sim.SimError, match="gminsteps"):
        sim.check_ngspice_options_echo(_DECLARED, {"environment": report_env})


def test_echo_check_fails_on_extra_echoed_option():
    with pytest.raises(sim.SimError, match="differs from the declared"):
        sim.check_ngspice_options_echo(
            _DECLARED,
            {"environment": {"ngspice_options": {"gminsteps": 0, "itl1": 9}}},
        )


def test_batch_job_with_stale_runner_echo_fails_loudly(tmp_path, monkeypatch):
    """A runner that predates the field returns a report without the echo."""
    monkeypatch.setattr(sim_batch, "submit_job", lambda *a, **k: None)
    monkeypatch.setattr(sim_batch, "launch_job", lambda *a, **k: None)
    monkeypatch.setattr(sim_batch, "poll_status", lambda *a, **k: {"state": "done"})
    monkeypatch.setattr(sim_batch, "collect_outputs", lambda *a, **k: None)
    monkeypatch.setattr(
        sim_batch,
        "read_collected_report",
        lambda _d: {"corners": [], "environment": {"engine_version": "ngspice-46"}},
    )
    monkeypatch.setattr(sim_batch, "_build_batch_job_spec", lambda *a, **k: {})
    monkeypatch.setattr(sim_batch, "_batch_environment", lambda *a, **k: {})
    monkeypatch.setattr(sim_batch, "_read_job_identity", lambda *a, **k: {})

    with pytest.raises(sim.SimError, match="options.ngspice_options.gminsteps"):
        sim_batch._run_batch_job(
            config=types.SimpleNamespace(runner_version_check="off"),
            job_id="job-1",
            corner_points=[sim.CornerPoint(None, {}, 27)],
            netlist_path=str(tmp_path / "body.spice"),
            timeout_s=5.0,
            keep_artifacts=False,
            want_waveforms=False,
            artifacts_dir=str(tmp_path),
            request={
                "netlist": "body.spice",
                "options": {"ngspice_options": {"gminsteps": 0}},
            },
            measurements_spec=[],
        )
