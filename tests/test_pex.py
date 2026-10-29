"""Tests for `klt pex` and the `klayout_tools.pex` library.

Two tiers, mirroring `tests/test_sim.py`/`tests/test_extract.py`:

- **Unit tests** (the majority) exercise the `.include`/`.inc` DUT-swap
  mechanics, the delta-row computation (status/`delta_pct`), and every error
  path that is cheap to trigger (bad testbench, no `.include` directive,
  testbenches disagreeing on their schematic DUT) without ever invoking
  `ngspice` -- these always run, everywhere.
- **Integration tests** (`@_SKIP_NO_NGSPICE`) run a real, self-contained
  end-to-end `klt pex`: a synthetic sky130 poly-resistor layout (the same
  fixture shape `tests/test_extract.py`'s drawn-resistor tests use, so no
  real PDK data is vendored here -- see that file's `_make_poly_resistor_
  layout`), extracted with `--parasitics`, re-simulated against a
  hand-written schematic DUT via a thin `.include`-based testbench. CI
  installs `ngspice` via the package manager (`.github/workflows/ci.yml`)
  so these always run there; they skip with a clear reason on a dev machine
  without it.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import sim
from klayout_tools.cli import main
from klayout_tools.pex import (
    BODY_BIAS_BIASED,
    BODY_BIAS_UNBIASED,
    PexError,
    _apply_tie_nets,
    _body_bias_report,
    _build_coverage,
    _build_delta_rows,
    _check_dut_declares_a_circuit,
    _delta_pct,
    _dut_declares_a_circuit,
    _find_dut_include,
    _flat_dut_mismatch,
    _model_mismatch,
    _normalize_measurement_document,
    _pin_count_mismatch,
    _pin_count_mismatch_from_report,
    _pin_order_mismatch,
    _prepare_extracted_request,
    _reference_is_hierarchical,
    _rewrite_dut_include,
    _row_status,
    _status_from_coverage,
    _subckt_interfaces,
    _unextracted_delta_rows,
    _validate_tie_nets,
    run_pex,
)

#: `KLT_SKIP_NGSPICE_TESTS=1` (local-only, set by `npm run check:ci` -- never
#: by CI) opts a host with ngspice installed out of this slow tier too; see
#: `tests/test_extract.py`'s `HAVE_NGSPICE` for the full rationale (issue #1651).
HAVE_NGSPICE = shutil.which("ngspice") is not None and (
    os.environ.get("KLT_SKIP_NGSPICE_TESTS") != "1"
)
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)


# --------------------------------------------------------------------------- #
# `.include`/`.inc` DUT-swap mechanics
# --------------------------------------------------------------------------- #


def test_find_dut_include_quoted():
    body = '.include "dut.spice"\nVdd RA 0 DC 1.8\n'
    index, path = _find_dut_include(body, "/work")
    assert index == 0
    assert path == "/work/dut.spice"


def test_find_dut_include_unquoted_and_case_insensitive():
    body = ".INCLUDE dut.spice\nVdd RA 0 DC 1.8\n"
    index, path = _find_dut_include(body, "/work")
    assert index == 0
    assert path == "/work/dut.spice"


def test_find_dut_include_inc_spelling():
    body = ".inc 'dut.spice'\nVdd RA 0 DC 1.8\n"
    index, path = _find_dut_include(body, "/work")
    assert index == 0
    assert path == "/work/dut.spice"


def test_find_dut_include_not_first_line():
    body = "* a comment\n.param vdd=1.8\n.include dut.spice\nVdd RA 0 DC {vdd}\n"
    index, path = _find_dut_include(body, "/work")
    assert index == 2
    assert path == "/work/dut.spice"


def test_find_dut_include_two_directives_raises(tmp_path):
    """Issue #1030: a testbench that also `.include`s a PDK switch-parameter
    file ahead of its DUT used to have that *first* file silently swapped
    for the extracted netlist (leaving the real DUT in place, and reporting
    the wrong `reference_netlist`). It is now a hard error naming every
    matched line."""
    body = (
        "* two includes -- which one is the DUT?\n"
        '.include "design.ngspice"\n'
        '.include "dut.spice"\n'
        "Vdd RA 0 DC 1.8\n"
    )
    with pytest.raises(PexError) as excinfo:
        _find_dut_include(body, "/work")

    message = str(excinfo.value)
    assert "2 `.include`/`.inc` directives" in message
    # Both matched lines are named, with 1-based line numbers, so a caller
    # can see exactly which directives were ambiguous.
    assert 'line 2: `.include "design.ngspice"`' in message
    assert 'line 3: `.include "dut.spice"`' in message
    # Issue #2871: shared content must never be steered into the DUT file,
    # since the DUT include is swapped out on the extracted side.
    assert "fold any other" not in message
    assert "into the DUT netlist itself" not in message
    assert "schematic side only" in message
    assert "Inline content both sides need into the testbench body" in message


def test_find_dut_include_two_directives_mixed_spellings_raises():
    body = ".INC params.spice\n.include dut.spice\n"
    with pytest.raises(PexError, match="2 `.include`/`.inc` directives"):
        _find_dut_include(body, "/work")


def test_find_dut_include_absolute_path_unchanged():
    body = '.include "/abs/dut.spice"\n'
    _index, path = _find_dut_include(body, "/work")
    assert path == "/abs/dut.spice"


def test_find_dut_include_missing_raises():
    body = "R1 RA RB 289.2\nVdd RA 0 DC 1.8\n"
    with pytest.raises(PexError, match=r"\.include"):
        _find_dut_include(body, "/work")


def test_rewrite_dut_include_replaces_only_that_line():
    body = '.include "dut.spice"\n.model res_generic_po r\nVdd RA 0 DC 1.8\n'
    index, _path = _find_dut_include(body, "/work")
    rewritten = _rewrite_dut_include(body, index, "/extracted/top.spice")
    assert rewritten == (
        '.include "/extracted/top.spice"\n.model res_generic_po r\nVdd RA 0 DC 1.8\n'
    )


def test_rewrite_dut_include_preserves_no_trailing_newline():
    body = '.model res_generic_po r\n.include "dut.spice"'
    index, _path = _find_dut_include(body, "/work")
    rewritten = _rewrite_dut_include(body, index, "/extracted/top.spice")
    assert rewritten == '.model res_generic_po r\n.include "/extracted/top.spice"'


# --------------------------------------------------------------------------- #
# DUT-include plausibility check (Gap 1, issue #1255)
# --------------------------------------------------------------------------- #


def test_dut_declares_a_circuit_true_for_subckt_wrapped():
    assert _dut_declares_a_circuit(".SUBCKT RES RA RB\nR1 RA RB 289.2\n.ENDS RES\n")


def test_dut_declares_a_circuit_true_for_empty_subckt():
    """Just the header/wrapper is enough -- `_subckt_interfaces` only parses
    headers, and a schematic DUT with a declared interface but (for
    whatever reason) no devices inside it is still "the DUT", not a
    parameter file."""
    assert _dut_declares_a_circuit(".SUBCKT TOP A B\n.ENDS TOP\n")


def test_dut_declares_a_circuit_true_for_flat_device_lines():
    """No `.SUBCKT` wrapper at all, but a real device card -- the flat-DUT
    shape (see `_flat_dut_mismatch`, Gap 2) is still plausibly a DUT."""
    assert _dut_declares_a_circuit("R1 RA RB 289.2\n")


def test_dut_declares_a_circuit_false_for_param_model_only():
    """The Gap 1 failure shape: a shared corner/parameter file with no
    circuit content of its own."""
    text = (
        "* corner selection\n"
        ".param sw_stat_mismatch=0\n"
        ".model res_generic_po r\n"
        ".global vsubs\n"
    )
    assert not _dut_declares_a_circuit(text)


def test_dut_declares_a_circuit_false_for_blank_and_comments_only():
    assert not _dut_declares_a_circuit("\n* just a comment\n\n")


def test_dut_declares_a_circuit_false_for_empty_text():
    assert not _dut_declares_a_circuit("")


def test_check_dut_declares_a_circuit_passes_for_real_dut(tmp_path):
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(".SUBCKT RES RA RB\nR1 RA RB 289.2\n.ENDS RES\n")
    # Does not raise.
    _check_dut_declares_a_circuit("testbench.spice", str(dut))


def test_check_dut_declares_a_circuit_raises_for_param_only_file(tmp_path):
    corner = tmp_path / "design.ngspice"
    corner.write_text(".param sw_stat_mismatch=0\n.model res_generic_po r\n")

    with pytest.raises(PexError) as excinfo:
        _check_dut_declares_a_circuit("testbench.spice", str(corner))

    message = str(excinfo.value)
    assert str(corner) in message
    assert "declares no `.SUBCKT`" in message


def test_check_dut_declares_a_circuit_raises_for_unreadable_file(tmp_path):
    missing = tmp_path / "does-not-exist.spice"
    with pytest.raises(PexError, match="could not be read"):
        _check_dut_declares_a_circuit("testbench.spice", str(missing))


# --------------------------------------------------------------------------- #
# `body_bias` (issue #1983): whether the extracted netlist this run
# re-simulated actually had a DC bias path for every device body.
# --------------------------------------------------------------------------- #


def test_body_bias_report_biased_when_extraction_found_no_floating_bodies():
    """The clean case: `klt extract` reported no unbiased PMOS body, so the
    post-layout numbers are comparable to the schematic leg -- and the block
    says so positively rather than merely staying empty."""
    assert _body_bias_report({"unbiased_pmos_body_nets": []}) == {
        "status": BODY_BIAS_BIASED,
        "unbiased_device_count": 0,
        "unbiased_nets": [],
        "unbiased_pmos_body_nets": [],
    }


def test_body_bias_report_biased_for_extraction_without_the_field():
    """An extraction report that carries no `unbiased_pmos_body_nets` key at
    all (nothing in this repo produces one, but the reduction must not
    explode on a hand-built report) reads as the clean case."""
    assert _body_bias_report({})["status"] == BODY_BIAS_BIASED


def test_body_bias_report_unbiased_names_devices_and_nets():
    """Issue #1983: a PMOS body on an anonymous, deck-synthesized net has no
    DC bias path at all, which per `docs/cli/extract.md` makes a
    resimulation of this netlist physically wrong -- `klt pex` now says so in
    its own envelope, instead of that fact living only in an extraction
    report a committed `klt pex` evidence record never carried.

    The entries are carried through verbatim (same `{"device", "net"}` shape
    `klt extract` uses), and `unbiased_nets` de-duplicates them so two PMOS
    devices sharing one floating well read as one net, not two."""
    entries = [
        {"device": "$1", "net": "\\$5"},
        {"device": "$2", "net": "\\$5"},
        {"device": "$3", "net": "\\$7"},
    ]
    report = _body_bias_report({"unbiased_pmos_body_nets": entries})

    assert report["status"] == BODY_BIAS_UNBIASED
    assert report["unbiased_device_count"] == 3
    assert report["unbiased_nets"] == ["\\$5", "\\$7"]
    assert report["unbiased_pmos_body_nets"] == entries


# --------------------------------------------------------------------------- #
# `--tie-net` (issue #4245): a caller-declared tie of a reported-unbiased net.
# --------------------------------------------------------------------------- #

_TIE_ENTRIES = [
    {"device": "M1", "net": "\\$5"},
    {"device": "M2", "net": "\\$7"},
]


def test_body_bias_report_without_ties_has_no_tied_key():
    assert "tied" not in _body_bias_report({"unbiased_pmos_body_nets": _TIE_ENTRIES})


def test_body_bias_report_partial_tie_stays_unbiased():
    report = _body_bias_report(
        {"unbiased_pmos_body_nets": _TIE_ENTRIES}, [("\\$5", "VDD")]
    )
    assert report["status"] == BODY_BIAS_UNBIASED
    assert report["tied"] == [
        {"extracted_net": "\\$5", "schematic_net": "VDD", "source": "declared"}
    ]
    # The layout's own finding is carried verbatim.
    assert report["unbiased_pmos_body_nets"] == _TIE_ENTRIES


def test_body_bias_report_fully_tied_is_tied_not_biased():
    report = _body_bias_report(
        {"unbiased_pmos_body_nets": _TIE_ENTRIES},
        [("\\$5", "VDD"), ("\\$7", "VDD")],
    )
    assert report["status"] == "tied"
    assert [t["schematic_net"] for t in report["tied"]] == ["VDD", "VDD"]


def test_validate_tie_nets_lists_valid_names_for_unknown_net():
    with pytest.raises(PexError) as exc:
        _validate_tie_nets([("vout", "VDD")], {"unbiased_pmos_body_nets": _TIE_ENTRIES})
    assert "vout" in str(exc.value)
    assert "\\$5" in str(exc.value) and "\\$7" in str(exc.value)
    assert exc.value.code == "invalid_tie_net"


def test_validate_tie_nets_errors_when_nothing_is_unbiased():
    with pytest.raises(PexError, match="no unbiased net"):
        _validate_tie_nets([("\\$5", "VDD")], {"unbiased_pmos_body_nets": []})


def test_apply_tie_nets_rewrites_whole_tokens_on_device_cards(tmp_path):
    path = tmp_path / "x.spice"
    path.write_text(
        "* \\$5 comment\n.SUBCKT top a b\n"
        "M1 a b c \\$5 pmos w=1\nM2 a b c \\$7 pmos w=1\n"
        "M3 a b c \\$55 pmos w=1\n.ENDS\n"
    )
    _apply_tie_nets(str(path), [("\\$5", "VDD"), ("\\$7", "VDD")])
    assert path.read_text() == (
        "* \\$5 comment\n.SUBCKT top a b\n"
        "M1 a b c VDD pmos w=1\nM2 a b c VDD pmos w=1\n"
        "M3 a b c \\$55 pmos w=1\n.ENDS\n"
    )


def test_apply_tie_nets_does_not_chain(tmp_path):
    path = tmp_path / "x.spice"
    path.write_text("M1 a b c n1 pmos\nM2 a b c n2 pmos\n")
    _apply_tie_nets(str(path), [("n1", "n2"), ("n2", "n3")])
    assert path.read_text() == "M1 a b c n2 pmos\nM2 a b c n3 pmos\n"


def test_apply_tie_nets_errors_when_net_absent(tmp_path):
    path = tmp_path / "x.spice"
    path.write_text("M1 a b c d pmos\n")
    with pytest.raises(PexError, match="does not appear"):
        _apply_tie_nets(str(path), [("\\$9", "VDD")])


def test_parse_tie_nets():
    from klayout_tools.cli._parsing import parse_tie_nets

    assert parse_tie_nets(None, PexError) is None
    assert parse_tie_nets(["\\$5=VDD", "\\$7=VDD", "\\$5=VDD"], PexError) == [
        ("\\$5", "VDD"),
        ("\\$7", "VDD"),
    ]
    for bad in ("nope", "=VDD", "a="):
        with pytest.raises(PexError):
            parse_tie_nets([bad], PexError)
    with pytest.raises(PexError, match="twice"):
        parse_tie_nets(["a=X", "a=Y"], PexError)


def test_run_pex_tie_net_on_layout_with_no_unbiased_nets_errors(
    tmp_path, resistor_layout, measure_reference
):
    with pytest.raises(PexError, match="not an unbiased"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=_write_harness(tmp_path),
            reference_netlist=measure_reference,
            tie_nets=[("\\$5", "VDD")],
        )


def test_run_pex_tie_net_reports_hash_of_rewritten_netlist(
    tmp_path, resistor_layout, measure_reference, monkeypatch
):
    import klayout_tools.pex as pex_module
    from klayout_tools._provenance import sha256_file

    real = pex_module.run_extract

    def _with_unbiased(*args, **kwargs):
        rep = real(*args, **kwargs)
        rep["unbiased_pmos_body_nets"] = [{"device": "M0", "net": "n_tie"}]
        return rep

    monkeypatch.setattr(pex_module, "run_extract", _with_unbiased)
    monkeypatch.setattr(
        pex_module,
        "_validate_tie_nets",
        lambda ties, rep: None,
    )
    out = tmp_path / "res.spice"

    def _fake_apply(path, ties):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("* tied\n")

    monkeypatch.setattr(pex_module, "_apply_tie_nets", _fake_apply)
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(out),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
        tie_nets=[("n_tie", "VDD")],
    )
    assert report["extraction"]["netlist_sha256"] == sha256_file(str(out))


# --------------------------------------------------------------------------- #
# `_delta_pct` / `_row_status`
# --------------------------------------------------------------------------- #


def test_delta_pct_basic():
    assert _delta_pct(100.0, 95.0) == pytest.approx(-5.0)
    assert _delta_pct(50.0, 55.0) == pytest.approx(10.0)


def test_delta_pct_none_when_schematic_zero():
    assert _delta_pct(0.0, 1.0) is None


def test_delta_pct_none_when_either_value_missing():
    assert _delta_pct(None, 1.0) is None
    assert _delta_pct(1.0, None) is None


def _measurement(value, status="pass"):
    return {
        "name": "vout",
        "value": value,
        "unit": "V",
        "status": status,
        "margin": 0.0,
    }


def test_row_status_pass_when_both_pass():
    assert _row_status(_measurement(1.0), _measurement(1.1)) == "pass"


def test_row_status_pass_follows_extracted_side_only():
    # Schematic side "fails" its own limit, extracted side passes -- the row
    # still grades on the extracted side (the side item 7 actually checks).
    assert _row_status(_measurement(1.0, "fail"), _measurement(1.1, "pass")) == "pass"


def test_row_status_fail_when_extracted_fails():
    assert _row_status(_measurement(1.0), _measurement(1.1, "fail")) == "fail"


def test_row_status_error_when_extracted_errors():
    assert _row_status(_measurement(1.0), _measurement(None, "error")) == "error"


def test_row_status_error_when_schematic_errors():
    assert _row_status(_measurement(None, "error"), _measurement(1.1)) == "error"


def test_row_status_error_when_schematic_missing():
    assert _row_status(None, _measurement(1.1)) == "error"


def test_row_status_error_when_only_extracted_absent():
    assert _row_status(_measurement(1.0), _measurement(None, "absent")) == "error"


def test_row_status_error_when_only_schematic_absent():
    assert _row_status(_measurement(None, "absent"), _measurement(1.1)) == "error"


def test_row_status_pass_when_both_absent():
    assert (
        _row_status(_measurement(None, "absent"), _measurement(None, "absent"))
        == "pass"
    )


# --------------------------------------------------------------------------- #
# `_build_delta_rows`
# --------------------------------------------------------------------------- #


def _sim_report(corners):
    return {"corners": corners}


def test_build_delta_rows_single_testbench_bare_spec_row():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert rows == [
        {
            "spec_row": "vout",
            "corner_id": "tt/1.800V/27C",
            "schematic_value": 1.0,
            "extracted_value": 1.1,
            "delta_pct": 10.0,
            "status": "pass",
            "schematic_status": "pass",
            "extracted_status": "pass",
        }
    ]


def test_build_delta_rows_carry_per_side_status():
    # Issue #2989: schematic fail / extracted pass keeps `status: "pass"` but
    # names the schematic-side violation.
    schematic = _sim_report(
        [{"corner_id": "tt", "measurements": [_measurement(1.0, "fail")]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt", "measurements": [_measurement(1.1, "pass")]}]
    )
    (row,) = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert row["status"] == "pass"
    assert row["schematic_status"] == "fail"
    assert row["extracted_status"] == "pass"


def test_build_delta_rows_missing_schematic_measurement_is_error_side():
    schematic = _sim_report([{"corner_id": "tt", "measurements": []}])
    extracted = _sim_report([{"corner_id": "tt", "measurements": [_measurement(1.1)]}])
    (row,) = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert row["schematic_status"] == "error"
    assert row["extracted_status"] == "pass"


def _absent_rows(schematic_status, extracted_status):
    def side(status):
        return _sim_report(
            [
                {
                    "corner_id": "tt",
                    "measurements": [
                        _measurement(None if status == "absent" else 1.0, status)
                    ],
                    "diagnostics": [],
                }
            ]
        )

    return _build_delta_rows(
        spec_row_prefix=None,
        schematic_report=side(schematic_status),
        extracted_report=side(extracted_status),
    )


def test_build_delta_rows_extracted_absent_is_error_blaming_extracted():
    (row,) = _absent_rows("pass", "absent")
    assert row["status"] == "error"
    assert row["delta_pct"] is None
    assert row["extracted_status"] == "absent"
    assert row["error"]["side"] == "extracted"


def test_build_delta_rows_schematic_absent_is_error_blaming_schematic():
    (row,) = _absent_rows("absent", "pass")
    assert row["status"] == "error"
    assert row["schematic_status"] == "absent"
    assert row["error"]["side"] == "schematic"


def test_build_delta_rows_both_absent_is_pass():
    (row,) = _absent_rows("absent", "absent")
    assert row["status"] == "pass"
    assert row["delta_pct"] is None
    assert "error" not in row


def test_unextracted_delta_rows_carry_per_side_status():
    schematic = _sim_report(
        [{"corner_id": "tt", "measurements": [_measurement(1.0, "fail")]}]
    )
    (row,) = _unextracted_delta_rows(spec_row_prefix=None, schematic_report=schematic)
    assert row["schematic_status"] == "fail"
    assert row["extracted_status"] == "error"


def test_build_delta_rows_multi_testbench_prefixes_spec_row():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix="gain-tb",
        schematic_report=schematic,
        extracted_report=extracted,
    )
    assert rows[0]["spec_row"] == "gain-tb.vout"


def test_build_delta_rows_missing_schematic_corner_is_error():
    schematic = _sim_report(
        [{"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert rows[0]["status"] == "error"
    assert rows[0]["schematic_value"] is None
    assert rows[0]["delta_pct"] is None


def test_build_delta_rows_iterates_extracted_corner_order():
    schematic = _sim_report(
        [
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(0.9)]},
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]},
        ]
    )
    extracted = _sim_report(
        [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.05)]},
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(0.95)]},
        ]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert [row["corner_id"] for row in rows] == ["tt/1.800V/27C", "ss/1.620V/-40C"]


# --------------------------------------------------------------------------- #
# `coverage` / `nothing_checked` (issue #1996)
# --------------------------------------------------------------------------- #


def test_build_coverage_empty_delta_reports_nothing_checked():
    """An empty delta reports known zero actual comparisons."""
    coverage = _build_coverage(testbenches_summary=[], delta=[], corner_count=0)

    assert coverage == {
        "testbenches": 0,
        "schema_version": 1,
        "known": True,
        "checked": [],
        "skipped": [],
        "inapplicable": [],
        "unknown": [],
        "delta_rows": 0,
        "corners_compared": 0,
        "nothing_checked": True,
        "nothing_checked_reasons": ["no_delta_rows"],
    }


def test_build_coverage_real_comparison_with_zero_differences_is_not_nothing_checked():
    """Issue #1996's edge case: a comparison that actually *ran* and found
    every row within tolerance is not "nothing checked". `_build_delta_rows`
    emits one row per compared `(corner, measurement)` pair unconditionally
    -- there is no "within tolerance, so omit the row" path -- so the two
    cases are structurally distinguishable, and this pins that."""
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    rows = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    # Identical values on both sides: a zero delta, and still a row.
    assert [row["delta_pct"] for row in rows] == [0.0]

    coverage = _build_coverage(
        testbenches_summary=[
            {"request": "tb.json", "corner_count": 1, "measurement_names": ["vout"]}
        ],
        delta=rows,
        corner_count=1,
    )

    assert coverage == {
        "testbenches": 1,
        "schema_version": 1,
        "known": True,
        "checked": ['comparison:[0,"tt/1.800V/27C","vout"]'],
        "skipped": [],
        "inapplicable": [],
        "unknown": [],
        "delta_rows": 1,
        "corners_compared": 1,
        "nothing_checked": False,
        "nothing_checked_reasons": [],
    }


def test_build_coverage_errored_rows_are_unavailable_comparisons():
    """Error rows are unavailable comparisons; the producer retains error status."""
    rows = _unextracted_delta_rows(
        spec_row_prefix=None,
        schematic_report=_sim_report(
            [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
        ),
    )

    coverage = _build_coverage(
        testbenches_summary=[
            {"request": "tb.json", "corner_count": 1, "measurement_names": ["vout"]}
        ],
        delta=rows,
        corner_count=1,
    )

    assert coverage["delta_rows"] == 1
    assert coverage["nothing_checked"] is True
    assert coverage["nothing_checked_reasons"] == ["unavailable_comparison"]


def test_status_from_coverage_uses_common_rollup_for_partial_and_zero():
    """A skipped requested comparison is reportable but never an unconditional
    pass; known zero comparisons remain refused as not checked."""
    partial = _build_coverage(
        testbenches_summary=[
            {"request": "empty.json", "corner_count": 0, "measurement_names": []}
        ],
        delta=[
            {
                "spec_row": "vout",
                "corner_id": "tt/1.800V/27C",
                "status": "pass",
            }
        ],
        corner_count=1,
    )
    assert _status_from_coverage(partial, failed=False, errored=False) == "pass_partial"

    zero = _build_coverage(testbenches_summary=[], delta=[], corner_count=0)
    assert _status_from_coverage(zero, failed=False, errored=False) == "not_checked"


def test_status_from_coverage_preserves_failure_and_error_precedence():
    coverage = _build_coverage(testbenches_summary=[], delta=[], corner_count=0)
    assert _status_from_coverage(coverage, failed=True, errored=False) == "fail"
    assert _status_from_coverage(coverage, failed=True, errored=True) == "error"


# --------------------------------------------------------------------------- #
# Pin-count-mismatch diagnostic (issue #1030)
# --------------------------------------------------------------------------- #


def test_subckt_interfaces_parses_pins_and_ignores_parameters():
    text = (
        "* header\n"
        ".SUBCKT RES RA RB VSUBS w=1 l=2\n"
        "R1 RA RB 289.2\n"
        ".ENDS RES\n"
        ".subckt inv a y params: wn=1\n"
        ".ends\n"
    )
    interfaces = _subckt_interfaces(text)
    assert interfaces["res"] == ("RES", ["RA", "RB", "VSUBS"])
    assert interfaces["inv"] == ("inv", ["a", "y"])


def test_subckt_interfaces_folds_continuation_lines():
    text = ".subckt amp inp inn\n+ out vdd vss\nM1 out inp vss vss nfet\n.ends\n"
    assert _subckt_interfaces(text)["amp"] == (
        "amp",
        ["inp", "inn", "out", "vdd", "vss"],
    )


def _write_pair(tmp_path, schematic_pins, extracted_pins):
    schematic = tmp_path / "schematic_dut.spice"
    schematic.write_text(
        f".SUBCKT RES {' '.join(schematic_pins)}\nR1 RA RB 289.2\n.ENDS RES\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(
        f".SUBCKT RES {' '.join(extracted_pins)}\nR1 RA RB 291.0\n.ENDS RES\n"
    )
    return str(schematic), str(extracted)


def test_pin_count_mismatch_reports_both_sides(tmp_path):
    """The real-world shape: extraction promotes a substrate/body-tap net the
    hand-written schematic subcircuit never declares."""
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["RA", "RB", "VSUBS"])

    mismatch = _pin_count_mismatch(
        reference_netlist=schematic, extracted_netlist=extracted
    )

    assert mismatch is not None
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["schematic"]["pin_count"] == 2
    assert mismatch["schematic"]["pins"] == ["RA", "RB"]
    assert mismatch["schematic"]["netlist"] == schematic
    assert mismatch["extracted"]["pin_count"] == 3
    assert mismatch["extracted"]["pins"] == ["RA", "RB", "VSUBS"]
    assert mismatch["ngspice_message"] is None
    assert "VSUBS" in mismatch["detail"]


def test_pin_count_mismatch_none_when_interfaces_agree(tmp_path):
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["RA", "RB"])
    assert (
        _pin_count_mismatch(reference_netlist=schematic, extracted_netlist=extracted)
        is None
    )


def test_pin_count_mismatch_case_insensitive_subckt_names(tmp_path):
    """`klt extract` writes an upper-case `.SUBCKT` header where a
    hand-written schematic netlist often does not -- ngspice does not care,
    and neither does this comparison."""
    schematic = tmp_path / "schematic_dut.spice"
    schematic.write_text(".subckt res ra rb\nR1 ra rb 289.2\n.ends res\n")
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT RES RA RB VSUBS\nR1 RA RB 291.0\n.ENDS RES\n")

    mismatch = _pin_count_mismatch(
        reference_netlist=str(schematic), extracted_netlist=str(extracted)
    )
    assert mismatch is not None
    assert mismatch["schematic"]["pin_count"] == 2
    assert mismatch["extracted"]["pin_count"] == 3


def test_pin_count_mismatch_subcircuit_scoping_ignores_unrelated_helper(tmp_path):
    """Issue #1041 (Champion-flagged false-positive guard): a `subcircuit`
    argument naming the one actually instantiated must restrict the
    comparison to it -- an unrelated helper `.SUBCKT` with a differing pin
    count elsewhere in either netlist must not trip the check."""
    schematic = tmp_path / "schematic_dut.spice"
    schematic.write_text(
        ".SUBCKT RES RA RB\nR1 RA RB 289.2\n.ENDS RES\n"
        ".SUBCKT HELPER A B C\nR2 A B 10\n.ENDS HELPER\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(
        ".SUBCKT RES RA RB\nR1 RA RB 291.0\n.ENDS RES\n"
        ".SUBCKT HELPER A B\nR2 A B 10\n.ENDS HELPER\n"
    )

    # Unscoped (`subcircuit=None`, the reactive fallback's own default)
    # scans every `.SUBCKT` the extracted netlist declares and would find
    # `HELPER`'s mismatch -- confirming the guard below actually matters.
    assert (
        _pin_count_mismatch(
            reference_netlist=str(schematic), extracted_netlist=str(extracted)
        )
        is not None
    )

    # Scoped to the actually-instantiated subcircuit (`RES`, whose pin
    # counts agree), the unrelated `HELPER` mismatch must not surface.
    assert (
        _pin_count_mismatch(
            reference_netlist=str(schematic),
            extracted_netlist=str(extracted),
            subcircuit="RES",
        )
        is None
    )


def _errored_sim_report(log_path=None):
    return {
        "corners": [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [_measurement(None, "error")],
                "artifacts": {"log": log_path},
            }
        ]
    }


def test_pin_count_mismatch_from_report_uses_ngspice_log_line(tmp_path):
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["RA", "RB", "VSUBS"])
    log = tmp_path / "ngspice.log"
    log.write_text(
        "\nCircuit: * testbench\n\n"
        'Too few parameters for subcircuit type "res" (instance: xxres)\n'
        "    Simulation interrupted due to error!\n"
    )

    mismatch = _pin_count_mismatch_from_report(
        _errored_sim_report(str(log)),
        reference_netlist=schematic,
        extracted_netlist=extracted,
    )

    assert mismatch is not None
    assert mismatch["ngspice_message"] == (
        'Too few parameters for subcircuit type "res" (instance: xxres)'
    )
    assert mismatch["extracted"]["pin_count"] == 3


def test_pin_count_mismatch_from_report_falls_back_to_headers_without_log(tmp_path):
    """`options.keep_artifacts` is off by default, so the per-corner log is
    usually gone by the time the response comes back -- the `.SUBCKT` header
    comparison still names the mismatch."""
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["RA", "RB", "VSUBS"])

    mismatch = _pin_count_mismatch_from_report(
        _errored_sim_report(),
        reference_netlist=schematic,
        extracted_netlist=extracted,
    )
    assert mismatch is not None
    assert mismatch["ngspice_message"] is None


def test_pin_count_mismatch_from_report_none_when_any_value_came_back(tmp_path):
    """No false positives: a run that measured anything at all cannot have
    been rejected for a pin-count mismatch, whatever the headers say."""
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["RA", "RB", "VSUBS"])
    report = {
        "corners": [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [_measurement(1.0), _measurement(None, "error")],
                "artifacts": {"log": None},
            }
        ]
    }

    assert (
        _pin_count_mismatch_from_report(
            report, reference_netlist=schematic, extracted_netlist=extracted
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Flat-schematic-DUT-vs-`.SUBCKT`-wrapped-extraction diagnostic (Gap 2,
# issue #1255)
# --------------------------------------------------------------------------- #


def _write_flat_dut(tmp_path) -> str:
    """A schematic DUT with no `.SUBCKT`/`.ENDS` wrapper at all -- the
    testbench references its `RA`/`RB` nodes directly as top-level nets."""
    path = tmp_path / "flat_dut.spice"
    path.write_text("R1 RA RB 289.2\n")
    return str(path)


def test_flat_dut_mismatch_reports_when_schematic_is_flat(tmp_path):
    schematic = _write_flat_dut(tmp_path)
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT RES RA RB\nR1 RA RB 291.0\n.ENDS RES\n")

    mismatch = _flat_dut_mismatch(
        reference_netlist=schematic, extracted_netlist=str(extracted), top_cell="RES"
    )

    assert mismatch is not None
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["schematic"]["netlist"] == schematic
    assert mismatch["schematic"]["subcircuit"] is None
    assert mismatch["schematic"]["pin_count"] is None
    assert mismatch["schematic"]["pins"] is None
    assert mismatch["extracted"]["pin_count"] == 2
    assert mismatch["extracted"]["pins"] == ["RA", "RB"]
    assert mismatch["ngspice_message"] is None
    assert "declares no `.SUBCKT`" in mismatch["detail"]
    assert "RES" in mismatch["detail"]


def test_flat_dut_mismatch_none_when_schematic_is_subckt_wrapped(tmp_path):
    """The normal case -- a `.SUBCKT`-wrapped schematic DUT -- is not flat,
    so this diagnostic never fires (a real interface mismatch there, if
    any, is `_pin_count_mismatch`'s job instead)."""
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["RA", "RB", "VSUBS"])
    assert (
        _flat_dut_mismatch(
            reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
        )
        is None
    )


def test_flat_dut_mismatch_none_when_extracted_lacks_top_cell(tmp_path):
    """Degrades safely (no diagnostic) rather than raising if the extracted
    netlist -- which `klt extract` always wraps in `.SUBCKT <top>` -- ever
    somehow doesn't declare `top_cell` at all."""
    schematic = _write_flat_dut(tmp_path)
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT OTHER A B\n.ENDS OTHER\n")

    assert (
        _flat_dut_mismatch(
            reference_netlist=schematic,
            extracted_netlist=str(extracted),
            top_cell="RES",
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Device-model / `.subckt` divergence diagnostic (issue #2402)
# --------------------------------------------------------------------------- #


def _write_model_pair(tmp_path, reference_model: str, extracted_model: str):
    """A `(reference_netlist, extracted_netlist)` pair whose `.SUBCKT`
    interfaces declare the identical pin list and whose devices share the
    identical connectivity, differing only in the MOS model/subcircuit name
    each side's `X` cards instantiate -- the "topologically comparable,
    electrically unrelated" shape issue #2402 reports (a layout with no
    voltage-domain marker geometry extracting onto a PDK family's default
    flavour against a reference netlist naming a different one)."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".SUBCKT INV A Y VDD VSS\n"
        f"Xm1 Y A VSS VSS {reference_model} l=0.15 w=0.42\n"
        f"Xm2 Y A VDD VDD {reference_model} l=0.15 w=0.84\n"
        ".ENDS INV\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(
        ".SUBCKT INV A Y VDD VSS\n"
        f"Xm1 Y A VSS VSS {extracted_model} l=0.15 w=0.42 AS=0 AD=0 PS=0 PD=0\n"
        f"Xm2 Y A VDD VDD {extracted_model} l=0.15 w=0.84 AS=0 AD=0 PS=0 PD=0\n"
        ".ENDS INV\n"
    )
    return str(reference), str(extracted)


def test_model_mismatch_reports_divergent_flavours(tmp_path):
    """The issue's own reproducer shape: same device count/connectivity/pin
    list on both sides (so `pin_count_mismatch`/`flat_dut_mismatch` would
    both be `None` here), differing only in which MOS flavour each side's
    `X` cards name."""
    reference, extracted = _write_model_pair(
        tmp_path,
        reference_model="sky130_fd_pr__nfet_g5v0",
        extracted_model="sky130_fd_pr__nfet_01v8",
    )

    mismatch = _model_mismatch(reference_netlist=reference, extracted_netlist=extracted)

    assert mismatch is not None
    assert mismatch["kind"] == "model_set"
    assert mismatch["reference_only"] == ["sky130_fd_pr__nfet_g5v0"]
    assert mismatch["extracted_only"] == ["sky130_fd_pr__nfet_01v8"]
    assert "different device models" in mismatch["detail"]
    assert mismatch["counts"]["reference"] == {"sky130_fd_pr__nfet_g5v0": 2}
    assert mismatch["counts"]["extracted"] == {"sky130_fd_pr__nfet_01v8": 2}
    assert "sky130_fd_pr__nfet_g5v0" in mismatch["detail"]
    assert "sky130_fd_pr__nfet_01v8" in mismatch["detail"]


def test_model_mismatch_none_when_models_agree(tmp_path):
    reference, extracted = _write_model_pair(
        tmp_path,
        reference_model="sky130_fd_pr__nfet_01v8",
        extracted_model="sky130_fd_pr__nfet_01v8",
    )
    assert (
        _model_mismatch(reference_netlist=reference, extracted_netlist=extracted)
        is None
    )


def test_model_mismatch_quoted_expression_params_with_spaces(tmp_path):
    """Issue #2866: a quoted parameter expression containing spaces must be
    one name=value token, not mis-read as the model name."""
    from klayout_tools.pex import _device_model_counts

    card = (
        "XMn y a vss vss nfet_03v3 L=0.28u W=0.22u nf=1 "
        "ad='int((nf+1)/2) * W/nf * 0.18u' "
        "as='int((nf+2)/2) * W/nf * 0.18u'\n"
    )
    reference = tmp_path / "ref.spice"
    extracted = tmp_path / "ext.spice"
    reference.write_text(card)
    extracted.write_text(card.replace("XMn", "X0"))
    assert _device_model_counts(card) == {"nfet_03v3": ("nfet_03v3", 1)}
    assert (
        _model_mismatch(
            reference_netlist=str(reference), extracted_netlist=str(extracted)
        )
        is None
    )


def test_model_mismatch_case_insensitive_model_names(tmp_path):
    """ngspice is case-insensitive about model/subcircuit names -- so is this
    comparison, mirroring `_subckt_interfaces`'s own convention."""
    reference, extracted = _write_model_pair(tmp_path, "NFET_HV", "nfet_hv")
    assert (
        _model_mismatch(reference_netlist=reference, extracted_netlist=extracted)
        is None
    )


def test_model_mismatch_detects_raw_m_card_divergence(tmp_path):
    """The comparison also covers raw `M<name> d g s b <model>` cards, not
    only `X`-card subcircuit calls -- a hand-written schematic DUT is more
    likely to use the raw SPICE MOSFET primitive directly."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".SUBCKT INV A Y VDD VSS\nM1 Y A VSS VSS nfet_hv L=0.15u W=0.42u\n.ENDS INV\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(
        ".SUBCKT INV A Y VDD VSS\n"
        "Xm1 Y A VSS VSS sky130_fd_pr__nfet_01v8 l=0.15 w=0.42 AS=0 AD=0 PS=0 PD=0\n"
        ".ENDS INV\n"
    )

    mismatch = _model_mismatch(
        reference_netlist=str(reference), extracted_netlist=str(extracted)
    )
    assert mismatch is not None
    assert mismatch["reference_only"] == ["nfet_hv"]
    assert mismatch["extracted_only"] == ["sky130_fd_pr__nfet_01v8"]


def test_model_mismatch_ignores_passive_cards(tmp_path):
    """Scoped to `M`/`X`/`Q`/`D` device-instance cards only (issue #2402's
    Acceptance Criteria) -- an `R`/`C` card's own trailing model-name token
    (e.g. a resistor's generic `r` model) must not be compared, so a run
    with no active devices at all reports `None` rather than a spurious
    per-passive-model mismatch."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".SUBCKT RES RA RB\nR1 RA RB 289.2 res_generic_po\n.ENDS RES\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT RES RA RB\nR1 RA RB 291.0\n.ENDS RES\n")

    assert (
        _model_mismatch(
            reference_netlist=str(reference), extracted_netlist=str(extracted)
        )
        is None
    )


def test_model_mismatch_detects_instance_count_only_divergence(tmp_path):
    """Same model name on both sides, but a different *instance count* --
    e.g. a device silently dropped or duplicated on one side -- is still a
    reportable divergence, even though `reference_only`/`extracted_only`
    both stay empty (no model name is unique to either side)."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".SUBCKT INV A Y VDD VSS\n"
        "Xm1 Y A VSS VSS nfet_01v8 l=0.15 w=0.42\n"
        "Xm2 Y A VSS VSS nfet_01v8 l=0.15 w=0.42\n"
        ".ENDS INV\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(
        ".SUBCKT INV A Y VDD VSS\nXm1 Y A VSS VSS nfet_01v8 l=0.15 w=0.42\n.ENDS INV\n"
    )

    mismatch = _model_mismatch(
        reference_netlist=str(reference), extracted_netlist=str(extracted)
    )
    assert mismatch is not None
    assert mismatch["kind"] == "count_only"
    assert mismatch["reference_only"] == []
    assert mismatch["extracted_only"] == []
    assert mismatch["counts"]["reference"] == {"nfet_01v8": 2}
    assert mismatch["counts"]["extracted"] == {"nfet_01v8": 1}
    # Issue #3054: the detail must not claim a model divergence.
    detail = mismatch["detail"]
    assert "different device models" not in detail
    assert "electrically unrelated" not in detail
    assert "A common cause" not in detail
    assert "finger/multiplier" in detail


def test_model_mismatch_hierarchical_reference_keeps_hierarchy_text(tmp_path):
    """Issue #3054: a hierarchical reference keeps the hierarchy-artifact
    wording alongside the new `kind` field."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".SUBCKT CELL A Y VSS\n"
        "Xn1 Y A VSS VSS nfet_01v8\n"
        "Xn2 Y A VSS VSS nfet_01v8\n"
        ".ENDS CELL\n"
        ".SUBCKT TOP A Y VSS\n"
        "Xi1 A Y VSS CELL\n"
        ".ENDS TOP\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT TOP A Y VSS\nXn1 Y A VSS VSS nfet_01v8\n.ENDS TOP\n")
    mismatch = _model_mismatch(
        reference_netlist=str(reference), extracted_netlist=str(extracted)
    )
    assert mismatch is not None
    assert mismatch["reference_only"] == ["CELL"]
    assert mismatch["kind"] == "model_set"
    assert "#1085" in mismatch["detail"]


def test_reference_is_hierarchical_detects_local_subckt_call(tmp_path):
    """`_reference_is_hierarchical` is `True` only when an `X` card's target
    is a `.subckt` defined in the same file -- an `X` card naming an
    external PDK model/subcircuit (the ordinary, flat case) does not
    count."""
    hierarchical = (
        ".SUBCKT INV A Y VDD VSS\n"
        "Xn Y A VSS VSS nfet_01v8\n"
        "Xp Y A VDD VDD pfet_01v8\n"
        ".ENDS INV\n"
        ".SUBCKT BUF A Y VDD VSS\n"
        "Xi1 A M VDD VSS INV\n"
        "Xi2 M Y VDD VSS INV\n"
        ".ENDS BUF\n"
    )
    assert _reference_is_hierarchical(hierarchical) is True

    flat = (
        ".SUBCKT BUF A Y VDD VSS\n"
        "Xn1 A M VSS VSS nfet_01v8\n"
        "Xp1 A M VDD VDD pfet_01v8\n"
        "Xn2 M Y VSS VSS nfet_01v8\n"
        "Xp2 M Y VDD VDD pfet_01v8\n"
        ".ENDS BUF\n"
    )
    assert _reference_is_hierarchical(flat) is False


def test_model_mismatch_names_hierarchy_not_electrically_unrelated(tmp_path):
    """A hierarchical reference netlist (a leaf `.subckt` called N times)
    diffed against `klt extract`'s always-flat output is the *same circuit*
    -- devices nested inside the leaf are counted once per *definition*
    here, not once per physical *instance* -- so `model_mismatch` must name
    hierarchy-vs-flat as the likely cause instead of asserting the two sides
    are electrically unrelated, the way it does for a genuine device-flavour
    swap (PR #2430 review feedback on issue #2402)."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".SUBCKT INV A Y VDD VSS\n"
        "Xn Y A VSS VSS nfet_01v8\n"
        "Xp Y A VDD VDD pfet_01v8\n"
        ".ENDS INV\n"
        ".SUBCKT BUF A Y VDD VSS\n"
        "Xi1 A M VDD VSS INV\n"
        "Xi2 M Y VDD VSS INV\n"
        ".ENDS BUF\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(
        ".SUBCKT BUF A Y VDD VSS\n"
        "Xn1 A M VSS VSS nfet_01v8\n"
        "Xp1 A M VDD VDD pfet_01v8\n"
        "Xn2 M Y VSS VSS nfet_01v8\n"
        "Xp2 M Y VDD VDD pfet_01v8\n"
        ".ENDS BUF\n"
    )

    mismatch = _model_mismatch(
        reference_netlist=str(reference), extracted_netlist=str(extracted)
    )
    assert mismatch is not None
    # Same circuit: the only divergence is hierarchy depth, not device
    # identity -- `reference_only` names the leaf `.subckt` call itself.
    assert mismatch["kind"] == "model_set"
    assert mismatch["reference_only"] == ["INV"]
    assert mismatch["extracted_only"] == []
    assert "electrically unrelated" not in mismatch["detail"]
    assert "hierarchical" in mismatch["detail"]
    assert "#1085" in mismatch["detail"]


# --------------------------------------------------------------------------- #
# `run_pex` error paths that are cheap (no `ngspice`, extraction only)
# --------------------------------------------------------------------------- #

_RES_SQUARES = 6.0
_RES_BAR = kdb.Box(0, 0, 12000, 1000)
_RES_MARKED = kdb.Box(3000, 0, 9000, 1000)
_SHEET_RHO_RES_GENERIC_PO = 48.2


def _write_gds(layout: kdb.Layout, path: Path) -> str:
    layout.write(str(path))
    return str(path)


def _make_sky130_poly_resistor_layout(
    top_name: str = "RES",
    extra_tap_labels: tuple[str, ...] = (),
) -> kdb.Layout:
    """A single drawn sky130 poly resistor with a labelled, contacted head
    at each end -- the same fixture shape `tests/test_extract.py`'s
    `_make_poly_resistor_layout` uses (kept self-contained here rather than
    imported across test modules), extracting to `res_generic_po` with
    `R = L / W * sheet_rho = 6.0 / 1.0 * 48.2 = 289.2` ohm (issue #222).

    ``extra_tap_labels`` adds that many *additional* labelled li1 islands
    clear of the resistor body. Each one extracts to its own named net and
    therefore its own top-cell pin, so the extracted `.SUBCKT RES` header
    gains a terminal the hand-written schematic DUT does not declare -- the
    shape issue #1030 reports from real decks, where extraction promotes a
    physical body/substrate-tap net the schematic never modelled. See
    `_PROMOTED_TAP_LABELS` for why the count matters.
    """
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    poly = (66, 20)  # poly.drawing
    marker = (66, 13)  # poly.res
    contact = (66, 44)  # licon1.drawing
    metal = (67, 20)  # li1.drawing
    metal_label = (67, 5)  # li1.pin

    draw(*poly, _RES_BAR)
    draw(*marker, _RES_MARKED)
    for x, name in ((1500, "RA"), (10500, "RB")):
        draw(*contact, kdb.Box(x - 100, 400, x + 100, 600))
        draw(*metal, kdb.Box(x - 400, 200, x + 400, 800))
        label(*metal_label, name, x, 500)

    # Well clear of the resistor bar (y 0..1000) and its heads, so these stay
    # separate nets rather than merging into RA/RB.
    for index, name in enumerate(extra_tap_labels):
        x = 1500 + index * 3000
        draw(*metal, kdb.Box(x - 400, 3000, x + 400, 3600))
        label(*metal_label, name, x, 3300)

    return layout


def _write_schematic_dut(path: Path, r_ohm: float = 289.2) -> Path:
    path.write_text(f".SUBCKT RES RA RB\nR1 RA RB {r_ohm}\n.ENDS RES\n")
    return path


def _write_testbench(
    path: Path, dut_path: Path, load_ohm: float = 1000.0, measurement_name: str = "vout"
) -> Path:
    path.write_text(
        f'.include "{dut_path}"\n'
        ".model res_generic_po r\n"
        "Vdd RA 0 DC 1.8\n"
        "Xres RA RB RES\n"
        f"Rload RB 0 {load_ohm}\n"
    )
    return path


def _write_request(
    path: Path, testbench_path: Path, measurement_name: str = "vout", node: str = "RB"
) -> Path:
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [
                    {
                        "name": measurement_name,
                        "spice": f".meas tran {measurement_name} FIND v({node}) AT=1u",
                    }
                ],
            }
        )
    )
    return path


@pytest.fixture
def resistor_layout(tmp_path):
    return _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")


# The two extra labelled taps the pin-count-mismatch integration tests below
# rely on. *Two* is deliberate, and load-bearing across ngspice versions:
#
# - The schematic DUT declares 2 pins (`.SUBCKT RES RA RB`), so the testbench's
#   own `Xres RA RB RES` line supplies 2 nodes. `klt pex` re-runs that exact
#   line against the extracted netlist, whose header is
#   `.SUBCKT RES RA RB VSUB VNW` -- 4 pins for 2 supplied nodes, i.e. ngspice's
#   "*Too few* parameters for subcircuit type" (extraction promoted more pins
#   than the schematic models -- issue #1030's real-world direction).
# - ngspice 46 rejects any pin-count difference, in either direction. ngspice
#   42 -- which this repo's CI installs via apt (`42+ds-3build1`) -- does not:
#   it silently accepts *every* "too many parameters" case, and tolerates
#   "too few" when the shortfall is only one node. It raises the error only
#   once the instance supplies at least *two* fewer nodes than the `.SUBCKT`
#   declares.
#
# So one tap (a 3-pin extraction) would reproduce PR #1039's original CI
# failure -- green on a dev machine's ngspice 46, silently passing on CI's
# ngspice 42. Two taps is the smallest mismatch both versions reject. Do not
# reduce this to one, and do not restate it as a "too many parameters" case.
_PROMOTED_TAP_LABELS = ("VSUB", "VNW")


@pytest.fixture
def resistor_layout_promoted_taps(tmp_path):
    """The same resistor, plus two labelled taps that extraction promotes to
    top-cell pins the schematic DUT does not declare (issue #1030)."""
    return _write_gds(
        _make_sky130_poly_resistor_layout(extra_tap_labels=_PROMOTED_TAP_LABELS),
        tmp_path / "res_taps.gds",
    )


def test_run_pex_requires_at_least_one_testbench(resistor_layout):
    with pytest.raises(PexError, match="at least one testbench"):
        run_pex(resistor_layout, [], "sky130")


def test_run_pex_testbench_missing_include_raises(tmp_path, resistor_layout):
    dut = tmp_path / "dut.spice"
    dut.write_text("R1 RA RB 289.2\n")  # no .include -- inlined directly
    tb = tmp_path / "testbench.spice"
    tb.write_text("R1 RA RB 289.2\nVdd RA 0 DC 1.8\nRload RB 0 1k\n")
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match=r"\.include"):
        run_pex(resistor_layout, [str(request)], "sky130")


def test_run_pex_testbench_include_not_dut_raises(
    tmp_path, resistor_layout, monkeypatch
):
    """Gap 1, issue #1255: a testbench whose *only* `.include` names a
    shared corner/parameter file (not the DUT) used to be silently swapped
    in -- now a hard error, raised before any `klt sim` call, confirmed
    here by making `run_sim` unreachable (mirrors the sibling
    two-`.include`s test above)."""
    import klayout_tools.pex as pex_module

    def _unreachable_run_sim(*_args, **_kwargs):
        raise AssertionError(
            "run_sim must not be called for a testbench whose sole "
            "`.include` is not a plausible DUT"
        )

    monkeypatch.setattr(pex_module, "run_sim", _unreachable_run_sim)

    corner = tmp_path / "design.ngspice"
    corner.write_text(".param sw_stat_mismatch=0\n.model res_generic_po r\n")
    tb = tmp_path / "testbench.spice"
    tb.write_text(
        f'.include "{corner}"\n'
        "R1 RA RB 289.2\n"  # the DUT's own devices, inlined directly
        "Vdd RA 0 DC 1.8\n"
        "Rload RB 0 1k\n"
    )
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError) as excinfo:
        run_pex(resistor_layout, [str(request)], "sky130")

    message = str(excinfo.value)
    assert str(corner) in message
    assert "declares no `.SUBCKT`" in message


def test_run_pex_testbench_two_includes_raises_before_simulating(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #1030: the ambiguity is caught in the same up-front pass that
    rejects a testbench with no `.include` at all -- before any `klt sim`
    call, confirmed by making `run_sim` unreachable."""
    import klayout_tools.pex as pex_module

    def _unreachable_run_sim(*_args, **_kwargs):
        raise AssertionError("run_sim must not be called for an ambiguous testbench")

    monkeypatch.setattr(pex_module, "run_sim", _unreachable_run_sim)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    params = tmp_path / "design.ngspice"
    params.write_text(".param sw_stat_mismatch=0\n")
    tb = tmp_path / "testbench.spice"
    tb.write_text(
        f'.include "{params}"\n'
        f'.include "{dut}"\n'
        ".model res_generic_po r\n"
        "Vdd RA 0 DC 1.8\n"
        "Xres RA RB RES\n"
        "Rload RB 0 1k\n"
    )
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError) as excinfo:
        run_pex(resistor_layout, [str(request)], "sky130")

    message = str(excinfo.value)
    assert "2 `.include`/`.inc` directives" in message
    assert str(params) in message
    assert str(dut) in message


def test_run_pex_pin_count_mismatch_preflight_skips_extracted_side_entirely(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #1041: the schematic/extracted top-level pin-list mismatch is
    now detected *proactively*, from the two netlists' own `.SUBCKT`
    headers alone, before the extracted-side `run_sim` call is even
    attempted -- confirmed here by making a second `run_sim` call
    unreachable (only the schematic-side call is allowed through), mirroring
    `test_run_pex_flat_dut_mismatch_skips_extracted_side_entirely` above.
    This is what makes detection identical on every ngspice version: no
    engine is ever invoked on the extracted side, so there is nothing for a
    lax ngspice (e.g. 42) to silently accept.
    """
    import klayout_tools.pex as pex_module

    schematic_report = {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(1.3)]},
        ],
        "corner_count": 2,
        "measurements": [{"name": "vout"}],
    }
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return schematic_report
        raise AssertionError(
            "the extracted-side run_sim call must not happen when the "
            "pre-flight pin_count_mismatch check already explains why it "
            "would be meaningless"
        )

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    # Same subcircuit name ("RES") as `resistor_layout`'s extraction top
    # cell -- an extra `VSUB` pin the schematic DUT does not declare, the
    # exact issue #1030 shape -- so the pre-flight check (scoped to
    # `extract_report["top"]`, i.e. "RES") can see and compare it.
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(".SUBCKT RES RA RB VSUB\nR1 RA RB 289.2\n.ENDS RES\n")
    tb = tmp_path / "testbench.spice"
    tb.write_text(
        f'.include "{dut}"\n'
        ".model res_generic_po r\n"
        "Vdd RA 0 DC 1.8\n"
        "Xres RA RB 0 RES\n"
        "Rload RB 0 1k\n"
    )
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert calls["n"] == 1
    # The whole run still produces a JSON report, per corner.
    assert report["status"] == "error"
    assert [row["corner_id"] for row in report["delta"]] == [
        "tt/1.800V/27C",
        "ss/1.620V/-40C",
    ]
    assert all(row["extracted_value"] is None for row in report["delta"])
    assert all(row["status"] == "error" for row in report["delta"])
    assert report["errored"] == 2

    mismatch = report["pin_count_mismatch"]
    assert mismatch is not None
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["schematic"]["pins"] == ["RA", "RB", "VSUB"]
    assert mismatch["schematic"]["pin_count"] == 3
    assert mismatch["extracted"]["pin_count"] == 2
    # No engine was ever invoked on the extracted side -- there is no
    # ngspice line to carry through.
    assert mismatch["ngspice_message"] is None


def test_run_pex_pin_count_mismatch_reactive_fallback_when_preflight_cannot_see_it(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #1030 / #1041: the reactive fallback (`_pin_count_mismatch_
    from_message`, driven by ngspice's own engine text) still fires for a
    mismatch the static pre-flight header comparison cannot see -- here, the
    schematic DUT declares its subcircuit under a *different* name (`RESX`)
    than the extracted netlist's top-level `.SUBCKT RES`. The pre-flight
    check is scoped to `extract_report["top"]` ("RES"), so it finds no
    same-named schematic interface to compare and returns `None`, exactly
    like a genuinely unrelated schematic file would -- the mismatch is only
    surfaced once ngspice's own refusal names the extracted-side subcircuit
    it tried (and failed) to elaborate.

    `run_sim` is monkeypatched here (schematic side succeeds, extracted side
    raises) so the diagnostic path is covered everywhere, including machines
    without `ngspice`; `test_integration_run_pex_pin_count_mismatch` below
    proves the equivalent pre-flight-visible report against a real ngspice
    run.
    """
    import klayout_tools.pex as pex_module
    from klayout_tools.sim import SimError

    schematic_report = {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(1.3)]},
        ],
        "corner_count": 2,
        "measurements": [{"name": "vout"}],
    }
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return schematic_report
        raise SimError(
            "remote backend failed: ngspice reported:\n"
            'Too few parameters for subcircuit type "res" (instance: xxres)\n'
            "    Simulation interrupted due to error!"
        )

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(".SUBCKT RESX RA RB VSUB\nR1 RA RB 289.2\n.ENDS RESX\n")
    tb = tmp_path / "testbench.spice"
    tb.write_text(
        f'.include "{dut}"\n'
        ".model res_generic_po r\n"
        "Vdd RA 0 DC 1.8\n"
        "Xres RA RB 0 RESX\n"
        "Rload RB 0 1k\n"
    )
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert calls["n"] == 2
    # The whole run still produces a JSON report, per corner.
    assert report["status"] == "error"
    assert [row["corner_id"] for row in report["delta"]] == [
        "tt/1.800V/27C",
        "ss/1.620V/-40C",
    ]
    assert all(row["extracted_value"] is None for row in report["delta"])
    assert all(row["status"] == "error" for row in report["delta"])
    assert report["errored"] == 2

    mismatch = report["pin_count_mismatch"]
    assert mismatch is not None
    # Named from ngspice's own message (lower-case, as ngspice printed it) --
    # the schematic side has no same-named interface at all.
    assert mismatch["subcircuit"] == "res"
    assert mismatch["schematic"]["pins"] is None
    assert mismatch["schematic"]["pin_count"] is None
    assert mismatch["extracted"]["pin_count"] == 2
    assert mismatch["ngspice_message"] == (
        'Too few parameters for subcircuit type "res" (instance: xxres)'
    )


def test_run_pex_relative_output_bakes_absolute_include_line(tmp_path, monkeypatch):
    """Issue #1525 (no-`ngspice`-required unit coverage): even with a
    relative `-o`/`--output`, the generated extracted-side testbench's
    `.include` line must name an *absolute* path -- that is the actual file
    `run_sim`'s per-corner `ngspice -b` call resolves a relative `.include`
    against (the generated testbench's own directory, nested under
    `--outdir`), not the caller's cwd. `run_sim` is monkeypatched so this is
    covered on machines without `ngspice` too; the end-to-end
    simulate-and-pass coverage lives in
    `test_run_pex_relative_output_and_outdir_extracted_side_still_simulates`.
    """
    import klayout_tools.pex as pex_module

    schematic_report = {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
        ],
        "corner_count": 1,
        "measurements": [{"name": "vout"}],
    }
    calls = {"n": 0}
    captured: dict[str, str] = {}

    def _fake_run_sim(request_path, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return schematic_report
        with open(request_path, encoding="utf-8") as handle:
            captured["extracted_body"] = json.load(handle)["netlist"]
        return {
            "corners": [
                {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.39)]},
            ],
            "corner_count": 1,
            "measurements": [{"name": "vout"}],
        }

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    root = _make_fake_repo(tmp_path)
    _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = root / "schematic_dut.spice"
    _write_schematic_dut(dut)
    tb = root / "testbench.spice"
    _write_testbench(tb, dut)
    request = _write_request(root / "request.json", tb)

    monkeypatch.chdir(root)
    report = run_pex(
        "res.gds",
        [str(request)],
        "sky130",
        output=os.path.join("relative-out", "extracted.spice"),
        artifacts_dir=os.path.join("relative-out", "pex"),
    )

    assert calls["n"] == 2
    assert "extracted_body" in captured
    with open(captured["extracted_body"], encoding="utf-8") as handle:
        body_text = handle.read()
    include_line = next(
        line
        for line in body_text.splitlines()
        if line.strip().lower().startswith((".include", ".inc"))
    )
    include_target = include_line.split(None, 1)[1].strip().strip('"').strip("'")
    assert os.path.isabs(include_target), (
        f"extracted-side .include line must be absolute, got: {include_line!r}"
    )
    assert os.path.isfile(include_target)
    # A relative `-o` does not leak into this run's own JSON report.
    assert report["netlist"]["scope"] == "repo"


def _two_corner_schematic_report():
    return {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
            {"corner_id": "ss/1.620V/-40C", "measurements": [_measurement(1.3)]},
        ],
        "corner_count": 2,
        "measurements": [{"name": "vout"}],
    }


def test_run_pex_extracted_side_backend_failure_keeps_schematic_results(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #2990: a non-pin-count extracted-side `SimError` (here a batch
    capacity refusal) no longer aborts the run and discards the completed
    schematic side -- every row is an error row with the schematic value
    intact, and the failure is named in `backend_failure`."""
    import klayout_tools.pex as pex_module
    from klayout_tools.sim import SimError

    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _two_corner_schematic_report()
        raise SimError(
            "batch backend failed: no capacity in any of the 30 pools",
            code="batch_no_capacity",
        )

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["status"] == "error"
    assert report["errored"] == 2
    assert [row["status"] for row in report["delta"]] == ["error", "error"]
    assert [row["schematic_value"] for row in report["delta"]] == [1.4, 1.3]
    assert all(row["extracted_value"] is None for row in report["delta"])
    failure = report["backend_failure"]
    assert failure["side"] == "extracted"
    assert failure["code"] == "batch_no_capacity"
    assert "no capacity" in failure["message"]
    assert failure["testbench"] == str(request)
    assert report["pin_count_mismatch"] is None


def test_run_pex_backend_failure_only_second_testbench_keeps_first_graded(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #2990: with two testbenches, a failure on the second one's
    extracted side leaves the first one's rows graded normally."""
    import klayout_tools.pex as pex_module
    from klayout_tools.sim import SimError

    one_corner = {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]}
        ],
        "corner_count": 1,
        "measurements": [{"name": "vout"}],
    }
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise SimError("batch backend failed: boom", code="batch_no_capacity")
        return one_corner

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    requests = []
    for name in ("a", "b"):
        tb = _write_testbench(tmp_path / f"{name}.spice", dut)
        requests.append(str(_write_request(tmp_path / f"{name}.json", tb)))

    report = run_pex(resistor_layout, requests, "sky130")

    assert calls["n"] == 4
    statuses = [row["status"] for row in report["delta"]]
    assert len(statuses) == 2
    assert statuses[0] != "error"
    assert statuses[1] == "error"
    assert report["backend_failure"]["testbench"] == requests[1]


def test_run_pex_schematic_side_sim_error_keeps_code(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #2990: a schematic-side `SimError` still aborts, but its
    machine-readable `code` survives on the `PexError`."""
    import klayout_tools.pex as pex_module
    from klayout_tools.sim import SimError

    def _fake_run_sim(*_args, **_kwargs):
        raise SimError("batch backend failed: no capacity", code="batch_no_capacity")

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match="schematic-side simulation failed") as info:
        run_pex(resistor_layout, [str(request)], "sky130")
    assert info.value.code == "batch_no_capacity"


def test_run_pex_flat_dut_mismatch_skips_extracted_side_entirely(
    tmp_path, resistor_layout, monkeypatch
):
    """Gap 2, issue #1255: a flat schematic DUT (no `.SUBCKT` wrapper)
    against `klt extract`'s always-`.SUBCKT`-wrapped output is detected
    proactively, before any extracted-side `run_sim` call is even
    attempted -- confirmed here by making a second `run_sim` call
    unreachable (only the schematic-side call is allowed through)."""
    import klayout_tools.pex as pex_module

    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "corners": [
                    {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
                    {
                        "corner_id": "ss/1.620V/-40C",
                        "measurements": [_measurement(1.3)],
                    },
                ],
                "corner_count": 2,
                "measurements": [{"name": "vout"}],
            }
        raise AssertionError(
            "the extracted-side run_sim call must not happen when "
            "flat_dut_mismatch already explains why it would be meaningless"
        )

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = _write_flat_dut(tmp_path)
    tb = tmp_path / "testbench.spice"
    tb.write_text(f'.include "{dut}"\nVdd RA 0 DC 1.8\nRload RB 0 1k\n')
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert calls["n"] == 1
    assert report["status"] == "error"
    assert report["errored"] == 2
    assert [row["corner_id"] for row in report["delta"]] == [
        "tt/1.800V/27C",
        "ss/1.620V/-40C",
    ]
    assert all(row["extracted_value"] is None for row in report["delta"])
    assert all(row["status"] == "error" for row in report["delta"])
    # Mutually exclusive with `pin_count_mismatch` -- the extracted side was
    # never even attempted, so there is no reactive diagnostic to report.
    assert report["pin_count_mismatch"] is None

    mismatch = report["flat_dut_mismatch"]
    assert mismatch is not None
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["schematic"]["netlist"] == dut
    assert mismatch["schematic"]["pin_count"] is None
    assert mismatch["extracted"]["pins"] == ["RA", "RB"]


def test_pin_order_mismatch_reports_same_set_different_order(tmp_path):
    schematic, extracted = _write_pair(tmp_path, ["b", "a", "vdd"], ["a", "b", "vdd"])
    mismatch = _pin_order_mismatch(
        reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
    )
    assert mismatch is not None
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["schematic"]["pins"] == ["b", "a", "vdd"]
    assert mismatch["extracted"]["pins"] == ["a", "b", "vdd"]
    assert mismatch["ngspice_message"] is None
    assert "(b, a, vdd)" in mismatch["detail"]
    assert "(a, b, vdd)" in mismatch["detail"]


def test_pin_order_mismatch_none_when_order_agrees(tmp_path):
    schematic, extracted = _write_pair(tmp_path, ["a", "b"], ["a", "b"])
    assert (
        _pin_order_mismatch(
            reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
        )
        is None
    )


def test_pin_order_mismatch_ignores_case_only_differences(tmp_path):
    schematic, extracted = _write_pair(tmp_path, ["RA", "RB"], ["ra", "rb"])
    assert (
        _pin_order_mismatch(
            reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
        )
        is None
    )
    schematic, extracted = _write_pair(tmp_path, ["RB", "RA"], ["ra", "rb"])
    assert (
        _pin_order_mismatch(
            reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
        )
        is not None
    )


def test_pin_order_mismatch_none_when_counts_or_sets_differ(tmp_path):
    schematic, extracted = _write_pair(tmp_path, ["b", "a"], ["a", "b", "vsub"])
    assert (
        _pin_order_mismatch(
            reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
        )
        is None
    )
    assert (
        _pin_count_mismatch(reference_netlist=schematic, extracted_netlist=extracted)
        is not None
    )
    schematic, extracted = _write_pair(tmp_path, ["b", "a"], ["a", "c"])
    assert (
        _pin_order_mismatch(
            reference_netlist=schematic, extracted_netlist=extracted, top_cell="RES"
        )
        is None
    )


def test_pin_order_mismatch_none_for_flat_schematic(tmp_path):
    dut = _write_flat_dut(tmp_path)
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT RES RA RB\nR1 RA RB 1\n.ENDS RES\n")
    assert (
        _pin_order_mismatch(
            reference_netlist=dut, extracted_netlist=str(extracted), top_cell="RES"
        )
        is None
    )


def test_run_pex_pin_order_mismatch_skips_extracted_side(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #2890: same pins, different order -> flagged pre-flight, the
    extracted leg is never simulated, rows error (exit 4 path)."""
    import klayout_tools.pex as pex_module

    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "corners": [
                    {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]}
                ],
                "corner_count": 1,
                "measurements": [{"name": "vout"}],
            }
        raise AssertionError("extracted leg must not run on pin_order_mismatch")

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(".SUBCKT RES RB RA\nR1 RA RB 289.2\n.ENDS RES\n")
    tb = tmp_path / "testbench.spice"
    tb.write_text(
        f'.include "{dut}"\n.model res_generic_po r\n'
        "Vdd RA 0 DC 1.8\nXres RB RA RES\nRload RB 0 1k\n"
    )
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert calls["n"] == 1
    assert report["status"] == "error"
    assert all(row["status"] == "error" for row in report["delta"])
    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None
    mismatch = report["pin_order_mismatch"]
    assert mismatch is not None
    assert mismatch["schematic"]["pins"] == ["RB", "RA"]
    assert mismatch["extracted"]["pins"] == ["RA", "RB"]


def test_run_pex_testbench_netlist_not_found_raises(tmp_path, resistor_layout):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "does-not-exist.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [],
            }
        )
    )

    with pytest.raises(PexError, match="netlist not found"):
        run_pex(resistor_layout, [str(request)], "sky130")


def test_run_pex_disagreeing_reference_netlists_raises_before_simulating(
    tmp_path, resistor_layout, monkeypatch
):
    """The consistency check runs *before* any `klt sim` call -- confirmed
    here by making `run_sim` itself unreachable (monkeypatched to raise if
    ever called) and still getting the documented `PexError`, not a real
    (and therefore `ngspice`-requiring) simulation attempt."""
    import klayout_tools.pex as pex_module

    def _unreachable_run_sim(*_args, **_kwargs):
        raise AssertionError(
            "run_sim must not be called before the DUT-consistency check"
        )

    monkeypatch.setattr(pex_module, "run_sim", _unreachable_run_sim)

    dut_a = _write_schematic_dut(tmp_path / "dut_a.spice")
    tb_a = _write_testbench(tmp_path / "tb_a.spice", dut_a)
    request_a = _write_request(tmp_path / "request_a.json", tb_a)

    dut_b = _write_schematic_dut(tmp_path / "dut_b.spice", r_ohm=300.0)
    tb_b = _write_testbench(tmp_path / "tb_b.spice", dut_b)
    request_b = _write_request(tmp_path / "request_b.json", tb_b)

    with pytest.raises(PexError, match="different schematic DUT netlists"):
        run_pex(resistor_layout, [str(request_a), str(request_b)], "sky130")


def test_run_pex_bad_deck_raises_extract_error_wrapped(tmp_path, resistor_layout):
    dut = _write_schematic_dut(tmp_path / "dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match="extraction failed"):
        run_pex(resistor_layout, [str(request)], "not-a-real-deck")


# --------------------------------------------------------------------------- #
# Integration: a real, self-contained `klt pex` run against `ngspice`
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_run_pex_end_to_end(tmp_path, resistor_layout):
    """Extraction adds interconnect parasitic resistance/capacitance on top
    of the drawn device's own 289.2 ohm (issue #592's per-net star topology)
    -- so the extracted-side divider reads a measurably lower voltage than
    the ideal 289.2-ohm schematic reference, and that difference is exactly
    what `delta[]` should report."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["schema_version"] == 2
    assert report["status"] == "pass"
    # Issue #1261: `tmp_path` sits outside this repo, so every input-path
    # field normalises to `{"path": None, "scope": "external"}` -- the
    # absolute path is never echoed, even though it used to be.
    assert report["layout"] == {"path": None, "scope": "external"}
    assert report["reference_netlist"] == {"path": None, "scope": "external"}
    assert report["extraction"]["deck"] == "sky130"
    assert report["extraction"]["device_count"] == 1
    assert report["extraction"]["net_count"] == 2
    assert report["extraction"]["model"] is not None
    # Issue #1983: a drawn poly resistor has no MOS body to leave floating,
    # so the run states the clean verdict positively -- an item-7 reader can
    # now tell "checked, every body biased" from "never reported", which
    # before this field was indistinguishable.
    assert report["body_bias"] == {
        "status": "biased",
        "unbiased_device_count": 0,
        "unbiased_nets": [],
        "unbiased_pmos_body_nets": [],
    }
    assert report["corner_count"] == 1
    assert report["passed"] == 1
    assert report["failed"] == 0
    assert report["errored"] == 0
    # Issue #1996: a real comparison ran, so this `"pass"` is earned -- the
    # edge case the coverage block exists to distinguish from an empty
    # `delta[]`, checked here end to end against real ngspice rather than
    # only against a hand-built row list.
    assert report["coverage"] == {
        "testbenches": 1,
        "schema_version": 1,
        "known": True,
        "checked": ['comparison:[0,"default/novdd/27C","vout"]'],
        "skipped": [],
        "inapplicable": [],
        "unknown": [],
        "delta_rows": 1,
        "corners_compared": 1,
        "nothing_checked": False,
        "nothing_checked_reasons": [],
    }
    assert len(report["testbenches"]) == 1
    assert report["testbenches"][0]["measurement_names"] == ["vout"]
    # Issue #1261: same normalization applies to each `testbenches[]`
    # entry's own `request`/`schematic_netlist`.
    assert report["testbenches"][0]["request"] == {"path": None, "scope": "external"}
    assert report["testbenches"][0]["schematic_netlist"] == {
        "path": None,
        "scope": "external",
    }

    (row,) = report["delta"]
    assert row["spec_row"] == "vout"
    assert row["corner_id"] == "default/novdd/27C"
    assert row["status"] == "pass"
    # Ideal schematic divider: 1.8 * 1000 / (1000 + 289.2).
    assert row["schematic_value"] == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-3)
    # Extraction adds series interconnect R -- strictly less signal reaches
    # the load than the ideal schematic value.
    assert row["extracted_value"] < row["schematic_value"]
    assert row["delta_pct"] < 0

    assert report["provenance"]["deck"]["name"] == "sky130"
    assert report["provenance"]["input"]["content_hash"] is not None


@_SKIP_NO_NGSPICE
def test_integration_run_pex_pin_count_mismatch(
    tmp_path, resistor_layout_promoted_taps
):
    """Issue #1030, end to end against real `ngspice`: extraction promotes two
    physical tap nets (`VSUB`, `VNW`) to top-cell pins, so its
    `.SUBCKT RES RA RB VSUB VNW` header declares two more terminals than the
    hand-written schematic DUT's `.SUBCKT RES RA RB`. The schematic side runs
    fine; the extracted side is refused by ngspice with `Too few parameters
    for subcircuit type` -- and the report says so by name instead of leaving
    it buried in a per-corner `ngspice.log`.

    See `_PROMOTED_TAP_LABELS` for why the mismatch is two pins wide and in
    this direction: it is the only shape both ngspice 46 and CI's ngspice 42
    reject.
    """
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout_promoted_taps, [str(request)], "sky130")

    assert report["status"] == "error"
    assert report["errored"] == 1
    # A per-corner report is still emitted -- the schematic side measured
    # fine, only the extracted side has no trustworthy value.
    (row,) = report["delta"]
    assert row["schematic_value"] == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-3)
    assert row["extracted_value"] is None
    assert row["status"] == "error"

    mismatch = report["pin_count_mismatch"]
    assert mismatch is not None
    assert mismatch["subcircuit"].upper() == "RES"
    assert mismatch["schematic"]["pin_count"] == 2
    assert mismatch["schematic"]["pins"] == ["RA", "RB"]
    assert mismatch["extracted"]["pin_count"] == 4
    assert sorted(mismatch["extracted"]["pins"]) == ["RA", "RB", "VNW", "VSUB"]
    # Issue #1261: `pin_count_mismatch`'s own nested `netlist` fields are out
    # of this issue's confirmed scope and stay raw strings -- `report["netlist"]`
    # (the top-level field) is now the normalised `{path, scope}` shape, so
    # the two are no longer directly comparable; check against the raw
    # extracted-netlist path (`klt extract`'s own default naming: `<layout>`
    # with its extension replaced by `.spice`) instead.
    expected_extracted_netlist = (
        os.path.splitext(resistor_layout_promoted_taps)[0] + ".spice"
    )
    assert mismatch["extracted"]["netlist"] == expected_extracted_netlist
    assert report["netlist"] == {"path": None, "scope": "external"}


# --------------------------------------------------------------------------- #
# Extracted-side request copy: `models.lib` resolution (issue #1395)
# --------------------------------------------------------------------------- #


def _write_models_request(path: Path, testbench_path: Path, models: dict) -> Path:
    """A `klt sim` request carrying an explicit `models` block -- the shape
    `_prepare_extracted_request` has to carry through to its extracted-side
    copy without breaking the original's resolution semantics."""
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "models": models,
                "corners": {"process": ["tt"]},
                "measurements": [
                    {
                        "name": "vout",
                        "spice": ".meas tran vout FIND v(RB) AT=1u",
                    }
                ],
            }
        )
    )
    return path


def _make_fake_pdk_install(root: Path, variant: str = "sky130A") -> Path:
    """A minimal on-disk PDK install `pdk.find_pdk` recognises, with a
    model library at `<variant>/libs.tech/combined/sky130.lib.spice`
    carrying a single `tt` section.

    Deliberately synthetic and content-free (no vendored PDK data): the
    section holds only the generic resistor model `klt extract`'s sky130
    deck names for a drawn poly resistor, which is all these tests' decks
    reference.
    """
    variant_dir = root / variant
    (variant_dir / "libs.tech" / "klayout").mkdir(parents=True)
    combined = variant_dir / "libs.tech" / "combined"
    combined.mkdir(parents=True)
    (combined / "sky130.lib.spice").write_text(
        ".lib tt\n.model res_generic_po r\n.endl tt\n"
    )
    return combined / "sky130.lib.spice"


def _prepared_models(tmp_path: Path, models: dict) -> tuple[dict, Path, Path]:
    """Run `_prepare_extracted_request` over a request declaring ``models``
    and return `(extracted_models, request_dir, work_dir)`."""
    request_dir = tmp_path / "design"
    request_dir.mkdir(exist_ok=True)
    dut = _write_schematic_dut(request_dir / "schematic_dut.spice")
    tb = _write_testbench(request_dir / "testbench.spice", dut)
    request = _write_models_request(request_dir / "request.json", tb, models)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    extracted_request_path = _prepare_extracted_request(
        testbench_path=str(request),
        extracted_netlist_path=str(request_dir / "extracted.spice"),
        work_dir=str(work_dir),
        label="res",
    )
    with open(extracted_request_path, encoding="utf-8") as handle:
        return json.load(handle)["models"], request_dir, work_dir


def test_prepare_extracted_request_pdk_relative_lib_resolves_via_pdk(tmp_path):
    """Issue #1395: a `{"pdk": ..., "lib": "<relative>"}` models block must
    keep resolving against the *PDK-variant* directory on the extracted-side
    leg, exactly as `sim._resolve_models_lib` does for the schematic-side
    leg. Before the fix, `_prepare_extracted_request` unconditionally joined
    the relative `lib` against the *request file's* directory, producing
    `<request-dir>/libs.tech/combined/sky130.lib.spice` and a
    `model library not found` failure on the extracted-side leg only."""
    install_root = tmp_path / "install"
    lib_path = _make_fake_pdk_install(install_root)
    models = {
        "pdk": "sky130A",
        "pdk_root": str(install_root),
        "lib": "libs.tech/combined/sky130.lib.spice",
    }

    extracted_models, request_dir, work_dir = _prepared_models(tmp_path, models)

    # The extracted-side copy must resolve to the same library the original
    # request does -- not to a request-dir-relative join.
    assert sim._resolve_models_lib(
        extracted_models, request_dir=str(work_dir)
    ) == sim._resolve_models_lib(models, request_dir=str(request_dir))
    assert sim._resolve_models_lib(extracted_models, request_dir=str(work_dir)) == str(
        lib_path
    )
    assert str(request_dir) not in json.dumps(extracted_models)


def test_prepare_extracted_request_literal_relative_lib_still_resolved_eagerly(
    tmp_path,
):
    """Regression guard for the no-`pdk` shape: a plain relative `lib` (no
    `models.pdk`/`models.pdk_root`) is genuinely request-dir-relative, so it
    must still be rewritten to an absolute path -- the extracted-side copy
    lives in `work_dir`, where the original relative string would resolve
    against the wrong directory."""
    request_dir = tmp_path / "design"
    request_dir.mkdir(parents=True, exist_ok=True)
    lib_path = request_dir / "corner.lib"

    extracted_models, request_dir, work_dir = _prepared_models(
        tmp_path, {"lib": "corner.lib"}
    )
    lib_path.write_text(".lib tt\n.endl tt\n")

    assert extracted_models["lib"] == str(lib_path)
    assert sim._resolve_models_lib(extracted_models, request_dir=str(work_dir)) == str(
        lib_path
    )


def test_prepare_extracted_request_env_var_lib_left_expandable(tmp_path, monkeypatch):
    """Regression guard for the documented workaround shape
    (`"$PDK_ROOT/<variant>/<path>"`, no `models.pdk`): env-var expansion
    happens in `_resolve_relative`, so the eagerly-resolved copy must still
    point at the same absolute file."""
    install_root = tmp_path / "install"
    lib_path = _make_fake_pdk_install(install_root)
    monkeypatch.setenv("PDK_ROOT", str(install_root))

    extracted_models, _request_dir, work_dir = _prepared_models(
        tmp_path, {"lib": "$PDK_ROOT/sky130A/libs.tech/combined/sky130.lib.spice"}
    )

    assert sim._resolve_models_lib(extracted_models, request_dir=str(work_dir)) == str(
        lib_path
    )


def test_prepare_extracted_request_absolute_lib_with_pdk_left_untouched(tmp_path):
    """An already-absolute `lib` alongside `models.pdk` must survive
    verbatim (`_resolve_models_lib`'s own `os.path.isabs` check)."""
    install_root = tmp_path / "install"
    lib_path = _make_fake_pdk_install(install_root)
    models = {"pdk": "sky130A", "pdk_root": str(install_root), "lib": str(lib_path)}

    extracted_models, _request_dir, work_dir = _prepared_models(tmp_path, models)

    assert extracted_models["lib"] == str(lib_path)
    assert sim._resolve_models_lib(extracted_models, request_dir=str(work_dir)) == str(
        lib_path
    )


def test_prepare_extracted_request_without_models_block_is_unchanged(tmp_path):
    """A request with no `models` block at all (no `corners.process` axis)
    must not grow one."""
    request_dir = tmp_path / "design"
    request_dir.mkdir()
    dut = _write_schematic_dut(request_dir / "schematic_dut.spice")
    tb = _write_testbench(request_dir / "testbench.spice", dut)
    request = _write_request(request_dir / "request.json", tb)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    extracted_request_path = _prepare_extracted_request(
        testbench_path=str(request),
        extracted_netlist_path=str(request_dir / "extracted.spice"),
        work_dir=str(work_dir),
        label="res",
    )
    with open(extracted_request_path, encoding="utf-8") as handle:
        extracted_request = json.load(handle)

    assert "models" not in extracted_request
    assert extracted_request["netlist_source"] == "extracted"


@_SKIP_NO_NGSPICE
def test_integration_run_pex_pdk_relative_models_lib_both_legs(
    tmp_path, resistor_layout
):
    """Issue #1395 end to end, against real `ngspice`: the reported repro
    shape -- a request with `models: {"pdk": ..., "lib": "<relative>"}` and a
    `corners.process` axis. Before the fix the schematic-side leg passed and
    the extracted-side leg alone failed with `model library not found:
    <request-dir>/libs.tech/combined/sky130.lib.spice`, aborting the whole
    run with a `PexError`. Both legs must now simulate."""
    install_root = tmp_path / "install"
    _make_fake_pdk_install(install_root)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    # No inline `.model` card: the model comes from the PDK-resolved library,
    # which is the whole point of the `{"pdk": ..., "lib": ...}` shape.
    tb = tmp_path / "testbench.spice"
    tb.write_text(
        f'.include "{dut}"\nVdd RA 0 DC 1.8\nXres RA RB RES\nRload RB 0 1000.0\n'
    )
    request = _write_models_request(
        tmp_path / "request.json",
        tb,
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/combined/sky130.lib.spice",
        },
    )

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["status"] == "pass"
    assert report["errored"] == 0
    (row,) = report["delta"]
    assert row["status"] == "pass"
    assert row["schematic_value"] == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-3)
    # Both legs actually ran: the extracted side has a real measured value.
    assert row["extracted_value"] is not None
    assert row["extracted_value"] < row["schematic_value"]


# --------------------------------------------------------------------------- #
# Path normalization (issue #1261): input paths report `{path, scope}`, never
# a raw absolute string -- a committed evidence record wraps this response
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


@_SKIP_NO_NGSPICE
def test_run_pex_paths_are_repo_relative_inside_a_repo(tmp_path):
    """The regression the issue's own Test Plan calls out: an input path
    *inside* the repo must still resolve to something usable (a
    repo-relative string, `scope: "repo"`) -- the fix only changes the
    outside-the-repo (leaking) case, never the in-repo one."""
    root = _make_fake_repo(tmp_path)
    layout_path = _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    request = _write_request(root / "request.json", tb)

    report = run_pex(layout_path, [str(request)], "sky130")

    assert report["layout"] == {"path": "res.gds", "scope": "repo"}
    assert report["netlist"] == {"path": "res.spice", "scope": "repo"}
    assert report["reference_netlist"] == {
        "path": "schematic_dut.spice",
        "scope": "repo",
    }
    (tb_summary,) = report["testbenches"]
    assert tb_summary["request"] == {"path": "request.json", "scope": "repo"}
    assert tb_summary["schematic_netlist"] == {
        "path": "schematic_dut.spice",
        "scope": "repo",
    }


@_SKIP_NO_NGSPICE
def test_run_pex_a_repo_relative_request_argument_is_not_doubly_nested(tmp_path):
    """Edge case from the issue's Test Plan: `<testbench>` is already given
    as a *relative* CLI argument (relative to the current process's cwd, not
    to the repo root) -- normalization must resolve it once, not treat the
    already-relative string as if it needed a second round of relativizing
    (which would nest it under itself or otherwise malform it)."""
    root = _make_fake_repo(tmp_path)
    layout_path = _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    request = _write_request(root / "request.json", tb)

    cwd = os.getcwd()
    os.chdir(root)
    try:
        report = run_pex(
            os.path.relpath(layout_path, root), [os.path.basename(request)], "sky130"
        )
    finally:
        os.chdir(cwd)

    assert report["layout"] == {"path": "res.gds", "scope": "repo"}
    (tb_summary,) = report["testbenches"]
    assert tb_summary["request"] == {"path": "request.json", "scope": "repo"}


@_SKIP_NO_NGSPICE
def test_run_pex_outdir_relative_with_dotdot_does_not_double_the_path(tmp_path):
    """Issue #1378 Bug 1: a `--outdir`/`artifacts_dir` value that is
    *relative* and climbs out of the current working directory (e.g.
    `../reports/klt-pex-artifacts/<name>`, invoked from a directory one
    level below the reports directory) must resolve against cwd exactly
    once. Before the fix, `work_dir` in `run_pex` was `artifacts_dir` used
    verbatim (never made absolute) -- the still-relative `../...` string got
    written into the extracted-request JSON's `netlist` field, then
    `run_sim`/`_resolve_relative` re-joined it against an *already-resolved*
    request directory, doubling the `reports/klt-pex-artifacts/<name>`
    segment and raising "netlist not found". Resolving `work_dir` to an
    absolute path up front (mirroring the fallback branch, which already
    resolves `layout_path`) fixes this without changing any of the report's
    path-echo fields."""
    root = tmp_path
    layout_path = _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    request = _write_request(root / "request.json", tb)

    # cwd one level below the target reports dir, matching the issue's repro.
    testbench_dir = root / "testbench_dir"
    testbench_dir.mkdir()
    outdir = root / "reports" / "klt-pex-artifacts" / "res"

    cwd = os.getcwd()
    os.chdir(testbench_dir)
    try:
        report = run_pex(
            str(layout_path),
            [str(request)],
            "sky130",
            artifacts_dir=os.path.join("..", "reports", "klt-pex-artifacts", "res"),
        )
    finally:
        os.chdir(cwd)

    assert report["status"] != "error" or report.get("errored", 0) == 0
    assert outdir.is_dir()


@_SKIP_NO_NGSPICE
def test_run_pex_outdir_plain_relative_path_still_works(tmp_path):
    """Regression guard: `--outdir`/`artifacts_dir` as a plain relative
    subdirectory (no `../` segments) must keep working unchanged."""
    root = tmp_path
    layout_path = _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    request = _write_request(root / "request.json", tb)

    outdir = root / "artifacts"

    cwd = os.getcwd()
    os.chdir(root)
    try:
        report = run_pex(
            str(layout_path),
            [str(request)],
            "sky130",
            artifacts_dir="artifacts",
        )
    finally:
        os.chdir(cwd)

    assert report["status"] != "error" or report.get("errored", 0) == 0
    assert outdir.is_dir()


@_SKIP_NO_NGSPICE
def test_run_pex_outdir_absolute_path_still_works(tmp_path):
    """Regression guard: `--outdir`/`artifacts_dir` as an absolute path must
    keep working unchanged (this already worked before the fix)."""
    root = tmp_path
    layout_path = _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    request = _write_request(root / "request.json", tb)

    outdir = root / "abs-artifacts"

    report = run_pex(
        str(layout_path),
        [str(request)],
        "sky130",
        artifacts_dir=str(outdir),
    )

    assert report["status"] != "error" or report.get("errored", 0) == 0
    assert outdir.is_dir()


@_SKIP_NO_NGSPICE
def test_run_pex_relative_output_and_outdir_extracted_side_still_simulates(
    tmp_path,
):
    """Issue #1525: `-o`/`--output` and `--outdir`, both given *relative* to
    the caller's own cwd, must not break the extracted-side simulation.

    Before the fix, `run_extract`'s `netlist_path` (which `-o`/`--output`
    controls) was echoed back exactly as spelled -- relative stays relative
    -- and that same relative string was baked verbatim into the generated
    extracted-side testbench's `.include` line. `run_sim`'s per-corner
    `ngspice -b` invocation resolves that line's `.include` against the
    *generated testbench file's own directory* (a corner-scoped subdirectory
    nested under `--outdir`), not the original cwd, so a relative `-o` value
    that was valid from the caller's cwd resolved to nothing there and every
    extracted-side corner errored with "Could not find include file".
    """
    root = _make_fake_repo(tmp_path)
    _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    _write_request(root / "request.json", tb)

    cwd = os.getcwd()
    os.chdir(root)
    try:
        report = run_pex(
            "res.gds",
            ["request.json"],
            "sky130",
            output=os.path.join("out", "extracted.spice"),
            artifacts_dir=os.path.join("out", "pex"),
        )
    finally:
        os.chdir(cwd)

    assert (root / "out" / "extracted.spice").is_file()
    assert report["errored"] == 0
    assert report["status"] == "pass"
    for row in report["delta"]:
        assert row["extracted_value"] is not None
    # The fix must not leak an absolute path into this run's own report --
    # `netlist` stays repo-relative (issue #1261), same shape a relative
    # `-o` already produced before this fix.
    assert report["netlist"]["scope"] == "repo"
    assert report["netlist"]["path"] == "out/extracted.spice"


@_SKIP_NO_NGSPICE
def test_run_pex_mixed_absolute_output_relative_outdir_still_simulates(tmp_path):
    """Issue #1525 edge case: an absolute `-o`/`--output` with a *relative*
    `--outdir` must also simulate cleanly -- both share the same
    unresolved-`netlist_path` root cause, so both directions are covered."""
    root = tmp_path
    _write_gds(_make_sky130_poly_resistor_layout(), root / "res.gds")
    dut = _write_schematic_dut(root / "schematic_dut.spice")
    tb = _write_testbench(root / "testbench.spice", dut)
    _write_request(root / "request.json", tb)

    absolute_output = root / "extracted.spice"

    cwd = os.getcwd()
    os.chdir(root)
    try:
        report = run_pex(
            "res.gds",
            ["request.json"],
            "sky130",
            output=str(absolute_output),
            artifacts_dir="pex-out",
        )
    finally:
        os.chdir(cwd)

    assert absolute_output.is_file()
    assert report["errored"] == 0
    assert report["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_integration_run_pex_pin_count_mismatch_cli(
    tmp_path, resistor_layout_promoted_taps, capsys
):
    """The same run through the CLI: exit code 4 (a delta row errored, not a
    hard exit-1 abort) and the named diagnostic present in both output
    formats."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    argv = [
        "pex",
        str(resistor_layout_promoted_taps),
        str(request),
        "--deck",
        "sky130",
    ]

    exit_code = main([*argv, "--format", "json"])
    assert exit_code == 4
    payload = json.loads(capsys.readouterr().out)
    mismatch = payload["pin_count_mismatch"]
    assert mismatch["schematic"]["pin_count"] == 2
    assert mismatch["extracted"]["pin_count"] == 4

    exit_code = main(argv)
    assert exit_code == 4
    out = capsys.readouterr().out
    assert "pin_count_mismatch:" in out
    assert "schematic:" in out
    assert "pins=2" in out
    assert "pins=4" in out


@_SKIP_NO_NGSPICE
def test_integration_run_pex_flat_dut_mismatch(tmp_path, resistor_layout):
    """Gap 2, issue #1255, end to end against real `ngspice`: the schematic
    DUT is netlisted flat (no `.SUBCKT` wrapper), with the testbench wiring
    its source/load directly to the DUT's own `RA`/`RB` nodes -- exactly
    the shape a real EDA tool's flat top-level netlist mode produces.
    `klt extract` always wraps its output in `.SUBCKT RES ...`, so the
    mismatch is detected proactively (before the extracted side is ever
    attempted) rather than surfacing only once ngspice rejects (or,
    depending on version/`.ic` usage, silently mis-simulates) the
    disconnected extracted-side deck."""
    dut = _write_flat_dut(tmp_path)
    tb = tmp_path / "testbench.spice"
    tb.write_text(f'.include "{dut}"\nVdd RA 0 DC 1.8\nRload RB 0 1k\n')
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["status"] == "error"
    assert report["errored"] == 1
    (row,) = report["delta"]
    # The schematic side still measures fine (a real ngspice run of the
    # flat DUT) -- only the extracted side has no trustworthy value,
    # exactly as `pin_count_mismatch`'s own shape already establishes.
    assert row["schematic_value"] == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-3)
    assert row["extracted_value"] is None
    assert row["status"] == "error"
    assert report["pin_count_mismatch"] is None

    mismatch = report["flat_dut_mismatch"]
    assert mismatch is not None
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["schematic"]["pin_count"] is None
    assert mismatch["extracted"]["pins"] == ["RA", "RB"]


@_SKIP_NO_NGSPICE
def test_integration_run_pex_flat_dut_mismatch_cli(tmp_path, resistor_layout, capsys):
    """The same run through the CLI: exit code 4 and the named diagnostic
    present in both output formats."""
    dut = _write_flat_dut(tmp_path)
    tb = tmp_path / "testbench.spice"
    tb.write_text(f'.include "{dut}"\nVdd RA 0 DC 1.8\nRload RB 0 1k\n')
    request = _write_request(tmp_path / "request.json", tb)
    argv = ["pex", str(resistor_layout), str(request), "--deck", "sky130"]

    exit_code = main([*argv, "--format", "json"])
    assert exit_code == 4
    payload = json.loads(capsys.readouterr().out)
    mismatch = payload["flat_dut_mismatch"]
    assert mismatch["subcircuit"] == "RES"
    assert mismatch["extracted"]["pin_count"] == 2

    exit_code = main(argv)
    assert exit_code == 4
    out = capsys.readouterr().out
    assert "flat_dut_mismatch:" in out
    assert "declares no `.SUBCKT`" in out


@_SKIP_NO_NGSPICE
def test_integration_run_pex_pin_identical_interfaces_no_mismatch(
    tmp_path, resistor_layout
):
    """Regression guard for the false-positive edge case: a pin-identical
    schematic/extracted pair still runs to `pass` with `pin_count_mismatch:
    null`."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["status"] == "pass"
    assert report["pin_count_mismatch"] is None


@_SKIP_NO_NGSPICE
def test_integration_run_pex_multiple_testbenches(tmp_path, resistor_layout):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")

    tb_a = _write_testbench(tmp_path / "tb_gain.spice", dut, load_ohm=1000.0)
    request_a = _write_request(tmp_path / "request_gain.json", tb_a)

    tb_b = _write_testbench(tmp_path / "tb_psrr.spice", dut, load_ohm=2000.0)
    request_b = _write_request(tmp_path / "request_psrr.json", tb_b)

    report = run_pex(resistor_layout, [str(request_a), str(request_b)], "sky130")

    assert report["status"] == "pass"
    assert len(report["delta"]) == 2
    assert {row["spec_row"] for row in report["delta"]} == {
        "request_gain.vout",
        "request_psrr.vout",
    }
    assert len(report["testbenches"]) == 2


@_SKIP_NO_NGSPICE
def test_run_pex_critical_nets_threaded_through_to_extraction(
    tmp_path, resistor_layout
):
    """`--critical-net` (issue #976, Epic #709 Phase 2a) is forwarded from
    `run_pex` to `run_extract`, and echoed back in `extraction.critical_nets`
    -- a fully wired unit-of-work test, distinct from
    `tests/test_extract.py`'s own exact-value lateral-coupling geometry
    tests. `RB` (this fixture's one labelled met1/li1 net) has no same-layer
    neighbour, so this also exercises the "declared but geometrically
    unmatched" path end to end without raising -- `run_pex` must still
    complete and report the same result as the no-critical-nets baseline
    (this fixture has no lateral-coupling geometry to add)."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130", critical_nets=["RB"])

    assert report["extraction"]["critical_nets"] == ["RB"]
    assert report["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_run_pex_critical_net_pattern_threaded_through_to_extraction(
    tmp_path, resistor_layout
):
    """`--critical-net-pattern` (issue #2974) is forwarded to `run_extract`;
    `extraction.critical_nets` echoes the resolved concrete names."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout, [str(request)], "sky130", critical_net_patterns=["R*"]
    )

    assert report["extraction"]["critical_nets"] == ["RA", "RB"]
    assert report["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_run_pex_parasitic_variant_threaded_through_to_extraction(
    tmp_path, resistor_layout
):
    """`--parasitic-variant` (issue #3056) is forwarded from `run_pex` to
    `run_extract` and echoed in `extraction.parasitic_variant`; absent
    otherwise."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        parasitic_variants=["drop-c-ground:*"],
    )

    assert report["extraction"]["parasitic_variant"] == ["drop-c-ground:*"]
    baseline = run_pex(resistor_layout, [str(request)], "sky130")
    assert "parasitic_variant" not in baseline["extraction"]


@_SKIP_NO_NGSPICE
def test_run_pex_distributed_rc_threaded_through_to_extraction(
    tmp_path, resistor_layout
):
    """`--distributed-rc` (issue #977, Epic #709 Phase 2b) is forwarded from
    `run_pex` to `run_extract`, and echoed back in
    `extraction.distributed_rc` -- a fully wired unit-of-work test, distinct
    from `tests/test_extract.py`'s own exact-value distributed-RC geometry
    tests and `test_run_pex_distributed_rc_canary` below's own fidelity
    proof. `RB` (this fixture's one labelled met1/li1 net) has exactly 1
    device terminal (its own resistor's `RB` pin) -- fewer than 2, so this
    also exercises the "named but nothing to chain, falls back to the star"
    path end to end without raising."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        critical_nets=["RB"],
        distributed_rc=True,
    )

    assert report["extraction"]["critical_nets"] == ["RB"]
    assert report["extraction"]["distributed_rc"] is True
    assert report["status"] == "pass"


@_SKIP_NO_NGSPICE
def test_run_pex_parasitic_integrity_propagates_matching_payloads(
    tmp_path, resistor_layout
):
    """Issue #2764: `extraction.parasitic_integrity` carries the
    extraction's own integrity diagnostics -- summary fields verbatim, and
    per-net entries whose `integrity` payloads are byte-identical to what
    `klt extract --parasitics` inlines on the same layout's
    `parasitics.nets[]` entries (same order, same content)."""
    from klayout_tools.extract import run_extract

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    propagated = report["extraction"]["parasitic_integrity"]
    assert propagated is not None
    summary_keys = {
        "short_threshold_ohm",
        "cap_tolerance",
        "max_detail_items",
        "nets_checked",
        "nets_with_findings",
        "open_nets",
        "island_components",
        "orphan_nodes",
        "suspicious_resistors",
        "short_candidates",
        "capacitance_mismatches",
    }
    assert summary_keys <= set(propagated)
    direct = run_extract(
        resistor_layout,
        "sky130",
        output=str(tmp_path / "direct.spice"),
        parasitics=True,
    )
    direct_summary = direct["parasitics"]["integrity"]
    for key in summary_keys:
        assert propagated[key] == direct_summary[key]
    assert len(propagated["nets"]) == len(direct["parasitics"]["nets"])
    for pex_net, direct_net in zip(
        propagated["nets"], direct["parasitics"]["nets"], strict=True
    ):
        assert pex_net["net"] == direct_net["net"]
        assert pex_net["net_id"] == direct_net["net_id"]
        assert pex_net["rc_model"] == direct_net["rc_model"]
        assert pex_net["integrity"] == direct_net["integrity"]
    # A healthy extraction trips nothing, and the pex verdict is untouched.
    assert propagated["nets_with_findings"] == 0
    assert report["status"] == "pass"


def test_run_pex_parasitic_integrity_none_without_the_field(
    tmp_path, resistor_layout, monkeypatch
):
    """An extraction report predating issue #2764 (no `integrity` summary)
    propagates `parasitic_integrity: null` instead of crashing -- the same
    tolerance `_body_bias_report`'s callers already extend to older
    extractions."""
    calls = _install_two_call_fake_run_sim(monkeypatch)
    assert calls is not None  # the fake answers both legs without ngspice

    import klayout_tools.pex as pex_module

    real_run_extract = pex_module.run_extract

    def strip_integrity(*args, **kwargs):
        report = real_run_extract(*args, **kwargs)
        if report.get("parasitics"):
            report["parasitics"].pop("integrity", None)
            for entry in report["parasitics"]["nets"]:
                entry.pop("integrity", None)
        return report

    monkeypatch.setattr(pex_module, "run_extract", strip_integrity)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["extraction"]["parasitic_integrity"] is None
    assert report["status"] == "pass"


def test_parasitic_integrity_report_unit_shapes():
    """`_parasitic_integrity_report` itself: the summary-plus-nets assembly
    for a well-formed `parasitics` block, and `None` for anything without
    the summary field."""
    from klayout_tools.pex import _parasitic_integrity_report

    parasitics = {
        "integrity": {
            "short_threshold_ohm": 1.0,
            "cap_tolerance": {"absolute_ff": 1e-06, "relative": 1e-09},
            "max_detail_items": 20,
            "nets_checked": 1,
            "nets_with_findings": 1,
            "open_nets": 1,
            "island_components": 0,
            "orphan_nodes": 0,
            "suspicious_resistors": 0,
            "short_candidates": 0,
            "capacitance_mismatches": 0,
        },
        "nets": [
            {
                "net": "clk",
                "net_id": 2,
                "rc_model": "distributed",
                "integrity": {"open": {"status": "open"}},
            },
            {"net": "old", "net_id": 3, "rc_model": "lumped"},  # pre-#2764 entry
        ],
    }
    block = _parasitic_integrity_report(parasitics)
    assert block is not None
    assert block["short_threshold_ohm"] == 1.0
    assert block["nets"] == [
        {
            "net": "clk",
            "net_id": 2,
            "rc_model": "distributed",
            "integrity": {"open": {"status": "open"}},
        }
    ]
    assert _parasitic_integrity_report({"nets": []}) is None
    assert _parasitic_integrity_report({}) is None


def test_run_pex_distributed_rc_requires_critical_net(tmp_path, resistor_layout):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match="--distributed-rc requires --critical-net"):
        run_pex(resistor_layout, [str(request)], "sky130", distributed_rc=True)


@_SKIP_NO_NGSPICE
def test_run_pex_substrate_tie_ohms_forwarded_to_extraction(
    tmp_path, resistor_layout, monkeypatch
):
    """`--substrate-tie-ohms` (issue #3089) reaches `run_extract` unchanged
    (and `None` -- the default tie -- when the flag is not given)."""
    import klayout_tools.pex as pex_module

    seen: list[object] = []
    real_run_extract = pex_module.run_extract

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("substrate_tie_ohms", "missing"))
        return real_run_extract(*args, **kwargs)

    monkeypatch.setattr(pex_module, "run_extract", _spy)
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    run_pex(resistor_layout, [str(request)], "sky130", substrate_tie_ohms=1e-3)
    run_pex(resistor_layout, [str(request)], "sky130")

    assert seen == [1e-3, None]


@_SKIP_NO_NGSPICE
def test_run_pex_mom_rlc_net_threaded_through_to_extraction(tmp_path, resistor_layout):
    """`--mom-rlc-net`/`--mom-rlc-resistance-ohm`/`--mom-rlc-capacitance-ff`
    (issue #988, Epic #709 Phase 3a) are forwarded from `run_pex` to
    `run_extract`, and the substitution report is echoed back in
    `extraction.mom_rlc_override` -- a fully wired unit-of-work test, distinct
    from `tests/test_extract.py`'s own exact-value substitution tests. `RB`
    (this fixture's one labelled met1/li1 net, also this resistor's own load
    node) genuinely carries lumped-RC parasitics, so the override measurably
    changes the extracted-side re-simulation's own value versus the
    no-override baseline."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    baseline = run_pex(resistor_layout, [str(request)], "sky130")
    assert baseline["extraction"]["mom_rlc_override"] is None

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        mom_rlc_net="RB",
        mom_rlc_resistance_ohm=50_000.0,
        mom_rlc_capacitance_ff=500.0,
    )

    override = report["extraction"]["mom_rlc_override"]
    assert override is not None
    assert override["net"] == "RB"
    assert override["resistance_ohm"] == 50_000.0
    assert override["capacitance_ff"] == 500.0
    assert override["inductance_nh"] is None
    assert report["status"] == "pass"

    # A much larger series resistance on `RB` measurably moves the
    # extracted-side re-simulation's own value versus the no-override
    # baseline (both share the identical schematic-side value/testbench).
    baseline_row = baseline["delta"][0]
    override_row = report["delta"][0]
    assert baseline_row["schematic_value"] == override_row["schematic_value"]
    assert override_row["extracted_value"] != baseline_row["extracted_value"]


def _make_pex_lateral_coupling_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two same-layer (met1) nets, ``AGR`` ("aggressor") and ``VIC``
    ("victim"), 0.1 um apart -- well within sky130 met1's own lateral-
    coupling lookback (``metal_sidewall_lookback_um[1]`` = 0.28 um) --
    purpose-built as the Phase 2a (issue #976) canary fixture below. Zero
    active devices and zero vertical (adjacent-metal-level) relationship
    between the two nets, so the *only* possible schematic-vs-extracted
    coupling path is the one ``--critical-net`` adds -- issue #760's
    already-shipped, unconditional vertical-overlap coupling contributes
    nothing here. Mirrors `tests/test_extract.py`'s own
    ``_make_lateral_coupling_layout`` geometry (kept self-contained here,
    per this module's existing no-cross-module-fixture-imports
    convention)."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(68, 20, kdb.Box(0, 0, 2000, 1000))  # met1, net AGR
    label(68, 5, "AGR", 1000, 500)
    draw(68, 20, kdb.Box(2100, 0, 4100, 1000))  # met1, net VIC -- 0.1 um gap
    label(68, 5, "VIC", 3100, 500)

    return layout


def _write_isolated_schematic_dut(path: Path) -> Path:
    """The Phase 1 schematic-level ideal for the canary below: ``AGR``/
    ``VIC`` are two entirely separate pins with **zero** devices between
    them -- no coupling of any kind, the textbook pre-#976 assumption
    lateral coupling closes part of the gap on."""
    path.write_text(".SUBCKT TOP AGR VIC\n.ENDS TOP\n")
    return path


def _write_lateral_coupling_testbench(path: Path, dut_path: Path) -> Path:
    """A fast (100 ps) step on ``AGR``; ``VIC`` is a genuine high-impedance
    node (Epic #709 Phase 2's own framing) -- a 1 Gohm pull to ground, no
    active driver -- so any coupling capacitance onto it from ``AGR`` shows
    up directly as a transient voltage kick. ``.global vsubs``/``Vsubs``
    ties the extraction's synthesized substrate net (``ground_net_name``,
    ``extract.py``) to a real 0 V reference -- required because this
    fixture has no diffusion/well geometry of its own to tie it down
    structurally, unlike a real chip's substrate contact network, so
    without it ``vsubs`` would float as one shared *internal* node of this
    single ``Xdut`` instance and create its own accidental AGR<->VIC
    coupling path via ``CAGR``/``CVIC`` in series -- an artifact of this
    minimal two-net fixture having no other substrate tie, not a real
    finding. ``.options rshunt=1e12`` matches the same floating-node
    convergence workaround ``examples/design-pipeline/09-sim.testbench.
    spice`` already documents for this deck's own composed-layout canary
    (issue #205).

    **Kept deliberately after issue #1263.** ``klt extract --parasitics``
    now writes its own ``Rvsubs_dctie vsubs 0 1e12`` DC reference into the
    extracted ``.SUBCKT`` body, so the *convergence* half of the cards below
    (``.options rshunt``, and the DC-solve role of ``Vsubs``) is redundant
    -- and harmlessly so, 1 Tohm in parallel with an ideal 0 V source draws
    ~0 A. What is **not** redundant is ``Vsubs``'s role as an **AC** ground:
    1 Tohm against this fixture's ~0.3 fF is an RC time constant nine orders
    of magnitude longer than the 5 ns transient below, so without the hard
    tie ``vsubs`` still floats *dynamically* and reinstates exactly the
    accidental ``CAGR``/``CVIC`` series coupling path described above. The
    cards stay."""
    path.write_text(
        f'.include "{dut_path}"\n'
        ".options rshunt=1e12\n"
        ".global vsubs\n"
        "Vsubs vsubs 0 DC 0\n"
        "Vagr AGR 0 PULSE(0 1 0 100p 100p 100n 200n)\n"
        "Rvic_hiz VIC 0 1e9\n"
        "Xdut AGR VIC TOP\n"
    )
    return path


def _write_lateral_coupling_request(path: Path, testbench_path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1p 5n"},
                "measurements": [
                    {
                        "name": "vic_kick",
                        "spice": ".meas tran vic_kick FIND V(VIC) AT=2n",
                    }
                ],
            }
        )
    )
    return path


@_SKIP_NO_NGSPICE
def test_run_pex_critical_net_lateral_coupling_canary(tmp_path):
    """**Phase 2a canary (issue #976, Epic #709)**: a real, ngspice-driven
    `klt pex` run demonstrating a measurable, explainable delta from
    coupling capacitance vs. Phase 1's lumped-RC-only baseline (issue
    #801/#802/#803) -- the acceptance criterion this test exists to prove,
    not merely a unit test of the CLI/library wiring (that is
    `test_run_pex_critical_nets_threaded_through_to_extraction` above).

    `AGR`/`VIC` (:func:`_make_pex_lateral_coupling_layout`) are two isolated
    met1 nets with zero devices and zero vertical relationship between
    them -- issue #760's already-shipped, unconditional vertical-overlap
    coupling contributes exactly nothing here, isolating this canary to
    issue #976's own lateral pass. `VIC` is a genuine high-impedance node
    (a 1 Gohm pull to ground, no active driver) -- the exact net class
    Epic #709 Phase 2's own text names as this phase's target ("high-
    impedance nodes").

    **Without `--critical-net`** (Phase 1's own `klt pex` baseline, issue
    #801/#802/#803, unchanged by this issue): the extracted netlist has no
    AGR<->VIC coupling path at all, so a 1 V step on `AGR` leaves `VIC`'s
    high-impedance node exactly at its 0 V bias -- `extracted_value == 0.0`,
    measured, not assumed.

    **With `--critical-net VIC`** (this issue): the identical step now
    visibly couples onto `VIC` through the lateral coupling capacitor the
    deck's `metal_sidewalls[1]` (met1) coefficient adds between the two
    nets' parasitic hub nodes -- `extracted_value` jumps to a real,
    nonzero, reproducible voltage shortly after the step.

    A worked-example (`examples/design-pipeline`'s `sky130-ota-5t` canary,
    `docs/cli/pex.md`) is not a fit for this proof: every one of its
    measurement points is an ideal-voltage-source-driven net hub (its own
    "Every row reads `delta_pct: 0.0`" explanation, PR #973), so it has no
    current/charge-carrying, high-impedance node for a coupling-capacitance
    delta to show up on. This test's fixture is purpose-built with exactly
    such a node instead, and is the reproducible, CI-verified canary this
    issue's acceptance criteria cite -- re-derived on every run rather than
    a one-off hand-captured evidence blob.
    """
    layout = _make_pex_lateral_coupling_layout()
    gds_path = _write_gds(layout, tmp_path / "lateral.gds")

    dut = _write_isolated_schematic_dut(tmp_path / "isolated_dut.spice")
    tb = _write_lateral_coupling_testbench(tmp_path / "coupling_tb.spice", dut)
    request = _write_lateral_coupling_request(tmp_path / "coupling.request.json", tb)

    baseline = run_pex(gds_path, [str(request)], "sky130")
    assert baseline["extraction"]["critical_nets"] == []
    baseline_row = baseline["delta"][0]
    assert baseline_row["extracted_value"] == pytest.approx(0.0, abs=1e-6)

    with_coupling = run_pex(gds_path, [str(request)], "sky130", critical_nets=["VIC"])
    assert with_coupling["extraction"]["critical_nets"] == ["VIC"]
    coupled_row = with_coupling["delta"][0]
    assert coupled_row["extracted_value"] == pytest.approx(0.129055, abs=1e-4)

    # The measurable, explainable delta itself: same testbench, same
    # geometry, same schematic reference -- the *only* variable is whether
    # `--critical-net` was given, and it visibly moves the extracted-side
    # measurement from "no coupling" to a real, nonzero coupling kick.
    assert coupled_row["extracted_value"] - baseline_row["extracted_value"] > 0.1


def _make_pex_distributed_rc_layout(top_name: str = "TOP") -> kdb.Layout:
    """Two sky130 poly resistors joined head-to-tail by a long (500 um) li1
    run -- ``SRC --R1(289.2 ohm)--> MID --R2(289.2 ohm)--> LOAD``, where
    ``MID`` is a genuinely internal net with real, substantial interconnect
    geometry of its own (2 device terminals: `R1`'s ``RB``, `R2`'s ``RA``),
    purpose-built as the Phase 2b (issue #977) canary fixture: a long, high-
    impedance routed net -- the exact shape Epic #709 Phase 2's own text
    names ("high-impedance nodes" / "a PLL loop filter"), and the shape
    `--distributed-rc` targets (2+ device terminals to chain into a ladder,
    unlike the Phase 2a canary's zero-device-terminal high-Z probe above).

    li1's own curated sheet resistance/capacitance (``ParasiticsDeck.metals[0]``,
    12.8 ohm/sq, 0.037 fF/um^2 area + 0.0407 fF/um perimeter) over a 500 um x
    0.6 um run gives ``MID`` roughly 11 kohm of series resistance and 53 fF
    of ground capacitance -- large enough that a fast (10 ps rise) step on
    ``SRC`` takes real, measurable time to reach ``MID``, which is exactly
    what distinguishes the star (all of `MID`'s capacitance at one hub, one
    resistor hop away) from the distributed ladder (`MID`'s capacitance
    split across its own node and its neighbour's, two resistor hops away
    from `SRC`) at an early sample point."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    poly = (66, 20)  # poly.drawing
    marker = (66, 13)  # poly.res
    contact = (66, 44)  # licon1.drawing
    li1 = (67, 20)  # li1.drawing
    li1_pin = (67, 5)  # li1.pin

    wire_len = 500_000  # 500 um, in database units (dbu = 0.001 um)

    # Resistor "R1": SRC -> MID (near end of the long li1 run).
    draw(*poly, kdb.Box(0, 0, 12000, 1000))
    draw(*marker, kdb.Box(3000, 0, 9000, 1000))
    draw(*contact, kdb.Box(1400, 400, 1600, 600))
    draw(*li1, kdb.Box(1100, 200, 1900, 800))
    label(*li1_pin, "SRC", 1500, 500)
    draw(*contact, kdb.Box(10400, 400, 10600, 600))
    draw(*li1, kdb.Box(10100, 200, 10100 + wire_len, 800))
    label(*li1_pin, "MID", 10500, 500)

    # Resistor "R2": MID (far end of the long li1 run) -> LOAD. Offset so
    # its own RA landing pad overlaps the wire's far edge (and nothing
    # else this resistor draws reaches back towards R1 or the wire).
    ox = (10100 + wire_len) - 800 - 1100
    draw(*poly, kdb.Box(0 + ox, 0, 12000 + ox, 1000))
    draw(*marker, kdb.Box(3000 + ox, 0, 9000 + ox, 1000))
    draw(*contact, kdb.Box(1400 + ox, 400, 1600 + ox, 600))
    draw(*li1, kdb.Box(1100 + ox, 200, 1900 + ox, 800))
    draw(*contact, kdb.Box(10400 + ox, 400, 10600 + ox, 600))
    draw(*li1, kdb.Box(10100 + ox, 200, 10900 + ox, 800))
    label(*li1_pin, "LOAD", 10500 + ox, 500)

    return layout


def _write_distributed_rc_schematic_dut(path: Path) -> Path:
    """The Phase 1 schematic-level ideal for the canary below: two 289.2 ohm
    resistors (:data:`_SHEET_RHO_RES_GENERIC_PO`'s own value, matching each
    drawn poly resistor's own body) in series through `MID` -- no
    interconnect R/C of any kind, so `V(MID)` tracks `V(SRC)` with zero
    delay (`Rload` below draws negligible current, so there is no resistive
    divider drop either)."""
    path.write_text(
        ".SUBCKT TOP LOAD MID SRC\nR1 SRC MID 289.2\nR2 MID LOAD 289.2\n.ENDS TOP\n"
    )
    return path


def _write_distributed_rc_testbench(path: Path, dut_path: Path) -> Path:
    """A fast (10 ps rise) 1 V step on `SRC`; `LOAD` is a genuine high-
    impedance node (a 1 Gohm pull to ground, no active driver), so the
    extracted-side `MID` node's own interconnect R/C is what determines how
    much of that step has reached `MID` at an early sample point --
    `.global vsubs`/`Vsubs` ties the extraction's synthesized substrate net
    to a real 0 V reference, the same convergence requirement
    `_write_lateral_coupling_testbench` above already documents (issue
    #205's floating-node workaround) -- without it every parasitic ground
    capacitor's `B` terminal floats, and `V(MID)` would track `V(SRC)`
    instantly regardless of any resistor in between (no real capacitive
    loading without a fixed reference).

    **Kept deliberately after issue #1263**, for the same reason
    `_write_lateral_coupling_testbench` above documents: the extraction's
    own `Rvsubs_dctie vsubs 0 1e12` card makes the *DC-convergence* role of
    these lines redundant (and idempotent -- nothing to remove), but this
    measurement needs `vsubs` held at a real 0 V **AC** reference, which a
    1 Tohm anchor does not provide on a 60 ps transient."""
    path.write_text(
        f'.include "{dut_path}"\n'
        ".model res_generic_po r\n"
        ".options rshunt=1e12\n"
        ".global vsubs\n"
        "Vsubs vsubs 0 DC 0\n"
        "Vsrc SRC 0 PULSE(0 1 0 10p 10p 2n 4n)\n"
        "Rload LOAD 0 1e9\n"
        "Xdut LOAD MID SRC TOP\n"
    )
    return path


def _write_distributed_rc_request(path: Path, testbench_path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench_path),
                "analysis": {"kind": "tran", "args": "1p 60p"},
                "measurements": [
                    {
                        "name": "mid_50p",
                        "spice": ".meas tran mid_50p FIND V(MID) AT=50p",
                    }
                ],
            }
        )
    )
    return path


@_SKIP_NO_NGSPICE
def test_run_pex_distributed_rc_canary(tmp_path):
    """**Phase 2b canary (issue #977, Epic #709)**: a real, ngspice-driven
    `klt pex` run demonstrating a measurable, explainable delta between the
    single-lumped-star model (Phase 2a's own baseline -- `--critical-net`
    given, `--distributed-rc` not) and the distributed multi-segment ladder
    (this issue -- both flags given) on the identical layout, testbench, and
    schematic reference -- the acceptance criterion this test exists to
    prove, not merely a unit test of the CLI/library wiring or the JSON
    shape (that is `tests/test_extract.py`'s own exact-value distributed-RC
    tests).

    `MID` (:func:`_make_pex_distributed_rc_layout`) is a genuine internal,
    2-device-terminal, high-impedance routed net (~11 kohm / ~53 fF of its
    own li1 interconnect) -- the exact net class Epic #709 Phase 2's own
    text names ("high-impedance nodes", "a PLL loop filter"). A fast (10 ps
    rise) step on `SRC` reaches `MID` through either 1 resistor hop (the
    star: `SRC -> MID__t0 -> MID` with `MID`'s *entire* capacitance sitting
    at the hub) or 2 (the ladder: `SRC -> MID__t0` first charges its own
    *local* capacitance, *then* the ~11 kohm segment resistor feeds `MID`'s
    own remaining capacitance) -- an extra pole the star topology cannot
    represent, since it always attaches a net's full capacitance directly
    to its own hub.

    **`--critical-net MID`, no `--distributed-rc`** (Phase 2a's own model,
    unchanged by this issue): at `t=50 ps`, `V(MID)` has already risen to
    roughly 13% of the step -- the star's one-hop-to-the-full-capacitance
    path responds faster than the physical two-hop ladder below.

    **`--critical-net MID --distributed-rc`** (this issue): the identical
    step, geometry, and measurement instant now reads a measurably smaller
    `V(MID)` -- the ladder's extra pole genuinely delays how much charge has
    reached `MID` by this early sample point, a real second-order effect a
    single lumped hub cannot represent by construction. Both models are
    computed from the *exact same* underlying `resistance_ohm`/
    `capacitance_ff` totals (conservation verified directly in
    `tests/test_extract.py`), so this is a difference in **where** that R/C
    sits along the net, not in how much of it there is -- and the extracted-
    side JSON's own `parasitics.nets[].segments`/`terminals` entries
    (`extraction` is not itself part of this command's `delta[]` output, but
    is available from the same `klt extract`/`klt pex` run, see
    `docs/cli/extract.md`) let a reader attribute the delta to the exact
    segment/node responsible -- something the star topology's single opaque
    hub capacitor cannot offer. That is this issue's own "more explainable"
    fidelity improvement over Phase 2a's baseline, distinct from (and not
    contingent on) which model's delta happens to have the smaller
    magnitude at any one arbitrarily-chosen sample instant on a transient
    that both models converge to the identical final value on."""
    layout = _make_pex_distributed_rc_layout()
    gds_path = _write_gds(layout, tmp_path / "distributed.gds")

    dut = _write_distributed_rc_schematic_dut(tmp_path / "distributed_dut.spice")
    tb = _write_distributed_rc_testbench(tmp_path / "distributed_tb.spice", dut)
    request = _write_distributed_rc_request(tmp_path / "distributed.request.json", tb)

    baseline = run_pex(gds_path, [str(request)], "sky130", critical_nets=["MID"])
    assert baseline["extraction"]["critical_nets"] == ["MID"]
    assert baseline["extraction"]["distributed_rc"] is False
    baseline_row = baseline["delta"][0]
    assert baseline_row["extracted_value"] == pytest.approx(0.1294, abs=2e-3)

    distributed = run_pex(
        gds_path,
        [str(request)],
        "sky130",
        critical_nets=["MID"],
        distributed_rc=True,
    )
    assert distributed["extraction"]["critical_nets"] == ["MID"]
    assert distributed["extraction"]["distributed_rc"] is True
    distributed_row = distributed["delta"][0]
    assert distributed_row["extracted_value"] == pytest.approx(0.0999, abs=2e-3)

    # The measurable, explainable delta itself: same testbench, same
    # geometry, same schematic reference, same net-level R/C totals -- the
    # *only* variable is whether `--distributed-rc` was given, and it
    # visibly moves the extracted-side measurement, reflecting the ladder's
    # genuine extra propagation pole.
    assert baseline_row["extracted_value"] - distributed_row["extracted_value"] > 0.02
    assert baseline_row["schematic_value"] == pytest.approx(
        distributed_row["schematic_value"]
    )


@_SKIP_NO_NGSPICE
def test_cli_pex_json_matches_klt_signoff_pex_kind(tmp_path, resistor_layout, capsys):
    """`klt pex`'s real `--format json` output classifies as kind `"pex"` in
    `klt signoff` -- the compatibility bar the Curator enhancement on issue
    #801 required against issue #871's provisional shape."""
    from klayout_tools import signoff

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(
        [
            "pex",
            str(resistor_layout),
            str(request),
            "--deck",
            "sky130",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)

    report_path = tmp_path / "pex_report.json"
    report_path.write_text(json.dumps(payload))

    aggregated = signoff.build_signoff([str(report_path)])
    assert aggregated["status"] == "pass"
    (check,) = aggregated["checks"]
    assert check["kind"] == "pex"
    assert check["passed"] is True


@_SKIP_NO_NGSPICE
def test_cli_pex_text_format(tmp_path, resistor_layout, capsys):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(["pex", str(resistor_layout), str(request), "--deck", "sky130"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: pass" in out
    assert "delta:" in out
    assert "vout @" in out


def test_cli_pex_error_exit_code_and_stderr(tmp_path, resistor_layout, capsys):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(
        [
            "pex",
            str(resistor_layout),
            str(request),
            "--deck",
            "not-a-real-deck",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "extraction failed" in err["error"]["message"]


# --------------------------------------------------------------------------- #
# `--deck-option`/`--pins` passthrough to `klt extract` (issue #1558, Gap 1)
#
# `klt pex` drives `klt extract --parasitics` internally but, until this
# issue, exposed no way to reach either flag -- so a design committed to a
# non-default deck flavour (gf180mcu's `poly_res`/`mim_cap` axes: one drawn
# geometry, several PDK-offered interpretations) was silently extracted
# against the *deck's* default flavour, and every `delta[]` row computed from
# the wrong parasitics, with no warning.
# --------------------------------------------------------------------------- #


def _make_gf180mcu_mim_layout(top_name: str = "MIM") -> kdb.Layout:
    """A minimal gf180mcu MiM-cap layout on the curated deck's Option-B
    stack: a 10x10um `FuseTop` top plate (marked `CAP_MK`/`MIM_L_MK`) over a
    larger `Metal4` bottom plate -- the same fixture shape
    `tests/test_extract.py`'s `_make_gf180mcu_mim_layout` uses, kept
    self-contained here as this module's own fixtures already are.

    One drawn geometry, three selectable densities (`--deck-option
    mim_cap=cap_mim_{1f0,1f5,2f0}_m4m5_noshield`): exactly the "the drawn
    geometry alone cannot tell you which flavour this is" case Gap 1 is
    about."""
    layout = kdb.Layout()
    top = layout.create_cell(top_name)
    dbu = layout.dbu

    def box_um(x0, y0, x1, y1):
        return kdb.Box(
            round(x0 / dbu), round(y0 / dbu), round(x1 / dbu), round(y1 / dbu)
        )

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    draw(46, 0, box_um(-20, -20, 20, 20))  # Metal4 (bottom plate conductor)
    draw(75, 0, box_um(0, 0, 10, 10))  # FuseTop (top plate)
    draw(117, 5, box_um(-5, -5, 15, 15))  # CAP_MK
    draw(117, 10, box_um(-5, -5, 15, 15))  # MIM_L_MK
    return layout


# `C = 100um^2 * 9.87e-16 F/um^2 + 40um * 3.3e-16 F/um`, the `cap_mim_1f0`
# flavour's own cited coefficients against this fixture's 10x10um plate --
# distinct from the deck default's 2.08532e-13, which is the whole point.
_MIM_1F0_C_F = 1.119e-13


def _write_mim_dut(path: Path) -> Path:
    """A schematic DUT whose `.SUBCKT` interface matches the extraction's:
    `MIM` with no top-level pins (this fixture draws no labels, so no net
    promotes to a pin), so no `pin_count_mismatch`/`flat_dut_mismatch`
    stands between the run and its extracted side."""
    path.write_text(".SUBCKT MIM\nC1 cp cm 208.532f\n.ENDS MIM\n")
    return path


def _write_mim_testbench(path: Path, dut_path: Path) -> Path:
    path.write_text(
        f'.include "{dut_path}"\nXdut MIM\nVdd n1 0 DC 1.8\nRload n1 0 1k\n'
    )
    return path


def _mim_pex_inputs(tmp_path):
    """`(layout, request)` for a gf180mcu MiM `klt pex` run."""
    layout = _write_gds(_make_gf180mcu_mim_layout(), tmp_path / "mim.gds")
    dut = _write_mim_dut(tmp_path / "mim_dut.spice")
    tb = _write_mim_testbench(tmp_path / "mim_tb.spice", dut)
    request = _write_request(tmp_path / "mim_request.json", tb, node="n1")
    return layout, request


def _install_two_call_fake_run_sim(monkeypatch):
    """Monkeypatch `run_sim` with a fake that answers both the schematic-
    and extracted-side calls, so a whole `run_pex` run completes without
    `ngspice` (the same idiom the `pin_count_mismatch` tests above use).
    Returns the call counter."""
    import klayout_tools.pex as pex_module

    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        return {
            "corners": [
                {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]}
            ],
            "corner_count": 1,
            "measurements": [{"name": "vout"}],
        }

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    return calls


def test_run_pex_deck_options_threaded_through_to_extraction(tmp_path, monkeypatch):
    """Issue #1558, Gap 1: `deck_options` (`klt pex --deck-option`) reaches
    `run_extract`, so the extraction selects the *requested* flavour rather
    than the deck's default -- proven against the acceptance bar itself: the
    netlist `klt pex` extracts is byte-for-byte the one a standalone `klt
    extract --deck-option ... --parasitics` writes for the same layout
    (`netlist_sha256`), and is *not* the default-flavour one."""
    from klayout_tools.extract import run_extract

    layout, request = _mim_pex_inputs(tmp_path)
    calls = _install_two_call_fake_run_sim(monkeypatch)

    report = run_pex(
        layout,
        [str(request)],
        "gf180mcu",
        output=str(tmp_path / "pex_extracted.spice"),
        deck_options={"mim_cap": "cap_mim_1f0_m4m5_noshield"},
    )

    assert calls["n"] == 2
    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None
    # The resolved flavour is pinned in this run's own provenance echo --
    # alongside the keys `klt pex` did not pass, at the deck's own defaults
    # and flagged as such (issue #2394).
    deck = report["provenance"]["deck"]
    assert deck["options"] == {
        "metal_top": "9K",
        "mim_cap": "cap_mim_1f0_m4m5_noshield",
        "poly_res": "1k",
    }
    assert deck["options_explicit"] == {
        "metal_top": False,
        "mim_cap": True,
        "poly_res": False,
    }

    # ... and the extracted netlist is exactly `klt extract --deck-option`'s.
    standalone = run_extract(
        layout,
        "gf180mcu",
        output=str(tmp_path / "standalone.spice"),
        parasitics=True,
        deck_options={"mim_cap": "cap_mim_1f0_m4m5_noshield"},
    )
    assert report["extraction"]["netlist_sha256"] == standalone["netlist_sha256"]

    default_flavour = run_extract(
        layout,
        "gf180mcu",
        output=str(tmp_path / "default.spice"),
        parasitics=True,
    )
    assert report["extraction"]["netlist_sha256"] != default_flavour["netlist_sha256"]
    # The device card really did move to the requested density.
    text = Path(str(tmp_path / "pex_extracted.spice")).read_text()
    assert f"{_MIM_1F0_C_F:.12g}" in text


def test_run_pex_model_mismatch_is_reported_but_does_not_change_status(
    tmp_path, resistor_layout, monkeypatch
):
    """Issue #2402: a `model_mismatch` finding is additive -- it is reported
    beside `status`/`delta[].status`, never folded into either, mirroring
    `body_bias`'s own "reported, not enforced" precedent (issue #1983). The
    real extraction of `resistor_layout` writes only a plain `R` card (no
    `M`/`X`/`Q`/`D` device at all), so a schematic DUT with an extra `X`-card
    device the extracted side never instantiates is enough to trigger the
    diagnostic without needing a real MOS layout fixture."""
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(
        ".SUBCKT RES RA RB\n"
        "R1 RA RB 289.2\n"
        # A device the extracted (resistor-only) netlist never instantiates
        # -- the same class of divergence issue #2402 reports (a MOS
        # flavour the layout carries no voltage-domain marker geometry for).
        "Xm1 RA RB RB sky130_fd_pr__nfet_g5v0 l=0.5 w=1\n"
        ".ENDS RES\n"
    )
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    calls = _install_two_call_fake_run_sim(monkeypatch)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert calls["n"] == 2
    # The finding never changes the grade.
    assert report["status"] == "pass"
    assert report["passed"] == 1
    assert report["failed"] == 0
    assert report["errored"] == 0
    assert all(row["status"] == "pass" for row in report["delta"])

    mismatch = report["model_mismatch"]
    assert mismatch is not None
    assert mismatch["reference_only"] == ["sky130_fd_pr__nfet_g5v0"]
    assert mismatch["extracted_only"] == []
    # Orthogonal to the top-level interface checks -- both stay clean.
    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None


def test_run_pex_model_mismatch_none_when_devices_match(
    tmp_path, resistor_layout, monkeypatch
):
    """The ordinary case -- no active devices on either side -- reports
    `model_mismatch: null`, byte-identical to a pre-#2402 run."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    _install_two_call_fake_run_sim(monkeypatch)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    assert report["model_mismatch"] is None


def test_run_pex_without_deck_options_still_extracts_the_deck_default(
    tmp_path, monkeypatch
):
    """Backward-compatibility guard: omitting `deck_options` entirely
    reproduces the pre-#1558 run exactly -- the deck's own default flavour.

    Issue #2394: `provenance.deck.options` now says which default that was
    (`explicit: false` throughout) instead of omitting the key, so the
    *extraction* is unchanged while the record of it is no longer silent."""
    from klayout_tools.extract import run_extract

    layout, request = _mim_pex_inputs(tmp_path)
    _install_two_call_fake_run_sim(monkeypatch)

    report = run_pex(
        layout,
        [str(request)],
        "gf180mcu",
        output=str(tmp_path / "pex_extracted.spice"),
    )

    deck = report["provenance"]["deck"]
    assert deck["options"]["mim_cap"] == "cap_mim_2f0_m4m5_noshield"
    assert deck["options_explicit"] == {
        "metal_top": False,
        "mim_cap": False,
        "poly_res": False,
    }
    default_flavour = run_extract(
        layout,
        "gf180mcu",
        output=str(tmp_path / "default.spice"),
        parasitics=True,
    )
    assert report["extraction"]["netlist_sha256"] == default_flavour["netlist_sha256"]


def test_run_pex_deck_option_invalid_value_is_a_clean_pex_error(tmp_path, monkeypatch):
    """An unrecognised `--deck-option` value is `klt extract`'s own loud
    validation error, surfaced through this command's `PexError` envelope --
    not a silently-kept default (the failure mode Gap 1 reports)."""
    layout, request = _mim_pex_inputs(tmp_path)
    _install_two_call_fake_run_sim(monkeypatch)

    with pytest.raises(PexError, match="mim_cap=cap_mim_3f0_m4m5_noshield"):
        run_pex(
            layout,
            [str(request)],
            "gf180mcu",
            output=str(tmp_path / "bad.spice"),
            deck_options={"mim_cap": "cap_mim_3f0_m4m5_noshield"},
        )


def test_run_pex_declared_pins_threaded_through_to_extraction(
    tmp_path, resistor_layout_promoted_taps, monkeypatch
):
    """Issue #1558, Gap 1: `declared_pins` (`klt pex --pins`) reaches
    `run_extract`, demoting the two labelled taps flat extraction would
    otherwise promote to top-level pins.

    The same layout *without* `--pins` is the `pin_count_mismatch` case
    `test_run_pex_pin_count_mismatch_preflight_skips_extracted_side_entirely`
    above locks in (extraction promotes `RA RB VSUB VNW`; the schematic DUT
    declares only `RA RB`, so the extracted side is never even attempted).
    Declaring the real port set reconciles the two, and the extracted side
    runs -- an observable behavioral difference, not just a forwarded
    kwarg."""
    calls = _install_two_call_fake_run_sim(monkeypatch)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    without_pins = run_pex(
        resistor_layout_promoted_taps,
        [str(request)],
        "sky130",
        output=str(tmp_path / "no_pins.spice"),
    )
    assert without_pins["pin_count_mismatch"] is not None
    assert without_pins["pin_count_mismatch"]["extracted"]["pins"] == [
        "RA",
        "RB",
        "VSUB",
        "VNW",
    ]
    # Extracted side skipped: only the schematic-side call happened.
    assert calls["n"] == 1

    with_pins = run_pex(
        resistor_layout_promoted_taps,
        [str(request)],
        "sky130",
        output=str(tmp_path / "with_pins.spice"),
        declared_pins=frozenset({"RA", "RB"}),
    )
    assert with_pins["pin_count_mismatch"] is None
    assert with_pins["status"] == "pass"
    # Both sides ran this time (2 further calls on top of the 1 above).
    assert calls["n"] == 3
    assert ".SUBCKT RES RA RB\n" in Path(str(tmp_path / "with_pins.spice")).read_text()


def test_cli_pex_deck_option_and_pins_reach_run_pex(tmp_path, monkeypatch):
    """The CLI half of the passthrough: `klt pex --deck-option ... --pins
    ...` parses both flags with the same shared helpers `klt extract` uses
    (`cli/_parsing.py`) and hands them to `run_pex` in the parsed shapes it
    declares."""
    import klayout_tools.cli.pex_cmd as pex_cmd

    captured: dict = {}

    def _fake_run_pex(*args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "pass",
            "layout": {"path": "mim.gds", "scope": "repo"},
            "netlist": {"path": "mim.spice", "scope": "repo"},
            "reference_netlist": {"path": "dut.spice", "scope": "repo"},
            "corner_count": 0,
            "delta": [],
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "extraction": {"deck": "gf180mcu", "device_count": 1, "net_count": 2},
            "testbenches": [],
        }

    monkeypatch.setattr(pex_cmd, "run_pex", _fake_run_pex)

    layout, request = _mim_pex_inputs(tmp_path)
    exit_code = main(
        [
            "pex",
            str(layout),
            str(request),
            "--deck",
            "gf180mcu",
            "--deck-option",
            "mim_cap=cap_mim_1f5_m4m5_noshield",
            "--deck-option",
            "poly_res=2k",
            "--pins",
            "RA, RB",
        ]
    )

    assert exit_code == 0
    assert captured["deck_options"] == {
        "mim_cap": "cap_mim_1f5_m4m5_noshield",
        "poly_res": "2k",
    }
    assert captured["declared_pins"] == frozenset({"RA", "RB"})


def test_cli_pex_rename_declared_pins_reaches_run_pex(tmp_path, monkeypatch):
    """Issue #2854's CLI half: `klt pex --rename-declared-pins` parses as a
    plain boolean flag and forwards `False` when omitted -- the exact
    `run_pex` call every site that predates it makes."""
    import klayout_tools.cli.pex_cmd as pex_cmd

    captured: dict = {}

    def _fake_run_pex(*args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "pass",
            "layout": {"path": "mim.gds", "scope": "repo"},
            "netlist": {"path": "mim.spice", "scope": "repo"},
            "reference_netlist": {"path": "dut.spice", "scope": "repo"},
            "corner_count": 0,
            "delta": [],
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "extraction": {"deck": "gf180mcu", "device_count": 1, "net_count": 2},
            "testbenches": [],
        }

    monkeypatch.setattr(pex_cmd, "run_pex", _fake_run_pex)

    layout, request = _mim_pex_inputs(tmp_path)
    exit_code = main(
        ["pex", str(layout), str(request), "--deck", "gf180mcu", "--format", "json"]
    )
    assert exit_code == 0
    assert captured["rename_declared_pins"] is False

    exit_code = main(
        [
            "pex",
            str(layout),
            str(request),
            "--deck",
            "gf180mcu",
            "--pins",
            "RA,RB",
            "--rename-declared-pins",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    assert captured["rename_declared_pins"] is True


def test_cli_pex_omitting_the_new_flags_passes_none(tmp_path, monkeypatch):
    """Omitting both flags forwards `None` for both -- the exact `run_pex`
    call every caller made before issue #1558."""
    import klayout_tools.cli.pex_cmd as pex_cmd

    captured: dict = {}

    def _fake_run_pex(*args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "pass",
            "layout": {"path": "mim.gds", "scope": "repo"},
            "netlist": {"path": "mim.spice", "scope": "repo"},
            "reference_netlist": {"path": "dut.spice", "scope": "repo"},
            "corner_count": 0,
            "delta": [],
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "extraction": {"deck": "gf180mcu", "device_count": 1, "net_count": 2},
            "testbenches": [],
        }

    monkeypatch.setattr(pex_cmd, "run_pex", _fake_run_pex)

    layout, request = _mim_pex_inputs(tmp_path)
    exit_code = main(
        ["pex", str(layout), str(request), "--deck", "gf180mcu", "--format", "json"]
    )

    assert exit_code == 0
    assert captured["deck_options"] is None
    assert captured["declared_pins"] is None


def test_cli_pex_deck_option_malformed_entry_is_a_clean_error(tmp_path, capsys):
    """A `--deck-option` entry with no `=` exits 1 with this command's own
    JSON error envelope, not a traceback -- the same message `klt extract`
    produces, because both go through the same shared parser."""
    layout, request = _mim_pex_inputs(tmp_path)

    exit_code = main(
        [
            "pex",
            str(layout),
            str(request),
            "--deck",
            "gf180mcu",
            "--deck-option",
            "mim_cap",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "KEY=VALUE" in err["error"]["message"]


def test_cli_pex_blank_pins_is_a_clean_error(tmp_path, capsys):
    """`--pins ,,` (nothing usable in it) is likewise a clean exit-1 error,
    not a silent "declare zero pins"."""
    layout, request = _mim_pex_inputs(tmp_path)

    exit_code = main(
        [
            "pex",
            str(layout),
            str(request),
            "--deck",
            "gf180mcu",
            "--pins",
            ",,",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "no non-empty name" in err["error"]["message"]


# --------------------------------------------------------------------------- #
# `--measure-command`: measuring both legs with a caller-supplied command
# (issue #2478)
# --------------------------------------------------------------------------- #


#: A measurement harness that needs no ngspice: it reports one spec row whose
#: value depends only on which side it was invoked for, so every assertion
#: below is about `klt pex`'s own plumbing (argv/env/document handling, delta
#: computation, disclosure) rather than about a simulator's numbers. It also
#: asserts, in-process, the three environment guarantees the contract makes.
_MEASURE_HARNESS = """\
import json, os, sys

netlist = sys.argv[-1]
side = os.environ["KLT_PEX_SIDE"]
assert os.environ["KLT_PEX_NETLIST"] == netlist, "netlist must match env"
assert os.path.isdir(os.environ["KLT_PEX_ARTIFACTS_DIR"]), "artifacts dir must exist"
assert os.path.isfile(netlist), "the netlist handed in must exist"

# Deliberately *not* expressible as one `.meas` card: a first-crossing search
# whose located value differs per side. The schematic leg crosses earlier
# (no parasitics); the extracted leg crosses later.
base = 1.0 if side == "schematic" else 1.1
print(
    json.dumps(
        {
            "corners": [
                {
                    "corner_id": "tt/1.800V/27C",
                    "measurements": [
                        {"name": "t_cross", "value": base, "status": "pass"}
                    ],
                },
                {
                    "corner_id": "tt/1.800V/125C",
                    "measurements": [
                        {"name": "t_cross", "value": base * 2, "status": "pass"}
                    ],
                },
            ]
        }
    )
)
"""


def _write_harness(tmp_path, body: str = _MEASURE_HARNESS) -> list[str]:
    """Write ``body`` as a script and return the argv list that runs it with
    *this* interpreter (never a bare `python3`, which need not be the venv's)."""
    script = tmp_path / "harness.py"
    script.write_text(body)
    return [sys.executable, str(script)]


@pytest.fixture
def measure_reference(tmp_path):
    """The same 2-pin schematic DUT the testbench-mode fixtures use, named
    directly as `--reference-netlist` instead of via a testbench `.include`."""
    return str(_write_schematic_dut(tmp_path / "measure-dut.spice"))


def test_run_pex_measure_command_produces_delta_rows(
    tmp_path, resistor_layout, measure_reference
):
    """The whole point of issue #2478: a block whose spec row no `.meas` card
    can express still produces a real, per-corner schematic-vs-extracted
    `delta[]` -- and therefore a `pex` envelope item 7 can grade."""
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
    )

    assert report["status"] == "pass"
    assert report["corner_count"] == 2
    assert [row["spec_row"] for row in report["delta"]] == ["t_cross", "t_cross"]
    assert [row["corner_id"] for row in report["delta"]] == [
        "tt/1.800V/27C",
        "tt/1.800V/125C",
    ]
    assert [row["schematic_value"] for row in report["delta"]] == [1.0, 2.0]
    assert [row["extracted_value"] for row in report["delta"]] == [1.1, 2.2]
    # Issue #2989: per-side limit status rides on command-mode rows too, and
    # `schematic_failed` is reported (here 0) without touching `status`.
    assert all(
        row["schematic_status"] in {"pass", "fail", "error"}
        and row["extracted_status"] in {"pass", "fail", "error"}
        for row in report["delta"]
    )
    assert report["schematic_failed"] == sum(
        row["schematic_status"] == "fail" for row in report["delta"]
    )
    # The delta is computed by `klt pex`, not supplied by the caller.
    assert [row["delta_pct"] for row in report["delta"]] == [10.0, 10.0]
    assert report["passed"] == 2
    assert report["failed"] == 0
    assert report["errored"] == 0


_ENV_RECORDING_HARNESS = _MEASURE_HARNESS.replace(
    "import json, os, sys\n",
    "import json, os, sys\n"
    "open(os.path.join(os.environ['KLT_PEX_ARTIFACTS_DIR'], 'env.json'), 'w')"
    ".write(json.dumps({k: os.environ.get(k) for k in "
    "('KLT_PEX_BACKEND', 'KLT_SIM_BACKEND')}))",
    1,
)


def _recorded_backend_env(tmp_path, side: str) -> dict:
    return json.loads((tmp_path / "art" / "measure" / side / "env.json").read_text())


@pytest.mark.parametrize("inherited", [None, "local"])
def test_run_pex_measure_command_exports_backend_flag(
    tmp_path, resistor_layout, measure_reference, monkeypatch, inherited
):
    """Issue #2962: `--backend` reaches the child, and beats a pre-set
    `$KLT_SIM_BACKEND`, so a child `klt sim` obeys it."""
    if inherited is None:
        monkeypatch.delenv("KLT_SIM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("KLT_SIM_BACKEND", inherited)
    run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path, _ENV_RECORDING_HARNESS),
        reference_netlist=measure_reference,
        backend="batch",
    )
    for side in ("schematic", "extracted"):
        assert _recorded_backend_env(tmp_path, side) == {
            "KLT_PEX_BACKEND": "batch",
            "KLT_SIM_BACKEND": "batch",
        }


def test_run_pex_measure_command_leaves_backend_env_alone_without_flag(
    tmp_path, resistor_layout, measure_reference, monkeypatch
):
    monkeypatch.setenv("KLT_SIM_BACKEND", "batch")
    monkeypatch.delenv("KLT_PEX_BACKEND", raising=False)
    run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path, _ENV_RECORDING_HARNESS),
        reference_netlist=measure_reference,
    )
    assert _recorded_backend_env(tmp_path, "schematic") == {
        "KLT_PEX_BACKEND": None,
        "KLT_SIM_BACKEND": "batch",
    }


#: A harness that takes ownership of the directory a nested ``klt sim`` would
#: default to (``.klt/sim`` beside a request under $KLT_PEX_ARTIFACTS_DIR) while
#: the parent ``klt pex`` holds its outdir locked (#3291).
_NESTED_LOCK_HARNESS = """\
import json, os
from klayout_tools import output_lock

with output_lock.hold_output_dir(
    os.path.join(os.environ["KLT_PEX_ARTIFACTS_DIR"], ".klt", "sim")
):
    pass
print(json.dumps({"corners": [{"corner_id": "tt", "measurements": [
    {"name": "t_cross", "value": 1.0, "status": "pass"}]}]}))
"""


def test_run_pex_measure_command_child_can_own_dir_under_artifacts_dir(
    tmp_path, resistor_layout, measure_reference, monkeypatch
):
    """Issue #3291: the parent's flock is per-process, so without the inherited
    marker a nested `klt sim` under $KLT_PEX_ARTIFACTS_DIR is `outdir_locked`."""
    monkeypatch.setenv("KLT_LOCK_DIR", str(tmp_path / "locks"))
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path, _NESTED_LOCK_HARNESS),
        reference_netlist=measure_reference,
    )

    assert report["status"] == "pass"


def test_run_pex_measure_command_keeps_extraction_disclosures(
    tmp_path, resistor_layout, measure_reference
):
    """Issue #2478's own acceptance bar: the envelope this mode emits carries
    the *same* `extraction.model`/`body_bias`/`provenance` disclosures the
    testbench-driven one does -- the fields that give item 7 its teeth (issue
    #1983). A new measurement path must not silently lose them."""
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
    )

    assert report["extraction"]["deck"] == "sky130"
    assert report["extraction"]["device_count"] >= 1
    assert report["extraction"]["netlist_sha256"]
    # `PARASITIC_MODEL_SCOPE`, verbatim -- the same four keys testbench mode
    # reports, not a reduced stand-in.
    assert set(report["extraction"]["model"]) == {
        "capacitance",
        "coupling",
        "resistance",
        "frequency",
    }
    assert report["body_bias"]["status"] in (BODY_BIAS_BIASED, BODY_BIAS_UNBIASED)
    assert "unbiased_pmos_body_nets" in report["body_bias"]
    assert report["provenance"]["deck"]["name"] == "sky130"
    assert report["reference_netlist"]["path"] is not None or (
        report["reference_netlist"]["scope"] == "external"
    )


def test_run_pex_measure_command_discloses_the_mode_and_both_sides(
    tmp_path, resistor_layout, measure_reference
):
    """`measurement` is the disclosure that lets a reader of a committed
    record tell an externally-measured post-layout comparison from a `klt
    sim`-measured one -- and re-run it."""
    command = _write_harness(tmp_path)
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=command,
        reference_netlist=measure_reference,
        measure_timeout_s=123.0,
    )

    measurement = report["measurement"]
    assert measurement["mode"] == "command"
    assert measurement["command"] == command
    assert measurement["timeout_s"] == 123.0
    assert set(measurement["sides"]) == {"schematic", "extracted"}
    for side in ("schematic", "extracted"):
        entry = measurement["sides"][side]
        assert entry["exit_status"] == 0
        assert entry["corner_count"] == 2
        assert entry["measurement_names"] == ["t_cross"]
        assert set(entry["netlist"]) == {"path", "scope"}
    # No testbench was involved, and the report says so rather than inventing
    # a synthetic entry.
    assert report["testbenches"] == []
    assert report["coverage"]["testbenches"] == 0


def test_run_pex_testbench_mode_reports_measurement_mode_testbench(
    tmp_path, resistor_layout, monkeypatch
):
    """The additive `measurement` block is present on the *default* path too,
    so a consumer never has to infer the mode from the absence of a field."""
    import klayout_tools.pex as pex_module

    def _fake_run_sim(request_path, **_kwargs):
        return {
            "corner_count": 1,
            "measurements": [{"name": "vout"}],
            "corners": [
                {
                    "corner_id": "tt/1.800V/27C",
                    "measurements": [{"name": "vout", "value": 1.0, "status": "pass"}],
                }
            ],
        }

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = _write_schematic_dut(tmp_path / "dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
    )

    assert report["measurement"] == {
        "mode": "testbench",
        "command": None,
        "timeout_s": None,
        "sides": None,
    }


def test_run_pex_measure_command_null_value_is_an_error_row_not_a_pass(
    tmp_path, resistor_layout, measure_reference
):
    """ "Unresolved" is a first-class outcome (issue #2478's own wording): a
    `null` value renders an `"error"` delta row, never a fabricated pass."""
    harness = """\
import json, os
side = os.environ["KLT_PEX_SIDE"]
print(json.dumps({"corners": [{"corner_id": "tt", "measurements": [
    {"name": "t_cross", "value": None if side == "extracted" else 1.0}
]}]}))
"""
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path, harness),
        reference_netlist=measure_reference,
    )

    assert report["status"] == "error"
    assert report["delta"][0]["status"] == "error"
    assert report["delta"][0]["extracted_value"] is None
    assert report["errored"] == 1


def _run_message_harness(tmp_path, resistor_layout, measure_reference, entry):
    harness = f"""\
import json, os
side = os.environ["KLT_PEX_SIDE"]
entry = {entry}
print(json.dumps({{"corners": [{{"corner_id": "tt", "measurements": [
    dict(name="t_cross", **entry.get(side, {{}}))
]}}]}}))
"""
    return run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path, harness),
        reference_netlist=measure_reference,
    )


_OK = {"value": 1.0}


@pytest.mark.parametrize("bad_side", ["extracted", "schematic"])
def test_run_pex_measure_command_message_reaches_error_row(
    tmp_path, resistor_layout, measure_reference, bad_side
):
    other = "schematic" if bad_side == "extracted" else "extracted"
    report = _run_message_harness(
        tmp_path,
        resistor_layout,
        measure_reference,
        {bad_side: {"value": None, "message": "out of interval"}, other: _OK},
    )
    assert report["delta"][0]["error"] == {
        "side": bad_side,
        "stage": "execute",
        "message": "out of interval",
    }


def test_run_pex_measure_command_no_message_keeps_fallback(
    tmp_path, resistor_layout, measure_reference
):
    report = _run_message_harness(
        tmp_path,
        resistor_layout,
        measure_reference,
        {"extracted": {"value": None}, "schematic": _OK},
    )
    error = report["delta"][0]["error"]
    assert error["stage"] == "unknown"
    assert error["message"] == "no diagnostic was recorded for this errored row"
    assert set(error) == {"side", "stage", "message"}


def test_run_pex_measure_command_both_null_blames_schematic_message(
    tmp_path, resistor_layout, measure_reference
):
    report = _run_message_harness(
        tmp_path,
        resistor_layout,
        measure_reference,
        {
            "schematic": {"value": None, "message": "sch msg"},
            "extracted": {"value": None, "message": "ext msg"},
        },
    )
    error = report["delta"][0]["error"]
    assert (error["side"], error["message"]) == ("schematic", "sch msg")


def test_run_pex_measure_command_message_on_pass_row_is_ignored(
    tmp_path, resistor_layout, measure_reference
):
    report = _run_message_harness(
        tmp_path,
        resistor_layout,
        measure_reference,
        {"extracted": {**_OK, "message": "fine"}, "schematic": _OK},
    )
    assert report["delta"][0]["status"] == "pass"
    assert "error" not in report["delta"][0]
    assert "fine" not in json.dumps(report["delta"])


@pytest.mark.parametrize("bad", [5, "", "  "])
def test_run_pex_measure_command_bad_message_is_pex_error(
    tmp_path, resistor_layout, measure_reference, bad
):
    with pytest.raises(PexError, match=r"measurements\[0\]\.message"):
        _run_message_harness(
            tmp_path,
            resistor_layout,
            measure_reference,
            {"extracted": {"value": 1.0, "message": bad}, "schematic": _OK},
        )


def test_run_pex_measure_command_failing_row_status_is_a_fail(
    tmp_path, resistor_layout, measure_reference
):
    """A caller that owns its own limits states them, and `klt pex` grades on
    the extracted side's own verdict exactly as it does for `klt sim`."""
    harness = """\
import json, os
side = os.environ["KLT_PEX_SIDE"]
print(json.dumps({"corners": [{"corner_id": "tt", "measurements": [
    {"name": "t_cross", "value": 1.0,
     "status": "pass" if side == "schematic" else "fail"}
]}]}))
"""
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path, harness),
        reference_netlist=measure_reference,
    )

    assert report["status"] == "fail"
    assert report["failed"] == 1


def test_run_pex_measure_command_skips_testbench_only_diagnostics(
    tmp_path, resistor_layout_promoted_taps, measure_reference
):
    """`pin_count_mismatch`/`flat_dut_mismatch` diagnose the testbench
    `X...`-line-reuse contract, which this mode does not use -- so a layout
    whose extraction promotes pins the schematic DUT never declares (the
    exact issue #1030 fixture) is measured, not refused."""
    report = run_pex(
        resistor_layout_promoted_taps,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
    )

    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None
    assert report["status"] == "pass"


def test_run_pex_measure_command_still_reports_model_mismatch(
    tmp_path, resistor_layout, measure_reference
):
    """`model_mismatch` compares what the two netlists *contain*, which is
    independent of how either side was measured -- so it is computed here
    exactly as in testbench mode (issue #2402 x #2478)."""
    reference = tmp_path / "wrong-flavour.spice"
    reference.write_text(
        ".SUBCKT RES RA RB\nXR1 RA RB res_generic_nonexistent\n.ENDS RES\n"
    )

    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=str(reference),
    )

    assert report["model_mismatch"] is not None
    assert "res_generic_nonexistent" in report["model_mismatch"]["reference_only"]


# --- input-contract errors ------------------------------------------------- #


def test_run_pex_measure_command_rejects_testbenches(
    tmp_path, resistor_layout, measure_reference
):
    with pytest.raises(PexError, match="replaces the testbench set"):
        run_pex(
            resistor_layout,
            ["some-request.json"],
            "sky130",
            measure_command=_write_harness(tmp_path),
            reference_netlist=measure_reference,
        )


def test_run_pex_measure_command_requires_a_reference_netlist(
    tmp_path, resistor_layout
):
    with pytest.raises(PexError, match="requires --reference-netlist"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            measure_command=_write_harness(tmp_path),
        )


def test_run_pex_measure_command_rejects_an_empty_argv(
    resistor_layout, measure_reference
):
    with pytest.raises(PexError, match="must name a command"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            measure_command=[],
            reference_netlist=measure_reference,
        )


def test_run_pex_reference_netlist_without_measure_command_raises(
    resistor_layout, measure_reference
):
    """In testbench mode `reference_netlist` is an *observation* (whatever
    every testbench `.include`s), never a caller's claim."""
    with pytest.raises(PexError, match="only meaningful with --measure-command"):
        run_pex(
            resistor_layout,
            ["some-request.json"],
            "sky130",
            reference_netlist=measure_reference,
        )


def test_run_pex_measure_timeout_without_measure_command_raises(resistor_layout):
    with pytest.raises(PexError, match="only meaningful with --measure-command"):
        run_pex(
            resistor_layout,
            ["some-request.json"],
            "sky130",
            measure_timeout_s=30.0,
        )


def test_run_pex_measure_command_reference_netlist_must_be_a_circuit(
    tmp_path, resistor_layout
):
    """The same Gap 1 (issue #1255) rule testbench mode applies to its
    `.include` target: a parameter/corner file is not a DUT."""
    reference = tmp_path / "corners.spice"
    reference.write_text(".param sw_stat_mismatch=0\n.model res_generic_po r\n")

    with pytest.raises(PexError, match="declares no `.SUBCKT`"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=_write_harness(tmp_path),
            reference_netlist=str(reference),
        )


def test_run_pex_measure_command_missing_reference_netlist_raises(
    tmp_path, resistor_layout
):
    with pytest.raises(PexError, match="could not be read"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=_write_harness(tmp_path),
            reference_netlist=str(tmp_path / "nope.spice"),
        )


@pytest.mark.parametrize(
    ("harness_body", "expected"),
    [
        ("import sys; sys.exit(7)", r"exited 7"),
        ("print('not json')", "not valid JSON"),
        ("import json; print(json.dumps([1, 2]))", "must be a JSON object"),
        ("import json; print(json.dumps({}))", "`corners` must be a non-empty list"),
        (
            "import json; print(json.dumps({'corners': []}))",
            "`corners` must be a non-empty list",
        ),
        (
            "import json; print(json.dumps({'corners': [{'measurements': []}]}))",
            "corner_id must be a non-empty string",
        ),
        (
            "import json; print(json.dumps({'corners': ["
            "{'corner_id': 'tt', 'measurements': []}]}))",
            "measurements must be a non-empty list",
        ),
        (
            "import json; print(json.dumps({'corners': [{'corner_id': 'tt',"
            " 'measurements': [{'value': 1.0}]}]}))",
            "name must be a non-empty string",
        ),
        (
            "import json; print(json.dumps({'corners': [{'corner_id': 'tt',"
            " 'measurements': [{'name': 'a', 'value': 'x'}]}]}))",
            "must be a number or null",
        ),
        (
            "import json; print(json.dumps({'corners': [{'corner_id': 'tt',"
            " 'measurements': [{'name': 'a', 'value': True}]}]}))",
            "must be a number or null",
        ),
        (
            'print(\'{"corners": [{"corner_id": "tt", "measurements":'
            ' [{"name": "a", "value": NaN}]}]}\')',
            "must be a finite number",
        ),
        (
            "import json; print(json.dumps({'corners': [{'corner_id': 'tt',"
            " 'measurements': [{'name': 'a', 'value': 1.0, 'status': 'ok'}]}]}))",
            "status must be one of",
        ),
        (
            "import json; print(json.dumps({'corners': ["
            "{'corner_id': 'tt', 'measurements': [{'name': 'a', 'value': 1.0}]},"
            "{'corner_id': 'tt', 'measurements': [{'name': 'a', 'value': 1.0}]}]}))",
            "is declared twice",
        ),
        (
            "import json; print(json.dumps({'corners': [{'corner_id': 'tt',"
            " 'measurements': [{'name': 'a', 'value': 1.0},"
            " {'name': 'a', 'value': 2.0}]}]}))",
            "is declared twice",
        ),
    ],
)
def test_run_pex_measure_command_malformed_output_is_a_clean_error(
    tmp_path, resistor_layout, measure_reference, harness_body, expected
):
    """A broken harness is a hard error, never a report full of ordinary
    `"error"` rows that reads like a real (failed) comparison."""
    with pytest.raises(PexError, match=expected):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=_write_harness(tmp_path, harness_body + "\n"),
            reference_netlist=measure_reference,
        )


def test_run_pex_measure_command_timeout_is_a_clean_error(
    tmp_path, resistor_layout, measure_reference
):
    with pytest.raises(PexError, match="did not finish within"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=_write_harness(tmp_path, "import time; time.sleep(30)\n"),
            reference_netlist=measure_reference,
            measure_timeout_s=0.5,
        )


def test_run_pex_measure_command_unrunnable_is_a_clean_error(
    tmp_path, resistor_layout, measure_reference
):
    with pytest.raises(PexError, match="could not be executed"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=[str(tmp_path / "definitely-not-a-program")],
            reference_netlist=measure_reference,
        )


def test_run_pex_measure_command_error_carries_the_harness_stderr(
    tmp_path, resistor_layout, measure_reference
):
    """A harness that dies mid-run gets its own diagnosis into the error
    envelope -- bounded to a tail, not the whole log."""
    with pytest.raises(PexError, match="ngspice refused the deck"):
        run_pex(
            resistor_layout,
            [],
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            measure_command=_write_harness(
                tmp_path,
                "import sys\nsys.stderr.write('ngspice refused the deck\\n')\n"
                "sys.exit(2)\n",
            ),
            reference_netlist=measure_reference,
        )


def test_measure_command_accepts_a_klt_sim_shaped_response():
    """The document shape is a *subset* of `klt sim`'s own response, so a real
    `klt sim` envelope (extra keys and all) validates unchanged -- a harness
    that can already produce its rows with `klt sim` pipes it straight
    through."""
    sim_shaped = {
        "schema_version": 4,
        "status": "pass",
        "corner_count": 1,
        "measurements": [{"name": "gain_db"}],
        "provenance": {"klt_version": "0.6.0"},
        "corners": [
            {
                "corner_id": "tt/1.800V/27C",
                "process": "tt",
                "artifacts": {"log": None},
                "measurements": [
                    {
                        "name": "gain_db",
                        "value": 42.1,
                        "status": "pass",
                        "unit": "dB",
                        "limits": {"min": 40},
                    }
                ],
            }
        ],
    }

    normalized = _normalize_measurement_document(
        sim_shaped, side="schematic", command_label="klt sim"
    )

    assert normalized == {
        "corners": [
            {
                "corner_id": "tt/1.800V/27C",
                "measurements": [{"name": "gain_db", "value": 42.1, "status": "pass"}],
            }
        ],
        "corner_count": 1,
        "measurement_names": ["gain_db"],
    }


def test_measure_command_status_defaults_from_the_value():
    normalized = _normalize_measurement_document(
        {
            "corners": [
                {
                    "corner_id": "tt",
                    "measurements": [
                        {"name": "measured", "value": 1.0},
                        {"name": "unresolved", "value": None},
                    ],
                }
            ]
        },
        side="extracted",
        command_label="harness",
    )

    assert [row["status"] for row in normalized["corners"][0]["measurements"]] == [
        "pass",
        "error",
    ]


# --- CLI ------------------------------------------------------------------- #


def test_cli_pex_measure_command_end_to_end(tmp_path, resistor_layout, capsys):
    command = " ".join(shlex.quote(part) for part in _write_harness(tmp_path))
    reference = _write_schematic_dut(tmp_path / "dut.spice")

    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "-o",
            str(tmp_path / "res.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--measure-command",
            command,
            "--reference-netlist",
            str(reference),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "pass"
    assert report["measurement"]["mode"] == "command"
    assert len(report["delta"]) == 2


def test_cli_pex_measure_command_text_output_names_both_sides(
    tmp_path, resistor_layout, capsys
):
    command = " ".join(shlex.quote(part) for part in _write_harness(tmp_path))
    reference = _write_schematic_dut(tmp_path / "dut.spice")

    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "-o",
            str(tmp_path / "res.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--measure-command",
            command,
            "--reference-netlist",
            str(reference),
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "measurement: command" in out
    assert "schematic:" in out
    assert "extracted:" in out


def test_cli_pex_without_testbenches_or_measure_command_is_a_clean_error(
    tmp_path, resistor_layout, capsys
):
    """`testbenches` became `nargs="*"` so `--measure-command` can stand
    alone -- naming neither is still refused, as this command's own exit-1
    error envelope rather than an argparse usage error."""
    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "-o",
            str(tmp_path / "res.spice"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "at least one testbench request is required" in err["error"]["message"]


def test_cli_pex_empty_measure_command_is_a_clean_error(
    tmp_path, resistor_layout, capsys
):
    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "--measure-command",
            "   ",
            "--reference-netlist",
            str(_write_schematic_dut(tmp_path / "dut.spice")),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "--measure-command is empty" in err["error"]["message"]


def test_cli_pex_unbalanced_quotes_in_measure_command_is_a_clean_error(
    tmp_path, resistor_layout, capsys
):
    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "--measure-command",
            "harness 'unterminated",
            "--reference-netlist",
            str(_write_schematic_dut(tmp_path / "dut.spice")),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "could not be parsed" in err["error"]["message"]


# --------------------------------------------------------------------------- #
# `--label-layer` (issue #2686): `klt extract --label-layer`'s per-request
# label-purpose override (issue #2656), passed through `klt pex`.
# --------------------------------------------------------------------------- #

#: An li1 purpose `decks.sky130` does not read (its `metal0` label layer is
#: `li1.pin`, 67/5).
_PEX_UNREAD_LI1_PURPOSE = (67, 10)


@pytest.fixture
def resistor_layout_unread_purpose(tmp_path):
    """`resistor_layout` with its `RA`/`RB` labels moved from `li1.pin`
    (67/5) to the unread `(67, 10)` purpose -- geometry unchanged."""
    layout = _make_sky130_poly_resistor_layout()
    src = layout.layer(67, 5)
    dst = layout.layer(*_PEX_UNREAD_LI1_PURPOSE)
    for cell in layout.each_cell():
        cell.shapes(dst).insert(cell.shapes(src))
        cell.shapes(src).clear()
    return _write_gds(layout, tmp_path / "res_unread.gds")


def _capture_run_extract_kwargs(monkeypatch) -> list[dict]:
    """Wrap `pex.run_extract` (still calling the real one) and record every
    call's keyword arguments."""
    import klayout_tools.pex as pex_module

    real = pex_module.run_extract
    calls: list[dict] = []

    def _recording_run_extract(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pex_module, "run_extract", _recording_run_extract)
    return calls


def test_run_pex_label_layers_promote_pins_from_the_alternate_purpose(
    tmp_path, resistor_layout_unread_purpose, monkeypatch
):
    """Without the override the moved names are unread: extraction promotes
    neither `RA` nor `RB`, so the extracted side cannot bind the testbench's
    `Xres RA RB RES` line (`pin_count_mismatch`, extracted side skipped).
    With `label_layers` pointing `metal0` at the purpose the names sit on,
    the exact mapping reaches `run_extract`, the pins come back, both sides
    run, and the report records the override (issue #2686)."""
    sim_calls = _install_two_call_fake_run_sim(monkeypatch)
    extract_calls = _capture_run_extract_kwargs(monkeypatch)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    without = run_pex(
        resistor_layout_unread_purpose,
        [str(request)],
        "sky130",
        output=str(tmp_path / "without.spice"),
    )
    assert extract_calls[-1]["label_layers"] is None
    assert without["extraction"]["label_layers"] is None
    assert without["pin_count_mismatch"] is not None
    assert sim_calls["n"] == 1

    with_override = run_pex(
        resistor_layout_unread_purpose,
        [str(request)],
        "sky130",
        output=str(tmp_path / "with.spice"),
        label_layers={"metal0": _PEX_UNREAD_LI1_PURPOSE, "poly": None},
    )
    assert extract_calls[-1]["label_layers"] == {
        "metal0": _PEX_UNREAD_LI1_PURPOSE,
        "poly": None,
    }
    assert with_override["pin_count_mismatch"] is None
    assert with_override["status"] == "pass"
    assert sim_calls["n"] == 3
    assert ".SUBCKT RES RA RB\n" in Path(tmp_path / "with.spice").read_text()
    assert with_override["extraction"]["label_layers"] == {
        "metal0": [67, 10],
        "poly": None,
    }


def test_run_pex_without_label_layers_is_unchanged(
    tmp_path, resistor_layout, monkeypatch
):
    """Omitting `label_layers` forwards `None` to `run_extract` and echoes
    `extraction.label_layers: null`, with the deck's default label purposes
    still read (the ordinary resistor run passes)."""
    _install_two_call_fake_run_sim(monkeypatch)
    extract_calls = _capture_run_extract_kwargs(monkeypatch)

    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout, [str(request)], "sky130", output=str(tmp_path / "r.spice")
    )

    assert extract_calls[-1]["label_layers"] is None
    assert report["extraction"]["label_layers"] is None
    assert report["status"] == "pass"


def test_run_pex_unknown_label_layer_role_is_a_pex_error(tmp_path, resistor_layout):
    """A role the deck has no slot for fails extraction cleanly, before any
    simulation runs."""
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError, match="no label-layer role"):
        run_pex(
            resistor_layout,
            [str(request)],
            "sky130",
            output=str(tmp_path / "r.spice"),
            label_layers={"metal9": (67, 10)},
        )


def _fake_run_pex_capturing(monkeypatch, label_layers_echo=None) -> dict:
    import klayout_tools.cli.pex_cmd as pex_cmd

    captured: dict = {}

    def _fake_run_pex(*args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "pass",
            "layout": {"path": "res.gds", "scope": "repo"},
            "netlist": {"path": "res.spice", "scope": "repo"},
            "reference_netlist": {"path": "dut.spice", "scope": "repo"},
            "corner_count": 0,
            "delta": [],
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "extraction": {
                "deck": "sky130",
                "device_count": 1,
                "net_count": 2,
                "label_layers": label_layers_echo,
            },
            "testbenches": [],
        }

    monkeypatch.setattr(pex_cmd, "run_pex", _fake_run_pex)
    return captured


def test_cli_pex_label_layer_reaches_run_pex(tmp_path, resistor_layout, monkeypatch):
    """`--label-layer` is parsed by the shared `parse_label_layers` helper:
    repeatable, `none` clears a role, and a later entry for the same role
    wins (issue #2686)."""
    captured = _fake_run_pex_capturing(monkeypatch)

    exit_code = main(
        [
            "pex",
            resistor_layout,
            str(tmp_path / "request.json"),
            "--deck",
            "sky130",
            "--label-layer",
            "metal0=67/5",
            "--label-layer",
            "poly=NONE",
            "--label-layer",
            "metal0=67/10",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert captured["label_layers"] == {"metal0": (67, 10), "poly": None}


def test_cli_pex_device_model_reaches_run_pex(tmp_path, resistor_layout, monkeypatch):
    """`--device-model` is repeatable and a later CLASS wins (issue #4234);
    omitting it passes `None`."""
    captured = _fake_run_pex_capturing(monkeypatch)
    argv = [
        "pex",
        resistor_layout,
        str(tmp_path / "request.json"),
        "--deck",
        "gf180mcu",
        "--format",
        "json",
    ]
    assert main(argv) == 0
    assert captured["device_models"] is None

    assert (
        main(
            argv + ["--device-model", "bjt=a", "--device-model", "bjt=pnp_05p00x05p00"]
        )
        == 0
    )
    assert captured["device_models"] == {"bjt": "pnp_05p00x05p00"}


def test_cli_pex_malformed_device_model_is_a_clean_error(
    tmp_path, resistor_layout, monkeypatch
):
    _fake_run_pex_capturing(monkeypatch)
    exit_code = main(
        [
            "pex",
            resistor_layout,
            str(tmp_path / "request.json"),
            "--deck",
            "gf180mcu",
            "--device-model",
            "bjt",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1


def test_cli_pex_omitting_label_layer_passes_none(
    tmp_path, resistor_layout, monkeypatch
):
    captured = _fake_run_pex_capturing(monkeypatch)

    exit_code = main(
        [
            "pex",
            resistor_layout,
            str(tmp_path / "request.json"),
            "--deck",
            "sky130",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert captured["label_layers"] is None


def test_cli_pex_text_output_names_the_label_layer_override(
    tmp_path, resistor_layout, monkeypatch, capsys
):
    _fake_run_pex_capturing(
        monkeypatch, label_layers_echo={"metal0": [67, 10], "poly": None}
    )

    exit_code = main(
        [
            "pex",
            resistor_layout,
            str(tmp_path / "request.json"),
            "--deck",
            "sky130",
            "--label-layer",
            "metal0=67/10",
            "--label-layer",
            "poly=none",
        ]
    )

    assert exit_code == 0
    assert "label_layers: metal0=67/10, poly=none" in capsys.readouterr().out


@pytest.mark.parametrize("entry", ["metal0", "=67/10", "metal0=67", "metal0=a/b"])
def test_cli_pex_malformed_label_layer_is_a_clean_error(
    tmp_path, resistor_layout, capsys, entry
):
    exit_code = main(
        [
            "pex",
            resistor_layout,
            str(tmp_path / "request.json"),
            "--deck",
            "sky130",
            "--label-layer",
            entry,
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "LAYER/DATATYPE" in err["error"]["message"]


def test_cli_pex_unknown_label_layer_role_is_a_clean_error(
    tmp_path, resistor_layout, capsys
):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(
        [
            "pex",
            resistor_layout,
            str(request),
            "--deck",
            "sky130",
            "-o",
            str(tmp_path / "r.spice"),
            "--label-layer",
            "metal9=67/10",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert "no label-layer role" in err["error"]["message"]


# --------------------------------------------------------------------------- #
# Issue #2875: content identity of the schematic-side inputs
# --------------------------------------------------------------------------- #


def _sha(path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fake_two_sided_sim(monkeypatch, *, extracted_side_runs=True):
    import klayout_tools.pex as pex_module

    report = {
        "corners": [
            {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]},
        ],
        "corner_count": 1,
        "measurements": [{"name": "vout"}],
    }
    monkeypatch.setattr(pex_module, "run_sim", lambda *a, **k: report)


def _run_two_testbenches(tmp_path, layout, monkeypatch, *, dut_ohm=289.2, load=1000.0):
    _fake_two_sided_sim(monkeypatch)
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice", dut_ohm)
    tb_a = _write_testbench(tmp_path / "tb_a.spice", dut, load_ohm=load)
    tb_b = _write_testbench(tmp_path / "tb_b.spice", dut)
    req_a = _write_request(tmp_path / "a.json", tb_a)
    req_b = _write_request(tmp_path / "b.json", tb_b, measurement_name="vout")
    report = run_pex(layout, [str(req_a), str(req_b)], "sky130")
    return report, req_a, tb_a, dut, req_b


def test_run_pex_records_content_identity_of_schematic_side_inputs(
    tmp_path, resistor_layout, monkeypatch
):
    report, req_a, tb_a, dut, req_b = _run_two_testbenches(
        tmp_path, resistor_layout, monkeypatch
    )

    first, second = report["testbenches"]
    assert first["request_sha256"] == _sha(req_a)
    assert first["netlist_sha256"] == _sha(tb_a)
    assert first["schematic_netlist_sha256"] == _sha(dut)
    assert second["request_sha256"] == _sha(req_b)
    # Two testbenches sharing one DUT keep the same DUT identity, and each
    # retains its own request/body identity.
    assert second["schematic_netlist_sha256"] == first["schematic_netlist_sha256"]
    assert second["request_sha256"] != first["request_sha256"]
    assert report["reference_netlist_sha256"] == _sha(dut)
    assert first["netlist"] == {"path": None, "scope": "external"}
    # The layout/extraction identity is a distinct role.
    assert report["extraction"]["netlist_sha256"] != first["schematic_netlist_sha256"]


def test_run_pex_each_schematic_side_digest_tracks_only_its_own_file(
    tmp_path, resistor_layout, monkeypatch
):
    base, *_ = _run_two_testbenches(tmp_path, resistor_layout, monkeypatch)
    tmp2 = tmp_path / "dut_changed"
    tmp2.mkdir()
    dut_changed, *_ = _run_two_testbenches(
        tmp2, resistor_layout, monkeypatch, dut_ohm=300.0
    )
    tmp3 = tmp_path / "body_changed"
    tmp3.mkdir()
    body_changed, *_ = _run_two_testbenches(
        tmp3, resistor_layout, monkeypatch, load=2000.0
    )

    keys = ("request_sha256", "netlist_sha256", "schematic_netlist_sha256")

    def differing(other):
        # Paths differ per tmp dir, so compare only digest *content* that
        # does not embed a path: the DUT text and the extracted netlist.
        return {
            k for k in keys if base["testbenches"][0][k] != other["testbenches"][0][k]
        }

    assert "schematic_netlist_sha256" in differing(dut_changed)
    assert "netlist_sha256" in differing(body_changed)
    assert (
        body_changed["testbenches"][0]["schematic_netlist_sha256"]
        == base["testbenches"][0]["schematic_netlist_sha256"]
    )
    for other in (dut_changed, body_changed):
        assert (
            other["extraction"]["netlist_sha256"]
            == base["extraction"]["netlist_sha256"]
        )


def test_run_pex_diagnostic_branch_carries_same_digest_keys(
    tmp_path, resistor_layout, monkeypatch
):
    """The extracted-side-skipped branch (pre-flight pin mismatch) builds its
    summary entry separately; it must carry the same provenance keys."""
    report_full, *_ = _run_two_testbenches(tmp_path, resistor_layout, monkeypatch)
    sub = tmp_path / "diag"
    sub.mkdir()
    _fake_two_sided_sim(monkeypatch)
    dut = sub / "schematic_dut.spice"
    dut.write_text(".SUBCKT RES RA RB VSUB\nR1 RA RB 289.2\n.ENDS RES\n")
    tb = sub / "tb.spice"
    tb.write_text(f'.include "{dut}"\nXres RA RB 0 RES\nRload RB 0 1k\n')
    request = _write_request(sub / "r.json", tb)
    report = run_pex(resistor_layout, [str(request)], "sky130")
    assert report["pin_count_mismatch"] is not None
    assert set(report["testbenches"][0]) == set(report_full["testbenches"][0])
    assert report["testbenches"][0]["schematic_netlist_sha256"] == _sha(dut)


def test_run_pex_measure_command_records_reference_netlist_digest(
    tmp_path, resistor_layout, measure_reference
):
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        measure_command=_write_harness(tmp_path),
        reference_netlist=measure_reference,
    )
    assert report["testbenches"] == []
    assert report["reference_netlist_sha256"] == _sha(measure_reference)
    assert report["extraction"]["netlist_sha256"] != report["reference_netlist_sha256"]


def test_run_pex_external_paths_still_carry_digests(
    tmp_path, resistor_layout, monkeypatch
):
    """A path redacted as `scope: external` keeps its digest."""
    report, *_ = _run_two_testbenches(tmp_path, resistor_layout, monkeypatch)
    entry = report["testbenches"][0]
    assert entry["schematic_netlist"]["scope"] == "external"
    assert entry["schematic_netlist"]["path"] is None
    assert entry["schematic_netlist_sha256"].startswith("sha256:")
    assert len(entry["schematic_netlist_sha256"]) == len("sha256:") + 64


# --- Issue #2872 / #2876: per-side execution evidence ------------------------

_BATCH_DIAG = {
    "severity": "error",
    "code": "batch_job_failed",
    "runner_code": "batch_runner_version_mismatch",
    "message": "the fleet runner runs klt 0.5.0 -- the request was not run",
    "future_unknown_key": {"nested": [1, 2]},
}


def _side_report(corners, *, remote=None, extra_env=None):
    """A minimal `klt sim` response. `corners` is a list of
    `(corner_id, value_or_None, diagnostics)`."""
    environment = {"engine": "ngspice", "engine_version": "45", **(extra_env or {})}
    if remote is not None:
        environment["remote"] = remote
    return {
        "corners": [
            {
                "corner_id": cid,
                "status": "pass" if value is not None else "error",
                "diagnostics": diags,
                "measurements": [
                    _measurement(value, "pass" if value is not None else "error")
                ],
            }
            for cid, value, diags in corners
        ],
        "corner_count": len(corners),
        "measurements": [{"name": "vout"}],
        "environment": environment,
    }


def _run_pex_with_sim_reports(tmp_path, layout, monkeypatch, reports, n_testbenches=1):
    """`reports` is the ordered list of `run_sim` responses (schematic,
    extracted, schematic, extracted, ...)."""
    import klayout_tools.pex as pex_module

    queue = list(reports)
    monkeypatch.setattr(pex_module, "run_sim", lambda *_a, **_k: queue.pop(0))
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    requests = []
    for i in range(n_testbenches):
        tb = _write_testbench(tmp_path / f"tb{i}.spice", dut)
        requests.append(str(_write_request(tmp_path / f"request{i}.json", tb)))
    return run_pex(layout, requests, "sky130")


def test_pex_neither_side_ran_carries_diagnostics_and_remote(
    tmp_path, resistor_layout, monkeypatch
):
    remote = {"provider": "aws-batch-fleet", "job_id": "job-123"}
    corners = [("tt/1.800V/27C", None, [_BATCH_DIAG])]
    report = _run_pex_with_sim_reports(
        tmp_path,
        resistor_layout,
        monkeypatch,
        [
            _side_report(
                corners,
                remote=remote,
                extra_env={"ngspice_binary": "/usr/bin/ngspice"},
            ),
            _side_report(
                corners, remote={"provider": "aws-batch-fleet", "job_id": "job-456"}
            ),
        ],
    )
    tb = report["testbenches"][0]
    assert tb["nothing_simulated_corners"] == ["tt/1.800V/27C"]
    schematic = tb["sides"]["schematic"]
    extracted = tb["sides"]["extracted"]
    assert schematic["environment"]["remote"] == remote
    assert schematic["environment"]["ngspice_binary"] == "/usr/bin/ngspice"
    # Only real `klt sim` environment keys are projected.
    assert set(schematic["environment"]) <= {
        "engine",
        "engine_version",
        "ngspice_binary",
        "remote",
    }
    assert extracted["environment"]["remote"]["job_id"] == "job-456"
    corner = schematic["corners"][0]
    assert corner["corner_id"] == "tt/1.800V/27C"
    assert corner["status"] == "error"
    assert corner["produced_value"] is False
    # Passed through unchanged, optional/unknown keys included.
    assert corner["diagnostics"] == [_BATCH_DIAG]
    # Existing behavior untouched.
    assert report["status"] == "error"
    assert report["delta"][0]["status"] == "error"


def test_pex_one_sided_failure_is_not_nothing_simulated(
    tmp_path, resistor_layout, monkeypatch
):
    report = _run_pex_with_sim_reports(
        tmp_path,
        resistor_layout,
        monkeypatch,
        [
            _side_report([("tt/1.800V/27C", 1.4, [])]),
            _side_report([("tt/1.800V/27C", None, [_BATCH_DIAG])]),
        ],
    )
    tb = report["testbenches"][0]
    assert tb["nothing_simulated_corners"] == []
    assert tb["sides"]["schematic"]["corners"][0]["produced_value"] is True
    assert tb["sides"]["extracted"]["corners"][0]["produced_value"] is False
    assert tb["sides"]["extracted"]["corners"][0]["diagnostics"] == [_BATCH_DIAG]
    # Local run: no remote key is fabricated.
    assert "remote" not in tb["sides"]["schematic"]["environment"]
    assert report["delta"][0]["status"] == "error"
    assert report["delta"][0]["schematic_value"] == 1.4


def test_pex_side_evidence_stays_with_its_testbench_and_corner(
    tmp_path, resistor_layout, monkeypatch
):
    c1, c2 = "tt/1.800V/27C", "ss/1.620V/-40C"
    report = _run_pex_with_sim_reports(
        tmp_path,
        resistor_layout,
        monkeypatch,
        [
            _side_report([(c1, 1.0, []), (c2, 1.1, [])], remote={"job_id": "s0"}),
            _side_report([(c1, 1.0, []), (c2, None, [_BATCH_DIAG])]),
            _side_report([(c1, None, [_BATCH_DIAG]), (c2, None, [])]),
            _side_report([(c1, None, []), (c2, None, [])], remote={"job_id": "e1"}),
        ],
        n_testbenches=2,
    )
    first, second = report["testbenches"]
    assert first["nothing_simulated_corners"] == []
    assert [c["corner_id"] for c in first["sides"]["extracted"]["corners"]] == [c1, c2]
    assert first["sides"]["extracted"]["corners"][1]["diagnostics"] == [_BATCH_DIAG]
    assert first["sides"]["schematic"]["environment"]["remote"]["job_id"] == "s0"
    assert second["nothing_simulated_corners"] == [c1, c2]
    assert second["sides"]["schematic"]["corners"][0]["diagnostics"] == [_BATCH_DIAG]
    assert second["sides"]["extracted"]["environment"]["remote"]["job_id"] == "e1"
    assert second["sides"]["schematic"]["environment"].get("remote") is None


# --------------------------------------------------------------------------- #
# Issue #2889: `--flatten-schematic` -- flatten a hierarchical schematic DUT
# to PDK leaf devices before the schematic-side run.
# --------------------------------------------------------------------------- #


def _write_hierarchical_dut(path: Path) -> Path:
    """The same 289.2-ohm `RES` the flat fixture models, written as two
    instances of a locally defined `HALF` subcircuit (each itself two
    resistors) -- `model_mismatch` reports `HALF` on hierarchy alone."""
    path.write_text(
        ".SUBCKT HALF A B R=1\n"
        "R1 A MID {R/2}\n"
        "R2 MID B {R/2}\n"
        ".ENDS HALF\n"
        ".SUBCKT RES RA RB\n"
        "X1 RA M HALF R=144.6\n"
        "X2 M RB HALF R=144.6\n"
        ".ENDS RES\n"
    )
    return path


def _sha256_hex(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _install_recording_fake_run_sim(monkeypatch):
    """Like `_install_two_call_fake_run_sim`, but records every request path."""
    import klayout_tools.pex as pex_module

    requests: list[str] = []

    def _fake_run_sim(request_path, *_args, **_kwargs):
        requests.append(str(request_path))
        return {
            "corners": [
                {"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.4)]}
            ],
            "corner_count": 1,
            "measurements": [{"name": "vout"}],
        }

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    return requests


def test_run_pex_without_flatten_schematic_is_unchanged(
    tmp_path, resistor_layout, monkeypatch
):
    """Default off: the schematic leg runs the testbench itself, and the
    hierarchical DUT still reports the hierarchy-caused `model_mismatch`,
    whose `detail` now names the new flag."""
    dut = _write_hierarchical_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    requests = _install_recording_fake_run_sim(monkeypatch)

    report = run_pex(
        resistor_layout, [str(request)], "sky130", artifacts_dir=str(tmp_path / "pex")
    )

    assert requests[0] == str(request)
    assert report["flatten_schematic"] is False
    assert report["schematic_flatten"] is None
    assert not (tmp_path / "pex" / "schematic-dut.flat.spice").exists()
    mismatch = report["model_mismatch"]
    assert mismatch is not None
    assert mismatch["reference_only"] == ["HALF"]
    assert "--flatten-schematic" in mismatch["detail"]


def test_run_pex_flatten_schematic_runs_the_flattened_dut(
    tmp_path, resistor_layout, monkeypatch
):
    dut = _write_hierarchical_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    requests = _install_recording_fake_run_sim(monkeypatch)
    outdir = tmp_path / "pex"

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        artifacts_dir=str(outdir),
        flatten_schematic=True,
    )

    flat_path = outdir / "schematic-dut.flat.spice"
    flat_text = flat_path.read_text()
    assert ".SUBCKT HALF" not in flat_text
    assert "R.X1.R1 RA X1.MID {144.6/2}" in flat_text

    # The schematic leg ran a DUT-swapped copy of the testbench, not the
    # testbench itself; the copy keeps the request's own netlist_source.
    schematic_request = json.loads(Path(requests[0]).read_text())
    assert requests[0] != str(request)
    assert "netlist_source" not in schematic_request
    body = Path(schematic_request["netlist"]).read_text()
    assert f'.include "{flat_path}"' in body
    assert "Xres RA RB RES" in body

    assert report["flatten_schematic"] is True
    block = report["schematic_flatten"]
    assert block == {
        "netlist": {"path": None, "scope": "external"},
        "netlist_sha256": "sha256:" + _sha256_hex(flat_path),
        "subckts": ["RES"],
        "inlined_instances": 2,
        "inlined_subckts": ["HALF"],
        "external_subckts": [],
        "already_flat": False,
    }
    # No hierarchy-caused model_mismatch any more.
    assert report["model_mismatch"] is None
    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None
    # `reference_netlist` still names (and hashes) the caller's own file.
    assert report["reference_netlist_sha256"] == "sha256:" + _sha256_hex(dut)
    assert report["testbenches"][0]["schematic_netlist_sha256"] == (
        "sha256:" + _sha256_hex(dut)
    )


def test_run_pex_flatten_schematic_already_flat_dut_is_reported(
    tmp_path, resistor_layout, monkeypatch
):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    _install_recording_fake_run_sim(monkeypatch)

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        artifacts_dir=str(tmp_path / "pex"),
        flatten_schematic=True,
    )

    block = report["schematic_flatten"]
    assert block["already_flat"] is True
    assert block["inlined_instances"] == 0
    assert block["subckts"] == ["RES"]
    assert report["model_mismatch"] is None


def test_run_pex_flatten_schematic_shared_by_multiple_testbenches(
    tmp_path, resistor_layout, monkeypatch
):
    dut = _write_hierarchical_dut(tmp_path / "schematic_dut.spice")
    requests_in = []
    for name in ("a", "b"):
        tb = _write_testbench(tmp_path / f"tb_{name}.spice", dut)
        requests_in.append(str(_write_request(tmp_path / f"req_{name}.json", tb)))
    requests = _install_recording_fake_run_sim(monkeypatch)
    outdir = tmp_path / "pex"

    report = run_pex(
        resistor_layout,
        requests_in,
        "sky130",
        artifacts_dir=str(outdir),
        flatten_schematic=True,
    )

    flat_path = outdir / "schematic-dut.flat.spice"
    # Call order: schematic, extracted per testbench.
    schematic_requests = requests[0::2]
    assert len(schematic_requests) == 2
    for path in schematic_requests:
        assert path not in requests_in
        body = Path(json.loads(Path(path).read_text())["netlist"]).read_text()
        assert f'.include "{flat_path}"' in body
    assert len(report["testbenches"]) == 2
    assert report["model_mismatch"] is None


@pytest.mark.parametrize(
    ("dut_text", "code"),
    [
        (
            ".SUBCKT A P\nX1 P B\n.ENDS A\n.SUBCKT B P\nX1 P A\n.ENDS B\n"
            ".SUBCKT RES RA RB\nX1 RA A\nR1 RA RB 1\n.ENDS RES\n",
            "schematic_flatten_cycle",
        ),
        (
            ".SUBCKT HALF A B\nR1 A B 1\n.ENDS HALF\n"
            ".SUBCKT RES RA RB\nX1 RA HALF\n.ENDS RES\n",
            "schematic_flatten_pin_count_mismatch",
        ),
        (
            ".SUBCKT HALF A B\nB1 A B V=v(A)\n.ENDS HALF\n"
            ".SUBCKT RES RA RB\nX1 RA RB HALF\n.ENDS RES\n",
            "schematic_flatten_unsupported_card",
        ),
    ],
)
def test_run_pex_flatten_schematic_errors_are_coded_and_simulate_nothing(
    tmp_path, resistor_layout, monkeypatch, dut_text, code
):
    import klayout_tools.pex as pex_module

    def _unreachable_run_sim(*_args, **_kwargs):
        raise AssertionError("run_sim must not be called when flattening fails")

    monkeypatch.setattr(pex_module, "run_sim", _unreachable_run_sim)
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(dut_text)
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    outdir = tmp_path / "pex"

    with pytest.raises(PexError, match="--flatten-schematic") as excinfo:
        run_pex(
            resistor_layout,
            [str(request)],
            "sky130",
            artifacts_dir=str(outdir),
            flatten_schematic=True,
        )
    assert excinfo.value.code == code
    # Never a partial flatten on disk.
    assert not (outdir / "schematic-dut.flat.spice").exists()


def test_cli_pex_flatten_schematic_error_carries_error_code(
    tmp_path, resistor_layout, capsys
):
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(
        ".SUBCKT HALF A B\nR1 A B 1\n.ENDS HALF\n"
        ".SUBCKT RES RA RB\nX1 RA HALF\n.ENDS RES\n"
    )
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    exit_code = main(
        [
            "pex",
            str(resistor_layout),
            str(request),
            "--deck",
            "sky130",
            "--outdir",
            str(tmp_path / "pex"),
            "--flatten-schematic",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "pex"
    assert err["error"]["code"] == "schematic_flatten_pin_count_mismatch"


def test_cli_pex_flatten_schematic_reaches_run_pex(tmp_path, monkeypatch):
    import klayout_tools.cli.pex_cmd as pex_cmd

    captured: dict = {}

    def _fake_run_pex(*args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "pass",
            "layout": {"path": "mim.gds", "scope": "repo"},
            "netlist": {"path": "mim.spice", "scope": "repo"},
            "reference_netlist": {"path": "dut.spice", "scope": "repo"},
            "corner_count": 0,
            "delta": [],
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "extraction": {"deck": "gf180mcu", "device_count": 1, "net_count": 2},
            "testbenches": [],
        }

    monkeypatch.setattr(pex_cmd, "run_pex", _fake_run_pex)
    layout, request = _mim_pex_inputs(tmp_path)

    assert main(["pex", str(layout), str(request), "--deck", "gf180mcu"]) == 0
    assert captured["flatten_schematic"] is False
    assert (
        main(
            [
                "pex",
                str(layout),
                str(request),
                "--deck",
                "gf180mcu",
                "--flatten-schematic",
            ]
        )
        == 0
    )
    assert captured["flatten_schematic"] is True


@_SKIP_NO_NGSPICE
def test_integration_run_pex_flatten_schematic_end_to_end(tmp_path, resistor_layout):
    """Issue #2889, end to end against real `ngspice`: the hierarchical DUT,
    flattened, simulates to the same ideal 289.2-ohm divider the flat DUT
    does, and the comparison carries no hierarchy-caused `model_mismatch`."""
    dut = _write_hierarchical_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    report = run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        artifacts_dir=str(tmp_path / "pex"),
        flatten_schematic=True,
    )

    assert report["status"] == "pass"
    assert report["model_mismatch"] is None
    assert report["schematic_flatten"]["inlined_instances"] == 2
    (row,) = report["delta"]
    assert row["schematic_value"] == pytest.approx(1.8 * 1000 / 1289.2, rel=1e-3)
    assert row["extracted_value"] < row["schematic_value"]


def test_run_pex_flatten_schematic_keeps_relative_includes_resolvable(
    tmp_path, resistor_layout, monkeypatch
):
    """The flattened copy lives under the artifact root, so a relative
    `.include` inside the DUT file is rewritten to an absolute path against
    the DUT's own directory (the #2891 shape); absolute ones are untouched."""
    (tmp_path / "params.spice").write_text(".param rhalf=144.6\n")
    dut = tmp_path / "schematic_dut.spice"
    dut.write_text(
        '.include "params.spice"\n'
        ".include /abs/models.spice\n"
        ".SUBCKT HALF A B\nR1 A B {rhalf}\n.ENDS HALF\n"
        ".SUBCKT RES RA RB\nX1 RA M HALF\nX2 M RB HALF\n.ENDS RES\n"
    )
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    _install_recording_fake_run_sim(monkeypatch)
    outdir = tmp_path / "pex"

    run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        artifacts_dir=str(outdir),
        flatten_schematic=True,
    )

    flat_text = (outdir / "schematic-dut.flat.spice").read_text()
    assert f'.include "{tmp_path / "params.spice"}"' in flat_text
    assert ".include /abs/models.spice" in flat_text


def test_run_pex_flatten_schematic_keeps_dollar_prefixed_targets(
    tmp_path, resistor_layout, monkeypatch
):
    """`$`-prefixed `.include`/`.lib` targets are environment-variable paths
    (`$PDK_ROOT/...`): the flattener must not read the `$` as an inline
    comment, and `_absolutize_includes` must leave them exactly as written."""
    dut = tmp_path / "schematic_dut.spice"
    include = ".include $PDK_ROOT/sky130A/libs.ref/sky130_fd_pr/spice/models.spice"
    lib = ".lib $SKY130/libs.tech/combined/sky130.lib.spice tt"
    dut.write_text(
        f"{include}\n{lib}\n"
        ".SUBCKT HALF A B\nR1 A B 144.6\n.ENDS HALF\n"
        ".SUBCKT RES RA RB\nX1 RA M HALF\nX2 M RB HALF\n.ENDS RES\n"
    )
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)
    _install_recording_fake_run_sim(monkeypatch)
    outdir = tmp_path / "pex"

    run_pex(
        resistor_layout,
        [str(request)],
        "sky130",
        artifacts_dir=str(outdir),
        flatten_schematic=True,
    )

    flat_lines = (outdir / "schematic-dut.flat.spice").read_text().splitlines()
    assert include in flat_lines
    assert lib in flat_lines


# --------------------------------------------------------------------------- #
# Issue #3062: errored `delta[]` rows carry an `error` object
# --------------------------------------------------------------------------- #


def _errored_corner(corner_id, diagnostics):
    return {
        "corner_id": corner_id,
        "status": "error",
        "measurements": [_measurement(None, "error")],
        "diagnostics": diagnostics,
    }


_BATCH_FAILED = {
    "severity": "error",
    "code": "batch_job_failed",
    "message": "batch job job-7 finished 'failed' without a usable report",
}


def test_delta_row_error_names_extracted_side_stage_and_remote_job():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = {
        "environment": {"remote": {"job_id": "job-7"}},
        "corners": [_errored_corner("tt/1.800V/27C", [_BATCH_FAILED])],
    }
    (row,) = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert row["status"] == "error"
    assert row["error"] == {
        "side": "extracted",
        "stage": "execute",
        "message": _BATCH_FAILED["message"],
        "code": "batch_job_failed",
        "remote": {"job_id": "job-7"},
    }


def test_delta_row_error_blames_schematic_side_when_it_errored():
    schematic = _sim_report([_errored_corner("tt/1.800V/27C", [_BATCH_FAILED])])
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    (row,) = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert row["error"]["side"] == "schematic"


def test_delta_row_error_with_empty_diagnostics_is_unknown_not_silent():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report([_errored_corner("tt/1.800V/27C", [])])
    (row,) = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert row["error"]["stage"] == "unknown"
    assert row["error"]["message"]
    assert "code" not in row["error"]


def test_ok_rows_carry_no_error_field():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    extracted = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.1)]}]
    )
    (row,) = _build_delta_rows(
        spec_row_prefix=None, schematic_report=schematic, extracted_report=extracted
    )
    assert "error" not in row


def test_unextracted_rows_carry_the_recorded_failure():
    schematic = _sim_report(
        [{"corner_id": "tt/1.800V/27C", "measurements": [_measurement(1.0)]}]
    )
    (row,) = _unextracted_delta_rows(
        spec_row_prefix=None,
        schematic_report=schematic,
        failure_message="no capacity",
        failure_code="batch_no_capacity",
    )
    assert row["error"] == {
        "side": "extracted",
        "stage": "submit",
        "message": "no capacity",
        "code": "batch_no_capacity",
    }


def test_run_pex_preserves_sim_error_code(tmp_path, resistor_layout, monkeypatch):
    import klayout_tools.pex as pex_module
    from klayout_tools.sim import SimError

    def _fake_run_sim(*_args, **_kwargs):
        raise SimError("batch fleet has no capacity", code="batch_no_capacity")

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_request(tmp_path / "request.json", tb)

    with pytest.raises(PexError) as excinfo:
        run_pex(resistor_layout, [str(request)], "sky130")
    assert excinfo.value.code == "batch_no_capacity"


def test_errored_rows_summary_one_line():
    from klayout_tools.cli.pex_cmd import _errored_rows_summary

    error = {
        "side": "extracted",
        "stage": "execute",
        "message": "boom\nsecond line",
        "code": "batch_job_failed",
    }
    report = {"delta": [{"error": error}, {"status": "pass"}]}
    line = _errored_rows_summary(report)
    assert "\n" not in line
    assert line.startswith("klt pex: 1 of 2 delta rows errored")
    assert "stage execute" in line and "batch_job_failed" in line
    assert _errored_rows_summary({"delta": [{"status": "pass"}]}) is None


# `--device-only-leg` (issue #3298): a parasitic-free third leg
# --------------------------------------------------------------------------- #


def _leg_report(value, corner_id="tt/1.800V/27C"):
    return {
        "corners": [{"corner_id": corner_id, "measurements": [_measurement(value)]}],
        "corner_count": 1,
        "measurements": [{"name": "vout"}],
    }


def _fake_three_leg_sim(monkeypatch, *, device_only_value=1.5, device_only_error=False):
    """`run_sim` stub that tells the legs apart by the request path `run_pex`
    hands it: schematic 1.0, extracted 2.0, device-only ``device_only_value``."""
    import klayout_tools.pex as pex_module
    from klayout_tools.sim import SimError

    seen: list[str] = []

    def _fake_run_sim(request_path, **_kwargs):
        seen.append(os.path.basename(request_path))
        if "device-only" in request_path:
            if device_only_error:
                raise SimError("device-only leg failed")
            return _leg_report(device_only_value)
        if "extracted" in request_path:
            return _leg_report(2.0)
        return _leg_report(1.0)

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)
    return seen


def _device_only_inputs(tmp_path):
    dut = _write_schematic_dut(tmp_path / "schematic_dut.spice")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    return str(_write_request(tmp_path / "request.json", tb))


def test_build_delta_rows_device_only_split_reconciles():
    corner = "tt/1.800V/27C"
    rows = _build_delta_rows(
        spec_row_prefix=None,
        schematic_report=_sim_report(
            [{"corner_id": corner, "measurements": [_measurement(1.0)]}]
        ),
        extracted_report=_sim_report(
            [{"corner_id": corner, "measurements": [_measurement(2.0)]}]
        ),
        device_only_report=_sim_report(
            [{"corner_id": corner, "measurements": [_measurement(1.25)]}]
        ),
        device_only_leg=True,
    )
    (row,) = rows
    assert row["device_only_value"] == 1.25
    assert row["representation_delta_pct"] == 25.0
    assert row["parasitic_delta_pct"] == 60.0
    assert row["delta_pct"] == 100.0
    assert row["device_only_status"] == "pass"
    reconciled = (1 + row["representation_delta_pct"] / 100) * (
        1 + row["parasitic_delta_pct"] / 100
    ) - 1
    assert reconciled == pytest.approx(row["delta_pct"] / 100, abs=1e-3)


def test_build_delta_rows_device_only_nulls_on_missing_or_zero():
    corner = "tt/1.800V/27C"

    def rows_for(schematic, device_only):
        return _build_delta_rows(
            spec_row_prefix=None,
            schematic_report=_sim_report(
                [{"corner_id": corner, "measurements": [_measurement(schematic)]}]
            ),
            extracted_report=_sim_report(
                [{"corner_id": corner, "measurements": [_measurement(2.0)]}]
            ),
            device_only_report=(
                None
                if device_only is None
                else _sim_report(
                    [{"corner_id": corner, "measurements": [_measurement(device_only)]}]
                )
            ),
            device_only_leg=True,
        )[0]

    zero_schematic = rows_for(0.0, 1.0)
    assert zero_schematic["representation_delta_pct"] is None
    assert zero_schematic["parasitic_delta_pct"] == 100.0

    no_leg = rows_for(1.0, None)
    assert no_leg["device_only_value"] is None
    assert no_leg["representation_delta_pct"] is None
    assert no_leg["parasitic_delta_pct"] is None
    assert no_leg["device_only_status"] == "error"
    # The graded fields are untouched by the missing leg.
    assert no_leg["delta_pct"] == 100.0
    assert no_leg["status"] == "pass"


def test_build_delta_rows_without_flag_carries_no_device_only_keys():
    corner = "tt/1.800V/27C"
    (row,) = _build_delta_rows(
        spec_row_prefix=None,
        schematic_report=_sim_report(
            [{"corner_id": corner, "measurements": [_measurement(1.0)]}]
        ),
        extracted_report=_sim_report(
            [{"corner_id": corner, "measurements": [_measurement(2.0)]}]
        ),
    )
    assert set(row) == {
        "spec_row",
        "corner_id",
        "schematic_value",
        "extracted_value",
        "delta_pct",
        "status",
        "schematic_status",
        "extracted_status",
    }


def test_run_pex_device_only_leg_adds_third_leg(tmp_path, resistor_layout, monkeypatch):
    seen = _fake_three_leg_sim(monkeypatch, device_only_value=1.25)
    request = _device_only_inputs(tmp_path)

    report = run_pex(resistor_layout, [request], "sky130", device_only_leg=True)

    assert any("device-only" in name for name in seen)
    (row,) = report["delta"]
    assert row["schematic_value"] == 1.0
    assert row["extracted_value"] == 2.0
    assert row["device_only_value"] == 1.25
    assert row["representation_delta_pct"] == 25.0
    assert row["parasitic_delta_pct"] == 60.0

    # The path itself is redacted (`scope: external`) outside the repo, so
    # find the file where `run_pex` wrote it: beside the extracted netlist.
    assert report["device_only_netlist"]["scope"] == "external"
    (netlist,) = tmp_path.rglob("*.device-only.*")
    text = netlist.read_text()
    assert "__t" not in text
    cards = [
        ln for ln in text.splitlines() if ln and not ln.startswith(("*", "+", "."))
    ]
    assert not any(ln[0] in "Cc" for ln in cards)
    assert report["device_only_netlist_sha256"].startswith("sha256:")
    assert report["device_cards_match"] is True
    assert report["device_card_count"] >= 1


def test_run_pex_without_device_only_leg_is_unchanged(
    tmp_path, resistor_layout, monkeypatch
):
    seen = _fake_three_leg_sim(monkeypatch)
    request = _device_only_inputs(tmp_path)

    report = run_pex(resistor_layout, [request], "sky130")

    assert not any("device-only" in name for name in seen)
    for key in (
        "device_only_netlist",
        "device_only_netlist_sha256",
        "device_card_count",
        "device_cards_match",
    ):
        assert key not in report
    assert "device_only_value" not in report["delta"][0]


def test_run_pex_device_only_leg_failure_nulls_only_its_fields(
    tmp_path, resistor_layout, monkeypatch
):
    _fake_three_leg_sim(monkeypatch, device_only_error=True)
    request = _device_only_inputs(tmp_path)

    report = run_pex(resistor_layout, [request], "sky130", device_only_leg=True)

    (row,) = report["delta"]
    assert row["device_only_value"] is None
    assert row["representation_delta_pct"] is None
    assert row["parasitic_delta_pct"] is None
    assert row["device_only_status"] == "error"
    assert row["delta_pct"] == 100.0
    assert row["status"] == "pass"
    assert report["status"] == "pass"


_DEVICE_ONLY_HARNESS = """\
import json, os, sys

side = os.environ["KLT_PEX_SIDE"]
assert sys.argv[-1] == os.environ["KLT_PEX_NETLIST"]
assert os.environ["KLT_PEX_ARTIFACTS_DIR"].endswith(os.path.join("measure", side))
with open(os.environ["KLT_PEX_LOG"], "a") as log:
    log.write(side + "\\n")
if side == "device-only" and os.environ.get("KLT_TEST_FAIL") == "1":
    sys.exit(3)
value = {"schematic": 1.0, "device-only": 1.2, "extracted": 1.5}[side]
if side == "device-only" and os.environ.get("KLT_TEST_NULL") == "1":
    value = None
print(
    json.dumps(
        {
            "corners": [
                {
                    "corner_id": "tt/1.800V/27C",
                    "measurements": [
                        {"name": "t_cross", "value": value, "status": "pass"}
                    ],
                }
            ]
        }
    )
)
"""


def _run_device_only_command(
    tmp_path, resistor_layout, measure_reference, monkeypatch, **env
):
    log = tmp_path / "sides.log"
    monkeypatch.setenv("KLT_PEX_LOG", str(log))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        measure_command=_write_harness(tmp_path, _DEVICE_ONLY_HARNESS),
        reference_netlist=str(measure_reference),
        device_only_leg=True,
    )
    return report, log


def test_run_pex_device_only_leg_with_measure_command_splits_delta(
    tmp_path, resistor_layout, measure_reference, monkeypatch
):
    report, log = _run_device_only_command(
        tmp_path, resistor_layout, measure_reference, monkeypatch
    )

    assert log.read_text().split() == ["schematic", "extracted", "device-only"]
    assert set(report["measurement"]["sides"]) == {
        "schematic",
        "extracted",
        "device_only",
    }
    (row,) = report["delta"]
    assert row["device_only_value"] == 1.2
    assert row["representation_delta_pct"] == pytest.approx(20.0)
    assert row["parasitic_delta_pct"] == pytest.approx(25.0)
    assert row["device_only_status"] == "pass"


def test_run_pex_device_only_leg_command_null_value_is_error_row(
    tmp_path, resistor_layout, measure_reference, monkeypatch
):
    report, _ = _run_device_only_command(
        tmp_path, resistor_layout, measure_reference, monkeypatch, KLT_TEST_NULL="1"
    )

    (row,) = report["delta"]
    assert row["device_only_value"] is None
    assert row["representation_delta_pct"] is None
    assert row["parasitic_delta_pct"] is None
    assert row["device_only_status"] != "pass"
    assert row["delta_pct"] == pytest.approx(50.0)


def test_run_pex_device_only_leg_command_failure_is_pex_error(
    tmp_path, resistor_layout, measure_reference, monkeypatch
):
    with pytest.raises(PexError):
        _run_device_only_command(
            tmp_path,
            resistor_layout,
            measure_reference,
            monkeypatch,
            KLT_TEST_FAIL="1",
        )


def test_run_pex_measure_command_without_device_only_leg_has_no_third_side(
    tmp_path, resistor_layout, measure_reference
):
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        measure_command=_write_harness(tmp_path),
        reference_netlist=str(measure_reference),
    )

    assert set(report["measurement"]["sides"]) == {"schematic", "extracted"}
    assert "device_only_value" not in report["delta"][0]


# --- `--extract-only` (issue #3739) ----------------------------------------


def test_run_pex_extract_only_reports_extraction_without_measuring(
    tmp_path, resistor_layout
):
    netlist = tmp_path / "res.spice"
    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(netlist),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
    )

    assert netlist.is_file()
    assert report["extraction"]["device_count"] >= 1
    assert report["body_bias"]
    assert report["provenance"]
    assert report["testbenches"] == []
    assert report["delta"] == []
    assert report["measurement"] == {
        "mode": "extract-only",
        "command": None,
        "timeout_s": None,
        "sides": None,
    }
    assert report["status"] == "not_checked"
    assert report["coverage"]["nothing_checked_reasons"] == ["no_delta_rows"]
    assert report["reference_netlist"] == {"path": None, "scope": "absent"}
    assert report["reference_netlist_sha256"] is None
    assert report["pin_count_mismatch"] is None
    assert report["flat_dut_mismatch"] is None
    assert report["model_mismatch"] is None


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"measure_command": ["true"]}, "--measure-command"),
        ({"reference_netlist": "x.spice"}, "--reference-netlist"),
        ({"measure_timeout_s": 5.0}, "--measure-timeout-s"),
        ({"device_only_leg": True}, "--device-only-leg"),
        ({"flatten_schematic": True}, "--flatten-schematic"),
        ({"testbench_paths": ["tb.json"]}, "a testbench request"),
    ],
)
def test_run_pex_extract_only_rejects_measurement_options(
    tmp_path, resistor_layout, kwargs, needle
):
    testbenches = kwargs.pop("testbench_paths", [])
    with pytest.raises(PexError, match=f"--extract-only cannot be combined.*{needle}"):
        run_pex(
            resistor_layout,
            testbenches,
            "sky130",
            output=str(tmp_path / "res.spice"),
            artifacts_dir=str(tmp_path / "art"),
            extract_only=True,
            **kwargs,
        )


def test_cli_pex_extract_only_json_and_exit_code(tmp_path, resistor_layout, capsys):
    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "--extract-only",
            "-o",
            str(tmp_path / "res.spice"),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 4
    report = json.loads(capsys.readouterr().out)
    assert report["measurement"]["mode"] == "extract-only"
    assert report["status"] == "not_checked"


def test_cli_pex_extract_only_text_prints_mode(tmp_path, resistor_layout, capsys):
    main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "--extract-only",
            "-o",
            str(tmp_path / "res.spice"),
            "--outdir",
            str(tmp_path / "art"),
        ]
    )
    assert "measurement: extract-only" in capsys.readouterr().out


def test_cli_pex_extract_only_conflict_is_clean_error(
    tmp_path, resistor_layout, capsys
):
    exit_code = main(
        [
            "pex",
            resistor_layout,
            "--deck",
            "sky130",
            "--extract-only",
            "--device-only-leg",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    assert (
        "--extract-only cannot be combined"
        in json.loads(capsys.readouterr().err)["error"]["message"]
    )


def test_signoff_refuses_extract_only_report_as_evidence(tmp_path, resistor_layout):
    """Issue #3739: an `--extract-only` envelope is not comparison evidence."""
    from klayout_tools.signoff import build_signoff

    report = run_pex(
        resistor_layout,
        [],
        "sky130",
        output=str(tmp_path / "res.spice"),
        artifacts_dir=str(tmp_path / "art"),
        extract_only=True,
    )
    path = tmp_path / "pex.json"
    path.write_text(json.dumps(report))

    check = build_signoff([str(path)])["checks"][0]

    assert check["passed"] is False
    assert check["detail"]["nothing_checked_reasons"] == ["no_delta_rows"]


# ---------------------------------------------------------------------------
# --resistance-graph (issue #4037): pex consumes the selected scalar
# ---------------------------------------------------------------------------

_STRAP_GDS = Path(__file__).parent / "fixtures" / "resistance_graph_strap"


def _netlist_resistors(path) -> list[float]:
    """Every ``R`` card's value in a written SPICE netlist."""
    values = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if parts and parts[0].upper().startswith("R") and len(parts) >= 4:
            value = float(parts[3])
            if value < 1e9:  # skip the 1 TOhm substrate-isolation card
                values.append(value)
    return values


def _pex_strapped(tmp_path, name, label, **kwargs):
    out = tmp_path / f"{label}.spice"
    report = run_pex(
        str(_STRAP_GDS / f"{name}.gds"),
        [],
        "sky130",
        output=str(out),
        artifacts_dir=str(tmp_path / f"art_{label}"),
        extract_only=True,
        **kwargs,
    )
    return report, out


def test_run_pex_resistance_graph_writes_selected_resistance(tmp_path):
    """The strapped conductor's pex-written R is the graph value (smaller than
    the legacy series sum), the envelope echoes the source, and without the
    flag the netlist and report carry neither."""
    base, base_out = _pex_strapped(tmp_path, "strapped", "base")
    graph, graph_out = _pex_strapped(
        tmp_path,
        "strapped",
        "graph",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0.0,
    )

    assert "resistance_graph" not in base["extraction"]
    echo = graph["extraction"]["resistance_graph"]
    assert echo["r_cut_ohm"] == 0.0
    (net,) = echo["nets"]
    assert net["resistance_source"] == "graph"
    assert net["selected_resistance_ohm"] < net["legacy_resistance_ohm"]
    assert sum(_netlist_resistors(base_out)) == pytest.approx(
        net["legacy_resistance_ohm"], rel=1e-3
    )
    assert sum(_netlist_resistors(graph_out)) == pytest.approx(
        net["selected_resistance_ohm"], rel=1e-3
    )
    assert graph["extraction"]["netlist_sha256"] != base["extraction"]["netlist_sha256"]


def test_run_pex_resistance_graph_legacy_net_keeps_legacy_value(tmp_path):
    """Without a per-cut resistance the strapped net is `unsupported`: the
    source stays `legacy` and the written netlist is unchanged."""
    base, _ = _pex_strapped(tmp_path, "strapped", "base")
    graph, _ = _pex_strapped(tmp_path, "strapped", "graph", resistance_graph=True)

    (net,) = graph["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "legacy"
    assert net["selected_resistance_ohm"] == net["legacy_resistance_ohm"]
    assert graph["extraction"]["netlist_sha256"] == base["extraction"]["netlist_sha256"]


def test_run_pex_resistance_graph_r_cut_zero_is_forwarded(tmp_path, monkeypatch):
    calls = _capture_run_extract_kwargs(monkeypatch)
    _pex_strapped(
        tmp_path,
        "single_level",
        "g",
        resistance_graph=True,
        resistance_graph_r_cut_ohm=0,
    )
    assert calls[0]["resistance_graph"] is True
    assert calls[0]["resistance_graph_r_cut_ohm"] == 0
    assert calls[0]["emit_selected_resistance"] is True


def test_run_pex_resistance_graph_default_passes_no_graph_request(
    tmp_path, monkeypatch
):
    calls = _capture_run_extract_kwargs(monkeypatch)
    _pex_strapped(tmp_path, "single_level", "d")
    assert calls[0]["resistance_graph"] is False
    assert calls[0]["emit_selected_resistance"] is False


def test_run_pex_resistance_graph_r_cut_requires_graph_flag(tmp_path):
    with pytest.raises(PexError, match="--resistance-graph-r-cut requires"):
        _pex_strapped(tmp_path, "strapped", "x", resistance_graph_r_cut_ohm=1.0)
    with pytest.raises(PexError, match="finite number >= 0"):
        _pex_strapped(
            tmp_path,
            "strapped",
            "y",
            resistance_graph=True,
            resistance_graph_r_cut_ohm=-1.0,
        )


def test_run_pex_resistance_graph_rejects_distributed_rc(tmp_path):
    with pytest.raises(PexError, match="cannot be combined with --distributed-rc"):
        _pex_strapped(
            tmp_path,
            "strapped",
            "z",
            resistance_graph=True,
            critical_nets=["S"],
            distributed_rc=True,
        )


def test_cli_pex_resistance_graph_extract_only_json(tmp_path, capsys):
    out = tmp_path / "cli.spice"
    code = main(
        [
            "pex",
            str(_STRAP_GDS / "strapped.gds"),
            "--deck",
            "sky130",
            "--extract-only",
            "--resistance-graph",
            "--resistance-graph-r-cut",
            "0",
            "-o",
            str(out),
            "--outdir",
            str(tmp_path / "art"),
            "--format",
            "json",
        ]
    )
    assert code == 4  # extract-only: nothing checked
    report = json.loads(capsys.readouterr().out)
    (net,) = report["extraction"]["resistance_graph"]["nets"]
    assert net["resistance_source"] == "graph"
