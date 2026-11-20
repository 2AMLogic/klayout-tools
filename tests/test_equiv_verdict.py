"""The shared ``klt equiv`` verdict gate, unit and caller level (issue
#6852).

:func:`klayout_tools.equiv_verdict.require_equivalence` is the one place
``synthesize``'s and ``techmap``'s ``verify_equivalence`` gates project a
just-returned :func:`klayout_tools.equiv.run_equiv` report: every status
other than ``"equivalent"`` raises the caller's own error class, and an
``"equivalent"`` verdict is projected to the same six summary fields.

The exact messages below were captured from the two per-caller formatters
this issue deduplicated (characterization first: both callers produced
byte-identical detail/diagnostic/log formatting, differing only in the
wording that names the pair of netlists compared) -- so these tests pin
that the shared gate preserves each command's error text, per the issue's
own compatibility-risk note. The end-to-end gates (real Yosys proofs
behind ``run_synthesize``/``run_techmap``) stay in
``tests/test_synthesize.py`` (fake-report suite) and
``tests/test_synthesize_equiv_gate.py`` /
``tests/test_techmap_equiv_gate.py`` (real-engine suites); the
caller-level tests here cover the shapes those files leave unpinned --
notably the fallback text when details are absent -- without growing them
past their file-size-ratchet ceilings.
"""

from __future__ import annotations

import pytest

from klayout_tools import synthesize, techmap
from klayout_tools.equiv_verdict import require_equivalence
from klayout_tools.synthesize import SynthesizeError
from klayout_tools.techmap import TechmapError

_EQUIVALENT = {
    "status": "equivalent",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.05,
    "counterexample": None,
    "diagnostics": [],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": "/fake/.klt/equiv/equiv_netlist.v",
        "log_path": "/fake/.klt/equiv/equiv.log",
    },
}

_COUNTEREXAMPLE_FULL = {
    "status": "counterexample",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.05,
    "counterexample": {
        "diverging_outputs": ["result", "q"],
        "confirmed_by_simulation": True,
    },
    "diagnostics": [],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": None,
        "log_path": "/fake/.klt/equiv/equiv.log",
    },
}

# Same verdict with every optional detail absent: no diverging outputs, no
# confirmation, no proof log -- pins the fallback text, not just the happy
# formatting.
_COUNTEREXAMPLE_BARE = {
    "status": "counterexample",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.05,
    "counterexample": {"diverging_outputs": [], "confirmed_by_simulation": None},
    "diagnostics": [],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": None,
        "log_path": None,
    },
}

_INCONCLUSIVE_FULL = {
    "status": "inconclusive",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 60.02,
    "counterexample": None,
    "diagnostics": [
        {
            "severity": "error",
            "code": "process_timeout",
            "message": "yosys did not complete within 60.0s",
        }
    ],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": None,
        "log_path": "/fake/.klt/equiv/equiv.log",
    },
}

_INCONCLUSIVE_BARE = {
    "status": "inconclusive",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 60.02,
    "counterexample": None,
    "diagnostics": [],
    "artifacts": {
        "script_path": "/fake/.klt/equiv/equiv.ys",
        "netlist_path": None,
        "log_path": None,
    },
}


def _require_synthesis(report):
    """Run the shared gate exactly as ``synthesize``'s own gate calls it."""
    return require_equivalence(
        report,
        error_cls=SynthesizeError,
        counterexample_prefix=(
            "synthesized netlist is NOT equivalent to its source RTL"
        ),
        no_verdict_prefix="equivalence check against source RTL",
    )


def _require_techmap(report):
    """Run the shared gate exactly as ``techmap``'s own gate calls it."""
    return require_equivalence(
        report,
        error_cls=TechmapError,
        counterexample_prefix=(
            "mapped netlist is NOT equivalent to its pre-mapping generic netlist"
        ),
        no_verdict_prefix=("equivalence check against the pre-mapping generic netlist"),
    )


# --------------------------------------------------------------------------- #
# The shared gate itself
# --------------------------------------------------------------------------- #


def test_equivalent_report_projects_exactly_the_six_summary_fields():
    summary = _require_synthesis(_EQUIVALENT)
    assert summary == {
        "status": "equivalent",
        "engine": "yosys",
        "engine_version": "0.67+post",
        "timeout_s": 60.0,
        "elapsed_s": 0.05,
        "artifacts": _EQUIVALENT["artifacts"],
    }


def test_counterexample_raises_caller_error_class_with_exact_message():
    with pytest.raises(SynthesizeError) as exc_info:
        _require_synthesis(_COUNTEREXAMPLE_FULL)
    assert str(exc_info.value) == (
        "synthesized netlist is NOT equivalent to its source RTL "
        "(klt equiv reported 'counterexample'; "
        "diverging outputs: result, q; confirmed_by_simulation=True) "
        "-- see /fake/.klt/equiv/equiv.log for the full proof"
    )


def test_counterexample_without_details_uses_fallback_text():
    with pytest.raises(TechmapError) as exc_info:
        _require_techmap(_COUNTEREXAMPLE_BARE)
    assert str(exc_info.value) == (
        "mapped netlist is NOT equivalent to its pre-mapping generic netlist "
        "(klt equiv reported 'counterexample'; "
        "no diverging outputs reported; confirmed_by_simulation=None)"
    )


def test_no_verdict_raises_caller_error_class_with_first_diagnostic():
    with pytest.raises(TechmapError) as exc_info:
        _require_techmap(_INCONCLUSIVE_FULL)
    assert str(exc_info.value) == (
        "equivalence check against the pre-mapping generic netlist "
        "did not reach a verdict (klt equiv reported 'inconclusive'): "
        "yosys did not complete within 60.0s "
        "-- see /fake/.klt/equiv/equiv.log for the full proof"
    )


def test_no_verdict_without_diagnostics_uses_fallback_text():
    with pytest.raises(SynthesizeError) as exc_info:
        _require_synthesis(_INCONCLUSIVE_BARE)
    assert str(exc_info.value) == (
        "equivalence check against source RTL "
        "did not reach a verdict (klt equiv reported 'inconclusive'): "
        "no diagnostic detail"
    )


def test_error_class_is_wholly_caller_supplied():
    class _Marker(Exception):
        pass

    with pytest.raises(_Marker):
        require_equivalence(
            _INCONCLUSIVE_BARE,
            error_cls=_Marker,
            counterexample_prefix="x is NOT equivalent to y",
            no_verdict_prefix="equivalence check against y",
        )


# --------------------------------------------------------------------------- #
# Both real callers, through their own gates (``run_equiv`` stubbed to hand
# back the report under test -- the same technique ``tests/test_synthesize
# .py``'s fake-report suite uses, applied to the caller shapes that file
# leaves unpinned)
# --------------------------------------------------------------------------- #


def _run_synthesis_gate(monkeypatch, tmp_path, report):
    monkeypatch.setattr(
        synthesize, "run_equiv", lambda request, *, timeout_s=None: report
    )
    return synthesize._verify_synthesis_equivalence(
        synthesize_output_dir=str(tmp_path),
        resolved_sources=["/fake/gcd.v"],
        hdl_toplevel="gcd",
        netlist_path="/fake/gcd_synth.v",
        liberty_path="/fake/sky130.lib",
        timeout_s=None,
    )


def _run_techmap_gate(monkeypatch, tmp_path, report):
    monkeypatch.setattr(techmap, "run_equiv", lambda request, *, timeout_s=None: report)
    return techmap._verify_techmap_equivalence(
        request_dir=str(tmp_path),
        techmap_response={
            "top": "g",
            "generic_netlist_verilog_path": "/fake/g_generic.v",
            "mapped_netlist_path": "/fake/g_mapped.v",
        },
        liberty_path="/fake/minilib.lib",
        timeout_s=None,
    )


@pytest.mark.parametrize("gate", [_run_synthesis_gate, _run_techmap_gate])
def test_caller_success_keys_survive_the_shared_gate(gate, tmp_path, monkeypatch):
    summary = gate(monkeypatch, tmp_path, _EQUIVALENT)
    assert sorted(summary) == [
        "artifacts",
        "elapsed_s",
        "engine",
        "engine_version",
        "status",
        "timeout_s",
    ]
    assert summary["status"] == "equivalent"
    assert summary["engine"] == "yosys"


@pytest.mark.parametrize("gate", [_run_synthesis_gate, _run_techmap_gate])
def test_caller_counterexample_message_is_byte_identical(gate, tmp_path, monkeypatch):
    with pytest.raises(Exception) as exc_info:
        gate(monkeypatch, tmp_path, _COUNTEREXAMPLE_FULL)
    assert type(exc_info.value) in (SynthesizeError, TechmapError)
    assert str(exc_info.value).endswith(
        "(klt equiv reported 'counterexample'; "
        "diverging outputs: result, q; confirmed_by_simulation=True) "
        "-- see /fake/.klt/equiv/equiv.log for the full proof"
    )


def test_synthesis_gate_keeps_its_own_wording(tmp_path, monkeypatch):
    with pytest.raises(SynthesizeError, match="its source RTL") as exc_info:
        _run_synthesis_gate(monkeypatch, tmp_path, _COUNTEREXAMPLE_FULL)
    assert "synthesized netlist is NOT equivalent" in str(exc_info.value)


def test_techmap_gate_keeps_its_own_wording(tmp_path, monkeypatch):
    with pytest.raises(TechmapError, match="pre-mapping generic netlist"):
        _run_techmap_gate(monkeypatch, tmp_path, _COUNTEREXAMPLE_BARE)


def test_synthesis_gate_inconclusive_is_hard_failure(tmp_path, monkeypatch):
    with pytest.raises(SynthesizeError, match="did not reach a verdict"):
        _run_synthesis_gate(monkeypatch, tmp_path, _INCONCLUSIVE_FULL)


def test_techmap_gate_inconclusive_is_hard_failure(tmp_path, monkeypatch):
    with pytest.raises(TechmapError, match="did not reach a verdict"):
        _run_techmap_gate(monkeypatch, tmp_path, _INCONCLUSIVE_BARE)
