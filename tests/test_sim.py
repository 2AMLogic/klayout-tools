"""Tests for `klt sim` and the `klayout_tools.sim` library.

Two tiers, per the issue's testing requirement (#91):

- **Unit tests** (the majority) exercise the corner-matrix expansion, log
  classification, `.meas` parsing, limit evaluation, rawfile parsing, and
  model-library resolution as pure functions -- either directly, or by
  stubbing `subprocess.run` so `run_sim`'s full per-corner pipeline is
  exercised without ever invoking the real `ngspice` binary. These always
  run, everywhere, and are the ones a classification/parsing regression
  actually gets caught by.
- **Integration tests** (`@pytest.mark.skipif(not HAVE_NGSPICE, ...)`) run
  the real `ngspice -b` subprocess end to end -- corner expansion, `.lib`
  process-corner selection, `alter`-based supply sweep, `.temp`, `.meas`
  extraction, and the optional waveform artifact -- against tiny, synthetic,
  non-PDK fixtures (see `examples/sim/generate.py`'s docstring for why no
  real PDK data is vendored here). CI installs `ngspice` via the package
  manager (`.github/workflows/ci.yml`) so these always run there; they skip
  with a clear reason on a dev machine without it, rather than silently
  passing.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import _paths as paths_module
from klayout_tools import pdk, remote_transport, sim, sim_batch, sim_remote
from klayout_tools import remote_launcher as rl
from klayout_tools.cli import main

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


@pytest.fixture(autouse=True)
def _bare_name_ngspice_resolution(monkeypatch):
    """Issue #2423: ``sim.py`` now resolves the ``ngspice`` binary via
    :func:`klayout_tools._paths.resolve_tool_binary` (``shutil.which``)
    *before* dispatching any corner, rather than hardcoding the bare name
    ``ngspice`` in ``argv[0]`` -- and, since ``ngspice`` is `klt sim`'s
    *default* engine, that resolution now runs for every mocked-
    ``subprocess.run`` test in this module too (not just ones that opt into
    an ``engine: "netgen"``-style non-default engine, as `klt lvs`'s
    analogous #2373 stub only had to cover). Several tests below are
    explicitly designed to need "no binary required" (this module's own
    ``# run_sim with a stubbed ngspice subprocess (no binary required)``
    section header) -- this autouse fixture keeps that promise true even on
    a host with no real ``ngspice`` install, by stubbing resolution to
    return the bare name unchanged (mirrors ``test_equiv.py``'s identically-
    named fixture, issue #2423). A real ``subprocess.run(["ngspice", ...])``
    still resolves via the OS's own ``PATH`` search exactly as before this
    issue, so tests that genuinely need a real ``ngspice`` (already gated on
    their own ``shutil.which("ngspice")``-based skip) are unaffected.
    """
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ngspice":
            return cmd
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(paths_module.shutil, "which", fake_which)


#: `KLT_SKIP_NGSPICE_TESTS=1` (local-only, set by `npm run check:ci` -- never
#: by CI) opts a host with ngspice installed out of this slow tier too; see
#: `tests/test_extract.py`'s `HAVE_NGSPICE` for the full rationale (issue #1651).
HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "sim"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _write_body(tmp_path: Path, name: str = "body.spice") -> Path:
    path = tmp_path / name
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_corner_lib(tmp_path: Path, name: str = "corner.lib") -> Path:
    path = tmp_path / name
    path.write_text(
        ".lib tt\n.param corner_scale=1.0\n.endl tt\n\n"
        ".lib ss\n.param corner_scale=0.9\n.endl ss\n"
    )
    return path


def _write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# load_request
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(sim.SimError, match="not found"):
        sim.load_request(str(tmp_path / "nope.json"))


def test_load_request_is_a_directory(tmp_path):
    with pytest.raises(sim.SimError, match="not a file"):
        sim.load_request(str(tmp_path))


def test_load_request_not_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("this is not json {{{")
    with pytest.raises(sim.SimError, match="not valid JSON"):
        sim.load_request(str(path))


def test_load_request_non_object(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(sim.SimError, match="JSON object"):
        sim.load_request(str(path))


@pytest.mark.parametrize("missing_field", ["netlist", "analysis"])
def test_load_request_missing_required_field(tmp_path, missing_field):
    request = {"netlist": "x.spice", "analysis": {"kind": "tran", "args": "1n 1u"}}
    del request[missing_field]
    path = _write_request(tmp_path, request)
    with pytest.raises(sim.SimError, match=missing_field):
        sim.load_request(str(path))


def test_load_request_does_not_require_models_or_schema(tmp_path):
    # `models` is validated in run_sim (only when corners.process is set);
    # a bare "schema" field (the spike's shape) is never required.
    request = {"netlist": "x.spice", "analysis": {"kind": "tran", "args": "1n 1u"}}
    path = _write_request(tmp_path, request)
    assert sim.load_request(str(path)) == request


# --------------------------------------------------------------------------- #
# run_sim: request-level validation (raised before any corner runs)
# --------------------------------------------------------------------------- #


def test_run_sim_unsupported_engine_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            # Issue #2016 landed an `xyce` execution path; this gate is for
            # engine names no path recognises at all.
            "engine": "spectre",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported engine"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# run_sim: engine "xyce" request-level gates (issue #2016)
#
# Each unimplemented combination is refused up front with a SimError naming
# what IS supported, before any corner runs.
# --------------------------------------------------------------------------- #


def test_run_sim_xyce_refuses_remote_backend(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "backend": "remote",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="xyce.*local backends"):
        sim.run_sim(str(request))


def test_run_sim_xyce_refuses_supply_corners(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "dc", "args": "Vdd 0 2 0.5"},
            "corners": {"supply_v": {"vdd": [1.0, 1.8]}},
        },
    )
    with pytest.raises(sim.SimError, match="xyce.*supply_v"):
        sim.run_sim(str(request))


def test_run_sim_xyce_refuses_monte_carlo(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "monte_carlo": {"n": 3, "seed": 7, "vary": "process"},
        },
    )
    with pytest.raises(sim.SimError, match="xyce.*monte_carlo"):
        sim.run_sim(str(request))


def test_run_sim_xyce_refuses_fail_fast_probe(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"fail_fast_probe": True},
        },
    )
    with pytest.raises(sim.SimError, match="fail_fast_probe.*xyce"):
        sim.run_sim(str(request))


def test_run_sim_xyce_missing_binary_reports_per_corner_error(tmp_path, monkeypatch):
    # A missing Xyce binary is not an exception: like a missing ngspice, it
    # folds into per-corner `status: "error"` diagnostics ("every corner is
    # reported"), so the caller sees which engine failed to launch. The
    # binary name is monkeypatched rather than PATH-manipulated so the test
    # is deterministic whether or not Xyce is installed.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "op", "args": ""},
        },
    )
    monkeypatch.setattr(sim, "XYCE_BINARY", "xyce-not-on-path-xyz")
    report = sim.run_sim(str(request))
    assert report["corner_count"] == 1
    corner = report["corners"][0]
    assert corner["status"] == "error"
    assert any(
        "could not launch" in d["message"] and "xyce-not-on-path-xyz" in d["message"]
        for d in corner["diagnostics"]
    )


# --------------------------------------------------------------------------- #
# Xyce deck generation and log parsing (issue #2016)
#
# Pure-function tests, no binary required: the deck Xyce receives must not
# carry the ngspice-only cards Xyce ignores or rejects (.control/alter,
# .temp), and the parser must read what Xyce actually writes.
# --------------------------------------------------------------------------- #


def _xyce_point() -> sim.CornerPoint:
    return sim.CornerPoint(
        process=None,
        supply_v={},
        temperature_c=85,
    )


def _xyce_bundle_point() -> sim.CornerPoint:
    return sim.CornerPoint(
        process="tt",
        supply_v={},
        temperature_c=85,
        process_sections=["tt", "bjt_tt"],
    )


def test_write_xyce_deck_shape(tmp_path):
    deck = tmp_path / "corner.cir"
    sim._write_xyce_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        point=_xyce_point(),
        analysis={"kind": "tran", "args": "1n 10u"},
        measurements_spec=[{"name": "vout", "spice": ".meas tran vmax MAX v(out)"}],
    )
    text = deck.read_text()
    # The analysis is a top-level dot card (no .control block to live in).
    assert ".tran 1n 10u" in text
    # Temperature rides `.options device temp=` -- a `.temp` card is a
    # silent no-op in Xyce (verified against 7.10.0).
    assert ".options device temp=85" in text
    assert ".temp " not in text
    # No ngspice-only machinery ever reaches a Xyce deck.
    assert ".control" not in text
    assert "alter " not in text
    # The request's .meas cards pass through verbatim, and the deck ends.
    assert ".meas tran vmax MAX v(out)" in text
    assert text.rstrip().endswith(".end")


def test_write_xyce_deck_process_corner_lib_cards(tmp_path):
    deck = tmp_path / "corner.cir"
    sim._write_xyce_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib="/pdk/corner.lib",
        point=_xyce_bundle_point(),
        analysis={"kind": "op", "args": ""},
        measurements_spec=[],
    )
    text = deck.read_text()
    assert ".lib /pdk/corner.lib tt" in text
    assert ".lib /pdk/corner.lib bjt_tt" in text


def test_extract_xyce_version_from_log():
    log = (
        "*****\n***** Welcome to the Xyce(TM) Parallel Electronic Simulator\n"
        "*****\n***** This is version XyceNF Release 7.10.0\n"
        "***** Date: Tue Sep 22 12:10:20 PDT 2026\n"
    )
    assert sim._extract_xyce_version(log) == "7.10.0"
    # The open-source (non-NORAD) build labels itself without the NF.
    opensource_banner = "***** This is version Xyce Release 7.9\n"
    assert sim._extract_xyce_version(opensource_banner) == "7.9"
    assert sim._extract_xyce_version("no banner here") is None


def test_parse_xyce_measurements():
    # The exact shapes XyceNF 7.10.0 prints in its "Measure Functions"
    # section: value lines with analysis-specific trailers, FAILED lines,
    # and the summary lines around the section (which must not parse).
    log = (
        "***** Netlist sensitive analysis...\n"
        "\n ***** Measure Functions ***** \n"
        "\n"
        "VMAX = 9.691320e-01 at time = 6.544640e-09\n"
        "\n"
        "VOUT_MID = 5.000000e-01 for AT = 2.500000e+00\n"
        "\n"
        "TPHL = 1.391548e-09 with targ = 3.641548e-09 and trig = 2.250000e-09\n"
        "\n"
        "TDEAD = FAILED with targ = not found and trig = not found\n"
        "\n"
        "Measure Start Time= 0.000000e+00\tMeasure End Time= 8.000000e-09\n"
        "***** Total Simulation Solvers Run Time: 0.01 seconds\n"
    )
    values = sim._parse_xyce_measurements(log)
    # Xyce upper-cases names; the parser lower-cases them for the
    # case-insensitive lookup `_run_corner` performs.
    assert values == {
        "vmax": pytest.approx(9.691320e-01),
        "vout_mid": pytest.approx(5.000000e-01),
        "tphl": pytest.approx(1.391548e-09),
    }
    # A failed measurement is absent, never zero-parsed into a value.
    assert "tdead" not in values
    # Section-scoped: nothing outside "Measure Functions" parses.
    assert sim._parse_xyce_measurements("Time= 1.0 V(a)=2.0") == {}


def test_classify_xyce_diagnostics():
    log = (
        "Netlist error in file lib at or near line 3\n"
        " Simulation aborted due to error.\n"
    )
    diagnostics = sim._classify_xyce_diagnostics(log)
    codes = [d["code"] for d in diagnostics]
    assert "netlist" in codes
    assert "unknown" in codes
    # All Xyce classifications start fatal; `_recovered_from_stepping` owns
    # any downgrade (same rule as the ngspice path).
    assert all(d["severity"] == "error" for d in diagnostics)
    assert sim._classify_xyce_diagnostics("***** End of Xyce(TM) Simulation") == []


def test_xyce_downgraded_singular_matrix_when_measurements_returned():
    # Xyce's Amesos "numerically singular matrix, returning zero" narration
    # can accompany a run that ultimately produced every measurement; the
    # same recovery rule as ngspice's stepping narration applies.
    assert sim._recovered_from_stepping(
        [{"name": "v"}],
        [{"name": "v", "value": 1.0, "status": "pass", "unit": None, "margin": None}],
        "Netlist warning: Numerically singular matrix found by Amesos",
        aborted_re=sim._XYCE_SIMULATION_ABORTED_RE,
    )
    # But never when a measurement is missing or the abort trailer fired.
    assert not sim._recovered_from_stepping(
        [{"name": "v"}],
        [{"name": "v", "value": None, "status": "error", "unit": None, "margin": None}],
        "Netlist warning: Numerically singular matrix found by Amesos",
        aborted_re=sim._XYCE_SIMULATION_ABORTED_RE,
    )
    assert not sim._recovered_from_stepping(
        [{"name": "v"}],
        [{"name": "v", "value": 1.0, "status": "pass", "unit": None, "margin": None}],
        "Simulation aborted due to error",
        aborted_re=sim._XYCE_SIMULATION_ABORTED_RE,
    )


def test_run_sim_unsupported_backend_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "bogus",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported backend"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# $KLT_SIM_BACKEND host default (2am#1004)
# --------------------------------------------------------------------------- #


def test_resolve_backend_precedence(monkeypatch):
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    # flag > request > env > local
    assert sim.resolve_backend("local", {"backend": "remote"}) == ("local", False)
    assert sim.resolve_backend(None, {"backend": "local"}) == ("local", False)
    assert sim.resolve_backend(None, {}) == ("batch", True)
    monkeypatch.setenv(sim.BACKEND_ENV, "  ")
    assert sim.resolve_backend(None, {}) == ("local", False)
    monkeypatch.delenv(sim.BACKEND_ENV)
    assert sim.resolve_backend(None, {}) == ("local", False)


def test_yield_host_default_backend_only_overrides_env_sourced_offhost():
    y = sim._yield_host_default_backend
    assert y("batch", True, reason_ok=False) == "local"
    assert y("batch", True, reason_ok=True) == "batch"
    # An explicit choice is never second-guessed.
    assert y("batch", False, reason_ok=False) == "batch"
    # An on-host env default has nothing to step back from.
    assert y("local-parallel", True, reason_ok=False) == "local-parallel"


_HOST_DEFAULT_LOG = (
    "  Measurements for Transient Analysis\n\nvout                =  1.00000e+00\n"
)


class _BatchCalled(Exception):
    pass


def _host_default_request(tmp_path, corners):
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": corners,
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )


def _arm_fake_batch(monkeypatch):
    def fake_batch(*args, **kwargs):
        raise _BatchCalled()

    monkeypatch.setitem(sim._BACKENDS, "batch", fake_batch)


def test_host_default_batch_sends_a_multi_corner_grid_offhost(tmp_path, monkeypatch):
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    _arm_fake_batch(monkeypatch)
    request = _host_default_request(
        tmp_path, {"process": ["tt", "ss"], "temperature_c": [-40, 125]}
    )
    with pytest.raises(_BatchCalled):
        sim.run_sim(str(request))


def test_host_default_batch_keeps_a_single_corner_probe_local(tmp_path, monkeypatch):
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    _arm_fake_batch(monkeypatch)
    _stub_subprocess_run(
        monkeypatch,
        log_text=_HOST_DEFAULT_LOG,
    )
    request = _host_default_request(tmp_path, {"process": ["tt"]})
    report = sim.run_sim(str(request))
    # Ran here: the fake batch backend would have raised.
    assert len(report["corners"]) == 1


def test_host_default_never_overrides_an_explicit_local_flag(tmp_path, monkeypatch):
    monkeypatch.setenv(sim.BACKEND_ENV, "batch")
    _arm_fake_batch(monkeypatch)
    _stub_subprocess_run(
        monkeypatch,
        log_text=_HOST_DEFAULT_LOG,
    )
    request = _host_default_request(
        tmp_path, {"process": ["tt", "ss"], "temperature_c": [-40, 125]}
    )
    report = sim.run_sim(str(request), backend="local")
    assert len(report["corners"]) == 4


def test_host_default_unknown_value_is_named_as_env_sourced(tmp_path, monkeypatch):
    monkeypatch.setenv(sim.BACKEND_ENV, "bogus")
    request = _host_default_request(tmp_path, {"process": ["tt", "ss"]})
    with pytest.raises(
        sim.SimError, match=r"unsupported backend 'bogus' \(from \$KLT_SIM_BACKEND\)"
    ):
        sim.run_sim(str(request))


def test_run_sim_unsupported_backend_via_cli_flag_raises(tmp_path):
    # The --backend flag overrides the request field and is validated the
    # same way (unknown name -> SimError, not a silent fallback to local).
    # "remote" is a real, implemented backend as of #265 -- use a genuinely
    # unsupported name here instead.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported backend"):
        sim.run_sim(str(request), backend="quantum")


def test_run_sim_netlist_not_found_raises(tmp_path):
    request = _write_request(
        tmp_path,
        {"netlist": "missing.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    with pytest.raises(sim.SimError, match="netlist not found"):
        sim.run_sim(str(request))


def test_run_sim_process_corner_without_models_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="models.lib"):
        sim.run_sim(str(request))


def test_run_sim_analysis_missing_fields_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran"}},
    )
    with pytest.raises(sim.SimError, match="analysis"):
        sim.run_sim(str(request))


def test_run_sim_measurement_missing_fields_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [{"name": "vout"}],
        },
    )
    with pytest.raises(sim.SimError, match="measurements"):
        sim.run_sim(str(request))


def test_run_sim_netlist_source_invalid_value_raises(tmp_path):
    # Per the JSON contract's error shape (docs/json-contract.md), an
    # unrecognized netlist_source is a loud application error (SimError,
    # exit 1) -- never silently ignored or coerced.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "netlist_source": "post-layout",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported netlist_source"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# `.meas op` rejection -- ngspice has no `.MEASURE OP` analysis type (#205)
# --------------------------------------------------------------------------- #


def test_validate_meas_card_rejects_op():
    with pytest.raises(sim.SimError, match=r"\.MEASURE OP"):
        sim._validate_meas_card("vout_meas", ".meas op vout_meas find v(out)")


def test_validate_meas_card_accepts_supported_types():
    # Every ngspice-implemented `.meas` type is accepted -- no SimError.
    for card in (
        ".meas dc vout find v(out) at=1.0",
        ".meas ac vout find v(out) at=1k",
        ".meas tran vout find v(out) at=1n",
        ".meas sp vout find v(out) at=1k",
        ".measure tran vout find v(out) at=1n",  # long-form spelling
    ):
        sim._validate_meas_card("vout", card)  # must not raise


def test_run_sim_analysis_kind_op_with_meas_op_card_raises_clear_error(tmp_path):
    # The exact pairing this issue reports: `analysis.kind: "op"` paired with
    # a `.meas op` card must fail with a clear, actionable SimError *before*
    # ngspice ever runs -- never a raw
    # "Error: unrecognized analysis type 'op'" ngspice parse failure.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "op", "args": ""},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas op vout_meas find v(out)"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match=r"\.MEASURE OP"):
        sim.run_sim(str(request))


def test_run_sim_meas_op_card_rejected_even_with_non_op_analysis_kind(tmp_path):
    # A `.meas op` card is invalid on ngspice's own terms regardless of the
    # request's `analysis.kind` -- not just when the two literally match.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas op vout_meas find v(out)"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match="unsupported .meas analysis type"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Model-library resolution (klt pdk integration, #45)
# --------------------------------------------------------------------------- #


def test_resolve_models_lib_via_pdk_variant(tmp_path):
    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech" / "ngspice"
    variant_dir.mkdir(parents=True)
    lib_path = variant_dir / "sky130.lib.spice"
    lib_path.write_text(".lib tt\n.endl tt\n")
    # A minimal libs.tech marker so pdk.find_pdk recognises the variant.
    (install_root / "sky130A" / "libs.tech" / "klayout").mkdir()

    resolved = sim._resolve_models_lib(
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sky130.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert resolved == str(lib_path)


def test_resolve_models_lib_via_pdk_missing_install_raises(tmp_path):
    with pytest.raises(sim.SimError):
        sim._resolve_models_lib(
            {
                "pdk": "sky130A",
                "pdk_root": str(tmp_path / "nope"),
                "lib": "sky130.lib.spice",
            },
            request_dir=str(tmp_path),
        )


def test_resolve_models_lib_env_var_expansion(tmp_path, monkeypatch):
    lib_path = _write_corner_lib(tmp_path)
    monkeypatch.setenv("PDK_ROOT", str(tmp_path))

    resolved = sim._resolve_models_lib(
        {"lib": "$PDK_ROOT/corner.lib"}, request_dir=str(tmp_path)
    )

    assert resolved == str(lib_path)


def test_resolve_models_lib_relative_to_request_dir(tmp_path):
    _write_corner_lib(tmp_path)

    resolved = sim._resolve_models_lib({"lib": "corner.lib"}, request_dir=str(tmp_path))

    assert resolved == str(tmp_path / "corner.lib")


def test_resolve_models_lib_missing_lib_field_raises(tmp_path):
    with pytest.raises(sim.SimError, match="models.lib"):
        sim._resolve_models_lib({}, request_dir=str(tmp_path))


def test_resolve_models_lib_missing_file_raises(tmp_path):
    with pytest.raises(sim.SimError, match="not found"):
        sim._resolve_models_lib({"lib": "nope.lib"}, request_dir=str(tmp_path))


# --------------------------------------------------------------------------- #
# Corner-matrix expansion
# --------------------------------------------------------------------------- #


def test_expand_corners_defaults_to_single_point():
    points = sim._expand_corners({}, [])

    assert len(points) == 1
    (point,) = points
    assert point.process is None
    assert point.supply_v == {}
    assert point.temperature_c == 27
    assert point.corner_id == "default/novdd/27C"


def test_expand_corners_cross_product_order():
    points = sim._expand_corners(
        {
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.62, 1.98]},
            "temperature_c": [-40, 125],
        },
        [],
    )

    # process outermost, temperature innermost -- odometer-style.
    ids = [p.corner_id for p in points]
    assert ids == [
        "tt/1.620V/-40C",
        "tt/1.620V/125C",
        "tt/1.980V/-40C",
        "tt/1.980V/125C",
        "ss/1.620V/-40C",
        "ss/1.620V/125C",
        "ss/1.980V/-40C",
        "ss/1.980V/125C",
    ]


def test_expand_corners_exclude_partial_match():
    points = sim._expand_corners(
        {"process": ["tt", "ss"], "temperature_c": [-40, 125]},
        [{"process": "ss", "temperature_c": -40}],
    )

    ids = [p.corner_id for p in points]
    assert "ss/novdd/-40C" not in ids
    assert len(ids) == 3


def test_expand_corners_supply_rails_move_together():
    points = sim._expand_corners(
        {"supply_v": {"vdd": [1.62, 1.98], "vdda": [1.7, 2.0]}}, []
    )

    assert len(points) == 2
    assert points[0].supply_v == {"vdd": 1.62, "vdda": 1.7}
    assert points[1].supply_v == {"vdd": 1.98, "vdda": 2.0}


def test_expand_corners_mismatched_supply_lengths_raises():
    with pytest.raises(sim.SimError, match="same length"):
        sim._expand_corners({"supply_v": {"vdd": [1.62, 1.98], "vdda": [1.7]}}, [])


# --------------------------------------------------------------------------- #
# corners.process bundle form: {"name": str, "sections": list[str]}
# --------------------------------------------------------------------------- #


def test_expand_corners_bare_string_process_is_unchanged():
    """Regression: a bare-string `corners.process` entry keeps producing a
    `CornerPoint` with `process_sections=None` -- the exact shape
    `_write_corner_deck` used before the bundle form existed."""
    points = sim._expand_corners({"process": ["tt", "ss"]}, [])

    assert [p.process for p in points] == ["tt", "ss"]
    assert [p.process_sections for p in points] == [None, None]
    assert [p.corner_id for p in points] == ["tt/novdd/27C", "ss/novdd/27C"]


def test_expand_corners_bundle_process_entry():
    points = sim._expand_corners(
        {
            "process": [
                "tt",
                {
                    "name": "ss",
                    "sections": [
                        "ss",
                        "bjt_ss",
                        "diode_ss",
                        "res_ss",
                        "moscap_ss",
                        "mimcap_ss",
                    ],
                },
            ]
        },
        [],
    )

    assert [p.process for p in points] == ["tt", "ss"]
    assert points[0].process_sections is None
    assert points[1].process_sections == [
        "ss",
        "bjt_ss",
        "diode_ss",
        "res_ss",
        "moscap_ss",
        "mimcap_ss",
    ]
    # corner_id/slug use the bundle's `name` -- report shape is unaffected.
    assert points[1].corner_id == "ss/novdd/27C"
    assert points[1].slug == "ss_novdd_27C"


def test_expand_corners_bundle_single_section_matches_bare_string_shape():
    """A single-section bundle is functionally equivalent to a bare string
    (see the issue's edge-case list) -- same `process`, same `corner_id`,
    just routed through the loop path in `_write_corner_deck` instead of the
    single-line path."""
    (bare,) = sim._expand_corners({"process": ["tt"]}, [])
    (bundle,) = sim._expand_corners(
        {"process": [{"name": "tt", "sections": ["tt"]}]}, []
    )

    assert bare.process == bundle.process == "tt"
    assert bare.corner_id == bundle.corner_id


@pytest.mark.parametrize(
    "entry,match",
    [
        ({"sections": ["tt"]}, "name"),
        ({"name": "", "sections": ["tt"]}, "name"),
        ({"name": 5, "sections": ["tt"]}, "name"),
        ({"name": "tt"}, "sections"),
        ({"name": "tt", "sections": []}, "sections"),
        ({"name": "tt", "sections": "tt"}, "sections"),
        ({"name": "tt", "sections": [1, 2]}, "sections"),
        ({"name": "tt", "sections": ["tt", ""]}, "sections"),
        (5, "string or an object"),
    ],
)
def test_expand_corners_bundle_process_entry_validation(entry, match):
    with pytest.raises(sim.SimError, match=match):
        sim._expand_corners({"process": [entry]}, [])


def test_expand_corners_exclude_matches_bundle_corner_by_name():
    """`corners.exclude[].process` compares against the bundle's `name`, not
    the raw `{"name", "sections"}` object -- an object `!=` a bare string
    would silently never match without this."""
    points = sim._expand_corners(
        {
            "process": [
                "tt",
                {"name": "ss", "sections": ["ss", "bjt_ss"]},
            ],
            "temperature_c": [-40, 125],
        },
        [{"process": "ss", "temperature_c": -40}],
    )

    ids = [p.corner_id for p in points]
    assert "ss/novdd/-40C" not in ids
    assert len(ids) == 3


# --------------------------------------------------------------------------- #
# _write_corner_deck: .lib card generation
# --------------------------------------------------------------------------- #


def _write_deck(tmp_path: Path, point: sim.CornerPoint, **overrides) -> list[str]:
    deck_path = tmp_path / "deck.spice"
    kwargs = {
        "deck_path": str(deck_path),
        "netlist_path": str(tmp_path / "body.spice"),
        "models_lib": str(tmp_path / "corner.lib"),
        "point": point,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "raw_path": None,
    }
    kwargs.update(overrides)
    sim._write_corner_deck(**kwargs)
    return deck_path.read_text().splitlines()


def test_write_corner_deck_bare_string_process_emits_one_lib_line(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    lines = _write_deck(tmp_path, point)

    lib_lines = [line for line in lines if line.startswith(".lib")]
    assert lib_lines == [f".lib {tmp_path / 'corner.lib'} tt"]


def test_write_corner_deck_bundle_process_emits_one_lib_line_per_section_in_order(
    tmp_path,
):
    point = sim.CornerPoint(
        "ss",
        {},
        27,
        process_sections=[
            "ss",
            "bjt_ss",
            "diode_ss",
            "res_ss",
            "moscap_ss",
            "mimcap_ss",
        ],
    )
    lines = _write_deck(tmp_path, point)

    lib_lines = [line for line in lines if line.startswith(".lib")]
    models_lib = tmp_path / "corner.lib"
    assert lib_lines == [
        f".lib {models_lib} ss",
        f".lib {models_lib} bjt_ss",
        f".lib {models_lib} diode_ss",
        f".lib {models_lib} res_ss",
        f".lib {models_lib} moscap_ss",
        f".lib {models_lib} mimcap_ss",
    ]


def test_write_corner_deck_no_process_emits_no_lib_line(tmp_path):
    point = sim.CornerPoint(None, {}, 27)
    lines = _write_deck(tmp_path, point)

    assert not any(line.startswith(".lib") for line in lines)


# --------------------------------------------------------------------------- #
# _write_corner_deck: "save all" (issue #2521)
# --------------------------------------------------------------------------- #


def test_write_corner_deck_no_measurements_or_waveforms_omits_save_all(tmp_path):
    """Regression: a corner with neither `measurements[]` nor
    `options.waveforms` (the probe-deck caller, and the historical
    no-measurement/no-waveform request) stays byte-identical to
    pre-#2521 behavior -- no `save all` card at all."""
    point = sim.CornerPoint("tt", {}, 27)
    lines = _write_deck(tmp_path, point, measurements_spec=[], raw_path=None)

    assert "save all" not in lines


def test_write_corner_deck_with_measurements_emits_save_all(tmp_path):
    point = sim.CornerPoint("tt", {}, 27)
    lines = _write_deck(
        tmp_path,
        point,
        measurements_spec=[
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        raw_path=None,
    )

    assert "save all" in lines


def test_write_corner_deck_with_waveforms_only_emits_save_all(tmp_path):
    """`options.waveforms` alone (no `measurements[]`) is also enough --
    the acceptance criteria's other declared-signal-set trigger."""
    point = sim.CornerPoint("tt", {}, 27)
    raw_path = str(tmp_path / "waveform.raw")
    lines = _write_deck(tmp_path, point, measurements_spec=[], raw_path=raw_path)

    assert "save all" in lines


def test_write_corner_deck_save_all_comes_after_include_and_before_alter(tmp_path):
    """Ordering matters: `save all` must appear after `.include` (so it
    supersedes any `.save` the included netlist body carries -- ngspice
    applies `save` cards in the order encountered) and before the `alter`
    cards / analysis line."""
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 27)
    lines = _write_deck(
        tmp_path,
        point,
        measurements_spec=[
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        raw_path=None,
    )

    include_idx = next(i for i, line in enumerate(lines) if line.startswith(".include"))
    save_idx = lines.index("save all")
    alter_idx = next(i for i, line in enumerate(lines) if line.startswith("alter"))
    analysis_idx = next(i for i, line in enumerate(lines) if line.startswith("tran "))

    assert include_idx < save_idx < alter_idx < analysis_idx
    # With no `options.osdi_preload` declared, `save all` is the first
    # command inside the `.control` block.
    control_idx = lines.index(".control")
    assert save_idx == control_idx + 1


def test_write_corner_deck_save_all_follows_pre_osdi_and_still_precedes_alter(
    tmp_path,
):
    """`options.osdi_preload` (issue #2513) also writes into the top of the
    `.control` block. The two are order-independent -- a `pre_`-prefixed
    command is hoisted out and run before the circuit is parsed, so it
    neither reads nor writes the saved set -- so #2513 keeps its position
    and `save all` follows it, still after `.include` (superseding any
    `.save` the netlist body carries) and still before the `alter` cards
    and the analysis line, which is all this issue's ordering requires."""
    osdi = tmp_path / "psp103.osdi"
    osdi.write_bytes(b"\x7fELF-ish")
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 27)
    lines = _write_deck(
        tmp_path,
        point,
        measurements_spec=[
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        raw_path=None,
        osdi_preload=(str(osdi),),
    )

    control_idx = lines.index(".control")
    include_idx = next(i for i, line in enumerate(lines) if line.startswith(".include"))
    pre_osdi_idx = lines.index(f"pre_osdi {osdi}")
    save_idx = lines.index("save all")
    alter_idx = next(i for i, line in enumerate(lines) if line.startswith("alter"))
    analysis_idx = next(i for i, line in enumerate(lines) if line.startswith("tran "))

    # This issue's invariant is unchanged: after `.include`, before analysis.
    assert include_idx < save_idx < alter_idx < analysis_idx
    # #2513's `pre_osdi` block stays at the top of `.control`, with
    # `save all` immediately after it.
    assert control_idx < pre_osdi_idx < save_idx
    assert pre_osdi_idx == control_idx + 1
    assert save_idx == pre_osdi_idx + 1


def test_corner_id_multi_rail_format():
    point = sim.CornerPoint("tt", {"vdd": 1.8, "vdda": 1.7}, 27)
    assert point.corner_id == "tt/vdd=1.800_vdda=1.700V/27C"


def test_corner_slug_is_filesystem_safe():
    point = sim.CornerPoint("ss", {"vdd": 1.62}, -40)
    assert "/" not in point.slug
    assert point.slug == "ss_1p620V_n40C"


def test_corner_id_and_slug_get_sample_suffix():
    point = sim.CornerPoint(
        "tt",
        {"vdd": 1.8},
        27,
        sample_index=3,
        mc_seed={"process_seed": 1, "mismatch_seed": 2, "rndseed": 3},
    )
    assert point.corner_id == "tt/1.800V/27C/mc3"
    assert point.slug == "tt_1p800V_27C_mc3"


# --------------------------------------------------------------------------- #
# Monte Carlo sampling: request validation, seed contract, negative control
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mc_spec,match",
    [
        ({"seed": 1, "vary": "mismatch"}, "monte_carlo.n"),
        ({"n": 0, "seed": 1, "vary": "mismatch"}, "monte_carlo.n"),
        ({"n": "5", "seed": 1, "vary": "mismatch"}, "monte_carlo.n"),
        ({"n": 5, "vary": "mismatch"}, "monte_carlo.seed"),
        ({"n": 5, "seed": "abc", "vary": "mismatch"}, "monte_carlo.seed"),
        ({"n": 5, "seed": 1}, "monte_carlo.vary"),
        ({"n": 5, "seed": 1, "vary": "bogus"}, "monte_carlo.vary"),
    ],
)
def test_validate_monte_carlo_spec_rejects_invalid(mc_spec, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_monte_carlo_spec(mc_spec)


def test_validate_monte_carlo_spec_accepts_every_vary_value():
    for vary in sim.SUPPORTED_MC_VARY:
        assert sim._validate_monte_carlo_spec({"n": 3, "seed": 42, "vary": vary}) == (
            3,
            42,
            vary,
        )


def test_expand_monte_carlo_produces_n_samples_per_corner():
    corner_points = sim._expand_corners({"process": ["tt", "ss"]}, [])
    sampled, info = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 1, "vary": "both"}
    )

    assert info == {"n": 3, "seed": 1, "vary": "both"}
    assert len(sampled) == 6  # 2 corners x 3 samples
    ids = [p.corner_id for p in sampled]
    assert len(set(ids)) == 6  # every corner_id/artifact path is unique
    assert ids == [
        "tt/novdd/27C/mc0",
        "tt/novdd/27C/mc1",
        "tt/novdd/27C/mc2",
        "ss/novdd/27C/mc0",
        "ss/novdd/27C/mc1",
        "ss/novdd/27C/mc2",
    ]


def test_expand_monte_carlo_is_reproducible_given_the_same_seed():
    corner_points = sim._expand_corners({"process": ["tt"]}, [])
    mc_spec = {"n": 5, "seed": 20260801, "vary": "both"}

    sampled_a, _ = sim._expand_monte_carlo(corner_points, mc_spec)
    sampled_b, _ = sim._expand_monte_carlo(corner_points, mc_spec)

    seeds_a = [p.mc_seed for p in sampled_a]
    seeds_b = [p.mc_seed for p in sampled_b]
    assert seeds_a == seeds_b
    # Not a broken/no-op sampler: consecutive samples actually differ.
    assert len({tuple(s.values()) for s in seeds_a}) == 5


def test_expand_monte_carlo_different_seed_diverges():
    corner_points = sim._expand_corners({}, [])
    sampled_a, _ = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 1, "vary": "both"}
    )
    sampled_b, _ = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 2, "vary": "both"}
    )
    assert [p.mc_seed for p in sampled_a] != [p.mc_seed for p in sampled_b]


@pytest.mark.parametrize(
    "vary,varying_key,pinned_key",
    [
        ("process", "process_seed", "mismatch_seed"),
        ("mismatch", "mismatch_seed", "process_seed"),
    ],
)
def test_expand_monte_carlo_negative_control(vary, varying_key, pinned_key):
    """The deterministic negative control this issue requires: the axis
    `vary` does *not* request stays identical (sigma=0) across every sample
    of a corner, while the requested axis actually varies -- a broken/no-op
    sampler that always returns the same seeds for both axes (or that
    varies both regardless of `vary`) fails this test."""
    corner_points = sim._expand_corners({}, [])
    sampled, _ = sim._expand_monte_carlo(
        corner_points, {"n": 4, "seed": 7, "vary": vary}
    )

    pinned_values = {p.mc_seed[pinned_key] for p in sampled}
    assert len(pinned_values) == 1  # sigma=0: identical across every sample

    varying_values = {p.mc_seed[varying_key] for p in sampled}
    assert len(varying_values) == 4  # actually varies, not silently pinned too


def test_expand_monte_carlo_both_varies_every_axis():
    corner_points = sim._expand_corners({}, [])
    sampled, _ = sim._expand_monte_carlo(
        corner_points, {"n": 4, "seed": 7, "vary": "both"}
    )
    assert len({p.mc_seed["process_seed"] for p in sampled}) == 4
    assert len({p.mc_seed["mismatch_seed"] for p in sampled}) == 4


def test_expand_monte_carlo_negative_control_is_per_corner():
    # The pinned axis is constant *within* a corner's own sample set, but
    # different corners still derive independent (not globally identical)
    # pinned values -- the negative control isn't a single hard-coded
    # sentinel that would mask a broken corner_index derivation.
    corner_points = sim._expand_corners({"process": ["tt", "ss"]}, [])
    sampled, _ = sim._expand_monte_carlo(
        corner_points, {"n": 3, "seed": 7, "vary": "process"}
    )
    by_process = {"tt": [], "ss": []}
    for point in sampled:
        by_process[point.process].append(point.mc_seed["mismatch_seed"])

    assert len(set(by_process["tt"])) == 1
    assert len(set(by_process["ss"])) == 1
    assert by_process["tt"][0] != by_process["ss"][0]


# --------------------------------------------------------------------------- #
# .meas log parsing
# --------------------------------------------------------------------------- #


def test_parse_measurements_success():
    log = (
        "  Measurements for Transient Analysis\n\n"
        "vout_final          =  1.00000e+00\n"
        "vout_avg            =  9.50000e-01 from=  5.00000e-06 to=  1.00000e-05\n"
    )
    assert sim._parse_measurements(log) == {"vout_final": 1.0, "vout_avg": 0.95}


def test_parse_measurements_failed_is_absent():
    log = (
        "  Measurements for Transient Analysis\n\n\n"
        "Error: measure  vout_high  find(AT) : out of interval\n"
        " .meas tran vout_high find v(out) when v(out)=5 failed!\n"
    )
    assert sim._parse_measurements(log) == {}


def test_parse_measurements_ignores_unrelated_lines():
    log = (
        "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
        "Node                                   Voltage\n"
        "vdd                                          1\n"
        "No. of Data Rows : 10008\n"
    )
    assert sim._parse_measurements(log) == {}


# --------------------------------------------------------------------------- #
# Diagnostic classification (from log text, never exit code)
# --------------------------------------------------------------------------- #


def test_classify_diagnostics_singular_matrix():
    log = "Warning: singular matrix:  check node b\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert codes == ["singular_matrix"]


def test_classify_diagnostics_nonconvergence():
    log = "Warning: Dynamic gmin stepping failed\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert "nonconvergence" in codes


def test_classify_diagnostics_netlist_error():
    log = "Error: unknown subckt: foo\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert "netlist" in codes


def test_classify_diagnostics_missing_include():
    """Issue #2485: ngspice's own `Could not find include file` line gets a
    dedicated code. Everything it causes downstream (missing devices, then
    `.meas` cards finding no vector) reads like a broken circuit, so without
    this the corner surfaces only as measurement failures indistinguishable
    from a real regression."""
    log = (
        "Error: Could not find include file 07-reference.spice\n"
        "    While reading /home/ubuntu/job/netlist.cir\n"
        "Error: no such device or model name vtail\n"
    )
    diagnostics = sim._classify_diagnostics(log)
    codes = [d["code"] for d in diagnostics]
    assert "missing_include" in codes
    (diag,) = [d for d in diagnostics if d["code"] == "missing_include"]
    assert diag["severity"] == "error"
    assert "07-reference.spice" in diag["message"]


def test_classify_diagnostics_no_such_vector():
    """Issue #2521: ngspice's `Error: no such vector as ...` line (emitted
    when a `.meas`/rawfile signal never resolved -- whether because the name
    is a genuine typo/dead node, or the vector was excluded from the saved
    set) gets its own code, additive to the generic per-measurement
    `measurement` diagnostic `_run_corner` already attaches."""
    log = (
        "  Measurements for DC Analysis\n\n"
        "Error: no such vector as v(mid).\n"
        " .meas dc mout_val find v(mid) at=0 failed!\n"
    )
    diagnostics = sim._classify_diagnostics(log)
    codes = [d["code"] for d in diagnostics]
    assert "no_such_vector" in codes
    (diag,) = [d for d in diagnostics if d["code"] == "no_such_vector"]
    assert diag["severity"] == "error"
    assert "v(mid)" in diag["message"]


def test_classify_diagnostics_clean_log_is_empty():
    log = "Note: Transient op finished successfully\nngspice-46 done\n"
    assert sim._classify_diagnostics(log) == []


def test_classify_diagnostics_does_not_false_positive_on_title_text():
    # A netlist comment that happens to mention "singular matrix" in prose
    # (not an actual `Warning:` line) must not be misclassified.
    log = (
        "Circuit: * a singular matrix test circuit\n"
        "Note: Transient op finished successfully\n"
    )
    assert sim._classify_diagnostics(log) == []


# --------------------------------------------------------------------------- #
# Model bin-range diagnostic (`model_bin_range`, issue #1214)
# --------------------------------------------------------------------------- #

_MODELNAME_LOG = "Error: could not find a valid modelname\n"


def test_classify_diagnostics_modelname_without_netlist_falls_back_to_raw_line():
    # No netlist_path provided (matches every pre-#1214 caller/test) --
    # falls back to ngspice's own raw, uninformative line.
    codes = [d["code"] for d in sim._classify_diagnostics(_MODELNAME_LOG)]
    assert codes == ["model_bin_range"]
    (diag,) = sim._classify_diagnostics(_MODELNAME_LOG)
    assert diag["message"] == "Error: could not find a valid modelname"
    assert diag["severity"] == "error"


def test_classify_diagnostics_modelname_with_wide_w_names_culprit(tmp_path):
    netlist = tmp_path / "netlist.spice"
    netlist.write_text("X5 d g s b nfet_06v0 w=150u l=0.6u nf=1 m=1\n")

    (diag,) = sim._classify_diagnostics(_MODELNAME_LOG, str(netlist))

    assert diag["code"] == "model_bin_range"
    assert diag["severity"] == "error"
    assert "X5" in diag["message"]
    assert "nfet_06v0" in diag["message"]
    assert "150" in diag["message"]
    assert "m=" in diag["message"]


def test_classify_diagnostics_modelname_with_wide_nf_names_culprit(tmp_path):
    # nf= hits the identical bin-range check w= does (issue #1214) -- the
    # diagnostic must report the *effective* width (w * nf), not just w.
    netlist = tmp_path / "netlist.spice"
    netlist.write_text("X5 d g s b nfet_06v0 w=50u l=0.6u nf=450 m=1\n")

    (diag,) = sim._classify_diagnostics(_MODELNAME_LOG, str(netlist))

    assert diag["code"] == "model_bin_range"
    assert "nf=450" in diag["message"]
    assert "22500" in diag["message"]  # 50 um * 450 fingers


def test_classify_diagnostics_modelname_with_m_workaround_finds_no_culprit(tmp_path):
    # Each instance's own w= is inside the bin range and m= parallels
    # outside the width check -- not a plausible culprit, so this must fall
    # back to the raw ngspice line rather than misidentify it.
    netlist = tmp_path / "netlist.spice"
    netlist.write_text("X5 d g s b nfet_06v0 w=50u l=0.6u m=450\n")

    (diag,) = sim._classify_diagnostics(_MODELNAME_LOG, str(netlist))

    assert diag["code"] == "model_bin_range"
    assert diag["message"] == "Error: could not find a valid modelname"


def test_classify_diagnostics_modelname_narrow_width_finds_no_culprit(tmp_path):
    netlist = tmp_path / "netlist.spice"
    netlist.write_text("M1 d g s b nfet_06v0 w=5u l=0.6u\n")

    (diag,) = sim._classify_diagnostics(_MODELNAME_LOG, str(netlist))

    assert diag["message"] == "Error: could not find a valid modelname"


def test_classify_diagnostics_modelname_missing_netlist_file_falls_back(tmp_path):
    (diag,) = sim._classify_diagnostics(
        _MODELNAME_LOG, str(tmp_path / "does-not-exist.spice")
    )
    assert diag["message"] == "Error: could not find a valid modelname"


def test_find_width_ceiling_culprit_picks_worst_offender(tmp_path):
    netlist_text = (
        "M1 d g s b nfet_06v0 w=20u l=0.6u\n"
        "X2 d g s b nfet_06v0 w=200u l=0.6u nf=1\n"
        "X3 d g s b nfet_06v0 w=50u l=0.6u m=10\n"
    )
    culprit = sim._find_width_ceiling_culprit(netlist_text)
    assert culprit is not None
    inst_name, model_name, w_um, nf_count = culprit
    assert (inst_name, model_name) == ("X2", "nfet_06v0")
    assert w_um == pytest.approx(200.0)
    assert nf_count == pytest.approx(1.0)


def test_find_width_ceiling_culprit_none_when_all_under_ceiling():
    netlist_text = "M1 d g s b nfet_06v0 w=5u l=0.6u nf=4\n"
    assert sim._find_width_ceiling_culprit(netlist_text) is None


def test_run_sim_stubbed_modelname_failure_reports_actionable_diagnostic(
    tmp_path, monkeypatch
):
    # End-to-end: the full `run_sim` pipeline, with ngspice stubbed to fail
    # exactly the way it does on gf180mcu's undocumented width ceiling
    # (issue #1214), must surface the actionable diagnostic in the report,
    # not just ngspice's raw "could not find a valid modelname" line.
    body = tmp_path / "body.spice"
    body.write_text(
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        "X5 vdd g out 0 nfet_06v0 w=50u l=0.6u nf=450 m=1\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_MODELNAME_LOG)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    (diag,) = [d for d in corner["diagnostics"] if d["code"] == "model_bin_range"]
    assert "X5" in diag["message"]
    assert "nf=450" in diag["message"]
    assert "m=" in diag["message"]


# --------------------------------------------------------------------------- #
# Recovered singular_matrix/nonconvergence classification (#205)
# --------------------------------------------------------------------------- #


def test_recovered_from_stepping_true_when_measurements_all_resolved():
    log = (
        "Warning: singular matrix:  check node b\n"
        "Note: Transient op finished successfully\n"
    )
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    measurement_results = [{"name": "vout", "value": 1.0, "status": "pass"}]
    assert sim._recovered_from_stepping(measurements_spec, measurement_results, log)


def test_recovered_from_stepping_false_with_no_measurements_declared():
    # No measurements[] at all -- nothing independently confirms the run
    # actually succeeded, so a singular-matrix classification stays fatal.
    log = "Warning: singular matrix:  check node b\n"
    assert sim._recovered_from_stepping([], [], log) is False


def test_recovered_from_stepping_false_when_a_measurement_errored():
    log = "Warning: singular matrix:  check node b\n"
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    measurement_results = [{"name": "vout", "value": None, "status": "error"}]
    assert (
        sim._recovered_from_stepping(measurements_spec, measurement_results, log)
        is False
    )


def test_recovered_from_stepping_false_on_simulation_aborted_trailer():
    log = (
        "Warning: singular matrix:  check node b\n"
        "doAnalyses: TRAN:  Timestep too small\n"
        "tran simulation(s) aborted\n"
    )
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    # Even if a stale measurement value somehow parsed, the abort trailer
    # alone is decisive.
    measurement_results = [{"name": "vout", "value": 1.0, "status": "pass"}]
    assert (
        sim._recovered_from_stepping(measurements_spec, measurement_results, log)
        is False
    )


# --------------------------------------------------------------------------- #
# Limit evaluation (margin sign convention)
# --------------------------------------------------------------------------- #


def test_evaluate_limits_no_limits_never_fails():
    status, margin = sim._evaluate_limits(1.0, None)
    assert status == "pass"
    assert margin is None


def test_evaluate_limits_pass_within_bounds():
    status, margin = sim._evaluate_limits(1.20, {"min": 1.19, "max": 1.21})
    assert status == "pass"
    assert margin == pytest.approx(0.01)  # nearest binding limit, positive


def test_evaluate_limits_fail_below_min():
    status, margin = sim._evaluate_limits(1.10, {"min": 1.19, "max": 1.21})
    assert status == "fail"
    assert margin < 0


def test_evaluate_limits_fail_above_max():
    status, margin = sim._evaluate_limits(5e-6, {"max": 4e-6})
    assert status == "fail"
    assert margin == pytest.approx(-1e-6)


def test_evaluate_limits_pass_min_only():
    status, margin = sim._evaluate_limits(2.0, {"min": 1.0})
    assert status == "pass"
    assert margin == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Plausibility bounds (issue #2493): `_validate_plausible_range` /
# `_within_plausible_range` unit coverage.
# --------------------------------------------------------------------------- #


def test_validate_plausible_range_normalises_min_and_max():
    result = sim._validate_plausible_range({"min": -1, "max": 2}, "field")
    assert result == {"min": -1.0, "max": 2.0}
    assert all(isinstance(v, float) for v in result.values())


def test_validate_plausible_range_allows_min_only():
    assert sim._validate_plausible_range({"min": 0}, "field") == {"min": 0.0}


def test_validate_plausible_range_allows_max_only():
    assert sim._validate_plausible_range({"max": 5}, "field") == {"max": 5.0}


def test_validate_plausible_range_rejects_non_object():
    with pytest.raises(sim.SimError, match="field must be an object"):
        sim._validate_plausible_range([1, 2], "field")


def test_validate_plausible_range_rejects_empty_object():
    with pytest.raises(sim.SimError, match="at least one of"):
        sim._validate_plausible_range({}, "field")


def test_validate_plausible_range_rejects_non_numeric_bound():
    with pytest.raises(sim.SimError, match="must be a number"):
        sim._validate_plausible_range({"min": "low"}, "field")


def test_validate_plausible_range_rejects_bool_bound():
    with pytest.raises(sim.SimError, match="must be a number"):
        sim._validate_plausible_range({"min": True}, "field")


def test_validate_plausible_range_rejects_min_above_max():
    with pytest.raises(sim.SimError, match=r"min must be <= .*max"):
        sim._validate_plausible_range({"min": 5, "max": 1}, "field")


def test_within_plausible_range_inside_bounds():
    assert sim._within_plausible_range(1.0, {"min": 0.0, "max": 2.0}) is True


def test_within_plausible_range_below_min():
    assert sim._within_plausible_range(-0.5, {"min": 0.0, "max": 2.0}) is False


def test_within_plausible_range_above_max():
    assert sim._within_plausible_range(142.7, {"min": -0.5, "max": 2.5}) is False


def test_within_plausible_range_min_only():
    assert sim._within_plausible_range(-1.0, {"min": 0.0}) is False
    assert sim._within_plausible_range(10.0, {"min": 0.0}) is True


def test_resolve_plausible_ranges_normalises_and_inherits_voltage_default():
    """`options.node_voltage_bounds` is voltage-scoped: only a `unit: "V"`
    measurement with no `plausible_range` of its own inherits it."""
    specs = [
        {"name": "vout", "unit": "V"},
        {"name": "iout", "unit": "A"},
        {"name": "untyped"},
        {"name": "vref", "unit": "V", "plausible_range": {"min": -1, "max": 200}},
    ]

    sim._resolve_plausible_ranges(
        specs, {"node_voltage_bounds": {"min": -0.5, "max": 2.5}}
    )

    assert specs[0]["plausible_range"] == {"min": -0.5, "max": 2.5}
    assert "plausible_range" not in specs[1]
    assert "plausible_range" not in specs[2]
    # The measurement's own declaration wins, normalised to floats.
    assert specs[3]["plausible_range"] == {"min": -1.0, "max": 200.0}


def test_resolve_plausible_ranges_is_a_no_op_without_any_declaration():
    """Backward-compatibility guard: with neither declaration present, no
    spec grows a `plausible_range` key -- the grading path below is then
    byte-identical to `_evaluate_limits` alone."""
    specs = [{"name": "vout", "unit": "V", "limits": {"max": 1.9}}]

    sim._resolve_plausible_ranges(specs, {})

    assert specs == [{"name": "vout", "unit": "V", "limits": {"max": 1.9}}]


def test_grade_measurement_value_checks_plausibility_before_limits():
    """Issue #2493's load-bearing ordering: an out-of-range value is never
    handed to `_evaluate_limits`, so `margin` (defined relative to `limits`)
    stays `None` and the verdict is neither `pass` nor `fail`."""
    status, margin, diagnostics = sim._grade_measurement_value(
        {
            "name": "vout",
            "limits": {"min": 0.0, "max": 1.9},
            "plausible_range": {"min": -0.5, "max": 2.5},
        },
        142.7,
    )

    # One status shared with issue #2492's `options.fail_on_diagnostic`; the
    # `implausible_solution` diagnostic code is what names *this* reason.
    assert status == "inconclusive"
    assert margin is None
    assert [d["code"] for d in diagnostics] == ["implausible_solution"]
    assert diagnostics[0]["severity"] == "warning"


@pytest.mark.parametrize(
    ("value", "expected_status"),
    [(1.0, "pass"), (5.0, "fail")],
)
def test_grade_measurement_value_without_a_bound_is_plain_evaluate_limits(
    value, expected_status
):
    """The opt-out path: with no `plausible_range` resolved onto the spec,
    grading is exactly `_evaluate_limits` -- same status, same margin, and
    no diagnostic -- for both verdicts it can produce."""
    spec = {"name": "vout", "limits": {"min": 0.0, "max": 1.9}}

    status, margin, diagnostics = sim._grade_measurement_value(spec, value)

    assert (status, margin) == sim._evaluate_limits(value, spec["limits"])
    assert status == expected_status
    assert diagnostics == []


def test_grade_measurement_value_inside_the_bound_still_grades_limits():
    """A declared bound that the value satisfies changes nothing: the
    ordinary `limits` verdict (here a real `fail`) passes through with its
    margin intact."""
    status, margin, diagnostics = sim._grade_measurement_value(
        {
            "name": "vout",
            "limits": {"max": 1.9},
            "plausible_range": {"min": -1000.0, "max": 1000.0},
        },
        2.4,
    )

    assert status == "fail"
    assert margin == pytest.approx(-0.5)
    assert diagnostics == []


# --------------------------------------------------------------------------- #
# `coverage` / `nothing_checked` (issue #1996)
# --------------------------------------------------------------------------- #


def test_build_coverage_empty_corner_matrix_reports_nothing_checked():
    """An empty corner matrix reports known zero actual measurements."""
    coverage = sim._build_coverage([{"name": "vout", "limits": {"max": 1.8}}], [])

    assert coverage["corners_simulated"] == 0
    assert coverage["nothing_checked"] is True
    assert coverage["nothing_checked_reasons"] == ["empty_corner_matrix"]


def test_build_coverage_unrecognized_limit_keys_report_nothing_checked():
    """`_evaluate_limits` reads only `min`/`max`, so a typo'd bound is scored
    as no bound at all and passes unconditionally. When *no* measurement ends
    up with a usable bound, the run graded nothing."""
    coverage = sim._build_coverage(
        [{"name": "vout", "limits": {"maximum": 1.8, "minimum": 1.6}}],
        [{"corner_id": "tt/1.800V/27C"}],
    )

    assert coverage["measurements_with_limits"] == 0
    assert coverage["unrecognized_limit_keys"] == [
        {"measurement": "vout", "keys": ["maximum", "minimum"]}
    ]
    assert coverage["nothing_checked"] is True
    assert coverage["nothing_checked_reasons"] == ["unrecognized_limit_keys"]


def test_build_coverage_one_typo_beside_a_real_limit_is_partial_not_vacuous():
    """`nothing_checked` is never a synonym for *partial* coverage: a typo'd
    bound alongside a well-formed one is still disclosed in
    `unrecognized_limit_keys`, but the run did grade something."""
    coverage = sim._build_coverage(
        [
            {"name": "vout", "limits": {"maximum": 1.8}},
            {"name": "gain", "limits": {"min": 20.0}},
        ],
        [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [
                    {"name": "vout", "value": 1.7, "status": "pass"},
                    {"name": "gain", "value": 21, "status": "pass"},
                ],
            }
        ],
    )

    assert coverage["measurements_with_limits"] == 1
    assert coverage["unrecognized_limit_keys"] == [
        {"measurement": "vout", "keys": ["maximum"]}
    ]
    assert coverage["nothing_checked"] is False
    assert coverage["nothing_checked_reasons"] == []


def test_build_coverage_a_characterisation_sweep_is_not_flagged():
    """Declaring no `limits` at all is a stated intent (characterise and
    report), not a silent miss -- `measurements_with_limits` lets a reader
    draw that distinction without the run being called vacuous."""
    coverage = sim._build_coverage(
        [{"name": "vout"}],
        [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [
                    {"name": "vout", "value": 1.7, "status": "pass"},
                    {"name": "gain", "value": 21, "status": "pass"},
                ],
            }
        ],
    )

    assert coverage["measurements_declared"] == 1
    assert coverage["measurements_with_limits"] == 0
    assert coverage["unrecognized_limit_keys"] == []
    assert coverage["nothing_checked"] is False
    assert coverage["nothing_checked_reasons"] == []


def test_build_coverage_implausible_measurement_skips_its_limit_bound():
    """Issue #2493: a measurement graded `"inconclusive"` by the plausibility
    pre-check produced a value, but its `limits` comparison never ran -- so
    the `/limit/...` coverage row is *skipped*, with the reason still naming
    the specific cause (`implausible_solution`), never counted as checked.
    An implausible value silently counted as a graded bound would let the
    very solve this status exists to flag sneak back into `coverage` as
    evidence the bound held."""
    coverage = sim._build_coverage(
        [{"name": "vout", "limits": {"max": 1.9}}],
        [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [
                    {
                        "name": "vout",
                        "value": 142.7,
                        "status": "inconclusive",
                    }
                ],
            }
        ],
    )

    assert coverage["checked"] == []
    assert [entry["reason"] for entry in coverage["skipped"]] == [
        "implausible_solution"
    ]
    assert coverage["skipped"][0]["id"].endswith('"vout","max"]')
    assert coverage["nothing_checked"] is True


def test_build_coverage_implausible_characterisation_still_counts_as_observed():
    """Issue #2493 counterpart: a *characterisation* measurement (no `limits`)
    has only an `/observation` row, and an implausible value is still a real
    observation -- it is the `limits` comparison, not the measurement, that
    the plausibility pre-check suppresses."""
    coverage = sim._build_coverage(
        [{"name": "vout"}],
        [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [
                    {
                        "name": "vout",
                        "value": 142.7,
                        "status": "inconclusive",
                    }
                ],
            }
        ],
    )

    assert len(coverage["checked"]) == 1
    assert coverage["checked"][0].endswith("/observation")
    assert coverage["nothing_checked"] is False


def test_build_coverage_both_reasons_can_apply_to_one_run():
    """More than one reason code can apply; both are reported, in the verb's
    own declared order."""
    coverage = sim._build_coverage([{"name": "vout", "limits": {"maximum": 1.8}}], [])

    assert coverage["nothing_checked"] is True
    assert coverage["nothing_checked_reasons"] == [
        "empty_corner_matrix",
        "unrecognized_limit_keys",
    ]


def test_build_coverage_a_real_graded_run_is_not_nothing_checked():
    """The control: real corners, a recognised bound -- an earned verdict."""
    coverage = sim._build_coverage(
        [{"name": "vout", "limits": {"min": 1.6, "max": 1.8}}],
        [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [
                    {"name": "vout", "value": 1.7, "status": "pass"},
                    {"name": "gain", "value": 21, "status": "pass"},
                ],
            },
            {
                "corner_id": "ss/1.620V/-40C",
                "measurements": [{"name": "vout", "value": 1.7, "status": "pass"}],
            },
        ],
    )

    assert coverage == {
        "schema_version": 1,
        "known": True,
        "checked": [
            'limit:[0,"tt/1.800V/27C","vout","max"]',
            'limit:[0,"tt/1.800V/27C","vout","min"]',
            'limit:[1,"ss/1.620V/-40C","vout","max"]',
            'limit:[1,"ss/1.620V/-40C","vout","min"]',
        ],
        "skipped": [],
        "inapplicable": [],
        "unknown": [],
        "corners_simulated": 2,
        "measurements_declared": 1,
        "measurements_with_limits": 1,
        "unrecognized_limit_keys": [],
        "nothing_checked": False,
        "nothing_checked_reasons": [],
    }


# --------------------------------------------------------------------------- #
# `run_sim`'s top-level `status`: the common coverage rollup rule (#2109)
# applied to `_build_coverage`'s output -- see `docs/coverage-contract.md`'s
# "The common rollup rule" and this module's own row in its producer/
# compatibility table. Real corner/measurement engine behaviour is faked via
# `_stub_subprocess_run` -- these are the fake-engine coverage-contract
# regressions the issue asks for; `tests/test_checked_work_integration.py`'s
# `test_sim_real_producer_measurement_coverage` is the equivalent
# real-producer/consumer-through-signoff table for the same rows, and
# `tests/test_signoff.py`'s `test_real_sim_gate_reproduces_the_canarys_
# corner_sim_pass` is the real-ngspice-engine control.
# --------------------------------------------------------------------------- #


def test_run_sim_zero_corners_via_exclude_is_not_checked_never_pass(
    tmp_path, monkeypatch
):
    """An empty corner matrix runs nothing -- the common rollup rule must
    report `not_checked` (exit 4), never fall through to an unconditional
    `pass` just because no corner had the chance to fail."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 1.8},
                }
            ],
            # No process/supply axis declared -- the one implicit default
            # corner's temperature is 27C (see `_expand_corners`'s
            # docstring), so excluding it leaves zero corners.
            "exclude": [{"temperature_c": 27}],
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["corner_count"] == 0
    assert report["coverage"]["nothing_checked"] is True
    assert report["coverage"]["nothing_checked_reasons"] == ["empty_corner_matrix"]
    assert report["status"] == "not_checked"


def test_run_sim_wholly_unrecognized_limits_is_not_checked_never_pass(
    tmp_path, monkeypatch
):
    """Every `limits` key the request declares is a typo (`_evaluate_limits`
    only reads `min`/`max`) -- not one bound was ever applied, so the run
    must report `not_checked`, never the unconditional `pass` a naive
    "no failing measurement" check would report."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"maximum": 1.8, "minimum": 1.6},
                }
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="vout                =  1.70000e+00\n")

    report = sim.run_sim(str(request))

    assert report["coverage"]["unrecognized_limit_keys"] == [
        {"measurement": "vout", "keys": ["maximum", "minimum"]}
    ]
    assert report["coverage"]["checked"] == []
    assert report["coverage"]["nothing_checked"] is True
    assert report["status"] == "not_checked"


def test_run_sim_typo_beside_a_real_limit_is_pass_partial_never_unconditional_pass(
    tmp_path, monkeypatch
):
    """The exact bug this issue closes: a typo'd `limits` key on one
    measurement must not silently disappear into an unconditional `pass`
    just because a *different* measurement's own bound was applied and
    satisfied. The common rollup rule reports `pass_partial` -- real,
    exit-0 evidence that is not the unconditional success."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"maximum": 1.8},
                },
                {
                    "name": "gain",
                    "spice": ".meas tran gain FIND v(gain) AT=1u",
                    "limits": {"min": 20.0},
                },
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "vout                =  1.70000e+00\ngain                =  2.10000e+01\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["coverage"]["nothing_checked"] is False
    assert report["coverage"]["skipped"] == [
        {
            "id": 'limit:[0,"default/novdd/27C","vout","maximum"]',
            "reason": "unrecognized_limit_key",
        }
    ]
    assert report["status"] == "pass_partial"


def test_run_sim_complete_valid_bounds_is_pass_never_partial_control(
    tmp_path, monkeypatch
):
    """The control: every declared bound is `min`/`max` and gets applied --
    complete, positively-established coverage earns the unconditional
    `pass`, not `pass_partial`."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"min": 1.6, "max": 1.8},
                }
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="vout                =  1.70000e+00\n")

    report = sim.run_sim(str(request))

    assert report["coverage"]["skipped"] == []
    assert report["coverage"]["nothing_checked"] is False
    assert report["status"] == "pass"


def test_run_sim_real_violation_outranks_partial_coverage(tmp_path, monkeypatch):
    """Failure precedence (issue #2109): a run with both a real limit
    violation and an unrelated skipped (typo'd) bound reports the
    violation -- never `pass_partial`, and never masked by the coverage
    gap either."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 1.5},  # 1.7 > 1.5 -- violates
                },
                {
                    "name": "gain",
                    "spice": ".meas tran gain FIND v(gain) AT=1u",
                    "limits": {"minimum": 20.0},  # typo'd -- skipped
                },
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "vout                =  1.70000e+00\ngain                =  2.10000e+01\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["coverage"]["skipped"]
    assert report["status"] == "fail"


def test_cli_exit_code_pass_partial_is_zero_not_pass(tmp_path, monkeypatch, capsys):
    """`pass_partial` is a real, non-failing result (issue #2109): the CLI
    exit code stays `0`, exactly like an unconditional `pass`, while the
    reported JSON `status` still names the gap."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"maximum": 1.8},
                },
                {
                    "name": "gain",
                    "spice": ".meas tran gain FIND v(gain) AT=1u",
                    "limits": {"min": 20.0},
                },
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "vout                =  1.70000e+00\ngain                =  2.10000e+01\n"
        ),
    )

    exit_code = main(["sim", str(request), "--format", "json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "pass_partial"


# --------------------------------------------------------------------------- #
# Measurement rollup (worst-case selection, aggregate status)
# --------------------------------------------------------------------------- #


def test_rollup_measurements_worst_case_is_most_negative_margin():
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {"name": "vref", "value": 1.25, "margin": -0.04, "status": "fail"}
            ],
        },
    ]
    rollup = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}], corners
    )

    (entry,) = rollup
    assert entry["status"] == "fail"
    assert entry["worst_case"]["corner_id"] == "b"
    assert entry["worst_case"]["value"] == 1.25


def test_rollup_measurements_error_outranks_fail():
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "iq", "value": None, "margin": None, "status": "error"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {"name": "iq", "value": 6e-6, "margin": -1e-6, "status": "fail"}
            ],
        },
    ]
    rollup = sim._rollup_measurements(
        [{"name": "iq", "unit": "A", "limits": {"max": 5e-6}}], corners
    )

    assert rollup[0]["status"] == "error"


# --------------------------------------------------------------------------- #
# ASCII rawfile -> waveform JSON
# --------------------------------------------------------------------------- #

_ASCII_RAWFILE = """Title: * rawfile test
Date: Fri Jul 31 12:00:00  2026
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tv(out)\tvoltage
Values:
 0\t0.000000000000000e+00
\t1.000000000000000e+00

 1\t1.000000000000000e-11
\t1.000000000000000e+00

 2\t2.000000000000000e-11
\t9.900000000000000e-01

"""


def test_parse_ascii_rawfile(tmp_path):
    path = tmp_path / "waveform.raw"
    path.write_text(_ASCII_RAWFILE)

    waveform = sim.parse_ascii_rawfile(str(path))

    assert waveform["plotname"] == "Transient Analysis"
    assert waveform["variables"] == [
        {"index": 0, "name": "time", "type": "time"},
        {"index": 1, "name": "v(out)", "type": "voltage"},
    ]
    assert waveform["points"] == [
        [0.0, 1.0],
        [1e-11, 1.0],
        [2e-11, 0.99],
    ]


_ASCII_RAWFILE_COMPLEX = """Title: * rawfile test
Date: Fri Jul 31 12:00:00  2026
Plotname: AC Analysis
Flags: complex
No. Variables: 2
No. Points: 2
Variables:
\t0\tfrequency\tfrequency
\t1\tv(out)\tvoltage
Values:
 0\t1.000000000000000e+00,0.000000000000000e+00
\t1.000000000000000e+00,0.000000000000000e+00

 1\t1.000000000000000e+01,0.000000000000000e+00
\t3.000000000000000e+00,4.000000000000000e+00

"""


def test_parse_ascii_rawfile_complex(tmp_path):
    path = tmp_path / "waveform_ac.raw"
    path.write_text(_ASCII_RAWFILE_COMPLEX)

    waveform = sim.parse_ascii_rawfile(str(path))

    assert waveform["plotname"] == "AC Analysis"
    assert waveform["variables"] == [
        {"index": 0, "name": "frequency", "type": "frequency"},
        {"index": 1, "name": "v(out)", "type": "voltage"},
    ]
    # frequency's imaginary part is always 0, so its magnitude equals its
    # real part; v(out)'s (3, 4) pair has a non-zero imaginary part, so its
    # magnitude (5.0) differs from either component -- confirming the parser
    # isn't accidentally truncating to the real part.
    assert waveform["points"] == [
        [1.0, 1.0],
        [10.0, 5.0],
    ]
    for point in waveform["points"]:
        for value in point:
            assert isinstance(value, float)


def test_parse_ascii_rawfile_not_a_rawfile_raises(tmp_path):
    path = tmp_path / "notraw.txt"
    path.write_text("hello world\n")
    with pytest.raises(sim.SimError):
        sim.parse_ascii_rawfile(str(path))


def test_parse_ascii_rawfile_missing_file_raises(tmp_path):
    with pytest.raises(sim.SimError):
        sim.parse_ascii_rawfile(str(tmp_path / "nope.raw"))


# --------------------------------------------------------------------------- #
# run_sim with a stubbed ngspice subprocess (no binary required)
# --------------------------------------------------------------------------- #


def _stub_subprocess_run(
    monkeypatch,
    *,
    log_text: str = "",
    stdout: str = "** ngspice-99\n",
    side_effect=None,
):
    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        if side_effect is not None:
            raise side_effect
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return fake_completed(stdout)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_sim_stubbed_missing_binary_is_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "unknown" in codes


def test_run_sim_stubbed_timeout_is_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 5},
        },
    )
    _stub_subprocess_run(
        monkeypatch, side_effect=subprocess.TimeoutExpired(cmd=["ngspice"], timeout=5)
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert codes == ["timeout"]


def test_run_sim_stubbed_no_such_vector_augments_generic_measurement_code(
    tmp_path, monkeypatch
):
    """Issue #2521: a `.meas` that never resolves to a vector gets both the
    generic `measurement` code (the per-measurement "produced no value"
    diagnostic `_run_corner` always attaches) *and* the more specific
    `no_such_vector` code from the log classifier -- additive, not a
    replacement, since `no_such_vector` alone cannot tell a genuine typo
    apart from a signal that was excluded from the saved set."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vmissing",
                    "spice": ".meas tran vmissing FIND v(missing) AT=1u",
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "Error: no such vector as v(missing).\n"
            " .meas tran vmissing find v(missing) at=1u failed!\n"
        ),
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "measurement" in codes
    assert "no_such_vector" in codes


# --------------------------------------------------------------------------- #
# ngspice binary resolution override (issue #2423): options.ngspice_binary /
# $KLT_NGSPICE_BINARY / the bare "ngspice" name on PATH -- this module's own
# port of lvs_netgen.py's _resolve_netgen_binary (issue #2373), whose
# equivalent coverage lives in tests/test_lvs.py's test_netgen_engine_*
# binary-resolution tests. These stub `sim.subprocess.run` and
# `_paths.shutil.which` directly (no real ngspice binary needed), so they
# run identically on every host -- the module-level `_bare_name_ngspice_
# resolution` autouse fixture above covers the unchanged bare-name default;
# these tests override its stub per-case to exercise the resolution chain
# itself.
# --------------------------------------------------------------------------- #


def _which_only(which_map: dict[str, str | None]):
    """A `shutil.which` stand-in that only recognises the names in
    `which_map` -- everything else "isn't installed" (`None`). Keeps these
    tests independent of whatever this host's own `PATH` actually
    contains."""

    def _which(cmd, *args, **kwargs):
        return which_map.get(cmd)

    return _which


def _stub_ngspice_subprocess(monkeypatch, *, captured_cmds: list | None = None):
    """Like `_stub_subprocess_run` above, but also records every invoked
    `cmd` so a test can assert which binary was actually spawned."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        if captured_cmds is not None:
            captured_cmds.append(cmd)
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_sim_explicit_ngspice_binary_option_wins(tmp_path, monkeypatch):
    """`options.ngspice_binary` beats the bare `ngspice` name -- the
    resolved absolute path is both what's spawned and what's recorded in
    `environment.ngspice_binary`."""
    _write_body(tmp_path)
    custom = str(tmp_path / "opt" / "ngspice-custom")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"ngspice_binary": custom},
        },
    )
    captured: list = []
    monkeypatch.setattr(paths_module.shutil, "which", _which_only({custom: custom}))
    _stub_ngspice_subprocess(monkeypatch, captured_cmds=captured)

    report = sim.run_sim(str(request))

    assert captured[0][0] == custom
    assert report["environment"]["ngspice_binary"] == custom


def test_run_sim_env_var_ngspice_binary_overrides_path(tmp_path, monkeypatch):
    """`$KLT_NGSPICE_BINARY` beats the bare `ngspice` name on PATH."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    captured: list = []
    monkeypatch.setattr(
        paths_module.shutil,
        "which",
        _which_only({"ngspice-from-env": "/opt/ngspice-from-env"}),
    )
    monkeypatch.setenv("KLT_NGSPICE_BINARY", "ngspice-from-env")
    _stub_ngspice_subprocess(monkeypatch, captured_cmds=captured)

    report = sim.run_sim(str(request))

    assert captured[0][0] == "/opt/ngspice-from-env"
    assert report["environment"]["ngspice_binary"] == "/opt/ngspice-from-env"


def test_run_sim_ngspice_binary_option_beats_env_var(tmp_path, monkeypatch):
    """Explicit `options.ngspice_binary` wins over `$KLT_NGSPICE_BINARY` --
    the documented precedence order."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"ngspice_binary": "/opt/ngspice-from-option"},
        },
    )
    captured: list = []
    which_map = {
        "/opt/ngspice-from-option": "/opt/ngspice-from-option",
        "ngspice-from-env": "/opt/ngspice-from-env",
    }
    monkeypatch.setattr(paths_module.shutil, "which", _which_only(which_map))
    monkeypatch.setenv("KLT_NGSPICE_BINARY", "ngspice-from-env")
    _stub_ngspice_subprocess(monkeypatch, captured_cmds=captured)

    report = sim.run_sim(str(request))

    assert captured[0][0] == "/opt/ngspice-from-option"
    assert report["environment"]["ngspice_binary"] == "/opt/ngspice-from-option"


def test_run_sim_unresolvable_ngspice_binary_option_raises(tmp_path, monkeypatch):
    """An explicit `options.ngspice_binary` that is not runnable is a clean
    application error naming the option -- never a silent fallback to the
    bare name on PATH, and never a bare `FileNotFoundError`."""
    _write_body(tmp_path)
    missing = str(tmp_path / "nope" / "ngspice-custom")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"ngspice_binary": missing},
        },
    )

    with pytest.raises(sim.SimError, match="options.ngspice_binary"):
        sim.run_sim(str(request))


def test_run_sim_unresolvable_ngspice_binary_env_var_raises(tmp_path, monkeypatch):
    """Same for the env-var form -- an override the caller believes is in
    force but silently is not would be worse than not supporting one."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    monkeypatch.setenv("KLT_NGSPICE_BINARY", str(tmp_path / "nope" / "ngspice-env"))

    with pytest.raises(sim.SimError, match="KLT_NGSPICE_BINARY"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Timeout-budget preflight (issue #1686)
# --------------------------------------------------------------------------- #


def test_preflight_timeout_warning_flags_implausible_budget():
    # ~1e5 timepoints (1n step, 100u window); even the deliberately generous
    # 50 timepoints/s floor needs ~2000s, so a 1s budget is obviously
    # implausible.
    warning = sim._preflight_timeout_warning(
        analysis={"kind": "tran", "args": "1n 100u"}, timeout_s=1
    )
    assert warning is not None
    assert "options.timeout_s" in warning
    assert "tran" in warning


def test_preflight_timeout_warning_none_for_plausible_budget():
    # Same window/step as above, but a budget well above the generous floor's
    # own minimum (~2000s) -- looks plausible, no warning.
    warning = sim._preflight_timeout_warning(
        analysis={"kind": "tran", "args": "1n 100u"}, timeout_s=10000
    )
    assert warning is None


def test_preflight_timeout_warning_none_for_non_tran_analysis():
    # `op`/`dc`/`ac` have no "window of simulated time" concept -- the
    # heuristic never fires for them, however tight `timeout_s` is.
    warning = sim._preflight_timeout_warning(
        analysis={"kind": "op", "args": ""}, timeout_s=1
    )
    assert warning is None


def test_preflight_timeout_warning_none_for_unparseable_args():
    warning = sim._preflight_timeout_warning(
        analysis={"kind": "tran", "args": "not-a-number"}, timeout_s=1
    )
    assert warning is None


def test_run_sim_stubbed_implausible_timeout_surfaces_preflight_warning(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 100u"},
            "options": {"timeout_s": 1},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    warning = report["environment"]["timeout_preflight_warning"]
    assert "options.timeout_s" in warning
    # Advisory only -- never blocks the sweep; the stubbed corner still runs
    # and passes normally.
    assert report["status"] == "not_checked"


def test_run_sim_stubbed_plausible_timeout_omits_preflight_warning(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 30},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "timeout_preflight_warning" not in report["environment"]


# --------------------------------------------------------------------------- #
# Two-pass fail-fast probe (issue #1694)
# --------------------------------------------------------------------------- #


def test_parse_tran_window_parses_step_and_window():
    parsed = sim._parse_tran_window({"kind": "tran", "args": "1n 100u"})
    assert parsed == ("1n", "100u", pytest.approx(1e-9), pytest.approx(100e-6))


def test_parse_tran_window_none_for_non_tran_analysis():
    assert sim._parse_tran_window({"kind": "ac", "args": "dec 10 1 1meg"}) is None


def test_parse_tran_window_none_for_unparseable_args():
    assert sim._parse_tran_window({"kind": "tran", "args": "not-a-number"}) is None


def test_probe_window_and_timeout_scales_from_window_and_timeout():
    # timeout_s=200 -> 200 * 0.1 = 20s, comfortably between the floor (1s)
    # and the cap (60s), so the plain fraction applies unmodified.
    probe_window_s, probe_timeout_s = sim._probe_window_and_timeout(
        window_s=100.0, timeout_s=200.0
    )
    assert probe_window_s == pytest.approx(100.0 * sim._PROBE_WINDOW_FRACTION)
    assert probe_timeout_s == pytest.approx(200.0 * sim._PROBE_TIMEOUT_FRACTION)


def test_probe_window_and_timeout_floors_tiny_timeout():
    _window_s, probe_timeout_s = sim._probe_window_and_timeout(
        window_s=1.0, timeout_s=2.0
    )
    # 2.0 * 0.1 = 0.2, below the floor.
    assert probe_timeout_s == pytest.approx(sim._PROBE_TIMEOUT_FLOOR_S)


def test_probe_window_and_timeout_caps_huge_timeout():
    _window_s, probe_timeout_s = sim._probe_window_and_timeout(
        window_s=1.0, timeout_s=100_000.0
    )
    assert probe_timeout_s == pytest.approx(sim._PROBE_TIMEOUT_CAP_S)


def test_probe_window_and_timeout_never_exceeds_real_timeout():
    # A `timeout_s` below the floor must not let the probe run *longer* than
    # the real corner would ever be allowed to.
    _window_s, probe_timeout_s = sim._probe_window_and_timeout(
        window_s=1.0, timeout_s=0.5
    )
    assert probe_timeout_s == pytest.approx(0.5)


def test_run_calibration_probe_measures_wall_time(tmp_path, monkeypatch):
    _write_body(tmp_path)
    point = sim.CornerPoint(None, {}, 27.0)
    probe_timeouts: list[float] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        probe_timeouts.append(timeout)
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    ticks = iter([100.0, 100.25])
    monkeypatch.setattr(sim.time, "monotonic", lambda: next(ticks))

    result = sim._run_calibration_probe(
        point=point,
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        step_token="1n",
        probe_window_s=2e-8,
        probe_timeout_s=5.0,
        artifacts_dir=str(tmp_path / ".klt" / "sim"),
        keep_artifacts=False,
    )

    assert result == {"wall_s": pytest.approx(0.25), "timed_out": False, "error": None}
    assert probe_timeouts == [5.0]


def test_run_calibration_probe_timeout_reports_timed_out(tmp_path, monkeypatch):
    _write_body(tmp_path)
    point = sim.CornerPoint(None, {}, 27.0)

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    ticks = iter([0.0, 5.0])
    monkeypatch.setattr(sim.time, "monotonic", lambda: next(ticks))

    result = sim._run_calibration_probe(
        point=point,
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        step_token="1n",
        probe_window_s=2e-8,
        probe_timeout_s=5.0,
        artifacts_dir=str(tmp_path / ".klt" / "sim"),
        keep_artifacts=False,
    )

    assert result["timed_out"] is True
    assert result["error"] is None
    assert result["wall_s"] == pytest.approx(5.0)


def test_run_calibration_probe_missing_binary_is_inconclusive(tmp_path, monkeypatch):
    _write_body(tmp_path)
    point = sim.CornerPoint(None, {}, 27.0)

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        raise FileNotFoundError("no ngspice")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    result = sim._run_calibration_probe(
        point=point,
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        step_token="1n",
        probe_window_s=2e-8,
        probe_timeout_s=5.0,
        artifacts_dir=str(tmp_path / ".klt" / "sim"),
        keep_artifacts=False,
    )

    assert result["error"] is not None
    assert "ngspice" in result["error"]


def test_run_calibration_probe_aborted_analysis_is_inconclusive(tmp_path, monkeypatch):
    _write_body(tmp_path)
    point = sim.CornerPoint(None, {}, 27.0)

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("Warning: singular matrix\nsimulation(s) aborted\n")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    result = sim._run_calibration_probe(
        point=point,
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=None,
        step_token="1n",
        probe_window_s=2e-8,
        probe_timeout_s=5.0,
        artifacts_dir=str(tmp_path / ".klt" / "sim"),
        keep_artifacts=False,
    )

    assert result["error"] is not None


def test_run_fail_fast_probe_none_for_non_tran_analysis():
    result = sim._run_fail_fast_probe(
        corner_points=[sim.CornerPoint(None, {}, 27.0)],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "ac", "args": "dec 10 1 1meg"},
        timeout_s=10.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )
    assert result is None


def test_run_fail_fast_probe_none_for_empty_grid():
    result = sim._run_fail_fast_probe(
        corner_points=[],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        timeout_s=10.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )
    assert result is None


def test_run_fail_fast_probe_none_when_probe_inconclusive(monkeypatch):
    monkeypatch.setattr(
        sim,
        "_run_calibration_probe",
        lambda **kwargs: {"wall_s": 0.0, "timed_out": False, "error": "boom"},
    )

    result = sim._run_fail_fast_probe(
        corner_points=[sim.CornerPoint(None, {}, 27.0)],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        timeout_s=10.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )
    assert result is None


def test_run_fail_fast_probe_none_when_wall_time_is_zero(monkeypatch):
    monkeypatch.setattr(
        sim,
        "_run_calibration_probe",
        lambda **kwargs: {"wall_s": 0.0, "timed_out": False, "error": None},
    )

    result = sim._run_fail_fast_probe(
        corner_points=[sim.CornerPoint(None, {}, 27.0)],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        timeout_s=10.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )
    assert result is None


def test_run_fail_fast_probe_aborts_when_rate_cannot_meet_timeout(monkeypatch):
    # The probe's own bounded slice took its full `probe_timeout_s` to run
    # (without actually timing out) -- an extremely slow measured rate that
    # cannot possibly cover the full window within `timeout_s`.
    monkeypatch.setattr(
        sim,
        "_run_calibration_probe",
        lambda **kwargs: {
            "wall_s": kwargs["probe_timeout_s"],
            "timed_out": False,
            "error": None,
        },
    )

    result = sim._run_fail_fast_probe(
        corner_points=[sim.CornerPoint(None, {}, 27.0)],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        timeout_s=1.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )

    assert result is not None
    assert result["abort"] is True
    assert result["rate_is_upper_bound"] is False
    assert result["estimated_wall_s"] > result["timeout_s"] * sim._PROBE_ABORT_MARGIN
    assert result["estimated_reached_s"] == pytest.approx(
        result["measured_rate_s_per_s"] * result["timeout_s"]
    )
    assert result["estimated_fraction"] == pytest.approx(
        result["estimated_reached_s"] / result["analysis_window_s"]
    )


def test_run_fail_fast_probe_no_abort_when_rate_is_fast(monkeypatch):
    monkeypatch.setattr(
        sim,
        "_run_calibration_probe",
        lambda **kwargs: {
            "wall_s": kwargs["probe_timeout_s"] / 1000.0,
            "timed_out": False,
            "error": None,
        },
    )

    result = sim._run_fail_fast_probe(
        corner_points=[sim.CornerPoint(None, {}, 27.0)],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        timeout_s=10.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )

    assert result is not None
    assert result["abort"] is False
    # The estimated coverage caps at the real window -- never reports more
    # than 100% "reached".
    assert result["estimated_reached_s"] <= result["analysis_window_s"]


def test_run_fail_fast_probe_timed_out_probe_uses_upper_bound_rate(monkeypatch):
    monkeypatch.setattr(
        sim,
        "_run_calibration_probe",
        lambda **kwargs: {
            "wall_s": kwargs["probe_timeout_s"],
            "timed_out": True,
            "error": None,
        },
    )

    result = sim._run_fail_fast_probe(
        corner_points=[sim.CornerPoint(None, {}, 27.0)],
        netlist_path="body.spice",
        models_lib=None,
        analysis={"kind": "tran", "args": "1n 1u"},
        timeout_s=1.0,
        artifacts_dir="/tmp/does-not-matter",
        keep_artifacts=False,
    )

    assert result is not None
    assert result["rate_is_upper_bound"] is True
    assert result["abort"] is True


def test_run_sim_fail_fast_probe_disabled_by_default_never_runs(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    probe_calls: list[int] = []
    monkeypatch.setattr(
        sim,
        "_run_fail_fast_probe",
        lambda **kwargs: probe_calls.append(1) or None,
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert probe_calls == []
    assert report["status"] == "not_checked"
    assert "fail_fast_probe" not in report["environment"]


@pytest.mark.parametrize("backend", ["local", "local-parallel"])
def test_run_sim_fail_fast_probe_aborts_grid_before_any_corner_runs(
    tmp_path, monkeypatch, backend
):
    _write_body(tmp_path)
    options = {"fail_fast_probe": True, "timeout_s": 100}
    if backend == "local-parallel":
        options["max_workers"] = 1
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": options,
        },
    )

    probe_report = {
        "corner_id": "10C",
        "probe_window_s": 2e-8,
        "probe_timeout_s": 10.0,
        "probe_wall_s": 10.0,
        "measured_rate_s_per_s": 2e-9,
        "rate_is_upper_bound": True,
        "analysis_window_s": 1e-6,
        "timeout_s": 100.0,
        "estimated_wall_s": 500.0,
        "estimated_reached_s": 2e-7,
        "estimated_fraction": 0.2,
        "abort_margin": 1.5,
        "abort": True,
    }
    monkeypatch.setattr(
        sim, "_run_fail_fast_probe", lambda **kwargs: dict(probe_report)
    )
    real_corner_calls: list[int] = []
    monkeypatch.setattr(
        sim.subprocess,
        "run",
        lambda *a, **k: real_corner_calls.append(1),
    )

    report = sim.run_sim(str(request))

    # `_run_fail_fast_probe` itself is monkeypatched away here (its own
    # subprocess use is covered by the dedicated `_run_calibration_probe`
    # tests above) -- this asserts the *dispatch loop* never spawns a real
    # per-corner `ngspice` once the probe says to abort.
    assert real_corner_calls == []
    assert report["status"] == "error"
    assert report["errored"] == 3
    for corner in report["corners"]:
        assert corner["status"] == "error"
        (diag,) = corner["diagnostics"]
        assert diag["code"] == "timeout_budget_unreachable"
        assert diag["reached_s"] == pytest.approx(2e-7)
        assert diag["fraction"] == pytest.approx(0.2)
        assert "sim-corner-reached-s.md" in diag["message"]

    assert report["environment"]["fail_fast_probe"] == probe_report


def test_run_sim_fail_fast_probe_no_abort_leaves_normal_path_unaffected(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": [10, 20]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"fail_fast_probe": True},
        },
    )
    probe_report = {
        "corner_id": "10C",
        "probe_window_s": 2e-8,
        "probe_timeout_s": 1.0,
        "probe_wall_s": 0.001,
        "measured_rate_s_per_s": 2e-5,
        "rate_is_upper_bound": False,
        "analysis_window_s": 1e-6,
        "timeout_s": 10.0,
        "estimated_wall_s": 0.05,
        "estimated_reached_s": 1e-6,
        "estimated_fraction": 1.0,
        "abort_margin": 1.5,
        "abort": False,
    }
    monkeypatch.setattr(
        sim, "_run_fail_fast_probe", lambda **kwargs: dict(probe_report)
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["status"] == "not_checked"
    assert [c["status"] for c in report["corners"]] == ["pass", "pass"]
    assert report["environment"]["fail_fast_probe"]["abort"] is False


def test_run_sim_fail_fast_probe_skipped_for_remote_backend(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        _base_remote_request(options={"fail_fast_probe": True}),
    )
    probe_calls: list[int] = []
    monkeypatch.setattr(
        sim,
        "_run_fail_fast_probe",
        lambda **kwargs: probe_calls.append(1) or None,
    )
    _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    assert probe_calls == []


def test_run_sim_stubbed_pass(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.9, "max": 1.1},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["schema_version"] == 3
    assert report["status"] == "pass"
    assert report["passed"] == 1
    assert report["environment"]["engine_version"] == "99"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] == 1.0
    assert corner["measurements"][0]["status"] == "pass"
    # Issue #1849: `metrics` re-keys the corner_count/passed/failed/errored
    # rollup under its declared METRICS2.1-style names, additive alongside
    # the existing fields. Issue #2492 adds the parallel `inconclusive`
    # count/metric -- always present, `0` for a request that declares no
    # `options.fail_on_diagnostic`.
    assert report["metrics"] == {
        "sim__corner__count": report["corner_count"],
        "sim__corner__passed_count": report["passed"],
        "sim__corner__failed_count": report["failed"],
        "sim__corner__errored_count": report["errored"],
        "sim__corner__inconclusive_count": report["inconclusive"],
    }
    assert report["metrics"] == {
        "sim__corner__count": 1,
        "sim__corner__passed_count": 1,
        "sim__corner__failed_count": 0,
        "sim__corner__errored_count": 0,
        "sim__corner__inconclusive_count": 0,
    }


@pytest.mark.parametrize("netlist_source", ["schematic", "extracted"])
def test_run_sim_stubbed_netlist_source_echoed_in_environment(
    tmp_path, monkeypatch, netlist_source
):
    # request.netlist_source, when present and valid, is echoed verbatim
    # into environment.netlist_source (see docs/cli/sim.md's "Post-layout
    # verification" section) -- this is how a caller distinguishes a
    # pre-layout (S6) sim pass from a post-layout (S9) one.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "netlist_source": netlist_source,
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["netlist_source"] == netlist_source


def test_run_sim_stubbed_netlist_source_absent_omits_environment_key(
    tmp_path, monkeypatch
):
    # Omitting netlist_source is unchanged, backward-compatible behavior:
    # no netlist_source key appears in environment at all (not null, not
    # an empty string) and every other field/behavior is unaffected.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "netlist_source" not in report["environment"]
    assert report["status"] == "not_checked"


def test_run_sim_stubbed_provenance_pins_model_library(tmp_path, monkeypatch):
    """A process-axis sweep resolves a model library; the shared provenance
    block pins it as the run's `deck` with a `sha256:` content hash."""
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request))

    prov = report["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    # No PDK variant declared in `models`, so no PDK is resolved.
    assert prov["pdk"] is None
    assert prov["deck"]["name"] == "corner.lib"
    assert prov["deck"]["content_hash"].startswith("sha256:")
    # Issue #331 added `provenance.input`, but `sim` wasn't in scope for it
    # then. Issue #2039 closes that gap: `klt sim` now pins the netlist it
    # simulated under `role: "netlist"`, matching `sha256_file`'s digest of
    # the same file `environment.netlist_sha256` already hashes.
    assert prov["input"]["role"] == "netlist"
    assert prov["input"]["content_hash"].startswith("sha256:")
    assert (
        prov["input"]["content_hash"]
        == f"sha256:{report['environment']['netlist_sha256']}"
    )


# --------------------------------------------------------------------------- #
# `monte_carlo` via `run_sim`, stubbed ngspice subprocess (#348)
# --------------------------------------------------------------------------- #


def _mc_request(tmp_path: Path, monte_carlo: dict, **overrides) -> Path:
    _write_body(tmp_path)
    request: dict[str, object] = {
        "netlist": "body.spice",
        "monte_carlo": monte_carlo,
        "analysis": {"kind": "tran", "args": "1n 1u"},
    }
    if overrides.pop("two_corners", False):
        _write_corner_lib(tmp_path)
        request["models"] = {"lib": "corner.lib"}
        request["corners"] = {"process": ["tt", "ss"]}
    request.update(overrides)
    return _write_request(tmp_path, request)


@pytest.mark.parametrize("missing_field", ["n", "seed", "vary"])
def test_run_sim_monte_carlo_missing_field_raises(tmp_path, missing_field):
    monte_carlo = {"n": 5, "seed": 1, "vary": "mismatch"}
    del monte_carlo[missing_field]
    request = _mc_request(tmp_path, monte_carlo)

    with pytest.raises(sim.SimError, match=f"monte_carlo.{missing_field}"):
        sim.run_sim(str(request))


def test_run_sim_monte_carlo_bad_vary_raises(tmp_path):
    request = _mc_request(tmp_path, {"n": 5, "seed": 1, "vary": "bogus"})

    with pytest.raises(sim.SimError, match="monte_carlo.vary"):
        sim.run_sim(str(request))


def test_run_sim_monte_carlo_expands_n_times_m_corners(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path, {"n": 3, "seed": 1, "vary": "both"}, two_corners=True
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["corner_count"] == 6  # 2 corners x 3 samples
    ids = [c["corner_id"] for c in report["corners"]]
    assert len(set(ids)) == 6
    mc_env = report["environment"]["monte_carlo"]
    assert mc_env["n"] == 3
    assert mc_env["seed"] == 1
    assert mc_env["vary"] == "both"
    # `vary: "both"` includes mismatch -> the per-family mismatch-activity
    # report (#355) is present; no `models.pdk` was declared, so activity is
    # unverified (`active: None`) for every family the netlist body (R1/C1)
    # instantiates.
    assert {f["family"] for f in mc_env["family_mismatch"]} == {
        "resistor",
        "capacitor",
    }
    assert all(f["active"] is None for f in mc_env["family_mismatch"])
    for corner in report["corners"]:
        assert corner["monte_carlo"]["sample_index"] in (0, 1, 2)
        assert isinstance(corner["monte_carlo"]["seed"], int)


def test_run_sim_monte_carlo_omitted_leaves_corner_field_null(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "monte_carlo" not in report["environment"]
    (corner,) = report["corners"]
    assert corner["monte_carlo"] is None


def test_run_sim_monte_carlo_reproducible_across_runs(tmp_path, monkeypatch):
    request = _mc_request(tmp_path, {"n": 4, "seed": 20260801, "vary": "both"})
    _stub_subprocess_run(monkeypatch)

    report_a = sim.run_sim(str(request))
    report_b = sim.run_sim(str(request))

    mc_a = [(c["corner_id"], c["monte_carlo"]) for c in report_a["corners"]]
    mc_b = [(c["corner_id"], c["monte_carlo"]) for c in report_b["corners"]]
    assert mc_a == mc_b


def test_run_sim_monte_carlo_negative_control_via_response(tmp_path, monkeypatch):
    request = _mc_request(tmp_path, {"n": 4, "seed": 20260801, "vary": "process"})
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    mismatch_seeds = {c["monte_carlo"]["mismatch_seed"] for c in report["corners"]}
    process_seeds = {c["monte_carlo"]["process_seed"] for c in report["corners"]}
    assert len(mismatch_seeds) == 1  # not requested -> pinned, sigma=0
    assert len(process_seeds) == 4  # requested -> actually varies


def test_run_sim_monte_carlo_unique_artifact_paths(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 3, "seed": 1, "vary": "mismatch"},
        options={"keep_artifacts": True},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    log_paths = [c["artifacts"]["log"] for c in report["corners"]]
    assert len(log_paths) == 3
    assert len(set(log_paths)) == 3  # every sample got its own artifact dir
    for path in log_paths:
        assert os.path.isfile(path)


def test_run_sim_monte_carlo_deck_carries_seed_and_param_cards(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 1, "seed": 20260801, "vary": "mismatch"},
        options={"keep_artifacts": True},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    deck_path = os.path.join(os.path.dirname(corner["artifacts"]["log"]), "corner.cir")
    deck_text = Path(deck_path).read_text()
    mc = corner["monte_carlo"]
    assert f".options seed={mc['seed']}" in deck_text
    assert f".param mc_process_seed={mc['process_seed']}" in deck_text
    assert f".param mc_mismatch_seed={mc['mismatch_seed']}" in deck_text
    # `.options seed=` must precede any `.lib`/`.include` card -- it seeds
    # AGAUSS/GAUSS calls evaluated while the netlist is parsed, before the
    # `.control` block ever runs.
    assert deck_text.index(".options seed=") < deck_text.index(".include")


# --------------------------------------------------------------------------- #
# Per-family mismatch-activity report (#355) -- a device family whose deck
# mismatch ships structurally disabled (e.g. gf180mcu's poly resistor) must
# never be indistinguishable from a family that actually got sampled just
# because the run's global mismatch switch/section was engaged.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line,expected",
    [
        ("R1 vdd out 1k", "resistor"),
        ("C1 out 0 1n", "capacitor"),
        ("D1 anode cathode diode_model", "diode"),
        ("Q1 c b e npn_model", "bipolar"),
        ("M1 d g s b nmos_model L=1u W=1u", "mosfet"),
        ("XM1 d g s b nfet_03v3 L=1u W=1u", "mosfet"),
        ("XR1 a b res_xpoly_1p1000pl W=1", "resistor"),
        ("XQ1 c b e bjt_pnp_model", "bipolar"),
        ("XC1 a b moscap_model", "capacitor"),
        ("XU1 a b some_unrecognised_block", "other"),
        ("V1 vdd 0 DC 1.0", None),
        ("I1 a b DC 1m", None),
        ("L1 a b 1u", None),
    ],
)
def test_classify_device_family(line, expected):
    element_type = line[0].upper()
    assert sim._classify_device_family(element_type, line) == expected


def test_detect_device_families_skips_comments_continuations_and_dot_cards():
    text = (
        "* a full-line comment\n"
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        "R1 vdd out 1k\n"
        "+ ; a continuation line, not a device\n"
        "C1 out 0 1n\n"
        "\n"
        "R2 out 0 1k\n"
    )
    # R2 repeats an already-seen family (resistor) -- first-seen order, no
    # duplicates.
    assert sim._detect_device_families(text) == ["resistor", "capacitor"]


def test_mismatch_family_report_gf180mcu_flags_resistor_inactive(tmp_path):
    """The concrete case this issue is about: gf180mcu's poly-resistor
    mismatch hook is structurally disabled, and must be reported as such
    even though MOS/BJT mismatch is active under the same global switch."""
    netlist = tmp_path / "body.spice"
    netlist.write_text(
        "XR1 a b res_xpoly_1p1000pl W=1\n"
        "XM1 d g s b nfet_03v3 L=1u W=1u\n"
        "XQ1 c b e bjt_npn_model\n"
    )

    report = sim._mismatch_family_report(str(netlist), "gf180mcuC")

    by_family = {entry["family"]: entry for entry in report}
    resistor_note = by_family["resistor"]["note"]
    assert by_family["resistor"]["active"] is False
    assert "hardcoded" in resistor_note or "disabled" in resistor_note
    assert by_family["mosfet"]["active"] is True
    assert by_family["bipolar"]["active"] is True


def test_mismatch_family_report_sky130(tmp_path):
    netlist = tmp_path / "body.spice"
    netlist.write_text(
        "XM1 d g s b sky130_fd_pr__nfet_01v8 L=1u W=1u\n"
        "XQ1 c b e sky130_fd_pr__npn_model\n"
    )

    report = sim._mismatch_family_report(str(netlist), "sky130A")

    by_family = {entry["family"]: entry for entry in report}
    assert by_family["mosfet"]["active"] is True
    assert by_family["bipolar"]["active"] is True


def test_mismatch_family_report_unrecognised_pdk_family_is_unverified(tmp_path):
    """A family with no curated table entry -- including an unrecognised
    PDK -- is reported `active: None` ("not independently verified"),
    never guessed `True`."""
    netlist = tmp_path / "body.spice"
    netlist.write_text("R1 vdd out 1k\nC1 out 0 1n\n")

    report = sim._mismatch_family_report(str(netlist), "not_a_real_pdk")

    for entry in report:
        assert entry["active"] is None
        assert "not independently verified" in entry["note"]


def test_mismatch_family_report_missing_pdk_variant_is_unverified(tmp_path):
    netlist = tmp_path / "body.spice"
    netlist.write_text("R1 vdd out 1k\n")

    report = sim._mismatch_family_report(str(netlist), None)

    assert report == [
        {
            "family": "resistor",
            "active": None,
            "note": (
                "PDK family could not be determined for this request (set "
                "models.pdk) -- mismatch activity for the 'resistor' "
                "family could not be verified; treat any sampled spread "
                "for it as unconfirmed."
            ),
        }
    ]


def test_run_sim_monte_carlo_family_mismatch_present_only_when_vary_includes_mismatch(
    tmp_path, monkeypatch
):
    """`vary: "process"` never touches mismatch sampling -- no
    `family_mismatch` report is emitted (there is nothing to report on)."""
    request = _mc_request(tmp_path, {"n": 2, "seed": 1, "vary": "process"})
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "family_mismatch" not in report["environment"]["monte_carlo"]


def test_run_sim_monte_carlo_family_mismatch_reports_gf180mcu_resistor_disabled(
    tmp_path, monkeypatch
):
    """End-to-end (`run_sim`): a gf180mcu request declaring `models.pdk`
    and mismatch sampling gets a `family_mismatch` report that flags the
    resistor family inactive, never lumping it in with the active MOS/BJT
    families."""
    body = tmp_path / "body.spice"
    body.write_text(
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        "XR1 vdd mid res_xpoly_1p1000pl W=1\n"
        "XM1 mid g out b nfet_03v3 L=1u W=1u\n"
        "C1 out 0 1n\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"pdk": "gf180mcuC"},
            "monte_carlo": {"n": 2, "seed": 1, "vary": "mismatch"},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    family_mismatch = report["environment"]["monte_carlo"]["family_mismatch"]
    by_family = {entry["family"]: entry["active"] for entry in family_mismatch}
    assert by_family["resistor"] is False
    assert by_family["mosfet"] is True
    # capacitor has no curated gf180mcu table entry -- unverified, not
    # assumed active or inactive.
    assert by_family["capacitor"] is None


# --------------------------------------------------------------------------- #
# Monte Carlo statistics rollup (#349)
# --------------------------------------------------------------------------- #

#: A deliberately hand-checkable sample set: mean 3.0, sample (n-1) sigma
#: sqrt(2.5), min 1.0, max 5.0, p5 1.2, p50 3.0, p95 4.8.
_KNOWN_SAMPLES = [3.0, 1.0, 5.0, 2.0, 4.0]
_KNOWN_SIGMA = 2.5**0.5


@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (0, 1.0),
        (5, 1.2),
        (25, 2.0),
        (50, 3.0),
        (95, 4.8),
        (100, 5.0),
    ],
)
def test_percentile_linear_interpolation(percentile, expected):
    assert sim._percentile(sorted(_KNOWN_SAMPLES), percentile) == pytest.approx(
        expected
    )


def test_percentile_single_sample_is_that_sample():
    assert sim._percentile([1.25], 5) == 1.25
    assert sim._percentile([1.25], 95) == 1.25


@pytest.mark.parametrize(
    ("percentile", "key"), [(5, "p5"), (50, "p50"), (2.5, "p2.5"), (97.5, "p97.5")]
)
def test_quantile_key_formatting(percentile, key):
    assert sim._quantile_key(percentile) == key


def test_sample_statistics_known_sample_set():
    stats = sim._sample_statistics(
        _KNOWN_SAMPLES,
        errored=0,
        quantiles=sim.DEFAULT_MC_QUANTILES,
        k_sigma=None,
        limits=None,
    )

    assert stats["n"] == 5
    assert stats["errored"] == 0
    assert stats["mean"] == pytest.approx(3.0)
    assert stats["stddev"] == pytest.approx(_KNOWN_SIGMA)
    assert stats["min"] == 1.0
    assert stats["max"] == 5.0
    assert stats["quantiles"]["p5"] == pytest.approx(1.2)
    assert stats["quantiles"]["p50"] == pytest.approx(3.0)
    assert stats["quantiles"]["p95"] == pytest.approx(4.8)
    assert stats["sigma_window"] is None  # no k_sigma declared


def test_sample_statistics_honours_configured_quantiles():
    stats = sim._sample_statistics(
        _KNOWN_SAMPLES,
        errored=0,
        quantiles=(2.5, 97.5),
        k_sigma=None,
        limits=None,
    )

    assert list(stats["quantiles"]) == ["p2.5", "p97.5"]
    assert stats["quantiles"]["p2.5"] == pytest.approx(1.1)


def test_sample_statistics_single_sample_has_no_stddev():
    stats = sim._sample_statistics(
        [1.2],
        errored=0,
        quantiles=sim.DEFAULT_MC_QUANTILES,
        k_sigma=3.0,
        limits={"min": 1.0, "max": 1.4},
    )

    assert stats["n"] == 1
    assert stats["mean"] == 1.2
    # Never faked as 0.0 -- a fabricated zero sigma would silently pass any
    # window check.
    assert stats["stddev"] is None
    assert stats["sigma_window"] is None


def test_sample_statistics_no_usable_values_is_all_null():
    stats = sim._sample_statistics(
        [],
        errored=3,
        quantiles=sim.DEFAULT_MC_QUANTILES,
        k_sigma=3.0,
        limits={"max": 1.0},
    )

    assert stats["n"] == 0
    assert stats["errored"] == 3
    assert stats["mean"] is None
    assert stats["stddev"] is None
    assert stats["min"] is None and stats["max"] is None
    assert stats["quantiles"] == {"p5": None, "p50": None, "p95": None}
    assert stats["sigma_window"] is None


def test_evaluate_sigma_window_pass_exactly_at_the_limits():
    # mean 1.0, sigma 0.25, k 2 -> window is exactly [0.5, 1.5] (all values
    # binary-exact), and a value *equal* to a bound passes, as for a single
    # deterministic value.
    window = sim._evaluate_sigma_window(1.0, 0.25, 2.0, {"min": 0.5, "max": 1.5})

    assert window == {
        "k": 2.0,
        "low": 0.5,
        "high": 1.5,
        "status": "pass",
        "margin": 0.0,
    }


def test_evaluate_sigma_window_fails_just_outside_the_lower_bound():
    window = sim._evaluate_sigma_window(1.0, 0.25, 2.0, {"min": 0.5625, "max": 1.5})

    assert window["status"] == "fail"
    assert window["margin"] == pytest.approx(-0.0625)


def test_evaluate_sigma_window_fails_just_outside_the_upper_bound():
    window = sim._evaluate_sigma_window(1.0, 0.25, 2.0, {"min": 0.5, "max": 1.4375})

    assert window["status"] == "fail"
    assert window["margin"] == pytest.approx(-0.0625)


def test_evaluate_sigma_window_without_limits_never_fails():
    window = sim._evaluate_sigma_window(1.0, 0.25, 3.0, None)

    assert window["status"] == "pass"
    assert window["margin"] is None


def test_evaluate_sigma_window_k_zero_is_the_mean_itself():
    window = sim._evaluate_sigma_window(1.0, 0.25, 0.0, {"min": 0.9, "max": 1.1})

    assert (window["low"], window["high"]) == (1.0, 1.0)
    assert window["status"] == "pass"


def test_rollup_measurements_without_monte_carlo_config_is_unchanged():
    """A plain corner matrix keeps today's exact per-measurement shape."""
    corners = [
        {
            "corner_id": "a",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        }
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}], corners
    )

    assert set(entry) == {"name", "unit", "limits", "status", "worst_case"}


def test_rollup_measurements_monte_carlo_config_but_no_sampled_corners():
    """`monte_carlo` config with only unsampled corners adds no statistics."""
    corners = [
        {
            "corner_id": "a",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        }
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": 3.0},
    )

    assert "monte_carlo" not in entry


def _sampled_corner(base: str, sample_index: int, value: float | None) -> dict:
    return {
        "corner_id": f"{base}/mc{sample_index}",
        "monte_carlo": {"sample_index": sample_index, "seed": 1},
        "measurements": [
            {
                "name": "vref",
                "value": value,
                "margin": None,
                "status": "error" if value is None else "pass",
            }
        ],
    }


def test_rollup_measurements_counts_unextractable_samples_separately():
    corners = [
        _sampled_corner("tt/1.800V/27C", 0, 1.0),
        _sampled_corner("tt/1.800V/27C", 1, None),
        _sampled_corner("tt/1.800V/27C", 2, 3.0),
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref"}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": None},
    )

    assert entry["monte_carlo"]["n"] == 2  # only the samples that produced a number
    assert entry["monte_carlo"]["errored"] == 1
    assert entry["monte_carlo"]["mean"] == pytest.approx(2.0)


def _sampled_implausible_corner(base: str, sample_index: int, value: float) -> dict:
    return {
        "corner_id": f"{base}/mc{sample_index}",
        "monte_carlo": {"sample_index": sample_index, "seed": 1},
        "measurements": [
            {
                "name": "vref",
                "value": value,
                "margin": None,
                "status": "inconclusive",
            }
        ],
    }


def test_rollup_measurements_excludes_implausible_samples_from_statistics():
    """Issue #2493: an implausible sample has a real (non-null) value, so it
    needs its own exclusion from Monte Carlo statistics -- not just the
    errored/`value is None` filter -- or a physically implausible outlier
    would silently skew `mean`/`stddev`/the quantiles exactly as an ungated
    `limits` comparison would have skewed a single corner's `pass`/`fail`.
    """
    corners = [
        _sampled_corner("tt/1.800V/27C", 0, 1.0),
        _sampled_corner("tt/1.800V/27C", 1, 3.0),
        _sampled_implausible_corner("tt/1.800V/27C", 2, 1e12),
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref"}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": None},
    )

    assert entry["status"] == "inconclusive"
    assert entry["monte_carlo"]["n"] == 2
    assert entry["monte_carlo"]["errored"] == 0
    assert entry["monte_carlo"]["inconclusive"] == 1
    assert entry["monte_carlo"]["mean"] == pytest.approx(2.0)
    assert entry["monte_carlo"]["max"] == pytest.approx(3.0)  # not 1e12


def test_rollup_measurements_sigma_window_never_downgrades_implausible_to_fail():
    """Issue #2493, the subtlest of the grading paths: the `mean ± k*sigma`
    window is a *second* `_evaluate_limits` call site, reached after the
    per-measurement aggregate is already set. A violated window normally
    forces the entry to `"fail"` -- it must not do so over an
    `"inconclusive"` aggregate, or the false-fail artifact would
    reappear one layer up, on a path the per-corner pre-check never sees.
    """
    corners = [
        _sampled_corner("tt/1.800V/27C", 0, 1.0),
        _sampled_corner("tt/1.800V/27C", 1, 3.0),
        _sampled_implausible_corner("tt/1.800V/27C", 2, 1e12),
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref", "limits": {"max": 1.5}}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": 3.0},
    )

    # The window itself is computed over the two plausible samples only and
    # does violate the `max: 1.5` bound ...
    assert entry["monte_carlo"]["sigma_window"]["status"] == "fail"
    # ... but it never overwrites the stronger `inconclusive` verdict.
    assert entry["status"] == "inconclusive"


def test_rollup_measurements_implausible_outranks_fail():
    """Issue #2493: the per-measurement aggregate mirrors `_run_corner`'s own
    `error > inconclusive > fail > pass` precedence."""
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "vref", "value": 5.0, "margin": -1.0, "status": "fail"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {
                    "name": "vref",
                    "value": 1e9,
                    "margin": None,
                    "status": "inconclusive",
                }
            ],
        },
    ]

    (entry,) = sim._rollup_measurements([{"name": "vref"}], corners)

    assert entry["status"] == "inconclusive"


def test_rollup_measurements_by_corner_splits_per_originating_corner():
    corners = [
        _sampled_corner("tt/1.800V/27C", 0, 1.0),
        _sampled_corner("tt/1.800V/27C", 1, 3.0),
        _sampled_corner("ss/1.800V/27C", 0, 10.0),
        _sampled_corner("ss/1.800V/27C", 1, 20.0),
    ]

    (entry,) = sim._rollup_measurements(
        [{"name": "vref"}],
        corners,
        {"quantiles": sim.DEFAULT_MC_QUANTILES, "k_sigma": None},
    )

    mc = entry["monte_carlo"]
    assert mc["n"] == 4  # pooled across every corner
    by_corner = {c["corner_id"]: c for c in mc["by_corner"]}
    assert list(by_corner) == ["tt/1.800V/27C", "ss/1.800V/27C"]  # corner order
    assert by_corner["tt/1.800V/27C"]["mean"] == pytest.approx(2.0)
    assert by_corner["ss/1.800V/27C"]["mean"] == pytest.approx(15.0)


def _stub_subprocess_values(monkeypatch, name: str, values: list[float | None]) -> None:
    """Stub ngspice so successive corner runs report ``name = values[i]``.

    The `local` backend runs the expanded corner list sequentially in the
    deterministic sample order (`_expand_monte_carlo`), so call index maps
    1:1 onto sample index. A ``None`` entry stands in for an unextractable
    measurement (ngspice's own ``failed!`` line).
    """
    state = {"index": 0}

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        value = values[state["index"]]
        state["index"] += 1
        with open(log_path, "w", encoding="utf-8") as handle:
            if value is None:
                handle.write(f".meas tran {name} find v(out) at=1u failed!\n")
            else:
                handle.write(f"{name} = {value!r}\n")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


_VOUT_MEAS = {
    "name": "vout",
    "spice": ".meas tran vout FIND v(out) AT=1u",
    "unit": "V",
}


def test_run_sim_monte_carlo_statistics_match_hand_computed_values(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch"},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    mc = entry["monte_carlo"]
    assert mc["n"] == 5
    assert mc["errored"] == 0
    assert mc["mean"] == pytest.approx(3.0)
    assert mc["stddev"] == pytest.approx(_KNOWN_SIGMA)
    assert mc["min"] == 1.0
    assert mc["max"] == 5.0
    assert mc["quantiles"]["p5"] == pytest.approx(1.2)
    assert mc["quantiles"]["p50"] == pytest.approx(3.0)
    assert mc["quantiles"]["p95"] == pytest.approx(4.8)
    assert mc["sigma_window"] is None
    assert [c["corner_id"] for c in mc["by_corner"]] == ["default/novdd/27C"]


def test_run_sim_without_monte_carlo_has_no_statistics_block(tmp_path, monkeypatch):
    """A plain corner matrix response is byte-identical in shape to today's."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [_VOUT_MEAS],
        },
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0])

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    assert set(entry) == {"name", "unit", "limits", "status", "worst_case"}


def test_run_sim_monte_carlo_sigma_window_pass_keeps_run_passing(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 1.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 1.0, "max": 5.0}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["k"] == 1.0
    assert window["low"] == pytest.approx(3.0 - _KNOWN_SIGMA)
    assert window["high"] == pytest.approx(3.0 + _KNOWN_SIGMA)
    assert window["status"] == "pass"
    assert report["measurements"][0]["status"] == "pass"
    assert report["status"] == "pass"


def test_run_sim_monte_carlo_sigma_window_fail_fails_the_run(tmp_path, monkeypatch):
    """Every individual sample passes its limits, but mean +/- 3*sigma does
    not fit inside the window -- a real design failure, so the run fails."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 0.5, "max": 5.5}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    # Every corner still passes -- nothing about per-sample evaluation moved.
    assert all(c["status"] == "pass" for c in report["corners"])
    assert report["failed"] == 0
    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["status"] == "fail"
    assert window["margin"] < 0
    assert report["measurements"][0]["status"] == "fail"
    assert report["status"] == "fail"


def test_run_sim_monte_carlo_sigma_window_needs_limits_to_fail(tmp_path, monkeypatch):
    """No `limits` -> reported but never fails, exactly as for a single value."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["status"] == "pass"
    assert window["margin"] is None
    assert report["status"] == "pass"


def test_run_sim_monte_carlo_per_measurement_k_sigma_overrides_the_default(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "k_sigma": 1.0, "limits": {"min": 1.0}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    window = report["measurements"][0]["monte_carlo"]["sigma_window"]
    assert window["k"] == 1.0  # per-measurement override, not the run-wide 3.0
    assert window["status"] == "pass"  # would fail at k=3


def test_run_sim_monte_carlo_errored_sample_excluded_from_statistics(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 3, "seed": 1, "vary": "mismatch"},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, None, 3.0])

    report = sim.run_sim(str(request))

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 2
    assert mc["errored"] == 1
    assert mc["mean"] == pytest.approx(2.0)
    assert report["status"] == "error"  # the unextractable sample still errors


def test_run_sim_monte_carlo_statistics_pool_and_split_across_corners(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch"},
        two_corners=True,
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 3.0, 10.0, 20.0])

    report = sim.run_sim(str(request))

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 4
    assert mc["mean"] == pytest.approx(8.5)
    by_corner = {c["corner_id"]: c["mean"] for c in mc["by_corner"]}
    assert by_corner == {
        "tt/novdd/27C": pytest.approx(2.0),
        "ss/novdd/27C": pytest.approx(15.0),
    }


def test_run_sim_monte_carlo_configured_quantiles_are_echoed(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "quantiles": [1, 50, 99], "k_sigma": 3},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    echo = dict(report["environment"]["monte_carlo"])
    # `family_mismatch` (#355) is an independent additive field a
    # mismatch-varying run always carries -- not part of the statistics
    # echo this test pins.
    echo.pop("family_mismatch", None)
    assert echo == {
        "n": 5,
        "seed": 1,
        "vary": "mismatch",
        "quantiles": [1.0, 50.0, 99.0],
        "k_sigma": 3.0,
    }
    assert list(report["measurements"][0]["monte_carlo"]["quantiles"]) == [
        "p1",
        "p50",
        "p99",
    ]


def test_run_sim_monte_carlo_sampling_only_request_echo_is_unchanged(
    tmp_path, monkeypatch
):
    request = _mc_request(
        tmp_path, {"n": 2, "seed": 1, "vary": "mismatch"}, measurements=[_VOUT_MEAS]
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 2.0])

    report = sim.run_sim(str(request))

    echo = dict(report["environment"]["monte_carlo"])
    # As above: `family_mismatch` (#355) is orthogonal to the statistics
    # echo -- a request declaring neither `quantiles` nor `k_sigma` must add
    # neither key.
    echo.pop("family_mismatch", None)
    assert echo == {
        "n": 2,
        "seed": 1,
        "vary": "mismatch",
    }


def test_run_sim_monte_carlo_statistics_and_family_mismatch_coexist(
    tmp_path, monkeypatch
):
    """The statistics echo (#349) and the per-family mismatch-activity
    report (#355) are independent additive fields on the *same*
    `environment.monte_carlo` block -- a mismatch-varying request that also
    declares `k_sigma` must carry both, and still get its per-measurement
    statistics rollup."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    echo = report["environment"]["monte_carlo"]
    assert echo["k_sigma"] == 3.0
    assert [entry["family"] for entry in echo["family_mismatch"]] == [
        "resistor",
        "capacitor",
    ]
    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 5
    assert mc["stddev"] == pytest.approx(_KNOWN_SIGMA)
    assert mc["sigma_window"]["k"] == 3.0


@pytest.mark.parametrize(
    "quantiles",
    [[], "p50", [101], [-1], ["p50"], [True]],
)
def test_run_sim_monte_carlo_bad_quantiles_raises(tmp_path, quantiles):
    request = _mc_request(
        tmp_path, {"n": 2, "seed": 1, "vary": "mismatch", "quantiles": quantiles}
    )

    with pytest.raises(sim.SimError, match="monte_carlo.quantiles"):
        sim.run_sim(str(request))


@pytest.mark.parametrize("k_sigma", [-1, "3", True])
def test_run_sim_monte_carlo_bad_k_sigma_raises(tmp_path, k_sigma):
    request = _mc_request(
        tmp_path, {"n": 2, "seed": 1, "vary": "mismatch", "k_sigma": k_sigma}
    )

    with pytest.raises(sim.SimError, match="monte_carlo.k_sigma"):
        sim.run_sim(str(request))


@pytest.mark.parametrize("k_sigma", [-1, "3", True])
def test_run_sim_measurement_bad_k_sigma_raises(tmp_path, k_sigma):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch"},
        measurements=[{**_VOUT_MEAS, "k_sigma": k_sigma}],
    )

    with pytest.raises(sim.SimError, match="k_sigma"):
        sim.run_sim(str(request))


def test_run_sim_monte_carlo_quantiles_deduped_preserving_order(tmp_path, monkeypatch):
    request = _mc_request(
        tmp_path,
        {"n": 2, "seed": 1, "vary": "mismatch", "quantiles": [50, 5, 50]},
        measurements=[_VOUT_MEAS],
    )
    _stub_subprocess_values(monkeypatch, "vout", [1.0, 3.0])

    report = sim.run_sim(str(request))

    assert list(report["measurements"][0]["monte_carlo"]["quantiles"]) == ["p50", "p5"]


def test_cli_sim_sigma_window_failure_exits_3(tmp_path, monkeypatch, capsys):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 3.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 0.5, "max": 5.5}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    assert main(["sim", str(request), "--format", "json"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["measurements"][0]["monte_carlo"]["sigma_window"]["status"] == "fail"


def test_cli_sim_text_output_reports_monte_carlo_statistics(
    tmp_path, monkeypatch, capsys
):
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 1.0},
        measurements=[{**_VOUT_MEAS, "limits": {"min": 1.0, "max": 5.0}}],
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    assert main(["sim", str(request)]) == 0

    out = capsys.readouterr().out
    assert "mc: n=5 mean=3.0" in out
    assert "mean+/-1sigma" in out


def _stripped_report(report: dict) -> dict:
    """Drop the two fields the docs flag as legitimately varying between
    runs (`runtime_s`, `engine_version`) so the rest of the report can be
    compared for byte-identical equality (see docs/cli/sim.md's
    byte-exact-fixture caveat)."""
    clone = json.loads(json.dumps(report))
    clone["environment"].pop("engine_version", None)
    for corner in clone["corners"]:
        corner.pop("runtime_s", None)
    return clone


def test_run_sim_local_backend_is_byte_identical_to_default(tmp_path, monkeypatch):
    # The `local` backend (explicit, and via the --backend flag override)
    # must reproduce the pre-seam default behaviour exactly: same report
    # JSON, same corner ordering. Uses a multi-corner matrix so ordering is
    # actually exercised, not just a single-corner smoke test.
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    base = {
        "netlist": "body.spice",
        "models": {"lib": "corner.lib"},
        "corners": {
            "process": ["tt", "ss"],
            "temperature_c": [-40, 125],
        },
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    default_req = _write_request(tmp_path, base, name="default.json")
    explicit_req = _write_request(
        tmp_path, {**base, "backend": "local"}, name="explicit.json"
    )

    default_report = sim.run_sim(str(default_req))
    explicit_report = sim.run_sim(str(explicit_req))
    flag_report = sim.run_sim(str(default_req), backend="local")

    # `netlist` echoes the request path field, identical across all three.
    assert _stripped_report(default_report) == _stripped_report(explicit_report)
    assert _stripped_report(default_report) == _stripped_report(flag_report)

    # Ordering is the odometer process x temperature expansion, unchanged.
    assert [c["corner_id"] for c in default_report["corners"]] == [
        "tt/novdd/-40C",
        "tt/novdd/125C",
        "ss/novdd/-40C",
        "ss/novdd/125C",
    ]


# --------------------------------------------------------------------------- #
# local-parallel backend (#255)
# --------------------------------------------------------------------------- #


def test_run_sim_local_parallel_backend_is_order_identical_to_local(
    tmp_path, monkeypatch
):
    # Corners share nothing, so `local-parallel` must reassemble results in
    # the same odometer order `local` produces, regardless of which corner's
    # subprocess happens to finish first. Sleep inversely with temperature
    # so corners complete in *reverse* submission order under the pool,
    # actually exercising reassembly rather than accidentally passing
    # because completion order matched submission order.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 20, 30, 40]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
        "options": {"max_workers": 4},
    }

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        time.sleep(max(0.0, 40.0 - temp) * 0.005)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                "vout                =  1.00000e+00\n"
            )
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    parallel_req = _write_request(
        tmp_path, {**base, "backend": "local-parallel"}, name="parallel.json"
    )
    local_req = _write_request(tmp_path, base, name="local.json")

    parallel_report = sim.run_sim(str(parallel_req))
    local_report = sim.run_sim(str(local_req))

    assert [c["corner_id"] for c in parallel_report["corners"]] == [
        "default/novdd/10C",
        "default/novdd/20C",
        "default/novdd/30C",
        "default/novdd/40C",
    ]
    assert [c["corner_id"] for c in parallel_report["corners"]] == [
        c["corner_id"] for c in local_report["corners"]
    ]
    assert _stripped_report(parallel_report) == _stripped_report(local_report)


def test_run_sim_local_parallel_failing_corner_does_not_abort_siblings(
    tmp_path, monkeypatch
):
    # A corner that times out (or otherwise errors) is reported exactly as
    # `local` reports it, and the other corners in the same pool still
    # complete and report their own real status.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
            "options": {"max_workers": 3},
        },
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        if temp == 20:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                "vout                =  1.00000e+00\n"
            )
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    assert report["passed"] == 2
    assert report["errored"] == 1
    statuses = {c["corner_id"]: c["status"] for c in report["corners"]}
    assert statuses == {
        "default/novdd/10C": "pass",
        "default/novdd/20C": "error",
        "default/novdd/30C": "pass",
    }
    (errored_corner,) = [c for c in report["corners"] if c["status"] == "error"]
    assert any(d["code"] == "timeout" for d in errored_corner["diagnostics"])


def test_run_sim_local_parallel_default_max_workers_is_used(tmp_path, monkeypatch):
    # No explicit `max_workers` (request field or --flag) -> the backend
    # falls back to `_default_max_workers()`, never an unbounded pool.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)
    monkeypatch.setattr(sim, "_default_max_workers", lambda: 2)

    sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 2


def test_run_sim_max_workers_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": 5},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)

    sim.run_sim(str(request), max_workers=1)

    assert seen_workers["max_workers"] == 1


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, "3"])
def test_run_sim_invalid_max_workers_raises(tmp_path, bad_value):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": bad_value},
        },
    )
    with pytest.raises(sim.SimError, match="max_workers"):
        sim.run_sim(str(request))


def test_default_max_workers_derives_from_cpu_count(monkeypatch):
    # Pinned to "no host cap" (issue #2286) so the derived-default contract is
    # asserted on its own terms even when the box running pytest exports
    # `KLT_SIM_MAX_WORKERS` -- which is exactly what that feature asks a
    # shared box to do.
    monkeypatch.delenv(sim.MAX_WORKERS_ENV, raising=False)
    monkeypatch.setattr(sim.os, "cpu_count", lambda: 32)
    assert sim._default_max_workers() == 4

    monkeypatch.setattr(sim.os, "cpu_count", lambda: 1)
    assert sim._default_max_workers() == 1

    monkeypatch.setattr(sim.os, "cpu_count", lambda: None)
    assert sim._default_max_workers() == 1


# --------------------------------------------------------------------------- #
# Host-level worker cap: $KLT_SIM_MAX_WORKERS (issue #2286)
# --------------------------------------------------------------------------- #


def _recording_pool(monkeypatch):
    """Record the `max_workers` the `local-parallel` backend actually hands
    to its `ThreadPoolExecutor`, and return the dict it is recorded into."""
    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)
    return seen_workers


#: A stubbed `ngspice` log that satisfies `_PASSING_MEASUREMENT` below.
_PASSING_LOG = (
    "  Measurements for Transient Analysis\n\nvout                =  1.00000e+00\n"
)
_PASSING_MEASUREMENT = {
    "name": "vout",
    "spice": ".meas tran vout FIND v(out) AT=1u",
    "limits": {"min": 0.5},
}


def _parallel_request(tmp_path, *, options=None, name="request.json", **extra):
    _write_body(tmp_path)
    body = {
        "netlist": "body.spice",
        "backend": "local-parallel",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        **extra,
    }
    if options is not None:
        body["options"] = options
    return _write_request(tmp_path, body, name=name)


def test_host_max_workers_cap_unset_is_unchanged(tmp_path, monkeypatch, capsys):
    # Acceptance criterion: unset -> behaviour identical to before #2286.
    monkeypatch.delenv(sim.MAX_WORKERS_ENV, raising=False)
    assert sim._host_max_workers_cap() is None

    monkeypatch.setattr(sim.os, "cpu_count", lambda: 32)
    assert sim._default_max_workers() == 4
    assert sim._resolve_max_workers(None) == 4
    assert sim._resolve_max_workers(64) == 64

    request = _parallel_request(tmp_path, options={"max_workers": 6})
    _stub_subprocess_run(monkeypatch)
    seen_workers = _recording_pool(monkeypatch)

    sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 6
    assert sim.MAX_WORKERS_ENV not in capsys.readouterr().err


def test_host_max_workers_cap_empty_string_means_unset(monkeypatch):
    # `KLT_SIM_MAX_WORKERS=` (an empty entry in a shell env file) is "no cap",
    # not a malformed cap -- see `_host_max_workers_cap`.
    monkeypatch.setattr(sim.os, "cpu_count", lambda: 32)
    for blank in ("", "   "):
        monkeypatch.setenv(sim.MAX_WORKERS_ENV, blank)
        assert sim._host_max_workers_cap() is None
        assert sim._default_max_workers() == 4
        assert sim._resolve_max_workers(64) == 64


def test_host_max_workers_cap_bounds_the_derived_default(monkeypatch):
    # Set, with no explicit max_workers anywhere: the CPU-derived default is
    # bounded by the cap.
    monkeypatch.setattr(sim.os, "cpu_count", lambda: 32)

    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "2")
    assert sim._host_max_workers_cap() == 2
    assert sim._default_max_workers() == 2
    assert sim._resolve_max_workers(None) == 2

    # A cap *above* the derived default leaves it alone -- the cap is an upper
    # bound, never a floor that widens the pool.
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "16")
    assert sim._default_max_workers() == 4
    assert sim._resolve_max_workers(None) == 4


def test_host_max_workers_cap_bounds_default_pool_end_to_end(
    tmp_path, monkeypatch, capsys
):
    # The same thing through `run_sim`, which is the path the CLI, a library
    # caller, and this test suite's own simulation fixtures all share.
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "1")
    monkeypatch.setattr(sim.os, "cpu_count", lambda: 64)
    request = _parallel_request(tmp_path)
    _stub_subprocess_run(monkeypatch)
    seen_workers = _recording_pool(monkeypatch)

    sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 1
    # Nobody asked for a specific number here, so nothing is "clamped" and no
    # notice is owed -- see `_default_max_workers`.
    assert sim.MAX_WORKERS_ENV not in capsys.readouterr().err


def test_host_max_workers_cap_leaves_request_below_cap_alone(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "4")
    request = _parallel_request(tmp_path, options={"max_workers": 2})
    _stub_subprocess_run(monkeypatch)
    seen_workers = _recording_pool(monkeypatch)

    sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 2
    assert sim.MAX_WORKERS_ENV not in capsys.readouterr().err

    # An exactly-at-the-cap request is likewise untouched.
    assert sim._resolve_max_workers(4) == 4


def test_host_max_workers_cap_clamps_request_above_cap(tmp_path, monkeypatch, capsys):
    # Acceptance criterion: clamped, not errored, and says so once.
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "2")
    request = _parallel_request(tmp_path, options={"max_workers": 16})
    _stub_subprocess_run(monkeypatch)
    seen_workers = _recording_pool(monkeypatch)

    report = sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 2
    assert report["status"] != "error"

    captured = capsys.readouterr()
    notices = [
        line for line in captured.err.splitlines() if sim.MAX_WORKERS_ENV in line
    ]
    assert len(notices) == 1
    assert "max_workers=16" in notices[0]
    assert f"{sim.MAX_WORKERS_ENV}=2" in notices[0]
    assert "clamping" in notices[0]
    # stderr only -- the report JSON on stdout is unaffected.
    assert sim.MAX_WORKERS_ENV not in captured.out


def test_host_max_workers_cap_clamps_cli_flag_above_cap(tmp_path, monkeypatch, capsys):
    # The `--max-workers` flag path (`klt sim ... --max-workers N`) is clamped
    # the same way, and the run still succeeds (exit 0, not an error exit).
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "1")
    request = _parallel_request(
        tmp_path,
        options={"max_workers": 2},
        measurements=[_PASSING_MEASUREMENT],
    )
    _stub_subprocess_run(monkeypatch, log_text=_PASSING_LOG)
    seen_workers = _recording_pool(monkeypatch)

    exit_code = main(["sim", str(request), "--max-workers", "8", "--format", "json"])

    assert exit_code == 0
    assert seen_workers["max_workers"] == 1
    captured = capsys.readouterr()
    assert (
        len([line for line in captured.err.splitlines() if sim.MAX_WORKERS_ENV in line])
        == 1
    )
    # The JSON envelope on stdout still parses -- the notice never leaks into it.
    json.loads(captured.out)


def test_host_max_workers_cap_notice_is_once_per_sweep(tmp_path, monkeypatch, capsys):
    # Deduped within a sweep (`hosts > 1` re-enters `_run_local_parallel` once
    # per shard), but a *second* sweep in the same process is told again.
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "2")
    request = _parallel_request(tmp_path, options={"max_workers": 16})
    _stub_subprocess_run(monkeypatch)

    sim._reset_worker_cap_notices()
    assert sim._resolve_max_workers(16) == 2
    assert sim._resolve_max_workers(16) == 2
    assert (
        len(
            [
                line
                for line in capsys.readouterr().err.splitlines()
                if sim.MAX_WORKERS_ENV in line
            ]
        )
        == 1
    )

    sim.run_sim(str(request))
    first = capsys.readouterr().err
    sim.run_sim(str(request))
    second = capsys.readouterr().err

    assert sim.MAX_WORKERS_ENV in first
    assert sim.MAX_WORKERS_ENV in second


def test_host_max_workers_cap_applies_to_sharded_local_parallel(
    tmp_path, monkeypatch, capsys
):
    # `--hosts N` fans the unit list across N in-process shards, each of which
    # builds its own `local-parallel` pool: every shard is capped, and the
    # caller is still told exactly once.
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "1")
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": 8},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = []
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers.append(max_workers)
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)

    report = sim.run_sim(str(request), hosts=2)

    assert report["corner_count"] == 4
    # Every `local-parallel` pool built during the sweep is capped. (The shard
    # fan-out itself uses a pool too; it is sized by `hosts`, not by the
    # worker cap, so only assert that no pool exceeded the cap for workers.)
    assert seen_workers.count(1) >= 2
    assert 8 not in seen_workers
    assert (
        len(
            [
                line
                for line in capsys.readouterr().err.splitlines()
                if sim.MAX_WORKERS_ENV in line
            ]
        )
        == 1
    )


@pytest.mark.parametrize("bad_value", ["0", "-1", "1.5", "many", "0x4", "2 workers"])
def test_invalid_host_max_workers_cap_is_an_application_error(
    tmp_path, monkeypatch, bad_value
):
    # Acceptance criterion: a `KLT_SIM_MAX_WORKERS=0`/non-integer value is a
    # hard error, deliberately consistent with `options.max_workers < 1` --
    # silently degrading to "uncapped" would defeat the point of the cap.
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, bad_value)
    with pytest.raises(sim.SimError, match=sim.MAX_WORKERS_ENV):
        sim._host_max_workers_cap()

    request = _parallel_request(tmp_path)
    _stub_subprocess_run(monkeypatch)
    with pytest.raises(sim.SimError, match=sim.MAX_WORKERS_ENV):
        sim.run_sim(str(request))


def test_invalid_host_max_workers_cap_fails_before_dispatch(tmp_path, monkeypatch):
    # The cap is validated up front, so a malformed value never lets a single
    # corner start (and never surfaces from inside a shard thread).
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "0")
    request = _parallel_request(tmp_path)

    calls = []

    def fake_run(  # pragma: no cover - guard
        cmd, capture_output, text, timeout, cwd=None
    ):
        calls.append(cmd)
        raise AssertionError("no corner should be dispatched with a malformed cap")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    with pytest.raises(sim.SimError, match="must be a positive integer"):
        sim.run_sim(str(request))
    assert calls == []


def test_invalid_host_max_workers_cap_is_a_clean_cli_error(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(sim.MAX_WORKERS_ENV, "nope")
    request = _parallel_request(tmp_path)
    _stub_subprocess_run(monkeypatch)

    exit_code = main(["sim", str(request)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert (
        f"klt sim: {sim.MAX_WORKERS_ENV} must be a positive integer (got 'nope')"
        in captured.err
    )
    assert captured.out == ""


# --------------------------------------------------------------------------- #
# Bounded corner-sweep runner: wall-clock budget, orphan safety, resume
# (issue #473)
# --------------------------------------------------------------------------- #


def _fake_run_sleeping(run_calls: list, sleep_s: float = 0.1):
    """A ``subprocess.run`` stand-in that records the corner's ``.temp`` and
    sleeps ``sleep_s`` before "completing" -- used to make budget/orphan
    checks between corners deterministic without a real ngspice binary."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        run_calls.append(temp)
        time.sleep(sleep_s)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("")
        return fake_completed("** ngspice-99\n")

    return fake_run


@pytest.mark.parametrize("backend", ["local", "local-parallel"])
def test_run_sim_budget_exceeded_skips_remaining_corners(
    tmp_path, monkeypatch, backend
):
    _write_body(tmp_path)
    options = {"wall_clock_budget_s": 0.05}
    if backend == "local-parallel":
        options["max_workers"] = 1  # keep dispatch order deterministic
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": backend,
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": options,
        },
    )

    run_calls: list[float] = []
    monkeypatch.setattr(sim.subprocess, "run", _fake_run_sleeping(run_calls))

    report = sim.run_sim(str(request))

    # The very first corner always gets a chance to run -- the deadline
    # check happens before dispatch, not after the budget window opens, so
    # a budget smaller than one corner's own runtime still attempts (and,
    # here, completes) the first corner rather than reporting zero.
    assert run_calls == [10]
    statuses = [c["status"] for c in report["corners"]]
    assert statuses == ["pass", "error", "error"]
    for corner in report["corners"][1:]:
        codes = [d["code"] for d in corner["diagnostics"]]
        assert codes == ["budget_exceeded"]

    assert report["status"] == "error"
    assert report["errored"] == 2
    budget = report["environment"]["budget"]
    assert budget["wall_clock_budget_s"] == 0.05
    assert budget["exceeded"] is True
    assert budget["corners_skipped"] == 2


def test_run_sim_budget_not_exceeded_is_unreported(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"wall_clock_budget_s": 3600},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    budget = report["environment"]["budget"]
    assert budget["exceeded"] is False
    assert budget["corners_skipped"] == 0
    assert report["status"] == "not_checked"


def test_run_sim_omitting_budget_leaves_environment_unchanged(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "budget" not in report["environment"]
    assert "orphaned" not in report["environment"]
    assert "resume" not in report["environment"]


@pytest.mark.parametrize("bad_value", [0, -1, "3"])
def test_run_sim_invalid_wall_clock_budget_s_raises(tmp_path, bad_value):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"wall_clock_budget_s": bad_value},
        },
    )
    with pytest.raises(sim.SimError, match="wall_clock_budget_s"):
        sim.run_sim(str(request))


def test_run_sim_budget_s_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": [10, 20]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"wall_clock_budget_s": 3600},
        },
    )
    run_calls: list[float] = []
    monkeypatch.setattr(sim.subprocess, "run", _fake_run_sleeping(run_calls))

    report = sim.run_sim(str(request), budget_s=0.05)

    assert report["environment"]["budget"]["wall_clock_budget_s"] == 0.05
    assert report["environment"]["budget"]["exceeded"] is True


def test_run_sim_orphaned_parent_stops_dispatch(tmp_path, monkeypatch):
    # Simulates the incident's root failure mode: the launching process
    # exits mid-sweep. `os.getppid()` is checked between corners; once it
    # no longer matches the value captured at the start of the sweep, no
    # further corner is dispatched.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    real_getppid = sim.os.getppid
    calls = {"n": 0}

    def fake_getppid():
        calls["n"] += 1
        # Call 1: `run_sim`'s own `initial_ppid` capture. Call 2: the check
        # before the first corner (still the same launcher -- it runs).
        # Every call after that reports a different PPID, simulating the
        # launcher having exited and this process being reparented.
        if calls["n"] <= 2:
            return real_getppid()
        return real_getppid() + 999_999

    monkeypatch.setattr(sim.os, "getppid", fake_getppid)

    report = sim.run_sim(str(request))

    statuses = [c["status"] for c in report["corners"]]
    assert statuses == ["pass", "error", "error"]
    for corner in report["corners"][1:]:
        codes = [d["code"] for d in corner["diagnostics"]]
        assert codes == ["orphaned"]
    assert report["environment"]["orphaned"] is True


def test_run_sim_resume_persists_and_skips_completed_corners(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request_path = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"resume": True, "wall_clock_budget_s": 0.05},
        },
    )

    run_calls: list[float] = []
    monkeypatch.setattr(sim.subprocess, "run", _fake_run_sleeping(run_calls))

    first = sim.run_sim(str(request_path))
    assert first["status"] == "error"
    assert [c["status"] for c in first["corners"]] == ["pass", "error", "error"]
    assert run_calls == [10]
    assert first["environment"]["resume"]["resumed_corners"] == 0
    assert first["environment"]["resume"]["checkpoint_retained"] is True

    checkpoint_path = os.path.join(tmp_path, ".klt", "sim", "checkpoint.json")
    assert os.path.isfile(checkpoint_path)

    # A second invocation of the *same* request, budget lifted: only the
    # corners the checkpoint doesn't already have get dispatched.
    request_doc = json.loads(request_path.read_text())
    del request_doc["options"]["wall_clock_budget_s"]
    request_path.write_text(json.dumps(request_doc))

    second = sim.run_sim(str(request_path))
    assert second["status"] == "not_checked"
    assert [c["status"] for c in second["corners"]] == ["pass", "pass", "pass"]
    assert run_calls == [10, 20, 30]  # the completed corner was never re-run
    assert second["environment"]["resume"]["resumed_corners"] == 1
    # Nothing left to resume -- the checkpoint is cleaned up.
    assert second["environment"]["resume"]["checkpoint_retained"] is False
    assert not os.path.isfile(checkpoint_path)


def test_run_sim_resume_ignores_stale_checkpoint_when_netlist_changes(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request_path = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"temperature_c": [10, 20]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"resume": True, "wall_clock_budget_s": 0.05},
        },
    )

    run_calls: list[float] = []
    monkeypatch.setattr(sim.subprocess, "run", _fake_run_sleeping(run_calls))

    sim.run_sim(str(request_path))
    assert run_calls == [10]

    # The netlist is edited in place -- the checkpoint's fingerprint no
    # longer matches, so it must not be silently reused even though the
    # file still exists and still names the same corner matrix.
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 2k\nC1 out 0 1n\n"
    )
    request_doc = json.loads(request_path.read_text())
    del request_doc["options"]["wall_clock_budget_s"]
    request_path.write_text(json.dumps(request_doc))

    second = sim.run_sim(str(request_path))

    assert run_calls == [10, 10, 20]  # corner 10 recomputed, not skipped
    assert second["environment"]["resume"]["resumed_corners"] == 0


def test_run_sim_resume_with_remote_backend_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        _base_remote_request(options={"resume": True}),
    )
    with pytest.raises(sim.SimError, match="resume"):
        sim.run_sim(str(request))


def test_run_sim_resume_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), resume=True)

    assert "resume" in report["environment"]


# --------------------------------------------------------------------------- #
# Path normalization (issue #1261): `netlist` and
# `environment.resume.checkpoint_path` report `{path, scope}`, never a raw
# absolute string -- a committed evidence record wraps this response
# unmodified (docs/design/sim-evidence-discipline-spike.md), so a leaked
# absolute path here used to land in every such record verbatim.
# --------------------------------------------------------------------------- #


def _make_fake_repo(tmp_path: Path) -> Path:
    """A `.git`-marked directory `env_provenance.find_repo_root` recognises
    as a repo root -- mirrors `tests/test_env_provenance.py`'s own
    `_make_repo` helper (kept local here rather than imported across test
    modules, matching this file's existing convention)."""
    root = tmp_path / "fake-repo"
    (root / ".git").mkdir(parents=True)
    return root


def test_run_sim_netlist_is_external_outside_any_repo(tmp_path, monkeypatch):
    """`tmp_path` sits outside this repo -- `netlist` must report
    `{"path": None, "scope": "external"}`, never the raw absolute path."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["netlist"] == {"path": None, "scope": "external"}


def test_run_sim_netlist_is_repo_relative_inside_a_repo(tmp_path, monkeypatch):
    """The regression the issue's own Test Plan calls out: an input path
    *inside* a repo must still resolve to something usable (a repo-relative
    string), not `null`/dropped -- the fix only changes the outside-the-repo
    (leaking) case."""
    root = _make_fake_repo(tmp_path)
    _write_body(root)
    request = _write_request(
        root,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["netlist"] == {"path": "body.spice", "scope": "repo"}


def test_run_sim_checkpoint_path_is_repo_relative_inside_a_repo(tmp_path, monkeypatch):
    root = _make_fake_repo(tmp_path)
    _write_body(root)
    request = _write_request(
        root,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), resume=True)

    assert report["environment"]["resume"]["checkpoint_path"] == {
        "path": ".klt/sim/checkpoint.json",
        "scope": "repo",
    }


def test_run_sim_a_repo_relative_request_argument_is_not_doubly_nested(
    tmp_path, monkeypatch
):
    """Edge case from the issue's Test Plan: `<request>` is given as a
    relative CLI argument (relative to the current process's cwd) -- the
    already-relative `netlist` value it references must resolve once, not
    be treated as needing a second round of relativizing (which would nest
    it under itself or otherwise malform it)."""
    root = _make_fake_repo(tmp_path)
    _write_body(root)
    request = _write_request(
        root,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    cwd = os.getcwd()
    os.chdir(root)
    try:
        report = sim.run_sim(os.path.basename(request))
    finally:
        os.chdir(cwd)

    assert report["netlist"] == {"path": "body.spice", "scope": "repo"}


# --------------------------------------------------------------------------- #
# Path normalization, part 2 (issue #1274): `environment.models_lib` was
# missed by #1261's field list, so it kept echoing the resolved *absolute*
# PDK path -- on a normal install, the author's home directory.
# --------------------------------------------------------------------------- #


def test_run_sim_models_lib_is_external_outside_any_repo(tmp_path, monkeypatch):
    """The leak this issue reports: a model library outside the repo (the
    normal case -- a PDK under `~/.volare`) must report
    `{"path": None, "scope": "external"}`, never its absolute path."""
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["models_lib"] == {
        "path": None,
        "scope": "external",
    }
    # The identity pins that make the location redundant are still present.
    assert report["environment"]["models_lib_sha256"] is not None
    assert report["provenance"]["deck"]["name"] == "corner.lib"


def test_run_sim_models_lib_is_repo_relative_inside_a_repo(tmp_path, monkeypatch):
    """A model library *inside* the repo must still resolve to something
    usable (a repo-relative string), not `null`/dropped -- the fix only
    changes the outside-the-repo (leaking) case."""
    root = _make_fake_repo(tmp_path)
    _write_body(root)
    _write_corner_lib(root)
    request = _write_request(
        root,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["models_lib"] == {
        "path": "corner.lib",
        "scope": "repo",
    }


def test_run_sim_models_lib_is_absent_when_no_process_axis(tmp_path, monkeypatch):
    """No `corners.process` axis means no model library was resolved at all
    -- reported as `scope: "absent"`, kept distinct from "outside the repo"
    (`env_provenance.repo_relative_path`'s own tri-state)."""
    root = _make_fake_repo(tmp_path)
    _write_body(root)
    request = _write_request(
        root,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["models_lib"] == {"path": None, "scope": "absent"}


# --------------------------------------------------------------------------- #
# Fleet shard/merge engine (Epic #375 Phase 1A, #376) -- pure logic, no AWS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hosts", range(1, 8))
def test_shard_corner_points_round_trips_for_every_host_count(hosts):
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(7)]

    shards = sim._shard_corner_points(points, hosts)

    assert len(shards) == hosts
    # Concatenating every shard, in shard order, reproduces the original
    # list exactly -- the round-trip every merge in this module depends on.
    assert [p for shard in shards for p in shard] == points
    sizes = [len(shard) for shard in shards]
    assert max(sizes) - min(sizes) <= 1


def test_shard_corner_points_hosts_exceeding_units_leaves_trailing_shards_empty():
    points = [sim.CornerPoint(None, {}, 1.0), sim.CornerPoint(None, {}, 2.0)]

    shards = sim._shard_corner_points(points, 5)

    assert len(shards) == 5
    assert [p for shard in shards for p in shard] == points
    assert sum(1 for shard in shards if shard) == 2


@pytest.mark.parametrize("bad_hosts", [0, -1])
def test_shard_corner_points_rejects_non_positive_hosts(bad_hosts):
    with pytest.raises(sim.SimError, match="hosts"):
        sim._shard_corner_points([], bad_hosts)


def test_lost_shard_corner_shape_matches_run_corner_error_shape():
    point = sim.CornerPoint("tt", {"vdd": 1.8}, 25.0)

    corner = sim._lost_shard_corner(
        point,
        [
            {"name": "vout", "unit": "V"},
            {"name": "iq"},
        ],
        "shard lost: boom",
    )

    assert corner["corner_id"] == point.corner_id
    assert corner["process"] == "tt"
    assert corner["supply_v"] == {"vdd": 1.8}
    assert corner["temperature_c"] == 25.0
    assert corner["status"] == "error"
    assert corner["monte_carlo"] is None
    assert corner["diagnostics"] == [
        {"severity": "error", "code": "lost_shard", "message": "shard lost: boom"}
    ]
    assert corner["measurements"] == [
        {"name": "vout", "value": None, "unit": "V", "status": "error", "margin": None},
        {"name": "iq", "value": None, "unit": None, "status": "error", "margin": None},
    ]
    assert corner["artifacts"] == {
        "log": None,
        "raw": None,
        "waveform": None,
        "deck": None,
    }


def test_lost_shard_corner_carries_monte_carlo_block_for_a_sampled_point():
    point = sim.CornerPoint(
        "tt",
        {},
        25.0,
        sample_index=3,
        mc_seed={"rndseed": 111, "process_seed": 222, "mismatch_seed": 333},
    )

    corner = sim._lost_shard_corner(point, [], "shard lost: boom")

    assert corner["corner_id"].endswith("/mc3")
    assert corner["monte_carlo"] == {
        "sample_index": 3,
        "seed": 111,
        "process_seed": 222,
        "mismatch_seed": 333,
    }


def test_run_sharded_merges_in_global_order_regardless_of_completion_order():
    # Sleep inversely with each shard's own marker so shards complete in
    # *reverse* submission order under the pool -- actually exercising
    # index-based reassembly rather than accidentally passing because
    # completion order happened to match shard order.
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(6)]

    def shard_runner(shard_points):
        time.sleep(0.01 * (10 - shard_points[0].temperature_c))
        corners = [
            {"corner_id": p.corner_id, "marker": p.temperature_c} for p in shard_points
        ]
        return corners, None, None

    corners, engine_version, remote_environment = sim._run_sharded(
        shard_runner=shard_runner,
        corner_points=points,
        hosts=3,
        measurements_spec=[],
    )

    assert [c["marker"] for c in corners] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert engine_version is None
    assert remote_environment is None


def test_run_sharded_lost_shard_produces_per_unit_errors_and_correct_counts():
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(6)]

    def shard_runner(shard_points):
        if shard_points[0].temperature_c == 2.0:
            raise RuntimeError("boom")
        corners = [
            {
                "corner_id": p.corner_id,
                "status": "pass",
                "measurements": [],
                "diagnostics": [],
                "artifacts": {},
                "monte_carlo": None,
            }
            for p in shard_points
        ]
        return corners, "46", None

    corners, engine_version, remote_environment = sim._run_sharded(
        shard_runner=shard_runner,
        corner_points=points,
        hosts=3,
        measurements_spec=[{"name": "vout", "unit": "V"}],
    )

    assert len(corners) == 6
    assert [c["status"] for c in corners] == [
        "pass",
        "pass",
        "error",
        "error",
        "pass",
        "pass",
    ]
    errored = [c for c in corners if c["status"] == "error"]
    assert len(errored) == 2
    for corner in errored:
        assert corner["diagnostics"] == [
            {
                "severity": "error",
                "code": "lost_shard",
                "message": "shard lost: boom",
            }
        ]
        assert corner["measurements"] == [
            {
                "name": "vout",
                "value": None,
                "unit": "V",
                "status": "error",
                "margin": None,
            }
        ]
    # `engine_version` -- last non-None wins, in shard (global unit) order --
    # is unaffected by the lost shard in the middle.
    assert engine_version == "46"
    assert remote_environment is None


def test_run_sharded_builds_fleet_array_when_any_shard_has_remote_environment():
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(4)]

    def shard_runner(shard_points):
        remote_env = {"instance_id": f"i-{int(shard_points[0].temperature_c)}"}
        corners = [{"corner_id": p.corner_id} for p in shard_points]
        return corners, None, remote_env

    _, _, remote_environment = sim._run_sharded(
        shard_runner=shard_runner, corner_points=points, hosts=2, measurements_spec=[]
    )

    assert remote_environment == {
        "fleet": [{"instance_id": "i-0"}, {"instance_id": "i-2"}]
    }


def test_run_sharded_fleet_array_has_null_entry_for_lost_shard():
    points = [sim.CornerPoint(None, {}, float(i)) for i in range(4)]

    def shard_runner(shard_points):
        if shard_points[0].temperature_c == 2.0:
            raise RuntimeError("provisioning failed")
        corners = [{"corner_id": p.corner_id} for p in shard_points]
        return corners, None, {"instance_id": "i-0"}

    _, _, remote_environment = sim._run_sharded(
        shard_runner=shard_runner, corner_points=points, hosts=2, measurements_spec=[]
    )

    assert remote_environment == {"fleet": [{"instance_id": "i-0"}, None]}


def test_run_sharded_never_calls_shard_runner_for_an_empty_shard():
    points = [sim.CornerPoint(None, {}, 1.0)]
    calls = []

    def shard_runner(shard_points):
        calls.append(list(shard_points))
        return [{"corner_id": p.corner_id} for p in shard_points], None, None

    corners, _, _ = sim._run_sharded(
        shard_runner=shard_runner, corner_points=points, hosts=3, measurements_spec=[]
    )

    assert len(calls) == 1  # only the one non-empty shard was ever run
    assert len(corners) == 1


def test_run_sim_hosts_one_is_byte_identical_to_hosts_absent(tmp_path, monkeypatch):
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 20, 30]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )
    request = _write_request(tmp_path, base)

    default_report = sim.run_sim(str(request))
    explicit_hosts_report = sim.run_sim(str(request), hosts=1)

    assert _stripped_report(default_report) == _stripped_report(explicit_hosts_report)


def test_run_sim_local_parallel_hosts_shards_match_unsharded_report(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "backend": "local-parallel",
        "corners": {"temperature_c": [10, 20, 30, 40, 50, 60, 70]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
        "options": {"max_workers": 4},
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )
    unsharded_req = _write_request(tmp_path, base, name="unsharded.json")
    sharded_req = _write_request(tmp_path, base, name="sharded.json")

    unsharded_report = sim.run_sim(str(unsharded_req))
    sharded_report = sim.run_sim(str(sharded_req), hosts=3)

    assert [c["corner_id"] for c in sharded_report["corners"]] == [
        c["corner_id"] for c in unsharded_report["corners"]
    ]
    assert _stripped_report(sharded_report) == _stripped_report(unsharded_report)
    assert "remote" not in sharded_report["environment"]


def test_run_sim_monte_carlo_statistics_over_sharded_run_equal_unsharded(
    tmp_path, monkeypatch
):
    # `hosts > 1` must not change a single sampled value -- MC seeds are
    # already absolute (`sample_index`-derived, #348) -- so the merged
    # report's Monte Carlo statistics must equal the unsharded run's,
    # regardless of which shard/thread actually produced which sample.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "backend": "local-parallel",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        "monte_carlo": {"n": 12, "seed": 42, "vary": "mismatch"},
        "options": {"max_workers": 4},
    }

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        sample_index = int(re.search(r"mc_sample_index=(\d+)", deck_text).group(1))
        value = 1.0 + 0.01 * sample_index
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                f"vout                =  {value:.5e}\n"
            )
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    unsharded_req = _write_request(tmp_path, base, name="unsharded.json")
    sharded_req = _write_request(tmp_path, base, name="sharded.json")

    unsharded_report = sim.run_sim(str(unsharded_req))
    sharded_report = sim.run_sim(str(sharded_req), hosts=4)

    assert [c["corner_id"] for c in sharded_report["corners"]] == [
        c["corner_id"] for c in unsharded_report["corners"]
    ]
    assert sharded_report["measurements"] == unsharded_report["measurements"]
    assert sharded_report["corner_count"] == unsharded_report["corner_count"]
    assert sharded_report["passed"] == unsharded_report["passed"]


def test_run_sim_hosts_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "remote": {"hosts": 4},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_hosts = []
    real_shard = sim._shard_corner_points

    def spy(points, hosts):
        seen_hosts.append(hosts)
        return real_shard(points, hosts)

    monkeypatch.setattr(sim, "_shard_corner_points", spy)

    sim.run_sim(str(request), hosts=2)

    assert seen_hosts == [2]


def test_run_sim_hosts_request_field_used_when_flag_omitted(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "remote": {"hosts": 4},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_hosts = []
    real_shard = sim._shard_corner_points

    def spy(points, hosts):
        seen_hosts.append(hosts)
        return real_shard(points, hosts)

    monkeypatch.setattr(sim, "_shard_corner_points", spy)

    sim.run_sim(str(request))

    assert seen_hosts == [4]


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, "3"])
def test_run_sim_invalid_hosts_raises(tmp_path, bad_value):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "remote": {"hosts": bad_value},
        },
    )
    with pytest.raises(sim.SimError, match="hosts"):
        sim.run_sim(str(request))


def test_run_sim_hosts_over_units_remote_backend_raises_before_aws_call(
    tmp_path, monkeypatch
):
    """``hosts`` may not exceed the number of units to dispatch for backend
    ``remote`` -- issue #906's fleet dispatch (:func:`sim._run_remote_fleet`)
    rejects this before ever touching ``remote_fleet.run_fleet`` (an idle
    fleet member would still be billed). This single-default-corner request
    has exactly one unit, so ``hosts=2`` is always over the line."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "remote",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "models": {"pdk": "sky130A"},
            "remote": {
                "region": "us-east-1",
                "ssh_key_path": "/tmp/key.pem",
                "hosts": 2,
            },
        },
    )

    def _unexpected(*args, **kwargs):
        raise AssertionError(
            "hosts (2) exceeding the unit count (1) must not touch AWS"
        )

    monkeypatch.setattr(sim, "RemoteLauncher", _unexpected)
    monkeypatch.setattr(sim.remote_fleet, "run_fleet", _unexpected)

    with pytest.raises(sim.SimError, match="exceeds the number of units"):
        sim.run_sim(str(request))


def test_cli_hosts_flag_shards_the_run(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30, 40]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_hosts = []
    real_shard = sim._shard_corner_points

    def spy(points, hosts):
        seen_hosts.append(hosts)
        return real_shard(points, hosts)

    monkeypatch.setattr(sim, "_shard_corner_points", spy)

    main(["sim", str(request), "--hosts", "2", "--format", "json"])

    assert seen_hosts == [2]


def test_run_sim_stubbed_fail(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.5},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "fail"
    assert report["failed"] == 1
    # Issue #1849: `metrics` reflects the at-least-one-failure case too,
    # matching the existing `failed` field exactly.
    assert report["metrics"] == {
        "sim__corner__count": report["corner_count"],
        "sim__corner__passed_count": report["passed"],
        "sim__corner__failed_count": report["failed"],
        "sim__corner__errored_count": report["errored"],
        "sim__corner__inconclusive_count": report["inconclusive"],
    }
    assert report["metrics"]["sim__corner__failed_count"] == 1


# --------------------------------------------------------------------------- #
# Plausibility bounds (issue #2493): `options.node_voltage_bounds` /
# `measurements[].plausible_range`, end to end through `run_sim`.
#
# Every fixture below shares one `log_text`: an ngspice `.meas` reading of
# `142.7` on a measurement whose declared `limits` is `{min: 0, max: 1.9}`
# (a 1.8V-rail-shaped bound) -- a numerically valid but physically
# impossible solve. The whole point of this block is demonstrating that the
# *same* out-of-range value grades differently depending on whether a
# plausibility bound was declared (the false-fail scenario from the issue's
# Problem Statement).
# --------------------------------------------------------------------------- #

_IMPLAUSIBLE_LOG_TEXT = (
    "  Measurements for Transient Analysis\n\nvout                =  1.42700e+02\n"
)


def test_run_sim_stubbed_out_of_range_value_without_bound_grades_fail(
    tmp_path, monkeypatch
):
    """Control case: no plausibility bound declared -> the ordinary `limits`
    path grades the implausible 142.7 reading `"fail"`, exactly as it does
    today -- reproducing the false-fail artifact the issue exists to fix."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.0, "max": 1.9},
                }
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    report = sim.run_sim(str(request))

    assert report["status"] == "fail"
    assert report["failed"] == 1
    assert report["inconclusive"] == 0
    (corner,) = report["corners"]
    assert corner["status"] == "fail"
    assert corner["measurements"][0]["status"] == "fail"


def test_run_sim_stubbed_measurement_plausible_range_grades_implausible(
    tmp_path, monkeypatch
):
    """Same 142.7 reading, same `limits` -- but this measurement also
    declares `plausible_range`, so it grades `"inconclusive"` (with an
    `implausible_solution` diagnostic naming the reason) instead of
    `"fail"`, and the check never reaches `_evaluate_limits` (`margin` stays
    `null`)."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.0, "max": 1.9},
                    "plausible_range": {"min": -0.5, "max": 2.5},
                }
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    report = sim.run_sim(str(request))

    assert report["status"] == "inconclusive"
    assert report["failed"] == 0
    assert report["passed"] == 0
    assert report["errored"] == 0
    assert report["inconclusive"] == 1
    # The four counts still sum to `corner_count` (the merged invariant).
    assert (
        report["passed"] + report["failed"] + report["errored"] + report["inconclusive"]
        == report["corner_count"]
    )
    (corner,) = report["corners"]
    assert corner["status"] == "inconclusive"
    measurement = corner["measurements"][0]
    assert measurement["status"] == "inconclusive"
    assert measurement["value"] == pytest.approx(142.7)
    assert measurement["margin"] is None
    # The *reason* stays distinguishable in the diagnostic code -- and so in
    # `diagnostic_counts` -- even though the status is shared with #2492.
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "implausible_solution" in codes
    assert report["diagnostic_counts"]["by_code"]["implausible_solution"] == 1
    diag = next(d for d in corner["diagnostics"] if d["code"] == "implausible_solution")
    assert diag["severity"] == "warning"  # never "error" -- status carries the grade
    (rollup_entry,) = report["measurements"]
    assert rollup_entry["status"] == "inconclusive"
    assert rollup_entry["plausible_range"] == {"min": -0.5, "max": 2.5}


def test_run_sim_stubbed_node_voltage_bounds_applies_to_voltage_unit(
    tmp_path, monkeypatch
):
    """`options.node_voltage_bounds` is a run-wide default that auto-applies
    to a measurement declaring `unit: "V"` with no own `plausible_range`."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.0, "max": 1.9},
                }
            ],
            "options": {"node_voltage_bounds": {"min": -0.5, "max": 2.5}},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    report = sim.run_sim(str(request))

    assert report["status"] == "inconclusive"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["status"] == "inconclusive"


def test_run_sim_stubbed_node_voltage_bounds_does_not_apply_to_non_voltage_unit(
    tmp_path, monkeypatch
):
    """Issue #2493 scoping decision: `options.node_voltage_bounds` is
    voltage-scoped -- a measurement with any other `unit` (or none) never
    inherits it, and grades via the ordinary `limits` path unchanged."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "A",
                    "limits": {"min": 0.0, "max": 1.9},
                }
            ],
            "options": {"node_voltage_bounds": {"min": -0.5, "max": 2.5}},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    report = sim.run_sim(str(request))

    assert report["status"] == "fail"
    assert report["inconclusive"] == 0
    (corner,) = report["corners"]
    assert corner["measurements"][0]["status"] == "fail"


def test_run_sim_stubbed_measurement_plausible_range_overrides_node_voltage_bounds(
    tmp_path, monkeypatch
):
    """A measurement's own `plausible_range` always wins over the inherited
    `options.node_voltage_bounds` default when both are declared."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.0, "max": 200.0},
                    # Wider than options.node_voltage_bounds -- 142.7 is
                    # plausible under this measurement's own declaration.
                    "plausible_range": {"min": -1.0, "max": 200.0},
                }
            ],
            # Narrower run-wide default that WOULD flag 142.7 if it applied.
            "options": {"node_voltage_bounds": {"min": -0.5, "max": 2.5}},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["inconclusive"] == 0
    (corner,) = report["corners"]
    assert corner["measurements"][0]["status"] == "pass"


def test_run_sim_stubbed_plausible_range_bracketing_limits_never_triggers(
    tmp_path, monkeypatch
):
    """Edge case: a plausibility bound wide enough to bracket the entire
    declared `limits` range never reclassifies anything -- the ordinary
    `limits` verdict (here, a clean fail) passes through unchanged."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.0, "max": 1.9},
                    "plausible_range": {"min": -1000.0, "max": 1000.0},
                }
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    report = sim.run_sim(str(request))

    assert report["status"] == "fail"  # not inconclusive
    assert report["inconclusive"] == 0
    (corner,) = report["corners"]
    assert corner["measurements"][0]["status"] == "fail"


def test_run_sim_rejects_malformed_node_voltage_bounds_before_any_corner_runs(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"node_voltage_bounds": {"min": 5, "max": 1}},
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("no corner should have been dispatched")

    monkeypatch.setattr(sim.subprocess, "run", fail_if_called)

    with pytest.raises(sim.SimError, match="node_voltage_bounds"):
        sim.run_sim(str(request))


def test_run_sim_rejects_malformed_measurement_plausible_range(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "plausible_range": {},
                }
            ],
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("no corner should have been dispatched")

    monkeypatch.setattr(sim.subprocess, "run", fail_if_called)

    with pytest.raises(sim.SimError, match="plausible_range"):
        sim.run_sim(str(request))


def test_cli_inconclusive_exits_4(tmp_path, monkeypatch, capsys):
    """Issues #2492 + #2493: `"inconclusive"` joins `"error"`/`"not_checked"`
    at CLI exit code 4 -- never the `EXIT_PASS` a status matching neither of
    the CLI's other two branches would otherwise fall through to."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.0, "max": 1.9},
                    "plausible_range": {"min": -0.5, "max": 2.5},
                }
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_IMPLAUSIBLE_LOG_TEXT)

    exit_code = main(["sim", str(request), "--format", "json"])

    assert exit_code == 4
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "inconclusive"


def test_run_sim_stubbed_missing_measurement_is_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch, log_text="  Measurements for Transient Analysis\n\n"
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] is None
    assert corner["measurements"][0]["status"] == "error"
    assert any(d["code"] == "measurement" for d in corner["diagnostics"])


#: A real ngspice-46 log (captured against the sky130 5T OTA composed by
#: `klt gen-compose`, per this issue's own repro) where gmin/source stepping
#: recovers from a singular DC operating-point matrix and the transient
#: still completes, producing correct measurement values -- yet also logs
#: "gmin stepping failed"/"source stepping failed" text along the way,
#: which is exactly why the fix cannot look at `singular_matrix` alone (see
#: `_recovered_from_stepping`'s docstring).
_RECOVERED_SINGULAR_MATRIX_LOG = (
    "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n\n"
    "Using SPARSE 1.3 as Direct Linear Solver\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Note: Starting dynamic gmin stepping\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Warning: Dynamic gmin stepping failed\n"
    "Note: Starting true gmin stepping\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Warning: True gmin stepping failed\n"
    "Note: Starting source stepping\n"
    "Warning: source stepping failed\n"
    "Note: Transient op started\n"
    "Note: Transient op finished successfully\n\n"
    "  Measurements for Transient Analysis\n\n"
    "vout_meas           =  1.00000e+00\n"
    "tail_meas           =  5.00000e-01\n"
)


def test_run_sim_stubbed_recovered_singular_matrix_reports_pass(tmp_path, monkeypatch):
    # The exact false positive this issue reports: a `singular matrix`
    # warning that ngspice's own gmin/source stepping recovers from, still
    # producing correct measurement values, must report `status: "pass"`,
    # not `"error"`.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {
                    "name": "vout_meas",
                    "spice": ".meas tran vout_meas find v(vout_node) at=1n",
                    "limits": {"min": 0.9, "max": 1.1},
                },
                {
                    "name": "tail_meas",
                    "spice": ".meas tran tail_meas find v(tail_node) at=1n",
                    "limits": {"min": 0.4, "max": 0.6},
                },
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert corner["measurements"][0]["value"] == 1.0
    assert corner["measurements"][1]["value"] == 0.5
    # Recorded for visibility, but downgraded -- not fatal.
    codes = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert codes["singular_matrix"] == "warning"
    assert codes.get("nonconvergence") == "warning"
    # Issue #2491: the top-level rollup must still count a recovered
    # warning-severity diagnostic on this otherwise-`pass`ed corner -- the
    # whole point of the rollup is that a caller reading only `passed`/
    # `failed`/`errored` cannot see this without it.
    diagnostic_counts = report["diagnostic_counts"]
    assert diagnostic_counts["by_code"]["singular_matrix"] == 1
    assert diagnostic_counts["by_code"]["nonconvergence"] == 1
    assert diagnostic_counts["by_severity"] == {"warning": 2}
    assert diagnostic_counts["corners_with_diagnostics"] == 1


def test_run_sim_stubbed_unrecovered_singular_matrix_still_errors(
    tmp_path, monkeypatch
):
    # A genuinely unrecovered singular matrix (ngspice's own abort trailer
    # present, no measurement values produced) must still classify as
    # `status: "error"` -- this fix narrows the false positive, it does not
    # remove the diagnostic.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas tran vout_meas find v(out) at=1n"}
            ],
        },
    )
    log = (
        "Warning: singular matrix:  check node b\n"
        "Warning: True gmin stepping failed\n"
        "Warning: source stepping failed\n"
        "Error: Transient op failed, timestep too small\n"
        "doAnalyses: TRAN:  Timestep too small; initial timepoint: cause unrecorded.\n"
        "tran simulation(s) aborted\n"
    )
    _stub_subprocess_run(monkeypatch, log_text=log)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert codes["singular_matrix"] == "error"


def test_run_sim_stubbed_diagnostic_counts_absent_for_clean_grid(tmp_path, monkeypatch):
    # Issue #2491: a diagnostic-free grid must still report the
    # `diagnostic_counts` field -- present-but-zero, never omitted, so a
    # caller can always read it unconditionally rather than checking for
    # its presence first.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"min": 0.0, "max": 2.0},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["diagnostic_counts"] == {
        "by_code": {},
        "by_severity": {},
        "corners_with_diagnostics": 0,
    }


def test_run_sim_stubbed_diagnostic_counts_every_corner(tmp_path, monkeypatch):
    # Issue #2491: the rollup counts diagnostics from *all* corners
    # regardless of each corner's final `status` -- here every corner in a
    # two-corner process sweep recovers from the same `singular_matrix`/
    # `nonconvergence` warning and still ends up `status: "pass"`, so the
    # rollup must show both corners contributing, not just the ones that
    # ultimately failed/errored.
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt", "ss"]},
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {
                    "name": "vout_meas",
                    "spice": ".meas tran vout_meas find v(vout_node) at=1n",
                    "limits": {"min": 0.9, "max": 1.1},
                },
                {
                    "name": "tail_meas",
                    "spice": ".meas tran tail_meas find v(tail_node) at=1n",
                    "limits": {"min": 0.4, "max": 0.6},
                },
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert report["corner_count"] == 2
    assert all(c["status"] == "pass" for c in report["corners"])
    diagnostic_counts = report["diagnostic_counts"]
    assert diagnostic_counts["by_code"]["singular_matrix"] == 2
    assert diagnostic_counts["by_code"]["nonconvergence"] == 2
    assert diagnostic_counts["by_severity"] == {"warning": 4}
    assert diagnostic_counts["corners_with_diagnostics"] == 2


def test_run_sim_keep_artifacts_writes_log(tmp_path, monkeypatch):
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    log_path = report["corners"][0]["artifacts"]["log"]
    assert log_path is not None
    assert Path(log_path).read_text() == "clean run\n"
    assert Path(log_path).is_relative_to(artifacts_dir)


def test_run_sim_keep_artifacts_exposes_deck_path(tmp_path, monkeypatch):
    """`artifacts.deck` references the exact synthesized `corner.cir` that
    `_write_corner_deck` already writes to disk (#356) -- same absolute
    path, same content, only populated when `keep_artifacts` is true."""
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    deck_path = report["corners"][0]["artifacts"]["deck"]
    assert deck_path is not None
    deck_file = Path(deck_path)
    assert deck_file.is_relative_to(artifacts_dir)
    assert deck_file.name == "corner.cir"
    # `_write_corner_deck` synthesizes this file; confirm it's the real deck
    # ngspice consumed, not a stub -- the analysis card must be present.
    deck_text = deck_file.read_text()
    assert "tran 1n 1u" in deck_text


def test_run_sim_bundle_process_corner_writes_one_lib_per_section(
    tmp_path, monkeypatch
):
    """End-to-end (stubbed ngspice): a `corners.process` bundle entry flows
    from the request through `_expand_corners` and `_write_corner_deck`
    into the actual generated deck on disk, gf180mcu-style (one `.lib` card
    per device-family section, in declaration order)."""
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {
                "process": [
                    "tt",
                    {
                        "name": "ss",
                        "sections": ["ss", "bjt_ss", "diode_ss"],
                    },
                ]
            },
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    assert report["status"] == "not_checked"
    by_process = {c["process"]: c for c in report["corners"]}
    assert set(by_process) == {"tt", "ss"}

    models_lib = tmp_path / "corner.lib"

    tt_log = Path(by_process["tt"]["artifacts"]["log"])
    tt_deck = (tt_log.parent / "corner.cir").read_text().splitlines()
    assert [line for line in tt_deck if line.startswith(".lib")] == [
        f".lib {models_lib} tt"
    ]

    ss_log = Path(by_process["ss"]["artifacts"]["log"])
    ss_deck = (ss_log.parent / "corner.cir").read_text().splitlines()
    assert [line for line in ss_deck if line.startswith(".lib")] == [
        f".lib {models_lib} ss",
        f".lib {models_lib} bjt_ss",
        f".lib {models_lib} diode_ss",
    ]


def test_run_sim_without_keep_artifacts_cleans_up(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request))

    assert report["corners"][0]["artifacts"] == {
        "log": None,
        "raw": None,
        "waveform": None,
        "deck": None,
    }


# --------------------------------------------------------------------------- #
# remote backend (#265) -- provisioning/transport are always faked; no test
# in this section ever touches a real AWS API, `ssh`/`scp` binary, or network
# socket (see remote_launcher/remote_transport's own test suites for their
# unit coverage of that plumbing in isolation).
# --------------------------------------------------------------------------- #


class _FakeRemoteLauncher:
    """Drop-in stand-in for ``remote_launcher.RemoteLauncher``: no AWS call,
    records constructor kwargs, tracks whether teardown (``__exit__``) ran."""

    #: Populated by the most recently constructed instance -- tests read this
    #: after ``run_sim`` returns (the launcher instance itself isn't
    #: reachable from the caller).
    last_instance: _FakeRemoteLauncher | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.job_id = kwargs.get("job_id", "fake-job-id")
        self.terminated = False
        self.provision_error: Exception | None = None
        type(self).last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.terminated = True
        return False

    def provision(self):
        if self.provision_error is not None:
            raise self.provision_error
        return {
            "provider": "aws",
            "region": self.kwargs["region"],
            "instance_type": "c7i.xlarge",
            "instance_id": "i-fake123",
            "spot": True,
            "estimated_hourly_cost_usd": 0.1785,
            "ami_id": "ami-fake",
            "pdk_snapshot": "sky130A-2026.06.01",
            "spin_up_s": 1.2,
        }

    def get_public_ip(self):
        return "203.0.113.9"


def _base_remote_request(**overrides) -> dict:
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "models": {"pdk": "sky130A"},
        "backend": "remote",
        "remote": {
            "region": "us-east-1",
            "key_name": "fake-key",
            "ssh_key_path": "/fake/key.pem",
            "launcher_cidr": "203.0.113.4/32",
        },
    }
    request.update(overrides)
    return request


def _install_fake_remote_transport(monkeypatch, *, remote_report_factory=None):
    """Patch every ``sim.remote_transport``/``sim.RemoteLauncher`` call site
    ``_run_remote`` uses with an in-memory fake, and return the shared
    ``state`` dict the fakes record their calls into."""
    state: dict = {"push_job_calls": 0, "pull_calls": 0, "cleanup_calls": 0}

    monkeypatch.setattr(sim, "RemoteLauncher", _FakeRemoteLauncher)
    monkeypatch.setattr(sim.remote_transport, "wait_for_ssh", lambda *a, **k: 0.5)

    def fake_push_job(*, host, remote_job_dir, job, **kwargs):
        state["push_job_calls"] += 1
        state["host"] = host
        state["remote_job_dir"] = remote_job_dir
        state["job"] = job
        # Recover the two `_build_remote_job_description` inputs by their
        # `label` (see `sim._build_remote_job_description`) -- mirrors what
        # a real `push_job` call would have uploaded.
        for item in job.inputs:
            if item.label == "netlist":
                state["local_netlist_path"] = item.local_path
            elif item.label == "request":
                state["remote_request"] = json.loads(item.content)

    def fake_run_remote_job(*, host, remote_job_dir, job, timeout_s, **kwargs):
        state["run_remote_job_timeout_s"] = timeout_s
        if remote_report_factory is not None:
            return remote_report_factory(state)
        return {
            "schema_version": 1,
            "status": "pass",
            "corner_count": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
            "environment": {"engine": "ngspice", "engine_version": "46"},
            "measurements": [],
            "corners": [],
        }

    def fake_pull_artifacts(**kwargs):
        state["pull_calls"] += 1
        state["pull_kwargs"] = kwargs

    def fake_cleanup_job(**kwargs):
        state["cleanup_calls"] += 1

    monkeypatch.setattr(sim.remote_transport, "push_job", fake_push_job)
    monkeypatch.setattr(sim.remote_transport, "run_remote_job", fake_run_remote_job)
    monkeypatch.setattr(sim.remote_transport, "pull_artifacts", fake_pull_artifacts)
    monkeypatch.setattr(sim.remote_transport, "cleanup_job", fake_cleanup_job)
    return state


def test_run_sim_remote_backend_requires_models_pdk(tmp_path):
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request(models={}))
    with pytest.raises(sim.SimError, match="requires request.models.pdk"):
        sim.run_sim(str(request))


def test_run_sim_remote_backend_requires_ssh_key_path(tmp_path):
    _write_body(tmp_path)
    bad = _base_remote_request()
    bad["remote"] = {**bad["remote"]}
    del bad["remote"]["ssh_key_path"]
    request = _write_request(tmp_path, bad)
    with pytest.raises(sim.SimError, match="requires request.remote.ssh_key_path"):
        sim.run_sim(str(request))


def test_run_sim_remote_backend_plumbs_ami_manifest_field_to_launcher(
    tmp_path, monkeypatch
):
    # request.remote.ami_manifest (issue #370) must reach
    # RemoteLauncher(manifest_path=...) unchanged -- the explicit-override
    # tier of remote_launcher's AMI manifest resolution order.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        _base_remote_request(
            remote={
                **_base_remote_request()["remote"],
                "ami_manifest": "/opt/klt/my-manifest.json",
            }
        ),
    )
    _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    assert (
        _FakeRemoteLauncher.last_instance.kwargs["manifest_path"]
        == "/opt/klt/my-manifest.json"
    )


def test_run_sim_remote_backend_omits_ami_manifest_defers_to_launcher_resolution(
    tmp_path, monkeypatch
):
    # No request.remote.ami_manifest -- manifest_path passed through as
    # None, so RemoteLauncher/load_ami_manifest's own $KLT_AMI_MANIFEST ->
    # user-scope -> packaged-default fallback chain applies.
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request())
    _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    assert _FakeRemoteLauncher.last_instance.kwargs["manifest_path"] is None


def test_run_sim_remote_backend_populates_environment_remote_block(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request())
    _install_fake_remote_transport(monkeypatch)

    report = sim.run_sim(str(request))

    remote_env = report["environment"]["remote"]
    assert remote_env["provider"] == "aws"
    assert remote_env["region"] == "us-east-1"
    assert remote_env["instance_type"] == "c7i.xlarge"
    assert remote_env["instance_id"] == "i-fake123"
    assert remote_env["spot"] is True
    assert remote_env["ami_id"] == "ami-fake"
    assert remote_env["pdk_snapshot"] == "sky130A-2026.06.01"
    assert isinstance(remote_env["estimated_hourly_cost_usd"], float)
    assert isinstance(remote_env["spin_up_s"], float)
    # engine_version comes from the remote report, not the local process.
    assert report["environment"]["engine_version"] == "46"


def test_run_sim_remote_backend_pushes_local_parallel_request(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path, _base_remote_request(corners={"temperature_c": [10, 40]})
    )
    state = _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    pushed = state["remote_request"]
    assert pushed["backend"] == "local-parallel"
    assert pushed["netlist"] == remote_transport.REMOTE_NETLIST_FILENAME
    assert "remote" not in pushed
    assert pushed["corners"] == {"temperature_c": [10, 40]}
    assert state["local_netlist_path"].endswith("body.spice")
    assert state["push_job_calls"] == 1
    assert state["cleanup_calls"] == 1


def test_run_sim_remote_backend_stages_the_netlists_include_closure(
    tmp_path, monkeypatch
):
    """Issue #2485: a testbench that `.include`s a separate DUT file (`klt
    pex`'s whole testbench contract) must arrive off-host *with* that file.
    Before this, only the netlist itself was pushed and the include resolved
    against the executing host -- silently, as `unavailable_measurement`
    rows."""
    (tmp_path / "dut.spice").write_text(".subckt dut a b\nR1 a b 1k\n.ends\n")
    (tmp_path / "body.spice").write_text(
        '.param vdd=1.0\n.include "dut.spice"\nVdd vdd 0 DC {vdd}\nXd vdd 0 dut\n'
    )
    request = _write_request(
        tmp_path, _base_remote_request(corners={"temperature_c": [10, 40]})
    )
    state = _install_fake_remote_transport(monkeypatch)

    sim.run_sim(str(request))

    job = state["job"]
    by_name = {item.remote_name: item for item in job.inputs}
    # netlist.cir + request.json (as before) + the staged DUT.
    assert set(by_name) == {"netlist.cir", "request.json", "dut.spice"}
    assert by_name["dut.spice"].local_path == str(tmp_path / "dut.spice")
    # The pushed netlist points at the staged, job-relative name -- never at
    # a path that only exists on the submitting host.
    pushed_netlist = by_name["netlist.cir"].content
    assert '.include "dut.spice"' in pushed_netlist
    assert str(tmp_path) not in pushed_netlist


def test_run_sim_remote_backend_refuses_an_unresolvable_include(tmp_path, monkeypatch):
    """The other half of #2485: an include that resolves nowhere on this
    host fails the submit with a named error instead of shipping a deck that
    cannot run."""
    (tmp_path / "body.spice").write_text('.include "no-such-dut.spice"\nR1 a b 1k\n')
    request = _write_request(tmp_path, _base_remote_request())
    state = _install_fake_remote_transport(monkeypatch)

    _FakeRemoteLauncher.last_instance = None

    with pytest.raises(sim.SimError, match="no-such-dut.spice"):
        sim.run_sim(str(request))

    # Refused before anything was pushed -- and before anything billable was
    # provisioned (`_preflight_include_closure`).
    assert state["push_job_calls"] == 0
    assert _FakeRemoteLauncher.last_instance is None


def test_run_sim_remote_backend_provisioning_failure_raises_simerror_and_tears_down(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(tmp_path, _base_remote_request())
    _install_fake_remote_transport(monkeypatch)

    original_init = _FakeRemoteLauncher.__init__

    def failing_init(self, **kwargs):
        original_init(self, **kwargs)
        self.provision_error = rl.RemoteLaunchError("estimated cost exceeds ceiling")

    monkeypatch.setattr(_FakeRemoteLauncher, "__init__", failing_init)

    with pytest.raises(sim.SimError, match="remote backend failed"):
        sim.run_sim(str(request))

    assert _FakeRemoteLauncher.last_instance.terminated is True


def test_run_sim_remote_backend_keep_artifacts_pulls_and_rewrites_paths(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path, _base_remote_request(options={"keep_artifacts": True})
    )

    def remote_report_factory(state):
        remote_root = remote_transport.artifacts_root(state["remote_job_dir"])
        return {
            "schema_version": 1,
            "status": "pass",
            "corner_count": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
            "environment": {"engine": "ngspice", "engine_version": "46"},
            "measurements": [],
            "corners": [
                {
                    "corner_id": "default/novdd/27C",
                    "status": "pass",
                    "runtime_s": 0.1,
                    "measurements": [],
                    "diagnostics": [],
                    "artifacts": {
                        "log": f"{remote_root}/default_novdd_27C/ngspice.log",
                        "raw": None,
                        "waveform": None,
                        "deck": f"{remote_root}/default_novdd_27C/corner.cir",
                    },
                }
            ],
        }

    state = _install_fake_remote_transport(
        monkeypatch, remote_report_factory=remote_report_factory
    )

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    assert state["pull_calls"] == 1
    assert state["pull_kwargs"]["local_artifacts_dir"] == str(artifacts_dir)
    log_path = report["corners"][0]["artifacts"]["log"]
    assert log_path == str(artifacts_dir / "default_novdd_27C" / "ngspice.log")
    deck_path = report["corners"][0]["artifacts"]["deck"]
    assert deck_path == str(artifacts_dir / "default_novdd_27C" / "corner.cir")


def _corner_measurement_summary(report: dict) -> list[dict]:
    """Extract only the values a `remote` run is guaranteed to reproduce
    bit-for-bit against an equivalent `local` run (decision 5) -- corner id,
    status, and each measurement's value/status/margin. Excludes
    `runtime_s`/`artifacts` (paths/timing legitimately differ by backend)."""
    return [
        {
            "corner_id": c["corner_id"],
            "status": c["status"],
            "measurements": [
                {
                    "name": m["name"],
                    "value": m["value"],
                    "status": m["status"],
                    "margin": m["margin"],
                }
                for m in c["measurements"]
            ],
        }
        for c in report["corners"]
    ]


def test_run_sim_remote_backend_measurements_are_value_identical_to_local(
    tmp_path, monkeypatch
):
    # Acceptance criterion: "A `remote` run against the same netlist/models
    # as a `local` run produces value-identical `.meas` measurements". The
    # remote transport's `run_remote_job` is faked to actually invoke
    # `sim.run_sim(..., backend="local-parallel")` locally against the
    # pushed request+netlist -- exactly what a real remote host would do by
    # running `klt sim ... --backend local-parallel` -- so this exercises
    # the real `_run_local_parallel`/`_run_corner` code path twice (once as
    # `local`, once "as if remote") against the identical stubbed ngspice
    # subprocess, proving the two backends are the same code, not two
    # implementations that happen to agree.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 40]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    local_request = _write_request(tmp_path, base, name="local.json")
    local_report = sim.run_sim(str(local_request), backend="local")

    remote_request = _write_request(
        tmp_path,
        _base_remote_request(**base, models={"pdk": "sky130A"}),
        name="remote.json",
    )

    remote_side_dir = tmp_path / "remote_side"
    remote_side_dir.mkdir()

    def remote_report_factory(state):
        netlist_dst = remote_side_dir / remote_transport.REMOTE_NETLIST_FILENAME
        netlist_dst.write_text(Path(state["local_netlist_path"]).read_text())
        request_dst = remote_side_dir / remote_transport.REMOTE_REQUEST_FILENAME
        request_dst.write_text(json.dumps(state["remote_request"]))
        return sim.run_sim(str(request_dst))

    _install_fake_remote_transport(
        monkeypatch, remote_report_factory=remote_report_factory
    )

    remote_report = sim.run_sim(str(remote_request))

    assert _corner_measurement_summary(local_report) == _corner_measurement_summary(
        remote_report
    )
    assert (
        local_report["environment"]["engine_version"]
        == remote_report["environment"]["engine_version"]
    )


# --------------------------------------------------------------------------- #
# remote fleet dispatch (`hosts > 1` + backend `remote`, Epic #375 Phase 1B
# wiring, issue #906). `remote_fleet.run_fleet`'s own K-instance launch,
# fleet-level cost gate, vCPU quota pre-check, one-shard retry, and
# teardown-all are `remote_fleet`'s own test suite's job
# (tests/test_remote_fleet.py) -- these tests fake `run_fleet` itself and
# check only `_run_remote_fleet`'s own integration seam: shard slicing,
# `explicit_points` wiring (so a shard's pushed request runs exactly its own
# slice, never re-deriving/re-seeding), and per-shard result merging.
# --------------------------------------------------------------------------- #


def _fake_run_fleet_success(monkeypatch):
    """Fake `sim.remote_fleet.run_fleet`: actually calls the given
    `shard_runner` once per shard (sequentially, each against its own
    `_FakeRemoteLauncher`), so `_run_remote_dispatch`'s push/run/pull
    sequence runs for real per shard through the same faked
    `remote_transport.*` calls `_install_fake_remote_transport` installs.
    Every shard succeeds -- see `_fake_run_fleet_with_shard_statuses` for a
    lost-shard variant. Returns the captured `run_fleet(...)` call kwargs."""
    calls: dict = {}

    def fake_run_fleet(*, region, pdk, shard_unit_counts, shard_runner, **kwargs):
        calls["region"] = region
        calls["pdk"] = pdk
        calls["shard_unit_counts"] = list(shard_unit_counts)
        calls["kwargs"] = kwargs
        outcomes = []
        for shard_index, _count in enumerate(shard_unit_counts):
            launcher = _FakeRemoteLauncher(
                job_id=f"fleet-shard{shard_index}", region=region
            )
            info = launcher.provision()
            result = shard_runner(shard_index, launcher, launcher.get_public_ip())
            outcomes.append(
                sim.remote_fleet.ShardOutcome(
                    shard_index=shard_index,
                    status="ok",
                    result=result,
                    attempts=1,
                    environment=info,
                )
            )
        return sim.remote_fleet.FleetLaunchResult(
            shards=outcomes, hosts_launched=len(shard_unit_counts)
        )

    monkeypatch.setattr(sim.remote_fleet, "run_fleet", fake_run_fleet)
    return calls


def _fake_run_fleet_with_lost_shard(monkeypatch, *, lost_shard_index: int):
    """Fake `sim.remote_fleet.run_fleet` where ``lost_shard_index`` reports
    `status="error"` (both attempts exhausted) without calling
    ``shard_runner`` at all -- mirrors what a real fleet member whose retry
    also failed hands back to `remote_fleet.FleetLauncher.run_shards`."""

    def fake_run_fleet(*, region, shard_unit_counts, shard_runner, **kwargs):
        del kwargs
        outcomes = []
        for shard_index, _count in enumerate(shard_unit_counts):
            if shard_index == lost_shard_index:
                outcomes.append(
                    sim.remote_fleet.ShardOutcome(
                        shard_index=shard_index,
                        status="error",
                        error="ssh timeout (both attempts)",
                        attempts=2,
                        environment=None,
                    )
                )
                continue
            launcher = _FakeRemoteLauncher(
                job_id=f"fleet-shard{shard_index}", region=region
            )
            info = launcher.provision()
            result = shard_runner(shard_index, launcher, launcher.get_public_ip())
            outcomes.append(
                sim.remote_fleet.ShardOutcome(
                    shard_index=shard_index,
                    status="ok",
                    result=result,
                    attempts=1,
                    environment=info,
                )
            )
        return sim.remote_fleet.FleetLaunchResult(
            shards=outcomes, hosts_launched=len(shard_unit_counts) - 1
        )

    monkeypatch.setattr(sim.remote_fleet, "run_fleet", fake_run_fleet)


def _remote_fleet_base_request(**overrides) -> dict:
    request = _base_remote_request(
        corners={"temperature_c": [10, 20, 30, 40]},
        measurements=[
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    )
    request.update(overrides)
    return request


def _make_shard_remote_report_factory(remote_side_dir: Path):
    """Build a `remote_report_factory` (for `_install_fake_remote_transport`)
    that actually runs `sim.run_sim` against each shard's own pushed netlist
    + request -- exactly like a real fleet member running `klt sim ...
    --backend local-parallel`. Each call gets its own subdirectory so
    concurrent/sequential shard dispatches never collide on disk."""
    counter = {"n": 0}

    def factory(state):
        counter["n"] += 1
        shard_dir = remote_side_dir / f"shard{counter['n']}"
        shard_dir.mkdir()
        netlist_dst = shard_dir / remote_transport.REMOTE_NETLIST_FILENAME
        netlist_dst.write_text(Path(state["local_netlist_path"]).read_text())
        request_dst = shard_dir / remote_transport.REMOTE_REQUEST_FILENAME
        request_dst.write_text(json.dumps(state["remote_request"]))
        return sim.run_sim(str(request_dst))

    return factory


def test_run_sim_remote_fleet_requires_region(tmp_path):
    _write_body(tmp_path)
    bad = _remote_fleet_base_request()
    bad["remote"] = {**bad["remote"], "hosts": 2}
    del bad["remote"]["region"]
    request = _write_request(tmp_path, bad)
    with pytest.raises(sim.SimError, match="requires request.remote.region"):
        sim.run_sim(str(request))


def test_run_sim_remote_fleet_requires_ssh_key_path(tmp_path):
    _write_body(tmp_path)
    bad = _remote_fleet_base_request()
    bad["remote"] = {**bad["remote"], "hosts": 2}
    del bad["remote"]["ssh_key_path"]
    request = _write_request(tmp_path, bad)
    with pytest.raises(sim.SimError, match="requires request.remote.ssh_key_path"):
        sim.run_sim(str(request))


def test_run_sim_remote_fleet_shards_dispatch_and_merge_in_unit_order(
    tmp_path, monkeypatch
):
    """`hosts > 1` + backend `remote` (issue #906) shards the corner list,
    dispatches each shard to its own (faked) EC2 instance via
    `remote_fleet.run_fleet`, and merges the results back in global unit
    order -- value-identical to the same request run unsharded, since each
    shard's pushed request carries its own already-expanded `_explicit_points`
    slice (never re-derived on the remote box from `corners` ranges)."""
    _write_body(tmp_path)
    base = _remote_fleet_base_request()

    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    local_request = _write_request(
        tmp_path,
        {k: v for k, v in base.items() if k not in ("backend", "remote")},
        name="local.json",
    )
    local_report = sim.run_sim(str(local_request), backend="local")

    remote_request = _write_request(tmp_path, base, name="remote.json")

    remote_side_dir = tmp_path / "remote_side"
    remote_side_dir.mkdir()
    _install_fake_remote_transport(
        monkeypatch,
        remote_report_factory=_make_shard_remote_report_factory(remote_side_dir),
    )
    fleet_calls = _fake_run_fleet_success(monkeypatch)

    remote_report = sim.run_sim(str(remote_request), hosts=2)

    assert fleet_calls["shard_unit_counts"] == [2, 2]
    assert fleet_calls["pdk"] == "sky130A"
    assert fleet_calls["region"] == "us-east-1"
    assert [c["corner_id"] for c in remote_report["corners"]] == [
        c["corner_id"] for c in local_report["corners"]
    ]
    assert _corner_measurement_summary(local_report) == _corner_measurement_summary(
        remote_report
    )
    assert len(remote_report["environment"]["remote"]["fleet"]) == 2
    for member in remote_report["environment"]["remote"]["fleet"]:
        assert member["instance_id"] == "i-fake123"
        assert member["attempts"] == 1


def test_run_sim_remote_fleet_shard_resolves_models_lib_for_process_corner(
    tmp_path, monkeypatch
):
    """Regression test: a fleet shard's pushed request carries
    ``_explicit_points`` instead of ``corners`` (`_build_remote_request` pops
    `corners` entirely), so `run_sim`'s `models_lib` gate must key off the
    *dispatched* `corner_points` rather than the (now-empty) `corners_spec`
    on the remote box -- otherwise every corner deck on every shard gets a
    literal `.lib None <process>` line instead of a real model library
    include whenever the campaign sweeps `corners.process` (the flagship
    fleet use case)."""
    _write_body(tmp_path)
    lib_path = _write_corner_lib(tmp_path)
    # This is a mocked transport/engine test: resolve its declared PDK from
    # a minimal local fixture, never from an installation on the test host.
    install_root = tmp_path / "pdk"
    (install_root / "sky130A" / "libs.tech" / "ngspice").mkdir(parents=True)
    (install_root / "sky130A" / "libs.tech" / "klayout").mkdir()
    base = _remote_fleet_base_request(
        corners={"process": ["tt", "ss"]},
        models={
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": str(lib_path),
        },
        options={"keep_artifacts": True},
    )

    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    remote_request = _write_request(tmp_path, base, name="remote.json")

    remote_side_dir = tmp_path / "remote_side"
    remote_side_dir.mkdir()
    _install_fake_remote_transport(
        monkeypatch,
        remote_report_factory=_make_shard_remote_report_factory(remote_side_dir),
    )
    _fake_run_fleet_success(monkeypatch)

    remote_report = sim.run_sim(str(remote_request), hosts=2)

    assert remote_report["status"] == "pass"
    deck_paths = sorted(remote_side_dir.rglob("corner.cir"))
    # Two process points across two shards -- one deck per dispatched corner.
    assert len(deck_paths) == 2
    for deck_path in deck_paths:
        lib_lines = [
            line
            for line in deck_path.read_text().splitlines()
            if line.startswith(".lib")
        ]
        assert lib_lines, f"{deck_path} has no .lib line"
        for line in lib_lines:
            assert "None" not in line, (
                f"{deck_path} emitted a broken .lib line: {line!r}"
            )
            assert line.startswith(f".lib {lib_path} ")


def test_run_sim_remote_fleet_preserves_mc_seeds_vs_unsharded_local_run(
    tmp_path, monkeypatch
):
    """Sharding across a fleet must never change a single unit's derived
    Monte Carlo seed -- `_explicit_points` transmits each point's own
    already-derived seed verbatim rather than letting a shard's own remote
    box re-run `_expand_monte_carlo` (which would index samples/corners
    relative to that shard alone, not the unsharded request -- issue #906's
    whole reason for `_explicit_points` instead of forwarding `corners`/
    `monte_carlo` verbatim)."""
    _write_body(tmp_path)
    base = _remote_fleet_base_request(
        corners={"temperature_c": [10, 20]},
        monte_carlo={"n": 2, "seed": 20260812, "vary": "mismatch"},
    )

    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    local_request = _write_request(
        tmp_path,
        {k: v for k, v in base.items() if k not in ("backend", "remote")},
        name="local.json",
    )
    local_report = sim.run_sim(str(local_request), backend="local")

    remote_request = _write_request(tmp_path, base, name="remote.json")

    remote_side_dir = tmp_path / "remote_side"
    remote_side_dir.mkdir()
    _install_fake_remote_transport(
        monkeypatch,
        remote_report_factory=_make_shard_remote_report_factory(remote_side_dir),
    )
    _fake_run_fleet_success(monkeypatch)

    # 4 units (2 corners x 2 MC samples), 2 hosts -> 2 units/shard.
    remote_report = sim.run_sim(str(remote_request), hosts=2)

    local_seeds = [(c["corner_id"], c["monte_carlo"]) for c in local_report["corners"]]
    remote_seeds = [
        (c["corner_id"], c["monte_carlo"]) for c in remote_report["corners"]
    ]
    assert local_seeds == remote_seeds


def test_run_sim_remote_fleet_lost_shard_reports_error_without_raising(
    tmp_path, monkeypatch
):
    """A shard whose fleet dispatch never succeeds (both the initial attempt
    and its one automatic retry failed) is reported as `lost_shard`
    `status: "error"` corners for that shard's own units -- mirroring
    `_run_sharded`'s local-backend lost-shard handling -- never raised, and
    never prevents the other shard's units from being reported."""
    _write_body(tmp_path)
    base = _remote_fleet_base_request()

    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    remote_request = _write_request(tmp_path, base, name="remote.json")

    remote_side_dir = tmp_path / "remote_side"
    remote_side_dir.mkdir()
    _install_fake_remote_transport(
        monkeypatch,
        remote_report_factory=_make_shard_remote_report_factory(remote_side_dir),
    )
    _fake_run_fleet_with_lost_shard(monkeypatch, lost_shard_index=1)

    remote_report = sim.run_sim(str(remote_request), hosts=2)

    assert remote_report["corner_count"] == 4
    lost = [
        c
        for c in remote_report["corners"]
        if any(d["code"] == "lost_shard" for d in c["diagnostics"])
    ]
    assert len(lost) == 2
    assert all(c["status"] == "error" for c in lost)
    fleet = remote_report["environment"]["remote"]["fleet"]
    assert fleet[0] is not None  # shard 0 dispatched fine
    assert fleet[1] is None  # shard 1 is the lost one


def test_run_sim_remote_fleet_over_units_raises_before_run_fleet(tmp_path, monkeypatch):
    _write_body(tmp_path)
    base = _remote_fleet_base_request(corners={"temperature_c": [10, 20]})
    request = _write_request(tmp_path, base)

    def _unexpected(*args, **kwargs):
        raise AssertionError("run_fleet must not be called when hosts > units")

    monkeypatch.setattr(sim.remote_fleet, "run_fleet", _unexpected)

    with pytest.raises(sim.SimError, match="exceeds the number of units"):
        sim.run_sim(str(request), hosts=3)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_stubbed_json_contract(tmp_path, monkeypatch, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    exit_code = main(["sim", str(request), "--format", "json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {
        "schema_version",
        "netlist",
        "status",
        "corner_count",
        "passed",
        "failed",
        "errored",
        # Issues #2492 + #2493: the single parallel count for corners graded
        # `inconclusive` (by `options.fail_on_diagnostic` or by a
        # plausibility bound) -- always present, purely additive, `0` for a
        # request that opts into neither.
        "inconclusive",
        # Issue #2491: rollup of `corners[].diagnostics[]` across the whole
        # grid -- always present, purely additive.
        "diagnostic_counts",
        "metrics",
        "environment",
        # Issue #1996: the shared vacuous-verdict convention
        # (`klayout_tools.coverage`) -- always present, purely additive.
        "coverage",
        "provenance",
        "measurements",
        "corners",
    }
    assert set(data["coverage"].keys()) == {
        "schema_version",
        "known",
        "checked",
        "skipped",
        "inapplicable",
        "unknown",
        "corners_simulated",
        "measurements_declared",
        "measurements_with_limits",
        "unrecognized_limit_keys",
        "nothing_checked",
        "nothing_checked_reasons",
    }
    assert data["coverage"]["corners_simulated"] == data["corner_count"]
    assert isinstance(data["coverage"]["nothing_checked"], bool)
    # Issue #1849: `metrics` re-keys the corner_count/passed/failed/errored
    # rollup under its declared METRICS2.1-style names, additive alongside
    # the existing fields.
    assert data["metrics"] == {
        "sim__corner__count": data["corner_count"],
        "sim__corner__passed_count": data["passed"],
        "sim__corner__failed_count": data["failed"],
        "sim__corner__errored_count": data["errored"],
        "sim__corner__inconclusive_count": data["inconclusive"],
    }
    prov = data["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert isinstance(prov["klt_version"], str)
    # This request declares no process axis / model library, so no model
    # deck or PDK is resolved.
    assert prov["pdk"] is None
    assert prov["deck"] is None
    # Issue #2039: `input` is always populated once a netlist resolved --
    # every `klt sim` run has one, unlike `deck`/`pdk` which depend on a
    # process axis being declared.
    assert prov["input"]["role"] == "netlist"
    assert prov["input"]["content_hash"].startswith("sha256:")


def test_cli_default_format_is_text(tmp_path, monkeypatch, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    exit_code = main(["sim", str(request)])

    assert exit_code == 4
    out = capsys.readouterr().out
    assert "netlist:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_exit_code_measurement_failed(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.1},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    assert main(["sim", str(request), "--format", "json"]) == 3


def test_cli_exit_code_corner_errored(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    assert main(["sim", str(request), "--format", "json"]) == 4


def test_cli_unresolvable_request_error_envelope(tmp_path, capsys):
    exit_code = main(["sim", str(tmp_path / "nope.json"), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "sim"
    assert "not found" in error["error"]["message"]


def test_cli_outdir_flag_overrides_default(tmp_path, monkeypatch):
    _write_body(tmp_path)
    outdir = tmp_path / "custom-out"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    main(["sim", str(request), "--outdir", str(outdir), "--format", "json"])

    assert outdir.is_dir()
    assert any(outdir.rglob("ngspice.log"))


# --------------------------------------------------------------------------- #
# Integration: real ngspice (skipped when not installed)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_save_all_overrides_netlist_bodys_restrictive_save_card(tmp_path):
    """Issue #2521's manual verification step, automated: a netlist body
    that carries its own restrictive `.save` card (a schematic netlister's
    supply-current probe convention) used to make every measurement outside
    that card's list silently return no value. `_write_corner_deck`'s own
    `save all` -- emitted after `.include`, so it supersedes the body's
    `.save` -- restores the full saved set, and the measurement on the
    excluded node now resolves."""
    body = tmp_path / "body.spice"
    body.write_text(
        "Vin in 0 DC 1\n"
        "R1 in mid 1k\n"
        "R2 mid out 1k\n"
        "R3 out 0 1k\n"
        "* Restrictive: only `v(in)` survives without this fix's `save all`.\n"
        ".save v(in)\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "dc", "args": "Vin 0 1 0.1"},
            "measurements": [
                {
                    "name": "vmid",
                    "spice": ".meas dc vmid FIND v(mid) AT=0.5",
                    "unit": "V",
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    (corner,) = report["corners"]
    assert corner["diagnostics"] == []
    (measurement,) = corner["measurements"]
    assert measurement["status"] == "pass"
    assert measurement["value"] == pytest.approx(1.0 / 3.0, abs=1e-3)


@_SKIP_NO_NGSPICE
def test_integration_process_corner_selects_lib_section(tmp_path):
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt", "ss"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    by_process = {
        c["process"]: c["measurements"][0]["value"] for c in report["corners"]
    }
    # tt: corner_scale isn't referenced by this body -- both corners just
    # confirm .lib section selection didn't error; values are equal here.
    assert set(by_process) == {"tt", "ss"}


@_SKIP_NO_NGSPICE
def test_integration_monte_carlo_seed_reaches_ngspice_and_is_reproducible(tmp_path):
    """End-to-end confirmation of the seed contract against real ngspice
    (the test plan's "Manual verification" step, automated here since
    ngspice happens to be available): `.options seed=` (written from each
    sample's derived `rndseed`, see `_write_corner_deck`) actually reaches
    ngspice's `AGAUSS` random-function seed -- distinct samples draw
    distinct values, and re-running the identical request reproduces the
    exact same sequence of measured values, not just the exact same
    sequence of derived seed metadata (already covered by the stubbed
    tests above)."""
    body = tmp_path / "body.spice"
    body.write_text(
        "* AGAUSS(0,1,1) is seeded by klt sim's `.options seed=` card -- a\n"
        "* stand-in for a mismatch-aware PDK model's own behavioral variation.\n"
        ".param vdd=1.0\n"
        "Vdd vdd 0 DC {vdd}\n"
        ".param mc_rand = {AGAUSS(0,1,1)}\n"
        "R1 vdd out {1k*(1+0.01*mc_rand)}\n"
        "R2 out 0 1k\n"
        "C1 out 0 1n\n"
    )
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "monte_carlo": {"n": 4, "seed": 20260801, "vary": "mismatch"},
            "analysis": {"kind": "tran", "args": "1n 5u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=5u"}
            ],
        },
    )

    report_a = sim.run_sim(str(request), backend="local-parallel")
    report_b = sim.run_sim(str(request), backend="local-parallel")

    assert report_a["status"] == "pass"
    assert report_a["corner_count"] == 4

    def values_by_sample(report):
        return {
            c["monte_carlo"]["sample_index"]: c["measurements"][0]["value"]
            for c in report["corners"]
        }

    values_a = values_by_sample(report_a)
    values_b = values_by_sample(report_b)
    # Reproducible: identical request -> identical measured sequence.
    assert values_a == pytest.approx(values_b)
    # Not a no-op sampler: the seed actually varies AGAUSS's draw.
    assert len({round(v, 9) for v in values_a.values()}) > 1


@_SKIP_NO_NGSPICE
def test_integration_supply_and_temperature_sweep(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {
                "supply_v": {"vdd": [1.0, 2.0]},
                "temperature_c": [27, 125],
            },
            "analysis": {"kind": "tran", "args": "1n 5u"},
            "measurements": [
                {
                    "name": "vout_final",
                    "spice": ".meas tran vout_final FIND v(out) AT=5u",
                    "unit": "V",
                    "limits": {"min": 0.5, "max": 2.5},
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["corner_count"] == 4
    values = sorted(c["measurements"][0]["value"] for c in report["corners"])
    assert values == pytest.approx([1.0, 1.0, 2.0, 2.0])


@_SKIP_NO_NGSPICE
def test_integration_timeout_is_killed_and_classified(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 60},
        },
    )
    # Force a real timeout deterministically -- ngspice's own process-startup
    # overhead (tens of milliseconds, per the stubbed tests' runtime_s
    # values) reliably exceeds an absurdly small budget, without needing to
    # construct an actual nonconvergent hang.
    request_data = json.loads(request.read_text())
    request_data["options"]["timeout_s"] = 0.001
    request.write_text(json.dumps(request_data))

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    assert any(d["code"] == "timeout" for d in corner["diagnostics"])


@_SKIP_NO_NGSPICE
def test_integration_missing_measurement_value_is_error(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "never",
                    "spice": ".meas tran never FIND v(out) WHEN v(out)=99",
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] is None
    assert corner["measurements"][0]["status"] == "error"


@_SKIP_NO_NGSPICE
def test_integration_waveform_artifact(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
            "options": {"keep_artifacts": True, "waveforms": True},
        },
    )

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    waveform_path = corner["artifacts"]["waveform"]
    assert waveform_path is not None
    waveform = json.loads(Path(waveform_path).read_text())
    assert waveform["variables"][0]["name"] == "time"
    assert len(waveform["points"]) > 0


# --------------------------------------------------------------------------- #
# Waveform plots (`--plot`, issue #1723)
# --------------------------------------------------------------------------- #


def test_measurement_diagnostic_hints_extracts_signal_window_and_target():
    hints = sim._measurement_diagnostic_hints(
        ".meas tran vout_avg AVG v(out) FROM=100n TO=900n"
    )
    assert hints["signal"] == "v(out)"
    assert hints["window"] == pytest.approx((1e-7, 9e-7))
    assert hints["target"] is None


def test_measurement_diagnostic_hints_extracts_when_target():
    hints = sim._measurement_diagnostic_hints(
        ".meas tran startup_time WHEN v(out)=1.5 RISE=1"
    )
    assert hints["signal"] == "v(out)"
    assert hints["window"] is None
    assert hints["target"] == pytest.approx(1.5)


def test_measurement_diagnostic_hints_unrecognised_card_returns_all_none():
    """A `TRIG`/`TARG` delay measurement has no single window/target this
    simple parser understands, and no `v(...)`/`i(...)` token at all when
    both endpoints are parameters -- must degrade to all-`None`, never
    raise."""
    hints = sim._measurement_diagnostic_hints(".meas tran tphl PARAM='a+b'")
    assert hints == {"signal": None, "window": None, "target": None}


def test_measurement_diagnostic_hints_is_case_insensitive_and_lowercases_signal():
    hints = sim._measurement_diagnostic_hints(
        ".MEAS TRAN VOUT FIND V(OUT) WHEN V(OUT)=1"
    )
    assert hints["signal"] == "v(out)"


@pytest.mark.parametrize(
    ("corner_id", "expected"),
    [
        ("tt/1.800V/27C", "tt_1p800V_27C"),
        ("ss/1.620V/-40C", "ss_1p620V_n40C"),
        ("tt/1.800V/27C/mc3", "tt_1p800V_27C_mc3"),
    ],
)
def test_slugify_corner_id_matches_cornerpoint_slug(corner_id, expected):
    """Mirrors :attr:`sim.CornerPoint.slug`'s own transform exactly -- a
    plain corner report dict has no live `CornerPoint` to ask, so this is a
    deliberate, tested duplication of that same string transform."""
    assert sim._slugify_corner_id(corner_id) == expected


def test_slugify_signal_produces_filesystem_safe_name():
    assert sim._slugify_signal("v(out)") == "v_out"
    assert sim._slugify_signal("I(Vdd)") == "i_vdd"
    assert sim._slugify_signal("") == "signal"


def _write_ascii_rawfile(
    path: Path, *, plotname: str, sweep_name: str, signal_name: str, points
) -> None:
    lines = [
        f"Plotname: {plotname}",
        "Flags: real",
        "No. Variables: 2",
        f"No. Points: {len(points)}",
        "Variables:",
        f"\t0\t{sweep_name}\ttime",
        f"\t1\t{signal_name}\tvoltage",
        "Values:",
    ]
    for index, (x, y) in enumerate(points):
        lines.append(f" {index}\t{x!r}")
        lines.append(f"\t{y!r}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")


def _stub_subprocess_run_with_waveform(monkeypatch, *, points, log_text="clean run\n"):
    """Like :func:`_stub_subprocess_run`, but also writes a synthetic ASCII
    rawfile at the corner's own `waveform.raw` path -- the real `ngspice`
    binary is stubbed out entirely (see `_stub_subprocess_run`), so nothing
    actually executes the deck's `.control` block `write` command; this
    fakes that side effect so the plot-writing code under test has a real
    waveform artifact to read."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        corner_dir = os.path.dirname(log_path)
        raw_path = os.path.join(corner_dir, "waveform.raw")
        _write_ascii_rawfile(
            Path(raw_path),
            plotname="Transient Analysis",
            sweep_name="time",
            signal_name="v(out)",
            points=points,
        )
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_sim_plot_forces_waveforms_and_keep_artifacts_even_when_unset(
    tmp_path, monkeypatch
):
    """`--plot`/`plot_dir` must force `options.waveforms`/`keep_artifacts` on
    for this run even though the request declares neither -- a waveform
    cannot be plotted without first being captured and persisted (see
    `run_sim`'s `plot_dir` docstring)."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run_with_waveform(
        monkeypatch, points=[(0.0, 1.0), (1e-9, 1.0), (2e-9, 1.0)]
    )
    plot_dir = tmp_path / "plots"

    report = sim.run_sim(
        str(request), artifacts_dir=str(tmp_path / "artifacts"), plot_dir=str(plot_dir)
    )

    (corner,) = report["corners"]
    # keep_artifacts was forced on: the log/waveform artifacts are populated
    # even though the request never set `options.keep_artifacts`.
    assert corner["artifacts"]["log"] is not None
    assert corner["artifacts"]["waveform"] is not None


def test_run_sim_plot_writes_one_svg_per_signal_and_lists_them_in_json(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run_with_waveform(
        monkeypatch, points=[(0.0, 0.0), (1e-9, 0.5), (2e-9, 1.0)]
    )
    plot_dir = tmp_path / "plots"

    report = sim.run_sim(str(request), plot_dir=str(plot_dir))

    (corner,) = report["corners"]
    corner_plots = corner["artifacts"]["plots"]
    assert corner_plots == [{"signal": "v(out)", "path": corner_plots[0]["path"]}]
    plot_path = Path(corner_plots[0]["path"])
    assert plot_path.is_relative_to(plot_dir)
    assert plot_path.read_text().startswith("<svg")

    # Deterministic, listed at the top level too (issue #1723's "file names
    # are deterministic and listed in the JSON" acceptance criterion).
    assert report["plots"] == [
        {
            "corner_id": corner["corner_id"],
            "signal": "v(out)",
            "path": str(plot_path),
        }
    ]


def test_run_sim_plot_links_svg_next_to_a_failing_measurement(tmp_path, monkeypatch):
    """The core acceptance criterion: a deliberately non-starting (flat)
    signal's `WHEN` measurement never crosses its threshold -- the resulting
    miss report must name the SVG path next to that measurement, and the SVG
    itself must show the flat line."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "startup_time",
                    "spice": ".meas tran startup_time WHEN v(out)=1.5 RISE=1",
                }
            ],
        },
    )
    flat_points = [(0.0, 1.0), (1e-9, 1.0), (2e-9, 1.0)]
    _stub_subprocess_run_with_waveform(
        monkeypatch,
        points=flat_points,
        # ngspice's own "out of interval" failure text for a `WHEN`
        # threshold that a flat signal never crosses.
        log_text=(
            "Error: measure  startup_time  when(WHEN) : out of interval\n"
            " .meas tran startup_time when v(out)=1.5 rise=1 failed!\n"
        ),
    )
    plot_dir = tmp_path / "plots"

    report = sim.run_sim(str(request), plot_dir=str(plot_dir))

    (entry,) = report["measurements"]
    assert entry["status"] == "error"
    assert entry["plot"] is not None
    svg = Path(entry["plot"]).read_text()
    # The flat line: every y-value is 1.0, so the curve is horizontal.
    assert "v(out)" in svg
    # The WHEN target (1.5) is drawn as a dashed reference line.
    assert "target=1.5" in svg


def test_run_sim_without_plot_dir_measurements_rollup_has_no_plot_key(
    tmp_path, monkeypatch
):
    """Additive-only: a plain (non-`--plot`) run's rollup entries must keep
    their exact pre-#1723 shape (locked by
    `test_rollup_measurements_without_monte_carlo_config_is_unchanged`)."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="vout = 1.0\n")

    report = sim.run_sim(str(request))

    assert "plot" not in report["measurements"][0]
    assert "plots" not in report
    assert "plots" not in report["corners"][0]["artifacts"]


def test_run_sim_plot_ac_analysis_uses_log_axis(tmp_path, monkeypatch):
    """`analysis.kind == "ac"` selects the log-scaled frequency axis (issue
    #1723's "log axes for ac" acceptance criterion). Stubbed rather than run
    against real `ngspice` so this test exercises only the
    `log_x = analysis_kind == "ac"` wiring in isolation, independent of
    rawfile parsing; see `test_integration_plot_ac_waveform_parses_complex_rawfile`
    for the real-`ngspice` end-to-end coverage of `ac`'s complex (`real,imag`
    per value) rawfile encoding, handled by `parse_ascii_rawfile` since issue
    #1756."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "ac", "args": "dec 10 1 1meg"}},
    )
    _stub_subprocess_run_with_waveform(
        monkeypatch, points=[(1.0, 1.0), (10.0, 0.99), (100.0, 0.9)]
    )
    plot_dir = tmp_path / "plots"

    report = sim.run_sim(str(request), plot_dir=str(plot_dir))

    (corner,) = report["corners"]
    corner_plots = corner["artifacts"]["plots"]
    assert corner_plots
    for plot in corner_plots:
        svg = Path(plot["path"]).read_text()
        assert "(log)" in svg


@_SKIP_NO_NGSPICE
def test_integration_plot_ac_waveform_parses_complex_rawfile(tmp_path):
    """A real `ac` analysis's rawfile declares `Flags: complex` and encodes
    every value as a `real,imag` pair (issue #1756) -- `parse_ascii_rawfile`
    now reduces each column to its magnitude instead of raising, so
    `run_sim` produces a real waveform artifact and a real (non-empty),
    log-frequency-axis plot for this corner rather than degrading to the
    `"unknown"`-diagnostic fallback `_run_corner` uses for a genuinely
    unparseable rawfile."""
    path = tmp_path / "body.spice"
    path.write_text(".param vdd=1.0\nVin in 0 DC 0 AC 1\nR1 in out 1k\nC1 out 0 1n\n")
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "ac", "args": "dec 10 1 1meg"}},
    )
    plot_dir = tmp_path / "plots"

    report = sim.run_sim(str(request), plot_dir=str(plot_dir))  # must not raise

    (corner,) = report["corners"]
    assert not any(d["code"] == "unknown" for d in corner["diagnostics"])
    assert corner["artifacts"]["waveform"] is not None
    waveform = json.loads(Path(corner["artifacts"]["waveform"]).read_text())
    assert waveform["variables"][0]["name"] == "frequency"
    for point in waveform["points"]:
        for value in point:
            assert isinstance(value, float)

    assert report["plots"]
    for plot in report["plots"]:
        svg = Path(plot["path"]).read_text()
        assert "(log)" in svg


@_SKIP_NO_NGSPICE
def test_integration_plot_tran_shades_measurement_window(tmp_path):
    """Real `ngspice`, the issue's own worked example: a `PP` (peak-to-peak)
    measurement over a `FROM=`/`TO=` window, on a signal that never
    oscillates (a plain RC settling response) -- both the shaded window and
    the failing-measurement-links-to-SVG behavior, end to end."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "ring_amplitude",
                    "spice": (".meas tran ring_amplitude PP v(out) FROM=100n TO=900n"),
                    "limits": {"min": 0.3},
                }
            ],
        },
    )
    plot_dir = tmp_path / "plots"

    report = sim.run_sim(str(request), plot_dir=str(plot_dir))

    (entry,) = report["measurements"]
    assert entry["status"] == "fail"  # a non-oscillating signal has ~0 PP
    assert entry["plot"] is not None
    svg = Path(entry["plot"]).read_text()
    assert 'fill="#fde68a"' in svg  # the shaded measurement window


@_SKIP_NO_NGSPICE
def test_cli_sim_plot_flag_writes_svgs_and_prints_plot_path(tmp_path, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "startup_time",
                    "spice": ".meas tran startup_time WHEN v(out)=99 RISE=1",
                }
            ],
        },
    )
    plot_dir = tmp_path / "plots"

    exit_code = main(["sim", str(request), "--plot", str(plot_dir), "--format", "json"])

    assert exit_code == 4  # the measurement never crosses -- a corner error
    payload = json.loads(capsys.readouterr().out)
    assert payload["plots"]
    assert payload["measurements"][0]["plot"] is not None
    assert Path(payload["measurements"][0]["plot"]).is_file()


@_SKIP_NO_NGSPICE
def test_integration_exit_codes(tmp_path):
    _write_body(tmp_path)
    pass_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"min": 0.5},
                }
            ],
        },
        name="pass_request.json",
    )
    assert main(["sim", str(pass_request), "--format", "json"]) == 0

    fail_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.1},
                }
            ],
        },
        name="fail_request.json",
    )
    assert main(["sim", str(fail_request), "--format", "json"]) == 3

    error_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "never",
                    "spice": ".meas tran never FIND v(out) WHEN v(out)=99",
                }
            ],
        },
        name="error_request.json",
    )
    assert main(["sim", str(error_request), "--format", "json"]) == 4


# --------------------------------------------------------------------------- #
# Worked example (examples/sim/), regenerated by examples/sim/generate.py
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
@pytest.mark.skipif(
    not EXAMPLES_DIR.exists(), reason="examples/sim/ fixtures not generated"
)
def test_examples_sim_worked_example_passes():
    request_path = EXAMPLES_DIR / "request.json"
    if not request_path.exists():
        pytest.skip(
            "examples/sim/request.json not generated -- run examples/sim/generate.py"
        )

    report = sim.run_sim(str(request_path))

    assert report["schema_version"] == 3
    assert report["status"] == "pass"
    assert report["corner_count"] == 8
    assert report["measurements"][0]["name"] == "vout"


@_SKIP_NO_NGSPICE
@pytest.mark.skipif(
    not EXAMPLES_DIR.exists(), reason="examples/sim/ fixtures not generated"
)
def test_examples_sim_monte_carlo_statistics_shape():
    """The Monte Carlo worked example, run live: `testbench-mc.spice`'s
    `agauss`-drawn `R1` must actually spread under the seed `klt sim` writes,
    and the sample set must reduce to the statistics block documented in
    docs/cli/sim.md. Stands in for the byte-exact golden fixture this example
    deliberately doesn't have (`runtime_s`/`engine_version` vary)."""
    request_path = EXAMPLES_DIR / "request-monte-carlo.json"
    if not request_path.exists():
        pytest.skip(
            "examples/sim/request-monte-carlo.json not generated -- "
            "run examples/sim/generate.py"
        )

    report = sim.run_sim(str(request_path))

    assert report["status"] == "pass"
    assert report["corner_count"] == 20
    assert report["environment"]["monte_carlo"]["quantiles"] == [5.0, 50.0, 95.0]

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["n"] == 20
    assert mc["errored"] == 0
    # The seeded `agauss` draw actually varies -- not a pinned constant.
    assert mc["stddev"] > 0
    assert mc["min"] < mc["mean"] < mc["max"]
    assert mc["min"] <= mc["quantiles"]["p5"] <= mc["quantiles"]["p50"]
    assert mc["quantiles"]["p50"] <= mc["quantiles"]["p95"] <= mc["max"]
    window = mc["sigma_window"]
    assert window["k"] == 3.0
    assert window["low"] == pytest.approx(mc["mean"] - 3 * mc["stddev"])
    assert window["high"] == pytest.approx(mc["mean"] + 3 * mc["stddev"])
    assert window["status"] == "pass"
    assert [c["corner_id"] for c in mc["by_corner"]] == ["tt/novdd/27C"]


def test_pdk_module_is_the_only_resolution_path(monkeypatch, tmp_path):
    """Guard against a future regression re-introducing a hand-rolled PDK
    path lookup: `_resolve_models_lib`'s pdk-variant branch must go through
    `klayout_tools.pdk.find_pdk` (issue #45), not a private reimplementation."""
    calls = []
    original = pdk.find_pdk

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(sim, "find_pdk", spy)

    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech"
    (variant_dir / "ngspice").mkdir(parents=True)
    (variant_dir / "ngspice" / "sky130.lib.spice").write_text(".lib tt\n.endl tt\n")

    sim._resolve_models_lib(
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sky130.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Remote worked example (examples/sim-remote/) -- Epic #253's closing
# validation matrix, committed as a worked example (#373).
#
# These tests are deliberately CI-checkable *without* AWS and without a
# 12-minute ngspice run: they validate the two requests through the same
# `sim.load_request()` path `klt sim` itself uses, exercise the remote
# block's pure-local cost gate, and assert the committed reference reports'
# documented equivalence (identical corner values across backends, the
# additive `environment.remote` block only on the remote one). Actually
# *running* `matrix-remote.request.json` needs AWS credentials.
# --------------------------------------------------------------------------- #

SIM_REMOTE_EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "sim-remote"

_SKIP_NO_SIM_REMOTE_EXAMPLE = pytest.mark.skipif(
    not SIM_REMOTE_EXAMPLES_DIR.exists(),
    reason="examples/sim-remote/ is not present",
)


def _load_sim_remote_json(name: str) -> dict:
    with open(SIM_REMOTE_EXAMPLES_DIR / name, encoding="utf-8") as handle:
        return json.load(handle)


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_requests_differ_only_by_backend():
    """The example's whole point: two requests that are the *same* corner
    matrix, differing only in where it runs. `load_request` is the shipped
    validation path (`klt sim` calls it before dispatch), so this is the
    schema check the issue's acceptance criteria ask for -- no AWS needed."""
    local = sim.load_request(str(SIM_REMOTE_EXAMPLES_DIR / "matrix-local.request.json"))
    remote = sim.load_request(
        str(SIM_REMOTE_EXAMPLES_DIR / "matrix-remote.request.json")
    )

    assert local["backend"] == "local"
    assert remote["backend"] == "remote"
    assert local["backend"] in sim.SUPPORTED_BACKENDS
    assert remote["backend"] in sim.SUPPORTED_BACKENDS

    # Everything except `backend` (and the remote-only provisioning block)
    # must be byte-identical -- otherwise the comparison proves nothing.
    assert {k: v for k, v in local.items() if k != "backend"} == {
        k: v for k, v in remote.items() if k not in ("backend", "remote")
    }

    # The netlist both requests name resolves next to them.
    assert (SIM_REMOTE_EXAMPLES_DIR / local["netlist"]).is_file()

    # `remote` requires `models.pdk` (docs/cli/sim.md's field table).
    assert remote["models"]["pdk"] == "sky130A"


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_request_carries_placeholder_credentials():
    """The committed request must never ship the validation run's real
    keypair/security-group identifiers (the issue calls this out
    explicitly) -- a reader substitutes their own."""
    remote = _load_sim_remote_json("matrix-remote.request.json")["remote"]

    for field in ("key_name", "ssh_key_path", "security_group_id"):
        assert "<" in remote[field] and ">" in remote[field], (
            f"remote.{field} must be a documented placeholder, not a real value"
        )
    assert not re.match(r"^sg-[0-9a-f]+$", remote["security_group_id"])


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_request_passes_the_cost_gate():
    """`remote_launcher`'s sizing + cost gate are pure local computation --
    they run to completion with no AWS credentials, so the example's
    `max_hourly_cost_usd` can be verified in CI to actually admit the
    instance the 5-corner matrix sizes to (the epic's `c7i.12xlarge`)."""
    request = _load_sim_remote_json("matrix-remote.request.json")
    remote = request["remote"]
    corner_count = len(request["corners"]["process"])

    instance_type = rl.select_instance_type(corner_count)
    assert instance_type == "c7i.12xlarge"

    hourly = rl.require_cost_config(
        region=remote["region"],
        instance_type=instance_type,
        spot=remote["spot"],
        max_hourly_cost_usd=remote["max_hourly_cost_usd"],
    )
    assert hourly <= remote["max_hourly_cost_usd"]


@_SKIP_NO_SIM_REMOTE_EXAMPLE
def test_examples_sim_remote_reference_reports_agree_across_backends():
    """Epic #253's headline claim, pinned as a test: the `remote` backend is
    the same code path on a different box, so every corner's measured value
    and verdict matches the `local` run exactly -- only `environment.remote`
    and wall-clock timing differ."""
    local = _load_sim_remote_json("matrix-local.report.json")
    remote = _load_sim_remote_json("matrix-remote.report.json")

    for report in (local, remote):
        assert report["schema_version"] == 3
        assert report["status"] == "pass"
        assert report["corner_count"] == 5
        assert report["passed"] == 5

    # Additive `environment.remote`, present only for the remote run.
    assert "remote" not in local["environment"]
    remote_env = remote["environment"]["remote"]
    for field in (
        "provider",
        "region",
        "instance_type",
        "instance_id",
        "spot",
        "estimated_hourly_cost_usd",
        "ami_id",
        "pdk_snapshot",
        "spin_up_s",
    ):
        assert field in remote_env, f"environment.remote is missing {field}"

    # Corner-for-corner, measurement-for-measurement equality.
    assert [c["corner_id"] for c in local["corners"]] == [
        c["corner_id"] for c in remote["corners"]
    ]
    for local_corner, remote_corner in zip(
        local["corners"], remote["corners"], strict=True
    ):
        assert local_corner["status"] == remote_corner["status"]
        assert local_corner["measurements"] == remote_corner["measurements"]

    # ... and the same rollup verdicts on top of them.
    assert local["measurements"] == remote["measurements"]


# --------------------------------------------------------------------------- #
# options.fail_on_diagnostic -> status: "inconclusive" (issue #2492)
# --------------------------------------------------------------------------- #


#: The modules that can put a `diagnostics[].code` into a `klt sim` report.
_DIAGNOSTIC_SOURCE_MODULES = (sim, sim_remote, sim_batch)

#: Emission sites where the code is supplied by the site's *caller* or by a
#: table it iterates, so the literal cannot be read off the site itself --
#: each is a fan-in whose vocabulary this test derives by a separate rule
#: (the pattern tables; the `_unrun_corner_report` call-site scan).
#:
#: This is a list of **mechanisms**, not of codes: adding a new diagnostic
#: code never changes it, which is the whole point. Adding a new *way* to
#: emit one does, and then `_emittable_diagnostic_codes` fails with the new
#: site's location rather than silently skipping it.
_EXPECTED_INDIRECT_CODE_SITES = {
    # `for code, pattern in _DIAGNOSTIC_PATTERNS` / `_XYCE_DIAGNOSTIC_PATTERNS`
    # -- the loop variable; the tables themselves are rule 1.
    ("klayout_tools.sim", "_classify_xyce_diagnostics", "code"),
    ("klayout_tools.sim", "_classify_diagnostics", "code"),
    # `code` is this function's own parameter; its call sites are rule 3.
    ("klayout_tools.sim_remote", "_unrun_corner_report", "code"),
    # `stop_reason = probe_abort["code"]` -- forwards a code the fail-fast
    # probe's own abort diagnostic already carries as a literal (rule 3).
    ("klayout_tools.sim", "_run_local", "stop_reason"),
    ("klayout_tools.sim", "_run_local_parallel", "stop_reason"),
}


def _resolve_string_expr(expr: ast.AST) -> set[str] | None:
    """The set of `str` values `expr` can statically evaluate to, or `None`
    when it cannot be resolved without running the program.

    Deliberately strict: only literals and literal-only `IfExp`/sequence
    shapes resolve. A `Subscript`/`Call`/`Attribute` returns `None` so the
    site is reported as a fan-in instead of silently contributing whatever
    unrelated string literals happen to appear inside it (e.g.
    `probe_abort["code"]` must not contribute the key `"code"`).
    """
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else None
    if isinstance(expr, ast.IfExp):
        body = _resolve_string_expr(expr.body)
        orelse = _resolve_string_expr(expr.orelse)
        return None if body is None or orelse is None else body | orelse
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        resolved: set[str] = set()
        for element in expr.elts:
            part = _resolve_string_expr(element)
            if part is None:
                return None
            resolved |= part
        return resolved
    return None


def _unrun_code_argument(node: ast.Call) -> ast.AST | None:
    """The `code` argument expression of an `_unrun_corner_report(...)` call
    (bound through the real signature, so positional and keyword forms are
    handled alike), or `None` for any other call."""
    called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    if called != "_unrun_corner_report":
        return None
    try:
        bound = inspect.signature(sim_remote._unrun_corner_report).bind_partial(
            *node.args, **{kw.arg: kw.value for kw in node.keywords if kw.arg}
        )
    except TypeError:  # pragma: no cover - a malformed call
        return None
    return bound.arguments.get("code")


def _code_expressions_at(node: ast.AST) -> list[ast.AST]:
    """Every expression `node` itself places in a `diagnostics[].code` slot:
    a `"code": <expr>` dict entry, or an `_unrun_corner_report` `code`
    argument."""
    if isinstance(node, ast.Dict):
        return [
            value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value == "code"
        ]
    if isinstance(node, ast.Call):
        argument = _unrun_code_argument(node)
        return [] if argument is None else [argument]
    return []


def _code_expression_sites(mod) -> list[tuple[str, ast.AST]]:
    """Every code-slot expression in `mod`, paired with the name of its
    enclosing function."""
    sites: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        sites.extend((func, expr) for expr in _code_expressions_at(node))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(ast.parse(inspect.getsource(mod)), "<module>")
    return sites


def _local_string_assignments(mod) -> dict[str, set[str] | None]:
    """`"<enclosing function>.<local name>" -> the strings assigned to it`,
    or `None` for a local with at least one statically unresolvable
    assignment."""
    assignments: dict[str, set[str] | None] = {}
    for node in ast.walk(ast.parse(inspect.getsource(mod))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for assign in ast.walk(node):
            if not isinstance(assign, (ast.Assign, ast.AnnAssign)):
                continue
            if assign.value is None:
                continue
            targets = (
                assign.targets if isinstance(assign, ast.Assign) else [assign.target]
            )
            resolved = _resolve_string_expr(assign.value)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                key = f"{node.name}.{target.id}"
                previous = assignments.get(key, set())
                assignments[key] = (
                    None
                    if resolved is None or previous is None
                    else previous | resolved
                )
    return assignments


def _resolve_code_site(
    expr: ast.AST,
    func: str,
    module_strings: dict[str, str],
    local_strings: dict[str, set[str] | None],
) -> set[str] | None:
    """The codes one code-slot expression can carry: a literal directly, a
    module-level `str` constant by name, or a local variable through the
    strings assigned to it in the same function. `None` for a fan-in."""
    resolved = _resolve_string_expr(expr)
    if resolved is not None:
        return resolved
    if not isinstance(expr, ast.Name):
        return None
    if expr.id in module_strings:
        return {module_strings[expr.id]}
    return local_strings.get(f"{func}.{expr.id}") or None


def _emittable_diagnostic_codes() -> set[str]:
    """Every `diagnostics[].code` `klt sim` can actually emit, **derived**
    from the source rather than restated.

    Three rules, in order of directness:

    1. the classifier pattern tables (`code, pattern` pairs);
    2. `sim_remote._UNRUN_MESSAGES`, the never-ran-corner message table;
    3. an AST scan of every `"code": <expr>` dict entry and every
       `_unrun_corner_report(..., code=<expr>)` argument in the emitting
       modules -- see `_resolve_code_site` for what it can resolve (which is
       how the batch paths' one
       `"batch_job_timeout" if ... else "batch_job_failed"` resolves).

    Anything rule 3 cannot reduce to a literal must be a declared fan-in
    (`_EXPECTED_INDIRECT_CODE_SITES`); an unexpected one fails, so a new
    emission mechanism cannot slip past this guard either.
    """
    codes: set[str] = {code for code, _ in sim._DIAGNOSTIC_PATTERNS}
    codes |= {code for code, _ in sim._XYCE_DIAGNOSTIC_PATTERNS}
    codes |= set(sim_remote._UNRUN_MESSAGES)

    indirect: set[tuple[str, str, str]] = set()
    for mod in _DIAGNOSTIC_SOURCE_MODULES:
        module_strings = {
            name: value for name, value in vars(mod).items() if isinstance(value, str)
        }
        local_strings = _local_string_assignments(mod)
        for func, expr in _code_expression_sites(mod):
            resolved = _resolve_code_site(expr, func, module_strings, local_strings)
            if resolved is None:
                indirect.add((mod.__name__, func, ast.unparse(expr)))
            else:
                codes |= resolved

    assert indirect == _EXPECTED_INDIRECT_CODE_SITES, (
        "a `diagnostics[].code` emission site changed shape; teach "
        "`_emittable_diagnostic_codes` how to resolve it (or declare it as a "
        f"fan-in) before its code can silently escape DIAGNOSTIC_CODES: {indirect}"
    )
    return codes


def test_diagnostic_codes_is_a_superset_of_every_emittable_code():
    """The `options.fail_on_diagnostic` vocabulary cannot silently drift.

    `sim.DIAGNOSTIC_CODES` is written out as a literal (so the accepted
    option vocabulary is readable in one place); this asserts it still covers
    every code the emitting modules can actually produce, with `emittable`
    **derived from the source** -- see `_emittable_diagnostic_codes`.

    Deriving both halves is the point (issue #2493's `implausible_solution`
    is why): the earlier version of this guard derived the pattern-table
    half but hand-wrote the synthesized half, so a newly-synthesized code
    was missing from *both* `DIAGNOSTIC_CODES` and the hand-written
    `emittable` set and the guard passed anyway -- exactly the silent drift
    it exists to prevent.
    """
    emittable = _emittable_diagnostic_codes()

    # Sanity-check the derivation itself: the codes we know are synthesized
    # outside any pattern table must actually be found by it, or the scan has
    # silently stopped seeing anything and the assertion below is vacuous.
    assert {"timeout", "measurement", "unknown", "lost_shard"} <= emittable
    assert {"batch_job_failed", "batch_job_timeout", "batch_poll_timeout"} <= emittable

    assert emittable - sim._GRADING_MARKER_CODES <= sim.DIAGNOSTIC_CODES
    # The two marker diagnostics the grading step itself attaches are
    # deliberately NOT selectable: both are emitted on a corner already being
    # graded `inconclusive`, downstream of the `options.fail_on_diagnostic`
    # code match, so listing either could never change a verdict.
    assert sim._GRADING_MARKER_CODES <= emittable
    assert not (sim._GRADING_MARKER_CODES & sim.DIAGNOSTIC_CODES)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ()),
        ([], ()),
        (["singular_matrix"], ("singular_matrix",)),
        (
            ["nonconvergence", "singular_matrix"],
            ("nonconvergence", "singular_matrix"),
        ),
        # De-duplicated, caller's order preserved; surrounding whitespace is
        # tolerated the same way every other string option is read.
        (
            ["singular_matrix", " singular_matrix ", "timeout"],
            ("singular_matrix", "timeout"),
        ),
    ],
)
def test_validate_fail_on_diagnostic_normalises_accepted_values(value, expected):
    assert sim._validate_fail_on_diagnostic(value) == expected


@pytest.mark.parametrize(
    "value,match",
    [
        ("singular_matrix", "must be an array"),
        ({"code": "singular_matrix"}, "must be an array"),
        ([1], "non-empty diagnostic code strings"),
        ([""], "non-empty diagnostic code strings"),
        (["singular-matrix"], "unknown diagnostic code"),
        (["Singular_Matrix"], "unknown diagnostic code"),
        (["inconclusive"], "unknown diagnostic code"),
    ],
)
def test_validate_fail_on_diagnostic_rejects_bad_values(value, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_fail_on_diagnostic(value)


def _recovered_request(tmp_path: Path, *, limits: dict | None = None, **overrides):
    """A one-corner request whose stubbed log is the recovered
    singular-matrix capture -- the corner that grades `pass` today."""
    _write_body(tmp_path)
    request: dict[str, object] = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1n"},
        "measurements": [
            {
                "name": "vout_meas",
                "spice": ".meas tran vout_meas find v(vout_node) at=1n",
                "limits": limits if limits is not None else {"min": 0.9, "max": 1.1},
            },
            {
                "name": "tail_meas",
                "spice": ".meas tran tail_meas find v(tail_node) at=1n",
                "limits": {"min": 0.4, "max": 0.6},
            },
        ],
    }
    request.update(overrides)
    return _write_request(tmp_path, request)


def test_fail_on_diagnostic_unset_leaves_recovered_corner_passing(
    tmp_path, monkeypatch
):
    """The no-opt-in baseline: byte-identical to before issue #2492."""
    request = _recovered_request(tmp_path)
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert (report["passed"], report["inconclusive"]) == (1, 0)
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert sim.INCONCLUSIVE_DIAGNOSTIC_CODE not in {
        d["code"] for d in corner["diagnostics"]
    }


def test_fail_on_diagnostic_grades_recovered_corner_inconclusive(tmp_path, monkeypatch):
    """The issue's headline case: the same corner, same log, opted in."""
    request = _recovered_request(
        tmp_path, options={"fail_on_diagnostic": ["singular_matrix"]}
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "inconclusive"
    # The measurements themselves are untouched -- they came back fine; it
    # is the *trust* in them that changed.
    assert [m["status"] for m in corner["measurements"]] == ["pass", "pass"]
    # ... and the corner says why it was graded that way.
    marker = [
        d
        for d in corner["diagnostics"]
        if d["code"] == sim.INCONCLUSIVE_DIAGNOSTIC_CODE
    ]
    assert len(marker) == 1
    assert "singular_matrix" in marker[0]["message"]

    # Top-level counts: the corner is in `inconclusive`, not silently
    # dropped from the other three.
    assert (report["passed"], report["failed"], report["errored"]) == (0, 0, 0)
    assert report["inconclusive"] == 1
    assert (
        report["passed"] + report["failed"] + report["errored"] + report["inconclusive"]
        == report["corner_count"]
    )
    assert report["metrics"]["sim__corner__inconclusive_count"] == 1
    # An untrustworthy sweep is not a pass: the top-level status is its own
    # `"inconclusive"` verdict token at exit 4 ("incomplete or
    # untrustworthy"), the same token/exit pair `klt lvs` already ships --
    # distinct from `"error"`, which still means the simulator broke.
    assert report["status"] == "inconclusive"
    # The per-measurement rollup carries the same claim.
    assert {m["status"] for m in report["measurements"]} == {"inconclusive"}


def test_fail_on_diagnostic_outranks_a_failing_measurement(tmp_path, monkeypatch):
    """Precedence: `inconclusive` beats `fail` (recorded decision, #2492).

    A limit miss computed from numbers the caller has declared untrustworthy
    is not a defensible claim about the design, so it must not be reported
    as one -- exactly the rationale `error` already has for outranking
    `fail`.
    """
    request = _recovered_request(
        tmp_path,
        limits={"max": 0.5},  # the stubbed vout_meas is 1.0 -> a real miss
        options={"fail_on_diagnostic": ["singular_matrix"]},
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    # The measurement still records its own limit miss ...
    assert corner["measurements"][0]["status"] == "fail"
    # ... but the corner's verdict is that no trustworthy result exists.
    assert corner["status"] == "inconclusive"
    assert (report["failed"], report["inconclusive"]) == (0, 1)


def test_fail_on_diagnostic_does_not_outrank_an_errored_corner(tmp_path, monkeypatch):
    """Precedence: `error` still beats `inconclusive`.

    An unrecovered singular matrix (ngspice's own abort trailer, no
    measurement values) produced no number at all -- strictly less
    information than a number the caller declines to trust.
    """
    request = _recovered_request(
        tmp_path, options={"fail_on_diagnostic": ["singular_matrix"]}
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "Warning: singular matrix:  check node b\n"
            "Error: Transient op failed, timestep too small\n"
            "run simulation(s) aborted\n"
        ),
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    assert (report["errored"], report["inconclusive"]) == (1, 0)


def test_fail_on_diagnostic_code_that_never_occurs_is_a_no_op(tmp_path, monkeypatch):
    """A valid code that this run never emits changes nothing.

    Listing one is a legitimate standing policy, not a mistake -- unlike an
    unknown code, which is rejected up front.
    """
    request = _recovered_request(tmp_path, options={"fail_on_diagnostic": ["timeout"]})
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert (report["passed"], report["inconclusive"]) == (1, 0)


def test_fail_on_diagnostic_unknown_code_is_an_application_error(tmp_path, monkeypatch):
    request = _recovered_request(
        tmp_path, options={"fail_on_diagnostic": ["singular_matrixx"]}
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    with pytest.raises(sim.SimError, match="unknown diagnostic code"):
        sim.run_sim(str(request))


def test_fail_on_diagnostic_matches_an_error_severity_diagnostic_too(
    tmp_path, monkeypatch
):
    """Listing a code that was never downgraded still behaves sanely.

    `nonconvergence` here is recovered (so `warning`); `timeout` never is.
    A timed-out corner is already `error`, which outranks `inconclusive` --
    the option can only ever *tighten* a corner's grade, never loosen it.
    """
    request = _recovered_request(
        tmp_path, options={"fail_on_diagnostic": ["nonconvergence"]}
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "inconclusive"
    # The downgrade itself is untouched -- this option changes grading, not
    # classification.
    severities = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert severities["singular_matrix"] == "warning"
    assert severities["nonconvergence"] == "warning"


def test_fail_on_diagnostic_honoured_by_the_local_parallel_backend(
    tmp_path, monkeypatch
):
    request = _recovered_request(
        tmp_path,
        backend="local-parallel",
        options={"fail_on_diagnostic": ["singular_matrix"], "max_workers": 2},
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert [c["status"] for c in report["corners"]] == ["inconclusive"]
    assert report["inconclusive"] == 1


def test_fail_on_diagnostic_argument_overrides_the_request_option(
    tmp_path, monkeypatch
):
    request = _recovered_request(tmp_path, options={"fail_on_diagnostic": []})
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request), fail_on_diagnostic=["singular_matrix"])

    assert report["inconclusive"] == 1


def test_cli_fail_on_diagnostic_flag_reports_inconclusive_and_exit_4(
    tmp_path, monkeypatch, capsys
):
    request = _recovered_request(tmp_path)
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    exit_code = main(
        [
            "sim",
            str(request),
            "--fail-on-diagnostic",
            "singular_matrix",
            "--format",
            "json",
        ]
    )

    assert exit_code == 4
    data = json.loads(capsys.readouterr().out)
    assert data["inconclusive"] == 1
    assert data["corners"][0]["status"] == "inconclusive"


def test_cli_text_output_always_reports_the_inconclusive_count(
    tmp_path, monkeypatch, capsys
):
    request = _recovered_request(tmp_path)
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    assert main(["sim", str(request)]) == 0

    # Present (as 0) even without the option, so the counts on this line
    # always add up to `corners:`.
    assert "inconclusive: 0" in capsys.readouterr().out


# --- Monte Carlo rollup treatment (issue #2492) ----------------------------- #


def _stub_subprocess_values_with_logs(
    monkeypatch, name: str, values: list[float], *, noisy_indices: set[int]
) -> None:
    """Like `_stub_subprocess_values`, but the samples in ``noisy_indices``
    additionally get the recovered singular-matrix narration in their log.
    """
    state = {"index": 0}

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-o") + 1]
        index = state["index"]
        state["index"] += 1
        preamble = (
            "Warning: singular matrix:  check node xota.g1\n"
            "Note: Starting dynamic gmin stepping\n"
            "Warning: Dynamic gmin stepping failed\n"
            "Note: Transient op finished successfully\n"
            if index in noisy_indices
            else ""
        )
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(f"{preamble}{name} = {values[index]!r}\n")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_monte_carlo_inconclusive_samples_are_excluded_and_counted(
    tmp_path, monkeypatch
):
    """Documented decision: excluded from the statistics, counted separately.

    An untrusted sample must not be allowed to move the very mean/sigma the
    window verdict is computed from -- so it is set aside like an
    unextractable one, and `n + errored + inconclusive` still accounts for
    every sample drawn.
    """
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch"},
        measurements=[_VOUT_MEAS],
        options={"fail_on_diagnostic": ["singular_matrix"]},
    )
    # Sample 2 (value 5.0) is the distrusted one; the trusted four are
    # 3.0/1.0/2.0/4.0, mean 2.5 -- distinctly different from the
    # all-five mean of 3.0, so an accidental inclusion cannot pass silently.
    _stub_subprocess_values_with_logs(
        monkeypatch, "vout", _KNOWN_SAMPLES, noisy_indices={2}
    )

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    mc = entry["monte_carlo"]
    assert mc["n"] == 4
    assert mc["errored"] == 0
    assert mc["inconclusive"] == 1
    assert mc["n"] + mc["errored"] + mc["inconclusive"] == 5
    assert mc["mean"] == pytest.approx(2.5)
    assert mc["max"] == 4.0  # the excluded 5.0 is not the reported maximum
    # ... and the same accounting holds in the per-corner breakdown.
    (by_corner,) = mc["by_corner"]
    assert (by_corner["n"], by_corner["inconclusive"]) == (4, 1)
    # The corner counts and the aggregate agree with the sample accounting.
    assert report["inconclusive"] == 1
    assert report["status"] == "inconclusive"


def test_monte_carlo_statistics_block_always_carries_inconclusive(
    tmp_path, monkeypatch
):
    """Additive and always present -- `0` for a run that never opted in."""
    request = _mc_request(
        tmp_path, {"n": 5, "seed": 1, "vary": "mismatch"}, measurements=[_VOUT_MEAS]
    )
    _stub_subprocess_values(monkeypatch, "vout", _KNOWN_SAMPLES)

    report = sim.run_sim(str(request))

    mc = report["measurements"][0]["monte_carlo"]
    assert mc["inconclusive"] == 0
    assert mc["n"] == 5
    assert mc["mean"] == pytest.approx(3.0)


def test_monte_carlo_sigma_window_is_computed_over_trusted_samples_only(
    tmp_path, monkeypatch
):
    """The window verdict never rests on a sample the caller disowned."""
    request = _mc_request(
        tmp_path,
        {"n": 5, "seed": 1, "vary": "mismatch", "k_sigma": 1.0},
        measurements=[dict(_VOUT_MEAS, limits={"min": 0.0, "max": 4.5})],
        options={"fail_on_diagnostic": ["singular_matrix"]},
    )
    # Untrusted sample 2 is the 5.0 outlier: including it would push the
    # window's upper endpoint past max=4.5 and report a spec miss on a
    # number the caller has declared untrustworthy.
    _stub_subprocess_values_with_logs(
        monkeypatch, "vout", _KNOWN_SAMPLES, noisy_indices={2}
    )

    report = sim.run_sim(str(request))

    (entry,) = report["measurements"]
    window = entry["monte_carlo"]["sigma_window"]
    assert window["high"] == pytest.approx(2.5 + (5.0 / 3.0) ** 0.5)
    assert window["status"] == "pass"
    # The entry itself is still `inconclusive` -- one of its corners is.
    assert entry["status"] == "inconclusive"


def test_rollup_measurements_inconclusive_corner_outranks_a_failing_one():
    """Unit-level precedence for the aggregate: error > inconclusive > fail."""
    corners = [
        {
            "corner_id": "tt/1.800V/27C",
            "status": "fail",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 2.0, "margin": -0.8, "status": "fail"}
            ],
        },
        {
            "corner_id": "ss/1.800V/27C",
            "status": "inconclusive",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 1.2, "margin": 0.0, "status": "pass"}
            ],
        },
    ]

    (entry,) = sim._rollup_measurements([{"name": "vref"}], corners)

    assert entry["status"] == "inconclusive"
    # `worst_case` still points at the worst margin across every corner --
    # an inconclusive rollup must stay debuggable.
    assert entry["worst_case"]["corner_id"] == "tt/1.800V/27C"


def test_rollup_measurements_errored_corner_still_outranks_inconclusive():
    corners = [
        {
            "corner_id": "tt/1.800V/27C",
            "status": "error",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": None, "margin": None, "status": "error"}
            ],
        },
        {
            "corner_id": "ss/1.800V/27C",
            "status": "inconclusive",
            "monte_carlo": None,
            "measurements": [
                {"name": "vref", "value": 1.2, "margin": 0.0, "status": "pass"}
            ],
        },
    ]

    (entry,) = sim._rollup_measurements([{"name": "vref"}], corners)

    assert entry["status"] == "error"


# --------------------------------------------------------------------------- #
# Issue #2520: `cwd=` + `options.ngspice_init` / `.spiceinit`
# --------------------------------------------------------------------------- #


def _stub_subprocess_run_capturing_cwd(
    monkeypatch,
    *,
    log_text: str = "",
    stdout: str = "** ngspice-99\n",
    captured_cwds: list | None = None,
):
    """Like `_stub_subprocess_run`, but also records every `cwd=` kwarg the
    real code passed, so a test can assert which directory ngspice was
    actually launched from (issue #2520)."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        if captured_cwds is not None:
            captured_cwds.append(cwd)
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return fake_completed(stdout)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_corner_cwd_is_the_keep_artifacts_corner_dir(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    captured_cwds: list = []
    _stub_subprocess_run_capturing_cwd(monkeypatch, captured_cwds=captured_cwds)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    corner_dir = os.path.dirname(corner["artifacts"]["log"])
    assert captured_cwds == [corner_dir]


def test_run_corner_cwd_is_a_scratch_dir_when_not_keeping_artifacts(
    tmp_path, monkeypatch
):
    """`keep_artifacts=False` corners run from a `_tmp_work_dir()` scratch
    directory, not `artifacts_dir` -- the `cwd=` fix must anchor to *that*
    directory too, not just the `keep_artifacts=True` branch (see the
    issue's own implementation guidance)."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    captured_cwds: list = []
    _stub_subprocess_run_capturing_cwd(monkeypatch, captured_cwds=captured_cwds)

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["artifacts"]["log"] is None  # not kept
    (cwd,) = captured_cwds
    assert cwd is not None
    assert cwd != os.getcwd()
    assert os.path.basename(cwd).startswith("klt-sim-")


def test_write_spiceinit_writes_requested_lines_in_order(tmp_path):
    run_dir = tmp_path / "corner"
    run_dir.mkdir()

    sim._write_spiceinit(str(run_dir), ("set ngbehavior=hsa", "set numdgt=7"))

    assert (run_dir / ".spiceinit").read_text() == "set ngbehavior=hsa\nset numdgt=7\n"


def test_write_spiceinit_is_a_no_op_when_unset_and_none_existed(tmp_path):
    run_dir = tmp_path / "corner"
    run_dir.mkdir()

    sim._write_spiceinit(str(run_dir), ())

    assert not (run_dir / ".spiceinit").exists()


def test_write_spiceinit_removes_a_stale_file_when_unset(tmp_path):
    """A `keep_artifacts` corner directory can be reused across requests --
    an earlier request that set `options.ngspice_init` must not leave a
    `.spiceinit` behind for a later request that doesn't opt in."""
    run_dir = tmp_path / "corner"
    run_dir.mkdir()
    (run_dir / ".spiceinit").write_text("set ngbehavior=hsa\n")

    sim._write_spiceinit(str(run_dir), ())

    assert not (run_dir / ".spiceinit").exists()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ()),
        ([], ()),
        (["set ngbehavior=hsa"], ("set ngbehavior=hsa",)),
        (
            ["set ngbehavior=hsa", "set numdgt=7"],
            ("set ngbehavior=hsa", "set numdgt=7"),
        ),
        # Unlike `_validate_fail_on_diagnostic`, duplicates are preserved --
        # a repeated line is the caller's call, not an obvious mistake.
        (
            ["set ngbehavior=hsa", "set ngbehavior=hsa"],
            ("set ngbehavior=hsa", "set ngbehavior=hsa"),
        ),
    ],
)
def test_validate_ngspice_init_normalises_accepted_values(value, expected):
    assert sim._validate_ngspice_init(value) == expected


@pytest.mark.parametrize(
    "value,match",
    [
        ("set ngbehavior=hsa", "must be an array"),
        ({"line": "set ngbehavior=hsa"}, "must be an array"),
        ([1], "non-empty ngspice init-line strings"),
        ([""], "non-empty ngspice init-line strings"),
        (["   "], "non-empty ngspice init-line strings"),
    ],
)
def test_validate_ngspice_init_rejects_bad_values(value, match):
    with pytest.raises(sim.SimError, match=match):
        sim._validate_ngspice_init(value)


def test_run_sim_ngspice_init_writes_spiceinit_into_corner_dir(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {
                "keep_artifacts": True,
                "ngspice_init": ["set ngbehavior=hsa", "set numdgt=7"],
            },
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    corner_dir = os.path.dirname(corner["artifacts"]["log"])
    spiceinit_path = os.path.join(corner_dir, ".spiceinit")
    assert os.path.isfile(spiceinit_path)
    assert Path(spiceinit_path).read_text() == "set ngbehavior=hsa\nset numdgt=7\n"


def test_run_sim_without_ngspice_init_writes_no_spiceinit(tmp_path, monkeypatch):
    """Regression check: a request that doesn't opt in gets byte-identical
    behaviour to before issue #2520 -- no `.spiceinit` anywhere."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    corner_dir = os.path.dirname(corner["artifacts"]["log"])
    assert not os.path.isfile(os.path.join(corner_dir, ".spiceinit"))


def test_run_sim_ngspice_init_honoured_by_the_local_parallel_backend(
    tmp_path, monkeypatch
):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "corners": {"temperature_c": [27, 125]},
            "options": {
                "keep_artifacts": True,
                "ngspice_init": ["set ngbehavior=hsa"],
            },
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(
        str(request),
        artifacts_dir=str(tmp_path / "artifacts"),
        backend="local-parallel",
    )

    assert len(report["corners"]) == 2
    for corner in report["corners"]:
        corner_dir = os.path.dirname(corner["artifacts"]["log"])
        assert os.path.isfile(os.path.join(corner_dir, ".spiceinit"))


def test_run_sim_ngspice_init_not_forwarded_to_xyce(tmp_path, monkeypatch):
    """`.spiceinit` is an ngspice-only lookup -- the `xyce` engine branch of
    `_prepare_corner_run` never writes one, even when `options.ngspice_init`
    is set (harmless, but also not silently doing something Xyce has no
    concept of)."""
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {
                "keep_artifacts": True,
                "ngspice_init": ["set ngbehavior=hsa"],
            },
        },
    )

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        log_path = cmd[cmd.index("-l") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("Xyce version 7.10.0\n")
        return fake_completed("")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    corner_dir = os.path.dirname(corner["artifacts"]["log"])
    assert not os.path.isfile(os.path.join(corner_dir, ".spiceinit"))


@_SKIP_NO_NGSPICE
def test_integration_ngspice_init_sets_compatibility_mode(tmp_path, monkeypatch):
    """The reproducibility fix itself (issue #2520), against real ngspice.

    Isolated from whatever `$HOME/.spiceinit` this host happens to have --
    exactly the "unreproducible" symptom the issue reports -- by pointing
    `$HOME` at an empty directory. Without `options.ngspice_init`, ngspice's
    own log carries `Note: No compatibility mode selected!`; with it, that
    note is replaced by a `Compatibility modes selected` note naming the
    requested mode -- proving `.spiceinit` was actually written to, and read
    from, `corner_dir` (via the `cwd=` fix), not the caller's ambient cwd or
    `$HOME`.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    _write_body(tmp_path)

    def _run(*, with_init: bool) -> str:
        options: dict[str, object] = {"keep_artifacts": True}
        if with_init:
            options["ngspice_init"] = ["set ngbehavior=hsa"]
        request = _write_request(
            tmp_path,
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "options": options,
            },
            name=f"request_{with_init}.json",
        )
        report = sim.run_sim(
            str(request),
            artifacts_dir=str(tmp_path / f"artifacts_{with_init}"),
        )
        (corner,) = report["corners"]
        return Path(corner["artifacts"]["log"]).read_text()

    without_log = _run(with_init=False)
    assert "Note: No compatibility mode selected!" in without_log

    with_log = _run(with_init=True)
    assert "Note: No compatibility mode selected!" not in with_log
    assert "Compatibility modes selected" in with_log


# --------------------------------------------------------------------------- #
# OSDI (Verilog-A) model preload (issue #2513): options.osdi_preload
# --------------------------------------------------------------------------- #


def _write_osdi(
    tmp_path: Path, name: str = "psp103.osdi", content: bytes = b""
) -> Path:
    path = tmp_path / name
    path.write_bytes(content or f"fake osdi {name}".encode())
    return path


def _osdi_request(tmp_path: Path, osdi_preload, **extra) -> Path:
    _write_body(tmp_path)
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
        ],
        "options": {"osdi_preload": osdi_preload},
    }
    request.update(extra)
    return _write_request(tmp_path, request)


def _capture_decks(monkeypatch) -> list[str]:
    """Stub ngspice, recording the text of every deck it is handed."""
    decks: list[str] = []

    def fake_run(cmd, capture_output, text, timeout, cwd=None):
        decks.append(Path(cmd[cmd.index("-b") + 1]).read_text())
        log_path = cmd[cmd.index("-o") + 1]
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(_HOST_DEFAULT_LOG)
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    return decks


def test_write_corner_deck_emits_pre_osdi_inside_control_before_the_analysis(
    tmp_path,
):
    first = str(tmp_path / "psp103.osdi")
    second = str(tmp_path / "r3_cmc.osdi")
    point = sim.CornerPoint("tt", {"vdd": 1.2}, 27)
    lines = _write_deck(tmp_path, point, osdi_preload=(first, second))

    control = lines.index(".control")
    endc = lines.index(".endc")
    analysis = lines.index("tran 1n 1u")
    alter = lines.index("alter vdd=1.2")
    pre = [i for i, line in enumerate(lines) if line.startswith("pre_osdi")]
    # Declared order, inside the generated control block, ahead of every
    # `alter` and the analysis command.
    assert [lines[i] for i in pre] == [f"pre_osdi {first}", f"pre_osdi {second}"]
    assert all(control < i < endc for i in pre)
    assert max(pre) < alter < analysis
    # Exactly one control block: the preload does not grow a second one.
    assert lines.count(".control") == 1


def test_write_corner_deck_without_osdi_preload_is_unchanged(tmp_path):
    point = sim.CornerPoint("tt", {"vdd": 1.2}, 27)
    default = _write_deck(tmp_path, point)
    explicit_empty = _write_deck(tmp_path, point, osdi_preload=())
    assert default == explicit_empty
    assert not any(line.startswith("pre_osdi") for line in default)


def test_run_sim_osdi_preload_reaches_the_deck_and_the_environment(
    tmp_path, monkeypatch
):
    osdi = _write_osdi(tmp_path)
    decks = _capture_decks(monkeypatch)
    # Relative to the request file's own directory, like `models.lib`.
    request = _osdi_request(tmp_path, ["psp103.osdi"])

    report = sim.run_sim(str(request))

    (deck,) = decks
    deck_lines = deck.splitlines()
    assert f"pre_osdi {osdi}" in deck_lines
    assert deck_lines.index(f"pre_osdi {osdi}") > deck_lines.index(".control")
    assert deck_lines.index(f"pre_osdi {osdi}") < deck_lines.index("tran 1n 1u")

    (entry,) = report["environment"]["osdi_preload"]
    assert entry["name"] == "psp103.osdi"
    assert entry["sha256"] == sim.sha256_file(str(osdi))
    assert set(entry) == {"name", "path", "scope", "sha256"}


def test_run_sim_without_osdi_preload_omits_the_environment_field(
    tmp_path, monkeypatch
):
    decks = _capture_decks(monkeypatch)
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    report = sim.run_sim(str(request))
    assert "osdi_preload" not in report["environment"]
    assert "pre_osdi" not in decks[0]


def test_run_sim_missing_osdi_preload_fails_before_any_corner_runs(
    tmp_path, monkeypatch
):
    def must_not_run(*args, **kwargs):
        raise AssertionError("no corner may be dispatched")

    monkeypatch.setattr(sim.subprocess, "run", must_not_run)
    request = _osdi_request(tmp_path, ["absent.osdi"])
    with pytest.raises(
        sim.SimError, match="osdi_preload file not found: .*absent.osdi"
    ):
        sim.run_sim(str(request))


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("psp103.osdi", "osdi_preload must be an array"),
        ({"a": "b"}, "osdi_preload must be an array"),
        ([""], r"osdi_preload\[0\] must be a non-empty path"),
        ([42], r"osdi_preload\[0\] must be a non-empty path"),
    ],
)
def test_run_sim_malformed_osdi_preload_is_a_named_error(tmp_path, value, match):
    request = _osdi_request(tmp_path, value)
    with pytest.raises(sim.SimError, match=match):
        sim.run_sim(str(request))


@pytest.mark.parametrize("backend", ["remote", "batch"])
def test_run_sim_osdi_preload_is_refused_for_an_explicit_offhost_backend(
    tmp_path, monkeypatch, backend
):
    """An `.osdi` is a host-architecture binary: the off-host backends refuse
    the option by name, before any instance/job exists, rather than stage it."""

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError(f"the {backend} backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, backend, must_not_dispatch)
    monkeypatch.setattr(sim, "_run_remote_fleet", must_not_dispatch)
    monkeypatch.setattr(sim, "_run_batch_fleet", must_not_dispatch)
    _write_osdi(tmp_path)
    request = _osdi_request(
        tmp_path,
        ["psp103.osdi"],
        corners={"temperature_c": [-40, 27, 125]},
    )
    with pytest.raises(
        sim.SimError,
        match=rf"options.osdi_preload is not supported for backend '{backend}'",
    ):
        sim.run_sim(str(request), backend=backend)


@pytest.mark.parametrize("backend", ["remote", "batch"])
def test_run_sim_osdi_preload_steps_a_host_default_offhost_backend_back_to_local(
    tmp_path, monkeypatch, backend
):
    monkeypatch.setenv(sim.BACKEND_ENV, backend)

    def must_not_dispatch(*args, **kwargs):
        raise AssertionError(f"the {backend} backend must not be reached")

    monkeypatch.setitem(sim._BACKENDS, backend, must_not_dispatch)
    decks = _capture_decks(monkeypatch)
    osdi = _write_osdi(tmp_path)
    request = _osdi_request(
        tmp_path,
        ["psp103.osdi"],
        corners={"temperature_c": [-40, 27, 125]},
    )

    report = sim.run_sim(str(request))

    assert len(report["corners"]) == 3
    assert all(f"pre_osdi {osdi}" in deck for deck in decks)


def test_run_sim_osdi_preload_is_refused_for_the_xyce_engine(tmp_path, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("no corner may be dispatched")

    monkeypatch.setattr(sim.subprocess, "run", must_not_run)
    _write_osdi(tmp_path)
    request = _osdi_request(tmp_path, ["psp103.osdi"], engine="xyce")
    with pytest.raises(
        sim.SimError, match="options.osdi_preload is not supported for engine 'xyce'"
    ):
        sim.run_sim(str(request))


def test_checkpoint_fingerprint_keys_on_osdi_preload_content(tmp_path):
    body = _write_body(tmp_path)
    osdi = _write_osdi(tmp_path, content=b"build 1")
    kwargs = {
        "netlist_path": str(body),
        "models_lib": None,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "corner_points": [sim.CornerPoint(None, {}, 27)],
        "timeout_s": 10.0,
        "engine": "ngspice",
    }
    without = sim._checkpoint_fingerprint(**kwargs)
    # No preload keeps the pre-#2513 fingerprint byte-identical.
    assert sim._checkpoint_fingerprint(**kwargs, osdi_preload=()) == without
    first = sim._checkpoint_fingerprint(**kwargs, osdi_preload=(str(osdi),))
    assert first != without
    osdi.write_bytes(b"build 2")
    assert sim._checkpoint_fingerprint(**kwargs, osdi_preload=(str(osdi),)) != first


# --------------------------------------------------------------------------- #
# Per-section corner libraries (issue #2522): a `corners.process` bundle whose
# `sections[]` entries name their own `.lib` file, for a PDK that ships one
# corner library per device family instead of one file with every section.
# --------------------------------------------------------------------------- #


def _write_family_lib(tmp_path: Path, name: str, section: str = "tt") -> Path:
    path = tmp_path / name
    path.write_text(f".lib {section}\n.param {section}_scale=1.0\n.endl {section}\n")
    return path


def test_expand_corners_bundle_per_section_libs():
    (point,) = sim._expand_corners(
        {
            "process": [
                {
                    "name": "tt",
                    "sections": [
                        {"lib": "mos.lib", "section": "mos_tt"},
                        {"lib": "cap.lib", "section": "cap_tt"},
                    ],
                }
            ]
        },
        [],
    )

    assert point.process == "tt"
    assert point.process_sections == ["mos_tt", "cap_tt"]
    assert point.process_section_libs == ["mos.lib", "cap.lib"]
    # Nothing is resolved at expansion time -- the declared refs are what a
    # fleet shard is handed (see `_resolve_corner_section_libs`).
    assert point.process_section_libs_resolved is None
    assert point.corner_id == "tt/novdd/27C"


def test_expand_corners_bundle_mixes_bare_and_per_section_lib_entries():
    """The issue's edge case: a bare-string section (models.lib) and a
    per-section-lib object in the same `sections` array."""
    (point,) = sim._expand_corners(
        {
            "process": [
                {
                    "name": "tt",
                    "sections": ["tt", {"lib": "cap.lib", "section": "cap_tt"}],
                }
            ]
        },
        [],
    )

    assert point.process_sections == ["tt", "cap_tt"]
    assert point.process_section_libs == [None, "cap.lib"]


def test_expand_corners_bundle_of_bare_strings_declares_no_section_libs():
    """Regression: today's all-bare-string bundle keeps
    `process_section_libs=None` -- the shape every pre-#2522 caller produced,
    which `_write_corner_deck` renders against `models.lib` alone."""
    (point,) = sim._expand_corners(
        {"process": [{"name": "ss", "sections": ["ss", "bjt_ss"]}]}, []
    )

    assert point.process_sections == ["ss", "bjt_ss"]
    assert point.process_section_libs is None


@pytest.mark.parametrize(
    "section,match",
    [
        ({"section": "cap_tt"}, "lib"),
        ({"lib": "", "section": "cap_tt"}, "lib"),
        ({"lib": 5, "section": "cap_tt"}, "lib"),
        ({"lib": "cap.lib"}, "section"),
        ({"lib": "cap.lib", "section": ""}, "section"),
        ({"lib": "cap.lib", "section": 5}, "section"),
        (["cap.lib", "cap_tt"], "sections"),
    ],
)
def test_expand_corners_bundle_per_section_lib_validation(section, match):
    with pytest.raises(sim.SimError, match=match):
        sim._expand_corners(
            {"process": [{"name": "tt", "sections": [section]}]},
            [],
        )


def test_corner_points_wire_round_trip_preserves_per_section_libs():
    points = sim._expand_corners(
        {
            "process": [
                "ff",
                {
                    "name": "tt",
                    "sections": ["tt", {"lib": "cap.lib", "section": "cap_tt"}],
                },
            ],
            "temperature_c": [-40, 125],
        },
        [],
    )
    wire = sim._corner_points_to_wire(points)

    # The wire carries the refs *as declared* -- never this host's resolved
    # absolute paths, which a fleet shard's own box could not honour.
    assert wire[2]["process_sections"] == ["tt", "cap_tt"]
    assert wire[2]["process_section_libs"] == [None, "cap.lib"]
    assert json.loads(json.dumps(wire)) == wire

    restored = sim._corner_points_from_wire(json.loads(json.dumps(wire)))
    assert [p.corner_id for p in restored] == [p.corner_id for p in points]
    assert [p.process_sections for p in restored] == [
        p.process_sections for p in points
    ]
    assert [p.process_section_libs for p in restored] == [
        p.process_section_libs for p in points
    ]


def test_corner_points_wire_round_trip_without_per_section_libs():
    """A pre-#2522 corner list round-trips with `process_section_libs` null,
    and reconstructs the exact shape `_write_corner_deck` used before."""
    points = sim._expand_corners(
        {"process": ["tt", {"name": "ss", "sections": ["ss", "bjt_ss"]}]}, []
    )
    wire = sim._corner_points_to_wire(points)

    assert [entry["process_section_libs"] for entry in wire] == [None, None]
    restored = sim._corner_points_from_wire(wire)
    assert [p.process_section_libs for p in restored] == [None, None]


def test_corner_points_from_wire_rejects_mismatched_section_lib_length():
    wire = [
        {
            "process": "tt",
            "process_sections": ["tt", "cap_tt"],
            "process_section_libs": ["cap.lib"],
            "supply_v": {},
            "temperature_c": 27,
        }
    ]
    with pytest.raises(sim.SimError, match="process_section_libs"):
        sim._corner_points_from_wire(wire)


def test_write_corner_deck_per_section_libs_emit_their_own_lib_file(tmp_path):
    point = sim.CornerPoint(
        "tt",
        {},
        27,
        process_sections=["tt", "cap_tt", "bjt_tt"],
        process_section_libs=[None, "cap.lib", "bjt.lib"],
        process_section_libs_resolved=[
            None,
            str(tmp_path / "cap.lib"),
            str(tmp_path / "bjt.lib"),
        ],
    )
    lines = _write_deck(tmp_path, point)

    models_lib = tmp_path / "corner.lib"
    assert [line for line in lines if line.startswith(".lib")] == [
        f".lib {models_lib} tt",
        f".lib {tmp_path / 'cap.lib'} cap_tt",
        f".lib {tmp_path / 'bjt.lib'} bjt_tt",
    ]


def test_write_corner_deck_unresolved_per_section_libs_use_the_declared_ref(
    tmp_path,
):
    """A point that never went through `_resolve_corner_section_libs` (a
    direct caller, not `run_sim`) still names the declared ref rather than
    silently falling back to `models.lib`."""
    point = sim.CornerPoint(
        "tt",
        {},
        27,
        process_sections=["cap_tt"],
        process_section_libs=["cap.lib"],
    )
    lines = _write_deck(tmp_path, point)

    assert [line for line in lines if line.startswith(".lib")] == [
        ".lib cap.lib cap_tt"
    ]


def test_write_xyce_deck_per_section_libs_emit_their_own_lib_file(tmp_path):
    deck = tmp_path / "corner.cir"
    sim._write_xyce_deck(
        deck_path=str(deck),
        netlist_path=str(tmp_path / "body.spice"),
        models_lib=str(tmp_path / "corner.lib"),
        point=sim.CornerPoint(
            "tt",
            {},
            85,
            process_sections=["tt", "cap_tt"],
            process_section_libs=[None, "cap.lib"],
            process_section_libs_resolved=[None, str(tmp_path / "cap.lib")],
        ),
        analysis={"kind": "tran", "args": "1n 10u"},
        measurements_spec=[],
    )

    lines = deck.read_text().splitlines()
    assert [line for line in lines if line.startswith(".lib")] == [
        f".lib {tmp_path / 'corner.lib'} tt",
        f".lib {tmp_path / 'cap.lib'} cap_tt",
    ]


def test_resolve_corner_section_libs_resolves_relative_to_the_request_dir(tmp_path):
    cap = _write_family_lib(tmp_path, "cap.lib", section="cap_tt")
    (point,) = sim._expand_corners(
        {
            "process": [
                {"name": "tt", "sections": [{"lib": "cap.lib", "section": "cap_tt"}]}
            ]
        },
        [],
    )

    libs = sim._resolve_corner_section_libs([point], {}, str(tmp_path))

    assert libs == (str(cap),)
    assert point.process_section_libs_resolved == [str(cap)]
    # The declared ref is left intact for the fleet wire.
    assert point.process_section_libs == ["cap.lib"]


def test_resolve_corner_section_libs_expands_env_vars(tmp_path, monkeypatch):
    cap = _write_family_lib(tmp_path, "cap.lib", section="cap_tt")
    monkeypatch.setenv("PDK_ROOT", str(tmp_path))
    (point,) = sim._expand_corners(
        {
            "process": [
                {
                    "name": "tt",
                    "sections": [
                        {"lib": "$PDK_ROOT/cap.lib", "section": "cap_tt"},
                    ],
                }
            ]
        },
        [],
    )

    assert sim._resolve_corner_section_libs([point], {}, str(tmp_path)) == (str(cap),)


def test_resolve_corner_section_libs_joins_relative_refs_against_the_pdk_variant(
    tmp_path,
):
    """Same rule as `models.lib`: with `models.pdk` set, a relative ref
    resolves against the PDK variant directory -- the shape that also
    survives a remote/batch shard, whose own `$PDK_ROOT` differs."""
    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech" / "ngspice"
    variant_dir.mkdir(parents=True)
    (install_root / "sky130A" / "libs.tech" / "klayout").mkdir()
    cap = variant_dir / "cap.lib"
    cap.write_text(".lib cap_tt\n.endl cap_tt\n")

    (point,) = sim._expand_corners(
        {
            "process": [
                {
                    "name": "tt",
                    "sections": [
                        {
                            "lib": "libs.tech/ngspice/cap.lib",
                            "section": "cap_tt",
                        }
                    ],
                }
            ]
        },
        [],
    )
    libs = sim._resolve_corner_section_libs(
        [point],
        {"pdk": "sky130A", "pdk_root": str(install_root)},
        str(tmp_path),
    )

    assert libs == (str(cap),)


def test_resolve_corner_section_libs_missing_file_raises(tmp_path):
    (point,) = sim._expand_corners(
        {
            "process": [
                {"name": "tt", "sections": [{"lib": "nope.lib", "section": "cap_tt"}]}
            ]
        },
        [],
    )

    with pytest.raises(sim.SimError, match="corner section library not found"):
        sim._resolve_corner_section_libs([point], {}, str(tmp_path))


def test_resolve_corner_section_libs_deduplicates_in_first_appearance_order(tmp_path):
    cap = _write_family_lib(tmp_path, "cap.lib", section="cap_tt")
    bjt = _write_family_lib(tmp_path, "bjt.lib", section="bjt_tt")
    points = sim._expand_corners(
        {
            "process": [
                {
                    "name": "tt",
                    "sections": [
                        {"lib": "cap.lib", "section": "cap_tt"},
                        {"lib": "bjt.lib", "section": "bjt_tt"},
                    ],
                },
                {
                    "name": "ss",
                    "sections": [{"lib": "cap.lib", "section": "cap_tt"}],
                },
            ]
        },
        [],
    )

    assert sim._resolve_corner_section_libs(points, {}, str(tmp_path)) == (
        str(cap),
        str(bjt),
    )


def test_resolve_corner_section_libs_leaves_ordinary_points_alone(tmp_path):
    points = sim._expand_corners(
        {"process": ["tt", {"name": "ss", "sections": ["ss", "bjt_ss"]}]}, []
    )

    assert sim._resolve_corner_section_libs(points, {}, str(tmp_path)) == ()
    assert [p.process_section_libs_resolved for p in points] == [None, None]


def _per_section_lib_request(tmp_path: Path, **models) -> tuple[Path, Path, Path]:
    _write_body(tmp_path)
    mos = _write_corner_lib(tmp_path, "mos.lib")
    cap = _write_family_lib(tmp_path, "cap.lib", section="cap_tt")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": models,
            "corners": {
                "process": [
                    {
                        "name": "tt",
                        "sections": [
                            {"lib": "mos.lib", "section": "tt"},
                            {"lib": "cap.lib", "section": "cap_tt"},
                        ],
                    }
                ]
            },
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    return request, mos, cap


def test_run_sim_per_section_libs_reach_the_deck_and_the_environment(
    tmp_path, monkeypatch
):
    decks = _capture_decks(monkeypatch)
    request, mos, cap = _per_section_lib_request(tmp_path)

    report = sim.run_sim(str(request))

    (deck,) = decks
    assert [line for line in deck.splitlines() if line.startswith(".lib")] == [
        f".lib {mos} tt",
        f".lib {cap} cap_tt",
    ]
    entries = report["environment"]["corner_section_libs"]
    assert [entry["name"] for entry in entries] == ["mos.lib", "cap.lib"]
    assert [entry["sha256"] for entry in entries] == [
        sim.sha256_file(str(mos)),
        sim.sha256_file(str(cap)),
    ]
    assert set(entries[0]) == {"name", "path", "scope", "sha256"}
    # Every section named its own library, so `models.lib` was never needed.
    assert report["environment"]["models_lib"]["path"] is None
    assert report["environment"]["models_lib_sha256"] is None


def test_run_sim_per_section_libs_alongside_a_models_lib_section(tmp_path, monkeypatch):
    decks = _capture_decks(monkeypatch)
    _write_body(tmp_path)
    models_lib = _write_corner_lib(tmp_path)
    cap = _write_family_lib(tmp_path, "cap.lib", section="cap_tt")
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {
                "process": [
                    {
                        "name": "tt",
                        "sections": ["tt", {"lib": "cap.lib", "section": "cap_tt"}],
                    }
                ]
            },
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )

    report = sim.run_sim(str(request))

    (deck,) = decks
    assert [line for line in deck.splitlines() if line.startswith(".lib")] == [
        f".lib {models_lib} tt",
        f".lib {cap} cap_tt",
    ]
    assert report["environment"]["models_lib_sha256"] == sim.sha256_file(
        str(models_lib)
    )


def test_run_sim_without_per_section_libs_omits_the_environment_field(
    tmp_path, monkeypatch
):
    _capture_decks(monkeypatch)
    request = _host_default_request(
        tmp_path, {"process": [{"name": "tt", "sections": ["tt"]}]}
    )
    report = sim.run_sim(str(request))
    assert "corner_section_libs" not in report["environment"]


def test_run_sim_missing_per_section_lib_fails_before_any_corner_runs(
    tmp_path, monkeypatch
):
    def must_not_run(*args, **kwargs):
        raise AssertionError("no corner may be dispatched")

    monkeypatch.setattr(sim.subprocess, "run", must_not_run)
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {
                "process": [
                    {
                        "name": "tt",
                        "sections": [{"lib": "nope.lib", "section": "cap_tt"}],
                    }
                ]
            },
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )

    with pytest.raises(sim.SimError, match="corner section library not found"):
        sim.run_sim(str(request))


def test_checkpoint_fingerprint_keys_on_per_section_lib_content(tmp_path):
    body = _write_body(tmp_path)
    cap = _write_family_lib(tmp_path, "cap.lib", section="cap_tt")
    kwargs = {
        "netlist_path": str(body),
        "models_lib": None,
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements_spec": [],
        "corner_points": [sim.CornerPoint(None, {}, 27)],
        "timeout_s": 10.0,
        "engine": "ngspice",
    }
    without = sim._checkpoint_fingerprint(**kwargs)
    # No per-section libraries keeps the pre-#2522 fingerprint identical.
    assert sim._checkpoint_fingerprint(**kwargs, corner_section_libs=()) == without
    first = sim._checkpoint_fingerprint(**kwargs, corner_section_libs=(str(cap),))
    assert first != without
    cap.write_text(".lib cap_tt\n.param cap_tt_scale=1.1\n.endl cap_tt\n")
    assert (
        sim._checkpoint_fingerprint(**kwargs, corner_section_libs=(str(cap),)) != first
    )
