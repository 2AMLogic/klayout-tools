"""Tests for `klt sim`'s `options.save_vectors` and `options.waveform_max_bytes`
(issue #3211), plus the `tran ... tmax` argument pass-through.

Reuses `tests/test_sim_save_mode.py`'s conventions: `subprocess.run` is
stubbed, so no ngspice binary is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim, sim_remote
from test_sim_save_mode import (  # noqa: F401  (autouse fixture + helpers)
    _bare_name_ngspice_resolution,
    _capture_decks,
    _write_body,
    _write_deck,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_CARD = {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
_POINT = sim.CornerPoint("tt", {"vdd": 1.8}, 27)


# --------------------------------------------------------------------------- #
# save_vectors: validation
# --------------------------------------------------------------------------- #


def test_validate_save_vectors_omitted_is_undeclared():
    assert sim._validate_save_vectors({}, "all") == ((), False)


def test_validate_save_vectors_normalizes_and_dedups():
    opts = {"save_vectors": ["V(Out)", "v(out)", "i(vdd)"]}
    assert sim._validate_save_vectors(opts, "all") == (("v(out)", "i(vdd)"), True)


@pytest.mark.parametrize(
    "value", [None, [], "v(out)", [""], ["v(out) v(in)"], [1], {"a": 1}]
)
def test_validate_save_vectors_rejects_malformed(value):
    with pytest.raises(sim.SimError, match="options.save_vectors"):
        sim._validate_save_vectors({"save_vectors": value}, "all")


def test_validate_save_vectors_excludes_netlist_save_mode():
    with pytest.raises(sim.SimError, match="mutually exclusive"):
        sim._validate_save_vectors({"save_vectors": ["v(out)"]}, "netlist")


def test_run_sim_xyce_refuses_save_vectors(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"save_vectors": ["v(out)"]},
        },
    )
    decks = _capture_decks(monkeypatch)
    with pytest.raises(sim.SimError, match="save_vectors is not supported"):
        sim.run_sim(str(request))
    assert decks == []


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10", []])
def test_validate_waveform_max_bytes_rejects(value):
    with pytest.raises(sim.SimError, match="waveform_max_bytes"):
        sim._validate_waveform_max_bytes({"waveform_max_bytes": value})


def test_validate_waveform_max_bytes_default_value_and_null():
    assert sim._validate_waveform_max_bytes({}) == sim.DEFAULT_WAVEFORM_MAX_BYTES
    assert sim._validate_waveform_max_bytes({"waveform_max_bytes": 5}) == 5
    assert sim._validate_waveform_max_bytes({"waveform_max_bytes": None}) is None


# --------------------------------------------------------------------------- #
# save_vectors: deck generation
# --------------------------------------------------------------------------- #


def test_save_vectors_replaces_save_all(tmp_path):
    lines = _write_deck(
        tmp_path,
        _POINT,
        measurements_spec=[],
        raw_path=str(tmp_path / "w.raw"),
        save_vectors=("v(in)", "i(vdd)"),
    )
    assert "save all" not in lines
    assert lines[lines.index(".control") + 1] == "save v(in) i(vdd)"


def test_save_vectors_adds_vectors_measurements_need(tmp_path):
    measurements = [
        _CARD,
        {"name": "pk", "expr": "vecmax(vdb(amp,ref))"},
        {"name": "x", "crossings": {"signal": "i(VLoad)", "level": 0}},
    ]
    lines = _write_deck(
        tmp_path, _POINT, measurements_spec=measurements, save_vectors=("v(in)",)
    )
    assert "save all" not in lines
    assert (
        lines[lines.index(".control") + 1] == "save v(in) v(out) v(amp) v(ref) i(vload)"
    )


def test_save_vectors_omitted_deck_is_unchanged(tmp_path):
    kwargs = {"measurements_spec": [_CARD], "raw_path": "/tmp/w.raw"}
    assert _write_deck(tmp_path, _POINT, **kwargs) == _write_deck(
        tmp_path, _POINT, save_vectors=(), **kwargs
    )


def test_save_vectors_without_anything_to_save_emits_no_card(tmp_path):
    lines = _write_deck(tmp_path, _POINT, save_vectors=("v(in)",))
    assert not any(line.startswith("save") for line in lines)


@pytest.mark.parametrize("backend", ["local", "local-parallel"])
def test_local_backends_honor_save_vectors(tmp_path, monkeypatch, backend):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "corners": {"temperature_c": [0, 27]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"max_workers": 2, "save_vectors": ["v(in)"]},
        },
    )
    decks = _capture_decks(monkeypatch)
    sim.run_sim(str(request))
    assert len(decks) == 2
    for deck in decks:
        assert "save all" not in deck.splitlines()
        assert "save v(in) v(out)" in deck.splitlines()


def test_checkpoint_fingerprint_keys_in_save_vectors(tmp_path):
    netlist = _write_body(tmp_path)
    kwargs = dict(
        netlist_path=str(netlist),
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        measurements_spec=[_CARD],
        corner_points=[_POINT],
        timeout_s=5.0,
        engine="ngspice",
    )
    base = sim._checkpoint_fingerprint(**kwargs)
    assert base == sim._checkpoint_fingerprint(save_vectors=(), **kwargs)
    assert base != sim._checkpoint_fingerprint(save_vectors=("v(in)",), **kwargs)


def test_build_remote_request_preserves_new_options():
    request = {
        "netlist": "body.spice",
        "options": {"save_vectors": ["v(out)"], "waveform_max_bytes": 10},
    }
    shard = sim_remote._build_remote_request(
        request, timeout_s=10.0, keep_artifacts=False, want_waveforms=True
    )
    assert shard["options"]["save_vectors"] == ["v(out)"]
    assert shard["options"]["waveform_max_bytes"] == 10


# --------------------------------------------------------------------------- #
# tmax pass-through
# --------------------------------------------------------------------------- #


def test_tran_tmax_reaches_the_deck_unchanged(tmp_path):
    lines = _write_deck(
        tmp_path, _POINT, analysis={"kind": "tran", "args": "100p 4u 0 25p"}
    )
    assert "tran 100p 4u 0 25p" in lines


# --------------------------------------------------------------------------- #
# waveform size guard
# --------------------------------------------------------------------------- #

_RAW = """Title: t
Date: x
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 2
Variables:
\t0\ttime\ttime
\t1\tv(out)\tvoltage
Values:
 0\t0.0e+00
\t1.0e+00

 1\t1.0e-06
\t2.0e+00
"""


def _run_with_fake_raw(tmp_path, monkeypatch, options):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"keep_artifacts": True, "waveforms": True, **options},
        },
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        Path(cmd[cmd.index("-o") + 1]).write_text("vout = 1.0e+00\n")
        (Path(cwd) / "waveform.raw").write_text(_RAW)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return sim.run_sim(str(request))


def test_oversize_waveform_skips_conversion_with_warning(tmp_path, monkeypatch):
    report = _run_with_fake_raw(
        tmp_path, monkeypatch, {"waveform_max_bytes": len(_RAW) - 1}
    )
    (corner,) = report["corners"]
    assert corner["artifacts"]["waveform"] is None
    assert corner["artifacts"]["raw"].endswith("waveform.raw")
    assert Path(corner["artifacts"]["raw"]).is_file()
    (diag,) = [d for d in corner["diagnostics"] if d["code"] == "waveform_too_large"]
    assert diag["severity"] == "warning"
    assert str(len(_RAW)) in diag["message"]
    assert "waveform_max_bytes" in diag["message"]
    assert corner["status"] == "pass"
    assert corner["measurements"][0]["value"] == 1.0
    assert "waveform_too_large" in sim.DIAGNOSTIC_CODES


def test_waveform_exactly_at_cap_is_converted(tmp_path, monkeypatch):
    report = _run_with_fake_raw(
        tmp_path, monkeypatch, {"waveform_max_bytes": len(_RAW)}
    )
    (corner,) = report["corners"]
    assert corner["artifacts"]["waveform"].endswith("waveform.raw.json")
    assert not [d for d in corner["diagnostics"] if d["code"] == "waveform_too_large"]
    assert json.loads(Path(corner["artifacts"]["waveform"]).read_text())["variables"]


def test_null_cap_disables_the_guard(tmp_path, monkeypatch):
    report = _run_with_fake_raw(tmp_path, monkeypatch, {"waveform_max_bytes": None})
    (corner,) = report["corners"]
    assert corner["artifacts"]["waveform"] is not None
