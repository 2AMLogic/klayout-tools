"""``run_pex``-level tests for ``klt pex``'s multi-top mode (issue #5494):
the flag refusals and the per-top pre-flight diagnostics.

``run_sim`` is monkeypatched throughout, so every test here runs on a machine
without ngspice. The real end-to-end runs live in
``test_pex_multi_top_e2e.py``; the pure helpers and the shared two-top
fixtures live in ``test_pex_multi_top.py``.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.pex import PexError, run_pex
from klayout_tools.pex_dry_run import run_pex_dry_run
from klayout_tools.pex_multi_top_flags import MULTI_TOP_UNSUPPORTED_CODE
from test_pex import (
    _PROMOTED_TAP_LABELS,
    _make_sky130_poly_resistor_layout,
    _measurement,
    _write_gds,
    _write_request,
    _write_schematic_dut,
    _write_testbench,
)
from test_pex_dry_run import _REPORT_KEYS
from test_pex_multi_top import (
    _make_two_top_resistor_layout,
    _two_top_request,
    _write_two_top_testbench,
)

# --- refusing what this first version does not define ---------------------- #


#: Each `(run_pex kwarg, flag spelling, takes_testbenches)` refused with two
#: or more `--top` values (issue #5494). Every one is single-top by
#: construction -- it takes one cell name, names one net of one extraction, or
#: records one request a list-valued top would not round-trip -- so the first
#: version refuses it by name rather than inventing a per-top syntax for each.
#: Support for any individual flag is additive later; see `docs/cli/pex.md`.
#:
#: `takes_testbenches` is `False` for the two modes that *replace* the
#: testbench set (`--measure-command`, `--extract-only`): passing one anyway
#: trips their own long-standing mutual-exclusion error (issues #2478/#3739)
#: before this refusal is reached, which would make the test assert the wrong
#: message.
_MULTI_TOP_REFUSED_FLAGS = (
    ({"flatten_schematic": True}, "--flatten-schematic", True),
    ({"device_only_leg": True}, "--device-only-leg", True),
    ({"tie_nets": [("vsubs", "0")]}, "--tie-net", True),
    ({"tie_declared_pins": True}, "--tie-declared-pins", True),
    ({"mom_rlc_net": "RA"}, "--mom-rlc-net", True),
    (
        {"measure_command": ["true"], "reference_netlist": "dut.spice"},
        "--measure-command",
        False,
    ),
    ({"extract_only": True}, "--extract-only", False),
    ({"submit_only": True, "backend": "batch"}, "--submit-only", True),
    ({"attach": "pex-submit.json", "backend": "batch"}, "--attach", True),
    ({"resume": True}, "--resume", True),
)


@pytest.mark.parametrize(
    ("kwargs", "flag", "takes_testbenches"),
    _MULTI_TOP_REFUSED_FLAGS,
    ids=[flag.lstrip("-") for _kwargs, flag, _tb in _MULTI_TOP_REFUSED_FLAGS],
)
def test_run_pex_multi_top_refuses_unsupported_flag(
    tmp_path, two_top_layout, monkeypatch, kwargs, flag, takes_testbenches
):
    """Refused *before extraction*, by name -- confirmed by making
    `run_extract` unreachable, so the refusal cannot be mistaken for a
    failure discovered after paying for an extraction."""
    import klayout_tools.pex as pex_module

    def _unreachable_run_extract(*_args, **_kwargs):
        raise AssertionError(
            f"run_extract must not be called: {flag} is refused with two tops"
        )

    monkeypatch.setattr(pex_module, "run_extract", _unreachable_run_extract)
    request = _two_top_request(tmp_path)
    if "reference_netlist" in kwargs:
        kwargs = {**kwargs, "reference_netlist": str(tmp_path / "schematic_dut.spice")}

    with pytest.raises(PexError) as excinfo:
        run_pex(
            two_top_layout,
            [str(request)] if takes_testbenches else [],
            "sky130",
            top=["RESA", "RESB"],
            **kwargs,
        )
    message = str(excinfo.value)
    assert flag in message
    assert "RESA, RESB" in message
    assert excinfo.value.code == MULTI_TOP_UNSUPPORTED_CODE


@pytest.mark.parametrize(
    ("kwargs", "flag", "takes_testbenches"),
    _MULTI_TOP_REFUSED_FLAGS,
    ids=[flag.lstrip("-") for _kwargs, flag, _tb in _MULTI_TOP_REFUSED_FLAGS],
)
def test_run_pex_single_top_does_not_refuse_those_flags(
    tmp_path, resistor_layout, monkeypatch, kwargs, flag, takes_testbenches
):
    """The refusals are scoped to two or more tops: a single-top run must not
    acquire a new refusal from this change. Each flag gets at least as far as
    the extraction it always did -- whatever it then does is unchanged, and
    is covered by that flag's own tests."""
    import klayout_tools.pex as pex_module

    reached = {"extract": False}

    def _stop_at_extraction(*_args, **_kwargs):
        reached["extract"] = True
        raise AssertionError("stop here")

    monkeypatch.setattr(pex_module, "run_extract", _stop_at_extraction)
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    if "reference_netlist" in kwargs:
        kwargs = {**kwargs, "reference_netlist": str(dut)}

    with pytest.raises((AssertionError, PexError)) as excinfo:
        run_pex(
            resistor_layout,
            [str(request)] if takes_testbenches else [],
            "sky130",
            top="RES",
            **kwargs,
        )
    # Either the extraction was reached (the flag is accepted on one top), or
    # the flag's own *pre-existing* refusal fired -- never this change's.
    assert reached["extract"] or getattr(excinfo.value, "code", None) != (
        MULTI_TOP_UNSUPPORTED_CODE
    )


def test_run_pex_rejects_a_repeated_top_before_extraction(
    tmp_path, two_top_layout, monkeypatch
):
    import klayout_tools.pex as pex_module

    monkeypatch.setattr(
        pex_module,
        "run_extract",
        lambda *_a, **_k: pytest.fail("run_extract must not be called"),
    )
    request = _two_top_request(tmp_path)

    with pytest.raises(PexError, match="more than once"):
        run_pex(two_top_layout, [str(request)], "sky130", top=["RESA", "RESA"])


# --- the per-top pre-flight, before any simulation ------------------------- #


def test_run_pex_multi_top_missing_subckt_errors_before_simulating(
    tmp_path, two_top_layout, monkeypatch
):
    """A schematic DUT lacking a `.SUBCKT` for one of the tops is an error
    naming that top, raised before any `klt sim` call -- confirmed by making
    `run_sim` unreachable (the extraction still runs: the check compares the
    DUT against the `--top` list, which needs the extraction's output to have
    been written first)."""
    import klayout_tools.pex as pex_module

    monkeypatch.setattr(
        pex_module,
        "run_sim",
        lambda *_a, **_k: pytest.fail(
            "run_sim must not be called when a --top is unmodelled by the DUT"
        ),
    )
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(".SUBCKT RESA RA RB\nR1 RA RB 289.2\n.ENDS RESA\n")
    tb = _write_two_top_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb, node="RD")

    with pytest.raises(PexError) as excinfo:
        run_pex(
            two_top_layout,
            [str(request)],
            "sky130",
            top=["RESA", "RESB"],
            output=str(tmp_path / "combined.spice"),
            artifacts_dir=str(tmp_path / "art"),
        )
    assert "RESB" in str(excinfo.value)
    assert excinfo.value.code == MULTI_TOP_UNSUPPORTED_CODE


def test_run_pex_multi_top_pin_count_mismatch_on_the_second_top(tmp_path, monkeypatch):
    """A mismatch on the *second* top is reported in the singular top-level
    `pin_count_mismatch` (naming that subcircuit), skips the extracted leg,
    and yields `status: "error"` exactly as a single-top mismatch does --
    confirmed by allowing only the schematic-side `run_sim` call through.

    `RESB` alone carries two extra labelled taps, so only its extraction
    promotes pins the schematic DUT does not declare; `RESA`'s interfaces
    agree. That is what makes this a test of the *first-firing* reduction
    rather than of the diagnostic itself.
    """
    import klayout_tools.pex as pex_module

    layout = _write_gds(
        _make_two_top_resistor_layout(second_taps=_PROMOTED_TAP_LABELS),
        tmp_path / "two_tops_taps.gds",
    )
    schematic_report = {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
        ],
        "corner_count": 1,
        "measurements": [{"name": "vout"}],
    }
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return schematic_report
        raise AssertionError(
            "the extracted-side run_sim call must not happen once a per-top "
            "pre-flight pin_count_mismatch has explained why it would be "
            "meaningless"
        )

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    request = _two_top_request(tmp_path)

    report = run_pex(
        layout,
        [str(request)],
        "sky130",
        top=["RESA", "RESB"],
        output=str(tmp_path / "combined.spice"),
        artifacts_dir=str(tmp_path / "art"),
    )

    assert calls["n"] == 1
    assert report["status"] == "error"
    mismatch = report["pin_count_mismatch"]
    assert mismatch is not None
    # The block is already self-identifying about which subcircuit it is.
    assert mismatch["subcircuit"] == "RESB"
    assert mismatch["schematic"]["pins"] == ["RC", "RD"]
    assert mismatch["extracted"]["pin_count"] == 4
    assert mismatch["ngspice_message"] is None
    # Per-top attribution: the clean top is clean, the mismatching one is not.
    by_top = {entry["top"]: entry for entry in report["tops"]}
    assert by_top["RESA"]["pin_count_mismatch"] is None
    assert by_top["RESB"]["pin_count_mismatch"]["subcircuit"] == "RESB"
    assert all(row["status"] == "error" for row in report["delta"])
    assert all(row["extracted_value"] is None for row in report["delta"])


def test_cli_pex_multi_top_pin_count_mismatch_exits_4(tmp_path, capsys, monkeypatch):
    """Exit code parity with a single-top mismatch (`EXIT_CHECK_FAILED`), via
    the real CLI, so the multi-top path is not a special case for a caller
    grading on the exit status."""
    import klayout_tools.pex as pex_module

    layout = _write_gds(
        _make_two_top_resistor_layout(second_taps=_PROMOTED_TAP_LABELS),
        tmp_path / "two_tops_taps.gds",
    )
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "corners": [
                    {"corner_id": "tt", "measurements": [_measurement(1.4)]},
                ],
                "corner_count": 1,
                "measurements": [{"name": "vout"}],
            }
        raise AssertionError("the extracted side must be skipped")

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    request = _two_top_request(tmp_path)

    exit_code = main(
        [
            "pex",
            str(layout),
            str(request),
            "--deck",
            "sky130",
            "--top",
            "RESA",
            "--top",
            "RESB",
            "-o",
            str(tmp_path / "combined.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )
    assert exit_code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["pin_count_mismatch"]["subcircuit"] == "RESB"
    assert [entry["top"] for entry in payload["tops"]] == ["RESA", "RESB"]


@pytest.fixture
def bench(tmp_path):
    """A valid single-top `klt pex` invocation's inputs -- the same shape
    `test_pex_dry_run.py`'s own `bench` builds, defined locally because
    pytest resolves fixtures by name (see `two_top_layout` below)."""
    layout = _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")
    dut = _write_schematic_dut(tmp_path / "dut.spice")
    testbench = _write_testbench(tmp_path / "tb.spice", dut)
    request = _write_request(tmp_path / "req.json", testbench)
    return {
        "root": tmp_path,
        "layout": str(layout),
        "dut": str(dut),
        "testbench": str(testbench),
        "request": str(request),
    }


@pytest.fixture
def resistor_layout(tmp_path):
    """The single-top drawn poly resistor (`RES`) -- defined locally for the
    same reason as `two_top_layout`: pytest resolves fixtures by name."""
    return _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")


@pytest.fixture
def two_top_layout(tmp_path):
    """A stream with two drawn top cells, `RESA` and `RESB` (issue #5494).

    Defined per module rather than imported: pytest resolves fixtures by
    name, so importing one trips `F811` on every test that takes it as a
    parameter. The layout builder itself is shared.
    """
    return _write_gds(_make_two_top_resistor_layout(), tmp_path / "two_tops.gds")


def test_cli_pex_multi_top_refusal_is_a_clean_error_envelope(
    tmp_path, two_top_layout, capsys
):
    request = _two_top_request(tmp_path)
    exit_code = main(
        [
            "pex",
            str(two_top_layout),
            str(request),
            "--deck",
            "sky130",
            "--top",
            "RESA",
            "--top",
            "RESB",
            "--flatten-schematic",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == MULTI_TOP_UNSUPPORTED_CODE
    assert "--flatten-schematic" in payload["error"]["message"]


# --------------------------------------------------------------------------- #
# `--dry-run` (issue #5464) is refused with two or more tops
# --------------------------------------------------------------------------- #


def test_dry_run_refuses_two_or_more_tops(bench):
    """`klt pex --top` became repeatable in issue #5494, so this entry point
    now receives a list. A dry run is **refused** with two or more tops
    rather than silently validating only one of them: its report is singular
    (`top`, one resolved cell name) and `_extraction_preflight` resolves one
    top cell, so describing a multi-top request properly is a second additive
    change to this command's own contract, tracked in #5511."""
    with pytest.raises(PexError) as excinfo:
        run_pex_dry_run(
            bench["layout"],
            [bench["request"]],
            "sky130",
            top=["RESA", "RESB"],
        )
    message = str(excinfo.value)
    assert "--dry-run" in message
    assert "RESA, RESB" in message
    # It says what to do instead.
    assert "dry-run each top on its own" in message
    assert excinfo.value.code == MULTI_TOP_UNSUPPORTED_CODE


def test_dry_run_rejects_a_repeated_top(bench):
    with pytest.raises(PexError, match="more than once"):
        run_pex_dry_run(
            bench["layout"], [bench["request"]], "sky130", top=["RES", "RES"]
        )


@pytest.mark.parametrize("top", [None, "RES", ["RES"]])
def test_dry_run_single_top_is_unchanged_by_the_repeatable_flag(bench, top):
    """Zero or one `--top`, in any of the three spellings the library now
    accepts (absent, a bare string, a one-entry list), produces the same
    report -- the dry run must not have acquired a new refusal."""
    report = run_pex_dry_run(bench["layout"], [bench["request"]], "sky130", top=top)
    assert report["status"] == "valid"
    assert set(report) == _REPORT_KEYS
    assert report["top"] == "RES"


def test_cli_dry_run_multi_top_refusal_exits_1(bench, capsys):
    exit_code = main(
        [
            "pex",
            bench["layout"],
            bench["request"],
            "--deck",
            "sky130",
            "--dry-run",
            "--top",
            "RESA",
            "--top",
            "RESB",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == MULTI_TOP_UNSUPPORTED_CODE
    assert "--dry-run" in payload["error"]["message"]
