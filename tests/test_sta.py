"""Hermetic tests for `klayout_tools.sta` -- the thin Python side of the
`klt_statime_native` boundary backing `klt synthesize`'s `sta` field (issue
#925, Epic #704 Phase 3).

Two tiers, mirroring `tests/test_synthesize.py`'s own layering:

- **Hermetic (always runs, no Rust toolchain and no PDK)**: the reshaping
  contract and every failure mode, with a stub extension module injected via
  `sys.modules`. These are what keep the "degrade to `None`, never fabricate
  a number" discipline honest on a machine that has never built the crate --
  including CI's own `test` job, which installs no Rust.
- **Extension-backed (skips without `klt_statime_native`)**: the real pyo3
  boundary's error contract. The *accuracy* half of the boundary lives in
  `tests/test_sta_corpus.py`, which additionally needs a real sky130 liberty.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types

import pytest

from klayout_tools.sta import (
    DEFAULT_INPUT_TRANSITION_NS,
    DEFAULT_OUTPUT_LOAD_PF,
    StaError,
    compute_critical_path,
)

_HAVE_STATIME_NATIVE = importlib.util.find_spec("klt_statime_native") is not None


#: A minimal but structurally real `sta::StaResult` payload -- the exact
#: shape `native/statime/src/sta.rs` serialises, trimmed to two hops.
_FAKE_RESULT = {
    "top": "gcd",
    "num_cells": 353,
    "num_nets": 388,
    "worst_path": {
        "startpoint": "_640_/Q",
        "startpoint_kind": "flip-flop (rising edge)",
        "endpoint": "_035_",
        "endpoint_kind": "internal net",
        "delay_ns": 4.4642972825718,
        "hops": [
            {
                "point": "_640_/Q",
                "cell": "sky130_fd_sc_hd__dfrtp_1",
                "edge": "fall",
                "arrival_ns": 0.41273907228437867,
                "slew_ns": 0.0925558496296774,
            },
            {
                "point": "_377_/Y",
                "cell": "sky130_fd_sc_hd__xnor2_1",
                "edge": "fall",
                "arrival_ns": 0.6083993722243425,
                "slew_ns": 0.11961144661859305,
            },
        ],
    },
    "worst_reg_to_reg_path": None,
}


def _install_stub(monkeypatch, impl):
    """Inject a fake `klt_statime_native` whose `critical_path_json` is
    `impl`, so the hermetic tier exercises `sta.py`'s own logic without a
    built extension (and without depending on whether one happens to be
    installed on this machine)."""
    module = types.ModuleType("klt_statime_native")
    module.critical_path_json = impl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "klt_statime_native", module)
    return module


# ---------------------------------------------------------------------------
# Hermetic tier -- no Rust toolchain, no PDK
# ---------------------------------------------------------------------------


def test_boundary_condition_defaults_match_the_spike():
    """The uniform boundary condition is deliberately the spike's own
    (`native/statime/README.md` "How the comparison was run": 0.05 ns input
    transition, 0.03 pF output load) -- **not** anything derived from
    `synthesize.py`'s ABC `_ABC_CONSTR_INPUTS` table, which is a different
    knob in different units. Pinned here because silently drifting these
    would invalidate the accuracy comparison `tests/test_sta_corpus.py`
    gates on without any test failing."""
    assert DEFAULT_INPUT_TRANSITION_NS == 0.05
    assert DEFAULT_OUTPUT_LOAD_PF == 0.03


def test_compute_critical_path_reshapes_the_native_result(monkeypatch):
    """The response echoes its own provenance and boundary condition
    (`source`/`input_transition_ns`/`output_load_pf`) alongside the engine's
    own fields, and passes the request's arguments through unchanged."""
    seen = {}

    def _impl(netlist_path, liberty_path, top, input_transition_ns, output_load_pf):
        seen.update(
            netlist_path=netlist_path,
            liberty_path=liberty_path,
            top=top,
            input_transition_ns=input_transition_ns,
            output_load_pf=output_load_pf,
        )
        return json.dumps(_FAKE_RESULT)

    _install_stub(monkeypatch, _impl)

    result = compute_critical_path("/tmp/gcd_synth.v", "/tmp/sky130.lib", "gcd")

    assert seen == {
        "netlist_path": "/tmp/gcd_synth.v",
        "liberty_path": "/tmp/sky130.lib",
        "top": "gcd",
        "input_transition_ns": 0.05,
        "output_load_pf": 0.03,
    }
    assert result["source"] == "klt_statime_native"
    assert result["input_transition_ns"] == 0.05
    assert result["output_load_pf"] == 0.03
    assert result["top"] == "gcd"
    assert result["num_cells"] == 353
    assert result["num_nets"] == 388
    assert result["worst_path"]["delay_ns"] == pytest.approx(4.4642972825718)
    # The limiting path's cells -- the half of this report `timing` (ABC's
    # own `stime -p` line) has no way to produce at all.
    assert [hop["cell"] for hop in result["worst_path"]["hops"]] == [
        "sky130_fd_sc_hd__dfrtp_1",
        "sky130_fd_sc_hd__xnor2_1",
    ]
    # `mult8`-style purely combinational design: no register-to-register
    # path exists, and that is reported as `None`, not omitted.
    assert "worst_reg_to_reg_path" in result
    assert result["worst_reg_to_reg_path"] is None


def test_compute_critical_path_forwards_explicit_boundary_condition(monkeypatch):
    """The defaults are defaults, not hard-coded: a caller (e.g. #926's
    restructuring loop) can override them, and the override is echoed back
    rather than the default being reported."""
    _install_stub(
        monkeypatch,
        lambda *args: json.dumps(_FAKE_RESULT),
    )

    result = compute_critical_path(
        "/tmp/n.v",
        "/tmp/l.lib",
        "gcd",
        input_transition_ns=0.1,
        output_load_pf=0.05,
    )

    assert result["input_transition_ns"] == 0.1
    assert result["output_load_pf"] == 0.05


def test_missing_extension_raises_sta_error(monkeypatch):
    """A Rust toolchain is optional for every other `klt synthesize` caller,
    so a missing extension must be a clean, actionable `StaError` (which
    `_read_sta_timing` turns into `sta: null`) -- never an `ImportError`
    escaping into the middle of a synthesis run."""
    # `None` in `sys.modules` is CPython's own "this import must fail"
    # sentinel -- `import klt_statime_native` raises `ImportError` regardless
    # of whether the extension is actually built on this machine, so this
    # test asserts the same thing in both environments.
    monkeypatch.setitem(sys.modules, "klt_statime_native", None)

    with pytest.raises(StaError, match="not installed"):
        compute_critical_path("/tmp/n.v", "/tmp/l.lib", "gcd")


def test_native_value_error_becomes_sta_error(monkeypatch):
    """Every native-side failure (unreadable file, malformed liberty,
    unknown cell type, or a parser panic `lib.rs`'s `catch_unwind` converts)
    arrives as `ValueError` and must be re-raised as `StaError` -- the one
    exception type `synthesize.py` catches to degrade to `sta: null`."""

    def _impl(*args):
        raise ValueError("cell type 'sky130_fd_sc_hd__nonesuch' has no liberty entry")

    _install_stub(monkeypatch, _impl)

    with pytest.raises(StaError, match="no liberty entry"):
        compute_critical_path("/tmp/n.v", "/tmp/l.lib", "gcd")


def test_synthesize_reports_sta_none_when_analysis_fails(monkeypatch):
    """`klt synthesize`'s stage wrapper is the "never fabricate a number"
    boundary: a `StaError` becomes `None`, mirroring `_read_abc_timing`'s
    own discipline, so an absent extension can never turn a successful
    synthesis run into a failure (issue #925's additive-stage contract)."""
    from klayout_tools.synthesize import _read_sta_timing

    def _raise(*args, **kwargs):
        raise StaError("the klt_statime_native extension is not installed")

    monkeypatch.setattr("klayout_tools.synthesize.compute_critical_path", _raise)

    assert _read_sta_timing("/tmp/n.v", "/tmp/l.lib", "gcd") is None


def test_synthesize_stage_passes_through_a_real_result(monkeypatch):
    """The complement of the test above: when the engine does answer, the
    stage wrapper publishes the reshaped result verbatim -- it adds no
    fields of its own and drops none."""
    from klayout_tools.synthesize import _read_sta_timing

    _install_stub(monkeypatch, lambda *args: json.dumps(_FAKE_RESULT))

    result = _read_sta_timing("/tmp/n.v", "/tmp/l.lib", "gcd")

    assert result is not None
    assert result["source"] == "klt_statime_native"
    assert result["worst_path"]["delay_ns"] == pytest.approx(4.4642972825718)


# ---------------------------------------------------------------------------
# Extension-backed tier -- needs the built pyo3 extension, but no PDK
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _HAVE_STATIME_NATIVE,
    reason=(
        "klt_statime_native is not built -- run `uv sync --group statime` "
        "(or `maturin develop --release` in native/statime/)"
    ),
)
def test_real_extension_missing_file_raises_sta_error_not_panic():
    """Against the **real** extension: an unreadable input must surface as
    `StaError` (via `ValueError`), never a `pyo3_runtime.PanicException`.
    `native/statime`'s liberty/netlist parsers `panic!` on unexpected input
    -- fine for the standalone CLI, fatal for `klt synthesize`'s
    best-effort stage -- so `lib.rs` wraps the analysis in `catch_unwind`;
    this test is what keeps that wrapper from being removed silently."""
    with pytest.raises(StaError):
        compute_critical_path(
            "/nonexistent/netlist.v", "/nonexistent/liberty.lib", "top"
        )


@pytest.mark.skipif(
    not _HAVE_STATIME_NATIVE,
    reason="klt_statime_native is not built -- run `uv sync --group statime`",
)
def test_real_extension_malformed_input_raises_sta_error_not_panic(tmp_path):
    """The `catch_unwind` path proper: real files whose *contents* are
    garbage, which drives the hand-written parsers into a `panic!` rather
    than a `Result::Err`."""
    netlist = tmp_path / "garbage.v"
    netlist.write_text("this is not verilog at all {{{", encoding="utf-8")
    liberty = tmp_path / "garbage.lib"
    liberty.write_text("nor is this liberty ;;;", encoding="utf-8")

    with pytest.raises(StaError):
        compute_critical_path(str(netlist), str(liberty), "top")
