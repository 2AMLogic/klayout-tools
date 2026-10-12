"""Tests for `klt sim`'s `options.save_mode` (issue #2732).

`"all"` (the default, also what an omitted key means) keeps issue #2521's
generated `save all`; `"netlist"` suppresses it so the included netlist's own
`.save` cards decide the resident vector set. Split out of `tests/test_sim.py`
(already ~11.7k lines) as a self-contained sibling; it reuses that module's
conventions: unit tests stub `subprocess.run`, integration tests run the real
`ngspice -b` and skip without it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import sim, sim_remote

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

_CARD = {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
_EXPR = {"name": "vpk", "expr": "vecmax(v(out))"}


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    """Same stub as `tests/test_sim.py`'s identically-named fixture: the
    mocked-`subprocess.run` tests below need no real ngspice binary."""
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ngspice":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


def _write_body(tmp_path: Path) -> Path:
    path = tmp_path / "body.spice"
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_request(tmp_path: Path, request: dict) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _write_deck(tmp_path: Path, point: sim.CornerPoint, **overrides) -> list[str]:
    deck_path = tmp_path / "deck.spice"
    kwargs = {
        "deck_path": str(deck_path),
        "netlist_path": str(tmp_path / "body.spice"),
        "models_lib": None,
        "point": point,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "raw_path": None,
    }
    kwargs.update(overrides)
    sim._write_corner_deck(**kwargs)
    return deck_path.read_text().splitlines()


def _capture_decks(monkeypatch) -> list[str]:
    """Stub `subprocess.run`; record the text of every deck it is handed."""
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        decks.append(Path(cmd[cmd.index("-b") + 1]).read_text())
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text("")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_validate_save_mode_omitted_is_default_and_undeclared():
    assert sim._validate_save_mode({}) == ("all", False)


@pytest.mark.parametrize("mode", ["all", "netlist"])
def test_validate_save_mode_accepts_both_modes_as_declared(mode):
    assert sim._validate_save_mode({"save_mode": mode}) == (mode, True)


@pytest.mark.parametrize(
    "value", [None, True, False, 0, ["all"], {}, "ALL", "", "none"]
)
def test_validate_save_mode_rejects_everything_else(value):
    with pytest.raises(sim.SimError, match="options.save_mode must be one of"):
        sim._validate_save_mode({"save_mode": value})


@pytest.mark.parametrize("value", [None, True, ["netlist"], "selected"])
def test_run_sim_rejects_invalid_save_mode_before_any_launch(
    tmp_path, monkeypatch, value
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"save_mode": value},
        },
    )
    decks = _capture_decks(monkeypatch)

    with pytest.raises(sim.SimError, match="options.save_mode"):
        sim.run_sim(str(request))
    assert decks == []


def test_run_sim_rejects_invalid_save_mode_before_batch_provisioning(
    tmp_path, monkeypatch
):
    """Off-host backends: refused before any job is staged or launched."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "batch",
            "corners": {"temperature_c": [0, 27]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"save_mode": "bogus"},
        },
    )

    def _no_dispatch(**kwargs):
        raise AssertionError("batch backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, "batch", _no_dispatch)

    with pytest.raises(sim.SimError, match="options.save_mode"):
        sim.run_sim(str(request))


@pytest.mark.parametrize("mode", ["all", "netlist"])
def test_run_sim_xyce_refuses_an_explicit_save_mode(tmp_path, monkeypatch, mode):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"save_mode": mode},
        },
    )
    calls: list = []
    monkeypatch.setattr(sim.subprocess, "run", lambda *a, **k: calls.append(a))

    with pytest.raises(sim.SimError, match="not supported for engine 'xyce'"):
        sim.run_sim(str(request))
    assert calls == []


def test_run_sim_xyce_without_save_mode_is_unaffected(tmp_path, monkeypatch):
    """Omitted mode: the Xyce support boundary never sees the option."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    seen: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        seen.append(cmd)
        Path(cmd[cmd.index("-l") + 1]).write_text("")
        return fake_completed("Xyce Release 7.10.0\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request))

    assert len(report["corners"]) == 1
    (cmd,) = seen
    assert cmd[0] == sim.XYCE_BINARY


# --------------------------------------------------------------------------- #
# Deck generation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("measurements", "waveforms"),
    [([_CARD], False), ([], True), ([_EXPR], False), ([_CARD, _EXPR], True)],
)
@pytest.mark.parametrize("mode", [None, "all"])
def test_default_mode_keeps_save_all_trigger(tmp_path, measurements, waveforms, mode):
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 27)
    raw = str(tmp_path / "w.raw") if waveforms else None
    extra = {} if mode is None else {"save_mode": mode}
    lines = _write_deck(
        tmp_path, point, measurements_spec=measurements, raw_path=raw, **extra
    )
    control = lines.index(".control")
    assert lines.count("save all") == 1
    assert lines.index("save all") == control + 1
    assert lines.index("save all") < next(
        i for i, line in enumerate(lines) if line.startswith("alter")
    )


@pytest.mark.parametrize("mode", [None, "all", "netlist"])
def test_no_measurement_no_waveform_never_emits_save_all(tmp_path, mode):
    point = sim.CornerPoint("tt", {}, 27)
    extra = {} if mode is None else {"save_mode": mode}
    assert "save all" not in _write_deck(tmp_path, point, **extra)


def test_default_and_explicit_all_decks_are_byte_identical(tmp_path):
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 27)
    kwargs = {"measurements_spec": [_CARD, _EXPR], "raw_path": "/tmp/w.raw"}
    assert _write_deck(tmp_path, point, **kwargs) == _write_deck(
        tmp_path, point, save_mode="all", **kwargs
    )


@pytest.mark.parametrize(
    ("measurements", "waveforms"),
    [([_CARD], False), ([], True), ([_EXPR], False), ([_CARD, _EXPR], True)],
)
def test_netlist_mode_emits_no_save_card(tmp_path, measurements, waveforms):
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 27)
    raw = str(tmp_path / "w.raw") if waveforms else None
    lines = _write_deck(
        tmp_path,
        point,
        measurements_spec=measurements,
        raw_path=raw,
        save_mode="netlist",
    )
    assert not any(line.strip().lower().startswith("save") for line in lines)
    # Everything else in the control block keeps its order.
    control = lines.index(".control")
    assert lines[control + 1] == "alter vdd=1.8"


def test_netlist_mode_with_osdi_preload_keeps_pre_osdi_first(tmp_path):
    osdi = tmp_path / "m.osdi"
    osdi.write_bytes(b"\x7fELF")
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 27)
    for mode, expected_next in (("all", "save all"), ("netlist", "alter vdd=1.8")):
        lines = _write_deck(
            tmp_path,
            point,
            measurements_spec=[_CARD],
            osdi_preload=(str(osdi),),
            save_mode=mode,
        )
        control = lines.index(".control")
        assert lines[control + 1] == f"pre_osdi {osdi}"
        assert lines[control + 2] == expected_next


# --------------------------------------------------------------------------- #
# Propagation: serial, local-parallel, fail-fast probe, checkpoint
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", ["local", "local-parallel"])
@pytest.mark.parametrize(
    ("mode", "expect_save_all"), [(None, True), ("netlist", False)]
)
def test_local_backends_honor_save_mode(
    tmp_path, monkeypatch, backend, mode, expect_save_all
):
    _write_body(tmp_path)
    options: dict = {"max_workers": 2}
    if mode is not None:
        options["save_mode"] = mode
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "corners": {"temperature_c": [0, 27, 85]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": options,
        },
    )
    decks = _capture_decks(monkeypatch)

    sim.run_sim(str(request))

    assert len(decks) == 3
    for deck in decks:
        assert ("save all" in deck.splitlines()) is expect_save_all


def test_hosts_sharded_local_run_honors_netlist_mode(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": [0, 27]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"save_mode": "netlist"},
        },
    )
    decks = _capture_decks(monkeypatch)

    sim.run_sim(str(request), hosts=2)

    assert len(decks) == 2
    assert all("save all" not in deck.splitlines() for deck in decks)


def test_fail_fast_probe_receives_save_mode(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"fail_fast_probe": True, "save_mode": "netlist"},
        },
    )
    seen: dict = {}

    def fake_probe(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(sim, "_run_fail_fast_probe", fake_probe)
    _capture_decks(monkeypatch)

    sim.run_sim(str(request))

    assert seen["save_mode"] == "netlist"


@pytest.mark.parametrize("mode", ["all", "netlist"])
def test_calibration_probe_deck_never_carries_save_all(tmp_path, monkeypatch, mode):
    """The probe runs with the netlist's own saved set in both modes (it has
    no measurements and no rawfile); under `netlist` it therefore matches
    the real corner's resident-vector policy exactly."""
    _write_body(tmp_path)
    decks = _capture_decks(monkeypatch)

    sim._run_calibration_probe(
        point=sim.CornerPoint(None, {}, 27),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        step_token="1n",
        probe_window_s=1e-7,
        probe_timeout_s=5.0,
        artifacts_dir=str(tmp_path / "artifacts"),
        keep_artifacts=True,
        save_mode=mode,
    )

    (deck,) = decks
    assert "save all" not in deck.splitlines()


def test_checkpoint_fingerprint_keys_in_only_the_non_default_mode(tmp_path):
    body = _write_body(tmp_path)
    kwargs = {
        "netlist_path": str(body),
        "models_lib": None,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [_CARD],
        "corner_points": [sim.CornerPoint(None, {}, 27)],
        "timeout_s": 10.0,
        "engine": "ngspice",
    }
    default = sim._checkpoint_fingerprint(**kwargs)
    assert sim._checkpoint_fingerprint(**kwargs, save_mode="all") == default
    assert sim._checkpoint_fingerprint(**kwargs, save_mode="netlist") != default


# --------------------------------------------------------------------------- #
# Remote / batch request construction
# --------------------------------------------------------------------------- #


def test_build_remote_request_preserves_save_mode_including_shards():
    request = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [0, 27]},
        "options": {"save_mode": "netlist", "timeout_s": 5},
    }
    kwargs = dict(timeout_s=10.0, keep_artifacts=False, want_waveforms=True)

    single = sim_remote._build_remote_request(request, **kwargs)
    assert single["options"]["save_mode"] == "netlist"

    shard = sim_remote._build_remote_request(
        request,
        explicit_points=[sim.CornerPoint(None, {}, 27.0)],
        **kwargs,
    )
    assert shard["options"]["save_mode"] == "netlist"
    assert "corners" not in shard
    # The caller's own request document is never mutated.
    assert request["options"] == {"save_mode": "netlist", "timeout_s": 5}


def test_build_remote_request_omitted_save_mode_stays_omitted():
    shard = sim_remote._build_remote_request(
        {"netlist": "body.spice", "options": {}},
        timeout_s=10.0,
        keep_artifacts=False,
        want_waveforms=False,
    )
    assert "save_mode" not in shard["options"]


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


def test_no_value_message_default_mode_is_unchanged():
    assert sim._no_value_message(_CARD) == "measurement 'vout' produced no value"
    assert sim._no_value_message(_CARD, save_mode="all") == (
        "measurement 'vout' produced no value"
    )


@pytest.mark.parametrize("spec", [_CARD, _EXPR])
def test_no_value_message_netlist_mode_names_the_opt_out(spec):
    message = sim._no_value_message(spec, save_mode="netlist")
    assert message.startswith(f"measurement '{spec['name']}' produced no value")
    assert 'options.save_mode is "netlist"' in message
    assert ".save" in message


# --------------------------------------------------------------------------- #
# Integration: real ngspice (skipped when not installed)
# --------------------------------------------------------------------------- #

#: Two nodes, a divider: `v(in)` = 1 V, `v(out)` = 0.5 V. The body's own
#: `.save` keeps only `v(out)`.
_RESTRICTIVE_BODY = "Vin in 0 DC 1\nR1 in out 1k\nR2 out 0 1k\n.save v(out)\n"


def _integration_request(tmp_path: Path, *, measurements, save_mode=None) -> Path:
    (tmp_path / "body.spice").write_text(_RESTRICTIVE_BODY)
    options: dict = {"keep_artifacts": True, "waveforms": True}
    if save_mode is not None:
        options["save_mode"] = save_mode
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1u 10u"},
            "measurements": measurements,
            "options": options,
        },
    )


def _saved_vectors(corner: dict) -> set[str]:
    waveform = json.loads(Path(corner["artifacts"]["waveform"]).read_text())
    return {variable["name"].lower() for variable in waveform["variables"]}


_VOUT = {
    "name": "vout",
    "spice": ".meas tran vout FIND v(out) AT=5u",
    "unit": "V",
    "limits": {"min": 0.45, "max": 0.55},
}
_VOUT_EXPR = {
    "name": "vout_pk",
    "expr": "vecmax(v(out))",
    "unit": "V",
    "limits": {"min": 0.45, "max": 0.55},
}
_VIN = {
    "name": "vin",
    "spice": ".meas tran vin FIND v(in) AT=5u",
    "unit": "V",
    "limits": {"min": 0.0, "max": 2.0},
}
_VIN_EXPR = {
    "name": "vin_pk",
    "expr": "vecmax(v(in))",
    "unit": "V",
    "limits": {"min": 0.0, "max": 2.0},
}


@_SKIP_NO_NGSPICE
def test_integration_netlist_mode_keeps_only_the_netlists_saved_vectors(tmp_path):
    request = _integration_request(
        tmp_path, measurements=[_VOUT, _VOUT_EXPR], save_mode="netlist"
    )

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    vectors = _saved_vectors(corner)
    assert "v(out)" in vectors
    assert "v(in)" not in vectors
    assert report["status"] == "pass"
    values = {m["name"]: m for m in corner["measurements"]}
    assert values["vout"]["status"] == "pass"
    assert values["vout"]["value"] == pytest.approx(0.5, abs=1e-6)
    assert values["vout_pk"]["status"] == "pass"
    assert values["vout_pk"]["value"] == pytest.approx(0.5, abs=1e-6)


@_SKIP_NO_NGSPICE
def test_integration_default_mode_retains_the_excluded_node(tmp_path):
    request = _integration_request(tmp_path, measurements=[_VOUT, _VIN, _VIN_EXPR])

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    vectors = _saved_vectors(corner)
    assert {"v(in)", "v(out)"} <= vectors
    assert report["status"] == "pass"
    values = {m["name"]: m for m in corner["measurements"]}
    assert values["vin"]["value"] == pytest.approx(1.0, abs=1e-6)
    assert values["vin_pk"]["value"] == pytest.approx(1.0, abs=1e-6)


@_SKIP_NO_NGSPICE
@pytest.mark.parametrize("excluded", [_VIN, _VIN_EXPR], ids=["spice", "expr"])
def test_integration_netlist_mode_excluded_vector_is_an_error_not_a_pass(
    tmp_path, excluded
):
    request = _integration_request(
        tmp_path, measurements=[_VOUT, excluded], save_mode="netlist"
    )

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    values = {m["name"]: m for m in corner["measurements"]}
    assert values["vout"]["status"] == "pass"
    missing = values[excluded["name"]]
    assert missing["status"] == "error"
    assert missing["value"] is None
    assert corner["status"] == "error"
    assert report["status"] == "error"
    assert report["passed"] == 0
    messages = [
        d["message"] for d in corner["diagnostics"] if d["code"] == "measurement"
    ]
    assert any('options.save_mode is "netlist"' in m for m in messages)
