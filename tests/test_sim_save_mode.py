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
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from helpers.tool_probes import SKIP_NO_NGSPICE as _SKIP_NO_NGSPICE
from klayout_tools import sim, sim_remote

pytestmark = pytest.mark.usefixtures(
    "real_build_identity_git", "bare_name_ngspice_resolution"
)

_CARD = {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
_EXPR = {"name": "vpk", "expr": "vecmax(v(out))"}


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

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
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

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
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


@pytest.mark.parametrize(
    "code", ["timeout", "engine_killed", "timestep_too_small", "nonconvergence"]
)
def test_effective_save_mode_drops_hint_when_an_error_diagnostic_explains_it(code):
    diagnostics = [{"severity": "error", "code": code, "message": "x"}]

    assert sim._effective_save_mode("netlist", diagnostics) == "all"


def test_effective_save_mode_keeps_hint_for_no_such_vector():
    # `no_such_vector` is the very symptom the hint explains (a signal the
    # netlist's `.save` cards no longer retain), so it must not suppress it.
    diagnostics = [{"severity": "error", "code": "no_such_vector", "message": "x"}]

    assert sim._effective_save_mode("netlist", diagnostics) == "netlist"


@pytest.mark.parametrize("severity", ["info", "warning"])
def test_effective_save_mode_keeps_hint_without_an_error_diagnostic(severity):
    diagnostics = [{"severity": severity, "code": "note", "message": "x"}]

    assert sim._effective_save_mode("netlist", diagnostics) == "netlist"
    assert sim._effective_save_mode("netlist", []) == "netlist"
    assert sim._effective_save_mode("all", []) == "all"


def _no_value_messages(tmp_path, monkeypatch, run) -> list[str]:
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_CARD],
            "options": {"save_mode": "netlist"},
        },
    )
    monkeypatch.setattr(sim.subprocess, "run", run)
    report = sim.run_sim(str(request))
    (corner,) = report["corners"]
    return [d["message"] for d in corner["diagnostics"] if d["code"] == "measurement"]


def test_timed_out_corner_measurement_has_no_save_mode_hint(tmp_path, monkeypatch):
    def run(cmd, capture_output, text, timeout, cwd=None, env=None):
        raise sim.subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    messages = _no_value_messages(tmp_path, monkeypatch, run)

    assert messages
    assert not any('options.save_mode is "netlist"' in m for m in messages)


def test_clean_run_with_missing_value_keeps_save_mode_hint(tmp_path, monkeypatch):
    def run(cmd, capture_output, text, timeout, cwd=None, env=None):
        return fake_completed("** ngspice-99\n")

    messages = _no_value_messages(tmp_path, monkeypatch, run)

    assert any('options.save_mode is "netlist"' in m for m in messages)


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


# --------------------------------------------------------------------------- #
# Issue #5622: warn when the generated `save all` supersedes a body `.save`
# --------------------------------------------------------------------------- #

_SAVE_BODY = ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\n.save v(vdd) v(out)\n"
_TRAN = {"kind": "tran", "args": "1n 1u"}


def _supersede(report: dict) -> list[dict]:
    return [
        d for d in report.get("diagnostics", []) if d["code"] == "save_all_supersedes"
    ]


def _run_save_request(tmp_path, monkeypatch, *, body=_SAVE_BODY, **overrides):
    (tmp_path / "body.spice").write_text(body)
    options = overrides.pop("options", {})
    request = {"netlist": "body.spice", "analysis": _TRAN, "options": options}
    request.update(overrides)
    path = _write_request(tmp_path, request)
    _capture_decks(monkeypatch)
    return sim.run_sim(str(path))


@pytest.mark.parametrize(
    ("options", "warns"),
    [
        ({"waveforms": True}, True),
        ({"waveforms": True, "save_mode": "all"}, True),
        ({"waveforms": True, "save_mode": "netlist"}, False),
        ({"waveforms": True, "save_vectors": ["v(out)"]}, False),
        ({"waveforms": False}, False),
        ({}, False),
    ],
)
def test_supersede_warning_gating_matrix(tmp_path, monkeypatch, options, warns):
    report = _run_save_request(tmp_path, monkeypatch, options=options)
    found = _supersede(report)
    assert bool(found) is warns
    assert len(found) <= 1


def test_supersede_warning_without_body_save_is_absent(tmp_path, monkeypatch):
    report = _run_save_request(
        tmp_path,
        monkeypatch,
        body=".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\n",
        options={"waveforms": True},
    )
    assert _supersede(report) == []


def test_supersede_warning_names_both_facts_and_remedies(tmp_path, monkeypatch):
    report = _run_save_request(tmp_path, monkeypatch, options={"waveforms": True})
    (diag,) = _supersede(report)
    assert diag["severity"] == "warning"
    message = diag["message"]
    assert "restricts the saved signal set" in message
    assert ".save v(vdd) v(out)" in message
    assert "widens it" in message and "save all" in message
    assert 'options.save_mode to "netlist"' in message
    assert "options.save_vectors" in message
    assert "save_all_supersedes" in sim.DIAGNOSTIC_CODES


def test_supersede_warning_does_not_fail_a_run_under_fail_on_diagnostic(
    tmp_path, monkeypatch
):
    """Request-level, so per-corner grading is untouched."""
    report = _run_save_request(
        tmp_path,
        monkeypatch,
        options={"waveforms": True, "fail_on_diagnostic": ["save_all_supersedes"]},
    )
    assert _supersede(report)
    assert all(c["status"] != "inconclusive" for c in report["corners"])


def test_supersede_warning_is_in_the_dry_run_report(tmp_path, monkeypatch):
    (tmp_path / "body.spice").write_text(_SAVE_BODY)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": _TRAN, "options": {"waveforms": True}},
    )
    decks = _capture_decks(monkeypatch)
    report = sim.run_sim(str(request), dry_run=True)
    assert report["dry_run"] is True
    assert len(_supersede(report)) == 1
    assert decks == []


@pytest.mark.parametrize(
    "body",
    [
        # `save` command inside .control is a different construct
        ".param vdd=1\nR1 a 0 1k\n.control\nsave v(a)\n.endc\n",
        # subcircuit-local card
        ".param vdd=1\nR1 a 0 1k\n.subckt inner x y\nR2 x y 1k\n.save v(x)\n.ends\n",
        # `.probe` does not restrict on ngspice-47
        ".param vdd=1\nR1 a 0 1k\n.probe v(a)\n",
    ],
    ids=["control-block", "subckt", "probe"],
)
def test_supersede_warning_negative_constructs(tmp_path, monkeypatch, body):
    report = _run_save_request(
        tmp_path, monkeypatch, body=body, options={"waveforms": True}
    )
    assert _supersede(report) == []


@pytest.mark.parametrize(
    "body",
    [
        ".SAVE v(a)\nR1 a 0 1k\n",
        ".save v(a)\n.save v(b)\nR1 a 0 1k\n",
        ".save v(a)\n+ v(b)\nR1 a 0 1k\n",
    ],
    ids=["uppercase", "several-cards-one-warning", "continuation"],
)
def test_supersede_warning_edge_cases(tmp_path, monkeypatch, body):
    report = _run_save_request(
        tmp_path, monkeypatch, body=body, options={"waveforms": True}
    )
    assert len(_supersede(report)) == 1


def test_supersede_preflight_never_raises_on_an_unreadable_netlist(tmp_path):
    kwargs = {
        "engine": "ngspice",
        "save_mode": "all",
        "save_vectors": (),
        "want_waveforms": True,
    }
    assert sim._save_supersede_diagnostic(str(tmp_path / "missing"), **kwargs) is None
    binary = tmp_path / "bin.spice"
    binary.write_bytes(b"\xff\xfe\x00.save v(a)\n")
    sim._save_supersede_diagnostic(str(binary), **kwargs)  # must not raise


def test_supersede_preflight_skips_xyce(tmp_path):
    path = tmp_path / "body.spice"
    path.write_text(_SAVE_BODY)
    assert (
        sim._save_supersede_diagnostic(
            str(path),
            engine="xyce",
            save_mode="all",
            save_vectors=(),
            want_waveforms=True,
        )
        is None
    )


def test_supersede_warning_does_not_change_the_emitted_deck(tmp_path, monkeypatch):
    (tmp_path / "body.spice").write_text(_SAVE_BODY)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": _TRAN, "options": {"waveforms": True}},
    )
    decks = _capture_decks(monkeypatch)
    sim.run_sim(str(request))
    assert decks and "save all" in decks[0]


def test_raw_bytes_reports_the_kept_rawfile_size(tmp_path, monkeypatch):
    (tmp_path / "body.spice").write_text(_SAVE_BODY)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": _TRAN,
            "options": {
                "waveforms": True,
                "keep_artifacts": True,
                "waveform_max_bytes": 1,
            },
        },
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None):
        log_path = Path(cmd[cmd.index("-o") + 1])
        log_path.write_text("")
        (log_path.parent / "waveform.raw").write_text("x" * 123)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    artifacts = sim.run_sim(str(request))["corners"][0]["artifacts"]
    assert artifacts["raw_bytes"] == 123 == Path(artifacts["raw"]).stat().st_size


def test_raw_bytes_absent_when_no_rawfile_is_kept(tmp_path, monkeypatch):
    report = _run_save_request(tmp_path, monkeypatch, options={"waveforms": True})
    assert "raw_bytes" not in report["corners"][0]["artifacts"]
