"""Tests for `klt signoff` and the `build_signoff`/`build_tier_report`
library functions.

Fixtures are hand-built JSON envelope dicts matching the documented shapes
of `klt drc` (docs/cli/drc.md), `klt lvs` (docs/cli/lvs.md), `klt extract`
(docs/cli/extract.md), and `klt sim` (docs/cli/sim.md) -- no dependency on
actually running those commands, mirroring tests/test_report.py's "no
external corpus needed" convention.

The tier-verdict report tests (issue #722, Phase 0 of epic #706) live in
this same file rather than a separate one -- they exercise the same `klt
signoff` CLI surface (`signoff_cmd.py`) and reuse this file's envelope
fixtures, so splitting them out would only add a cross-file import.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools import signoff as signoff_module
from klayout_tools.cli import main
from klayout_tools.design_evidence_tiers import DesignEvidenceTiersError
from klayout_tools.signoff import (
    SignoffError,
    build_fleet_report,
    build_signoff,
    build_tier_report,
)

#: Repo root, resolved once -- used by the real-subprocess gate-binding
#: tests below (issue #825) to locate `examples/design-pipeline/`'s
#: already-passing artifacts without depending on pytest's cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

DRC_CLEAN_ENVELOPE = {
    "schema_version": 1,
    "file": "design.gds",
    "deck": "sky130",
    "dbu_um": 0.001,
    "status": "clean",
    "violation_count": 0,
    "rule_counts": {},
    "violations": [],
    "coverage": {
        "deck_layers": ["65/20"],
        "layers_checked": ["65/20"],
        "layers_in_stream_without_rules": [],
        "rules_skipped": [],
    },
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": None,
        "deck": {"name": "sky130", "content_hash": "sha256:drcdeck"},
        "input": {"content_hash": "sha256:layoutA"},
    },
}

DRC_VIOLATIONS_ENVELOPE = {
    **DRC_CLEAN_ENVELOPE,
    "status": "violations",
    "violation_count": 1,
    "rule_counts": {"poly.width.1": 1},
    "violations": [
        {
            "rule": "poly.width.1",
            "description": "minimum poly width",
            "check": "width",
            "layer": "poly.drawing",
            "cell": "TOP",
            "bbox": {"left": 0, "bottom": 0, "right": 100, "top": 2000},
            "polygon": None,
        }
    ],
}

LVS_MATCH_ENVELOPE = {
    "schema_version": 1,
    "engine": "klayout",
    "layout": "design.spice",
    "reference": "golden.spice",
    "top": "INV",
    "parameter_tolerance": None,
    "status": "match",
    "mismatch_count": 0,
    "category_counts": {},
    "counts": {
        "nets": {"layout": 2, "reference": 2, "matched": 2},
        "devices": {"layout": 2, "reference": 2, "matched": 2},
        "pins": {"layout": 2, "reference": 2, "matched": 2},
    },
    "device_classes": ["nfet", "pfet"],
    "environment": {
        "engine": "klayout",
        "engine_version": "0.30.10",
        "layout_sha256": "abc",
        "reference_sha256": "def",
        "extracted_netlist": None,
    },
    "mismatches": [],
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        # Same name+hash as DRC_CLEAN_ENVELOPE/EXTRACT_ENVELOPE's "sky130"
        # deck -- see EXTRACT_ENVELOPE's comment on why a real, unmodified
        # checkout always agrees here.
        "deck": {"name": "sky130", "content_hash": "sha256:drcdeck"},
        "input": None,
    },
}

LVS_MISMATCH_ENVELOPE = {
    **LVS_MATCH_ENVELOPE,
    "status": "mismatch",
    "mismatch_count": 1,
    "category_counts": {"net.unmatched": 1},
    "mismatches": [
        {
            "category": "net.unmatched",
            "severity": "error",
            "description": "reference net has no layout counterpart",
            "side": "reference",
            "net": {"layout": None, "reference": "VDD"},
            "device": None,
            "property": None,
        }
    ],
}

EXTRACT_ENVELOPE = {
    "schema_version": 2,
    "file": "design.gds",
    "deck": "sky130",
    "top": "TOP",
    "dbu_um": 0.001,
    "netlist_path": "design.spice",
    "netlist_sha256": "abc123",
    "status": "extracted",
    "device_count": 4,
    "net_count": 6,
    "pin_count": 3,
    "device_counts": {"nfet": 2, "pfet": 2},
    "ignored_layers": [],
    "device_recognition_only_layers": [],
    "device_classes": ["nfet", "pfet"],
    "devices": [],
    "nets": [{"name": "VDD", "pin": True, "device_count": 2}],
    "warnings": [],
    "black_box_regions": [],
    "abstracted_cells": [],
    "unmodelled_poly": [],
    "voltage_domain_warnings": [],
    "merged_net_labels": [],
    "unbiased_pmos_body_nets": [],
    "single_terminal_nets": [],
    "pdk": None,
    "parasitics": None,
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": None,
        # Same "sky130" name AND same hash as DRC_CLEAN_ENVELOPE's deck: a
        # deck name maps 1:1 to one Python module (decks/sky130.py) that
        # backs both `klt drc --deck sky130` and `klt extract --deck
        # sky130` today (decks/__init__.py's deck_source_path()), so two
        # real runs against an unmodified checkout always agree here.
        "deck": {"name": "sky130", "content_hash": "sha256:drcdeck"},
        "input": {"content_hash": "sha256:layoutA"},
    },
}

SIM_PASS_ENVELOPE = {
    "schema_version": 1,
    "netlist": "design.spice",
    "status": "pass",
    "corner_count": 3,
    "passed": 3,
    "failed": 0,
    "errored": 0,
    "environment": {},
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": {"name": "sky130.lib.spice", "content_hash": "sha256:models"},
        "input": None,
    },
    "measurements": [],
    "corners": [],
}

SIM_FAIL_ENVELOPE = {
    **SIM_PASS_ENVELOPE,
    "status": "fail",
    "passed": 2,
    "failed": 1,
}

#: `klt yield` (issue #816, Phase 1a of epic #710) JSON report shape, per
#: docs/cli/yield.md's "JSON schema (the contract)" section -- hand-built
#: here exactly like every other kind's fixture (no dependency on the
#: klt_yield_native extension being built, and #816 hasn't merged into main
#: yet -- see signoff.py's "Statistical-evidence binding" docstring
#: section). Deliberately carries **no** `provenance` block, matching that
#: doc's schema today -- the whole reason signoff.py's yield binding
#: computes its own content hash instead of reading one off the envelope.
YIELD_PASS_ENVELOPE = {
    "schema_version": 1,
    "samples": "mc-samples.json",
    "limits": "spec-limits.json",
    "source": {
        "kind": "sample-set",
        "netlist": None,
        "monte_carlo": None,
        "sample_count": 300,
    },
    "confidence": 0.95,
    "target_ci_halfwidth": 0.01,
    "min_samples": 2,
    "status": "pass",
    "measurement_count": 1,
    "measurements": [
        {
            "name": "vref",
            "unit": "V",
            "n": 300,
            "errored": 0,
            "limits": {"min": 1.15, "max": 1.25, "target_yield": 0.95},
            "source_corners": [],
            "distribution": {
                "model": "normal",
                "mean": 1.2,
                "stddev": 0.01,
                "min": 1.17,
                "max": 1.23,
                "median": 1.2,
                "skewness": 0.0,
                "excess_kurtosis": 0.0,
                "normality": {
                    "test": "anderson-darling",
                    "statistic": 0.2,
                    "critical_value": 0.787,
                    "significance": 0.05,
                    "verdict": "consistent",
                },
            },
            "yield": {
                "empirical": {
                    "method": "clopper-pearson",
                    "estimate": 1.0,
                    "confidence": 0.95,
                    "confidence_interval": {"low": 0.99, "high": 1.0},
                    "n": 300,
                },
                "normal": {
                    "method": "normal-delta",
                    "estimate": 0.999,
                    "confidence": 0.95,
                    "confidence_interval": {"low": 0.995, "high": 1.0},
                    "n": 300,
                },
            },
            "capability": {
                "cp": 1.5,
                "cpk": 1.4,
                "cpk_confidence_interval": {"low": 1.2, "high": 1.6},
                "sigma_to_spec": 4.2,
                "sigma_to_spec_confidence_interval": {"low": 3.6, "high": 4.8},
                "limiting_side": "upper",
            },
            "sample_size": {
                "n": 300,
                "observed_ci_halfwidth": 0.005,
                "target_ci_halfwidth": 0.01,
                "required_n": 100,
                "required_n_for_target": 150,
                "verdict": "sufficient",
                "method": "clopper-pearson-zero-failures",
            },
            "status": "pass",
            "warnings": [],
        }
    ],
    "warnings": [],
}

YIELD_FAIL_ENVELOPE = {**YIELD_PASS_ENVELOPE, "status": "fail"}

#: `status: "reported"` -- no measurement declared a `target_yield`, so
#: nothing could fail (docs/cli/yield.md's `status` field); this must still
#: count as a passing check, distinct from `"fail"`.
YIELD_REPORTED_ENVELOPE = {**YIELD_PASS_ENVELOPE, "status": "reported"}

#: `klt pex` (Epic #709) JSON report shape -- hand-built here exactly like
#: every other kind's fixture. This is the **Curator-proposed, provisional**
#: shape issue #871 introduced ahead of `klt pex` itself existing (its
#: defining issue, #801, has since shipped the real command,
#: `src/klayout_tools/pex.py`, matching this shape exactly -- see
#: `tests/test_pex.py`). See signoff.py's "Post-layout binding" docstring
#: section.
PEX_PASS_ENVELOPE = {
    "schema_version": 1,
    "status": "pass",
    "netlist": "extracted.spice",
    "reference_netlist": "schematic.spice",
    "corner_count": 3,
    "delta": [
        {
            "spec_row": "gain_db",
            "corner_id": "tt/1.800V/27C",
            "schematic_value": 42.1,
            "extracted_value": 41.6,
            "delta_pct": -1.19,
            "status": "pass",
        }
    ],
    "passed": 3,
    "failed": 0,
    "errored": 0,
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": None,
        "input": {"content_hash": "sha256:extractedpex"},
    },
}

PEX_FAIL_ENVELOPE = {**PEX_PASS_ENVELOPE, "status": "fail", "passed": 2, "failed": 1}

DRC_ERROR_ENVELOPE = {
    "schema_version": 1,
    "error": {"command": "drc", "message": "file not found: missing.gds"},
}


def _write(tmp_path, name: str, payload: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


# --------------------------------------------------------------------------- #
# build_signoff(): per-kind classification + pass/fail
# --------------------------------------------------------------------------- #


def test_drc_clean_check_passes(tmp_path):
    path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_signoff([path])

    assert result["schema_version"] == 1
    assert result["status"] == "pass"
    assert result["check_count"] == 1
    assert result["passed_count"] == 1
    assert result["failed_count"] == 0
    check = result["checks"][0]
    assert check["kind"] == "drc"
    assert check["status"] == "clean"
    assert check["passed"] is True
    assert check["detail"]["violation_count"] == 0


def test_drc_violations_check_fails(tmp_path):
    path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False


def test_lvs_match_check_passes(tmp_path):
    path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "lvs"
    assert check["passed"] is True
    assert check["detail"]["mismatch_count"] == 0


def test_lvs_mismatch_check_fails(tmp_path):
    path = _write(tmp_path, "lvs.json", LVS_MISMATCH_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False


def test_sim_pass_check_passes(tmp_path):
    path = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "sim"
    assert check["passed"] is True
    assert check["detail"]["corner_count"] == 3


def test_sim_fail_check_fails(tmp_path):
    path = _write(tmp_path, "sim.json", SIM_FAIL_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False


def test_yield_pass_check_passes(tmp_path):
    path = _write(tmp_path, "yield.json", YIELD_PASS_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "yield"
    assert check["passed"] is True
    assert check["detail"]["measurement_count"] == 1
    assert check["detail"]["samples"] == "mc-samples.json"
    assert check["detail"]["source_kind"] == "sample-set"
    assert check["detail"]["sample_count"] == 300


def test_yield_reported_check_passes(tmp_path):
    """`status: "reported"` (no measurement declared a `target_yield`, so
    nothing could fail) counts as passing, distinct from `"fail"`."""
    path = _write(tmp_path, "yield.json", YIELD_REPORTED_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    assert result["checks"][0]["passed"] is True


def test_yield_fail_check_fails(tmp_path):
    path = _write(tmp_path, "yield.json", YIELD_FAIL_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False


def test_extract_check_always_passes(tmp_path):
    path = _write(tmp_path, "extract.json", EXTRACT_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "extract"
    assert check["passed"] is True
    assert check["detail"]["device_count"] == 4


def test_error_envelope_never_passes(tmp_path):
    path = _write(tmp_path, "error.json", DRC_ERROR_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["kind"] == "error"
    assert check["passed"] is False
    assert check["provenance"] is None
    assert check["detail"]["command"] == "drc"
    assert check["detail"]["message"] == "file not found: missing.gds"


# --------------------------------------------------------------------------- #
# Multi-envelope combination
# --------------------------------------------------------------------------- #


def test_all_passing_checks_combine_to_pass(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    extract_path = _write(tmp_path, "extract.json", EXTRACT_ENVELOPE)
    sim_path = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)

    result = build_signoff([drc_path, lvs_path, extract_path, sim_path])

    assert result["status"] == "pass"
    assert result["check_count"] == 4
    assert result["passed_count"] == 4
    assert result["failed_count"] == 0
    assert [c["kind"] for c in result["checks"]] == ["drc", "lvs", "extract", "sim"]


def test_one_failing_check_makes_overall_fail(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MISMATCH_ENVELOPE)

    result = build_signoff([drc_path, lvs_path])

    assert result["status"] == "fail"
    assert result["passed_count"] == 1
    assert result["failed_count"] == 1


# --------------------------------------------------------------------------- #
# Provenance consistency (issue #251 + #309 AC #2)
# --------------------------------------------------------------------------- #


def test_matching_input_hashes_stay_consistent(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    extract_path = _write(tmp_path, "extract.json", EXTRACT_ENVELOPE)

    result = build_signoff([drc_path, extract_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["provenance_consistency"]["mismatches"] == []
    assert result["status"] == "pass"


def test_mismatched_input_hash_is_refused(tmp_path):
    stale_extract = {
        **EXTRACT_ENVELOPE,
        "provenance": {
            **EXTRACT_ENVELOPE["provenance"],
            "input": {"content_hash": "sha256:layoutB"},
        },
    }
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    extract_path = _write(tmp_path, "extract.json", stale_extract)

    result = build_signoff([drc_path, extract_path])

    assert result["status"] == "refused"
    mismatches = result["provenance_consistency"]["mismatches"]
    assert len(mismatches) == 1
    assert mismatches[0]["field"] == "input.content_hash"
    values = {entry["source"]: entry["value"] for entry in mismatches[0]["values"]}
    assert values[drc_path] == "sha256:layoutA"
    assert values[extract_path] == "sha256:layoutB"


def test_mismatched_pdk_name_is_refused(tmp_path):
    other_pdk_sim = {
        **SIM_PASS_ENVELOPE,
        "provenance": {
            **SIM_PASS_ENVELOPE["provenance"],
            "pdk": {"name": "gf180mcuD", "source": "volare", "version": "20240101"},
        },
    }
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)  # pdk.name sky130A
    sim_path = _write(tmp_path, "sim.json", other_pdk_sim)

    result = build_signoff([lvs_path, sim_path])

    assert result["status"] == "refused"
    fields = {m["field"] for m in result["provenance_consistency"]["mismatches"]}
    assert "pdk.name" in fields


def test_mismatched_pdk_version_is_refused(tmp_path):
    newer_pdk_sim = {
        **SIM_PASS_ENVELOPE,
        "provenance": {
            **SIM_PASS_ENVELOPE["provenance"],
            "pdk": {"name": "sky130A", "source": "volare", "version": "20250101"},
        },
    }
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)  # version 20240101
    sim_path = _write(tmp_path, "sim.json", newer_pdk_sim)

    result = build_signoff([lvs_path, sim_path])

    assert result["status"] == "refused"
    fields = {m["field"] for m in result["provenance_consistency"]["mismatches"]}
    assert "pdk.version" in fields


def test_mismatched_same_named_deck_hash_is_refused(tmp_path):
    other_deck_lvs = {
        **LVS_MATCH_ENVELOPE,
        "provenance": {
            **LVS_MATCH_ENVELOPE["provenance"],
            "deck": {"name": "sky130", "content_hash": "sha256:otherdeck"},
        },
    }
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)  # deck sky130 / drcdeck
    lvs_path = _write(tmp_path, "lvs.json", other_deck_lvs)

    result = build_signoff([drc_path, lvs_path])

    assert result["status"] == "refused"
    mismatches = result["provenance_consistency"]["mismatches"]
    assert mismatches[0]["field"] == "deck[sky130].content_hash"


def test_different_named_decks_never_compared(tmp_path):
    # klt drc's "sky130" deck and klt lvs's differently-named model deck
    # never collide with each other -- only same-named decks are compared.
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)  # deck "sky130"
    sim_path = _write(
        tmp_path, "sim.json", SIM_PASS_ENVELOPE
    )  # deck "sky130.lib.spice"

    result = build_signoff([drc_path, sim_path])

    assert result["provenance_consistency"]["ok"] is True


def test_error_check_excluded_from_provenance_comparison(tmp_path):
    # An error-kind check carries no provenance block at all -- it must not
    # spuriously trip the consistency gate (it already fails the check
    # itself).
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    error_path = _write(tmp_path, "error.json", DRC_ERROR_ENVELOPE)

    result = build_signoff([drc_path, error_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["status"] == "fail"  # the error check itself still fails


def test_single_input_never_flags_provenance(tmp_path):
    path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_signoff([path])

    assert result["provenance_consistency"]["ok"] is True


# --------------------------------------------------------------------------- #
# Malformed / unrecognized envelopes
# --------------------------------------------------------------------------- #


def test_missing_file_raises(tmp_path):
    with pytest.raises(SignoffError, match="file not found"):
        build_signoff([str(tmp_path / "nope.json")])


def test_directory_raises(tmp_path):
    with pytest.raises(SignoffError, match="not a file"):
        build_signoff([str(tmp_path)])


def test_malformed_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")

    with pytest.raises(SignoffError, match="not valid JSON"):
        build_signoff([str(path)])


def test_non_object_json_raises(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")

    with pytest.raises(SignoffError, match="must be a JSON object"):
        build_signoff([str(path)])


def test_missing_schema_version_raises(tmp_path):
    path = _write(tmp_path, "no_version.json", {"foo": "bar"})

    with pytest.raises(SignoffError, match="schema_version"):
        build_signoff([path])


def test_unrecognized_shape_raises(tmp_path):
    path = _write(tmp_path, "unknown.json", {"schema_version": 1, "foo": "bar"})

    with pytest.raises(SignoffError, match="unrecognized shape"):
        build_signoff([path])


def test_layout_metrics_shape_is_unrecognized(tmp_path):
    # klt signoff aggregates only drc/lvs/extract/sim -- a layout-metrics
    # envelope (which klt report *does* recognize) is out of scope here.
    layout_metrics_envelope = {
        "schema_version": 1,
        "slug": "example-block",
        "name": "Example Block",
        "status": "ok",
    }
    path = _write(tmp_path, "metrics.json", layout_metrics_envelope)

    with pytest.raises(SignoffError, match="unrecognized shape"):
        build_signoff([path])


def test_no_sources_raises():
    with pytest.raises(SignoffError, match="at least one"):
        build_signoff([])


# --------------------------------------------------------------------------- #
# CLI (`klt signoff`)
# --------------------------------------------------------------------------- #


def test_cli_pass_exits_zero(tmp_path, capsys):
    path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    exit_code = main(["signoff", path, "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "pass"


def test_cli_fail_exits_three(tmp_path, capsys):
    path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    exit_code = main(["signoff", path, "--format", "json"])

    assert exit_code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "fail"


def test_cli_refused_exits_four(tmp_path, capsys):
    stale_extract = {
        **EXTRACT_ENVELOPE,
        "provenance": {
            **EXTRACT_ENVELOPE["provenance"],
            "input": {"content_hash": "sha256:layoutB"},
        },
    }
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    extract_path = _write(tmp_path, "extract.json", stale_extract)

    exit_code = main(["signoff", drc_path, extract_path, "--format", "json"])

    assert exit_code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused"


def test_cli_text_default_format(tmp_path, capsys):
    path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    exit_code = main(["signoff", path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: pass" in out
    assert "[PASS] drc" in out


def test_cli_stdin_input(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(DRC_CLEAN_ENVELOPE)))

    exit_code = main(["signoff", "-", "--format", "json"])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checks"][0]["source"] == "-"


def test_cli_missing_file_exits_one_json_error(tmp_path, capsys):
    exit_code = main(["signoff", str(tmp_path / "missing.json"), "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "signoff"
    assert "file not found" in err["error"]["message"]
    assert capsys.readouterr().out == ""


def test_cli_missing_file_exits_one_text_error(tmp_path, capsys):
    exit_code = main(["signoff", str(tmp_path / "missing.json")])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt signoff:")


def test_cli_no_files_and_no_manifest_exits_one(capsys):
    # `files` is `nargs="*"` (not `nargs="+"`) so that `--manifest`-only
    # invocations (issue #722) don't require a dummy positional file; the
    # "give me something to do" check moves from argparse (exit 2) to an
    # application-level error (exit 1) instead.
    exit_code = main(["signoff"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt signoff:")
    assert "--manifest" in err


# --------------------------------------------------------------------------- #
# build_tier_report(): T1-T4 tier-verdict report (issue #722)
# --------------------------------------------------------------------------- #


def _manifest(**overrides) -> dict:
    base = {"block": "demo-block", "kind": "analog", "evidence": {}}
    base.update(overrides)
    return base


def test_analog_manifest_renders_ten_t1_items_plus_three_ladder_rows():
    result = build_tier_report(_manifest(kind="analog"))

    assert result["schema_version"] == 1
    assert result["block"] == "demo-block"
    assert result["kind"] == "analog"
    assert result["t1_item_count"] == 10
    assert result["t1_met_count"] == 0
    assert result["tier"] is None
    assert result["source_doc"] == "docs/design-evidence-tiers.md"

    t1_items = [item for item in result["items"] if item["tier"] == "T1"]
    ladder_items = [item for item in result["items"] if item["tier"] != "T1"]
    assert [item["id"] for item in t1_items] == list(range(1, 11))
    assert {item["tier"] for item in ladder_items} == {"T2", "T3", "T4"}
    assert all(item["status"] == "unmet" for item in result["items"])


def test_digital_manifest_uses_the_digital_column():
    result = build_tier_report(_manifest(kind="digital"))

    item_1 = next(item for item in result["items"] if item["id"] == 1)
    assert "RTL" in item_1["text"]
    assert "schematic" not in item_1["text"]


def test_analog_manifest_uses_the_analog_column():
    result = build_tier_report(_manifest(kind="analog"))

    item_1 = next(item for item in result["items"] if item["id"] == 1)
    assert "schematic" in item_1["text"]


def test_mixed_signal_manifest_doubles_up_kind_independent_items():
    result = build_tier_report(_manifest(kind="mixed-signal"))

    # Items 1/2/5/7 split per-kind, items 3/4/6/8/9/10 are kind-independent
    # but still rendered once per partition per the doc's mixed-signal
    # guidance -- 10 items x 2 partitions.
    t1_items = [item for item in result["items"] if item["tier"] == "T1"]
    assert len(t1_items) == 20
    assert result["t1_item_count"] == 20
    partitions = {item["partition"] for item in t1_items}
    assert partitions == {"analog", "digital"}


def test_t2_t4_ladder_items_are_always_unmet_and_use_ladder_text():
    result = build_tier_report(_manifest(kind="analog"))

    t2 = next(item for item in result["items"] if item["tier"] == "T2")
    assert t2["status"] == "unmet"
    assert t2["citation"] is None
    assert t2["id"] is None
    assert "commercial" in t2["text"]


def test_met_item_carries_a_citation_with_file_hash_and_exit_status(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["reason"] is None
    assert item_3["citation"] == {
        "file": drc_path,
        "command": None,
        "kind": "drc",
        "check_status": "clean",
        "content_hash": "sha256:layoutA",
        "exit_status": 0,
    }
    assert result["t1_met_count"] == 1


def test_failing_check_renders_unmet_with_no_citation(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "check_failed"
    assert item_3["citation"] is None


def test_no_evidence_renders_unmet_never_assumed_met():
    result = build_tier_report(_manifest(evidence={}))

    assert all(item["status"] == "unmet" for item in result["items"])
    assert all(item["citation"] is None for item in result["items"])
    # Every item lacking a runnable check names why it is unmet -- never a
    # bare "unmet" with no further signal (issue #826).
    assert all(item["reason"] is not None for item in result["items"])


def test_missing_evidence_file_renders_unmet_not_an_error():
    result = build_tier_report(_manifest(evidence={"3": "/nonexistent/path/drc.json"}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "unreadable_evidence"
    assert item_3["citation"] is None


def test_malformed_evidence_entry_renders_unmet_not_an_error():
    result = build_tier_report(_manifest(evidence={"3": {"no_file_key": True}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "invalid_evidence"


def test_stale_content_hash_renders_unmet_not_a_false_pass(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_tier_report(
        _manifest(
            evidence={"3": {"file": drc_path, "content_hash": "sha256:stale-revision"}}
        )
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "stale_evidence"
    assert item_3["citation"] is None


def test_matching_content_hash_renders_met(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_tier_report(
        _manifest(evidence={"3": {"file": drc_path, "content_hash": "sha256:layoutA"}})
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["reason"] is None


def test_lvs_evidence_populates_lvs_kind_check(tmp_path):
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"4": lvs_path}))

    item_4 = next(item for item in result["items"] if item["id"] == 4)
    assert item_4["status"] == "met"
    assert item_4["citation"]["kind"] == "lvs"
    # klt lvs's provenance.input is always null (docs/json-contract.md) --
    # the citation still carries the field, just unpopulated.
    assert item_4["citation"]["content_hash"] is None


# --------------------------------------------------------------------------- #
# Statistical-evidence binding: item 6 <- `klt yield` (issue #870, Phase 2a
# of epic #706)
# --------------------------------------------------------------------------- #


def test_yield_evidence_populates_yield_kind_check_on_item_6(tmp_path):
    samples_path = tmp_path / "mc-samples.json"
    samples_path.write_text(json.dumps({"measurements": []}))
    envelope = {**YIELD_PASS_ENVELOPE, "samples": str(samples_path)}
    yield_path = _write(tmp_path, "yield.json", envelope)

    result = build_tier_report(_manifest(evidence={"6": yield_path}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "met"
    assert item_6["reason"] is None
    citation = item_6["citation"]
    assert citation["file"] == yield_path
    assert citation["command"] is None
    assert citation["kind"] == "yield"
    assert citation["check_status"] == "pass"
    assert citation["exit_status"] == 0
    # klt yield's current JSON shape carries no `provenance` block of its
    # own (issue #816) -- signoff.py hashes the referenced samples document
    # itself instead of reading a pre-existing content hash off the
    # envelope. See _yield_samples_content_hash().
    expected_hash = "sha256:" + hashlib.sha256(samples_path.read_bytes()).hexdigest()
    assert citation["content_hash"] == expected_hash


def test_yield_reported_status_renders_met_on_item_6(tmp_path):
    """`status: "reported"` (no measurement declared a `target_yield`) is a
    legitimate "met" outcome, distinct from `"fail"`."""
    yield_path = _write(tmp_path, "yield.json", YIELD_REPORTED_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"6": yield_path}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "met"


def test_yield_fail_status_renders_unmet_check_failed(tmp_path):
    yield_path = _write(tmp_path, "yield.json", YIELD_FAIL_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"6": yield_path}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "check_failed"
    assert item_6["citation"] is None


def test_no_backing_yield_campaign_renders_unmet_never_assumed_met():
    """AC (issue #870): "An item with no backing Monte-Carlo campaign
    renders unmet, never assumed met." -- item 6 goes through the exact same
    `_REASON_NO_EVIDENCE` machinery as every other item; there is no
    separate "statistical" code path that could fabricate a "met" here."""
    result = build_tier_report(_manifest(evidence={}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "no_evidence"
    assert item_6["citation"] is None


def test_yield_evidence_missing_samples_file_leaves_content_hash_none(tmp_path):
    envelope = {**YIELD_PASS_ENVELOPE, "samples": "/nonexistent/mc-samples.json"}
    yield_path = _write(tmp_path, "yield.json", envelope)

    result = build_tier_report(_manifest(evidence={"6": yield_path}))

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "met"
    assert item_6["citation"]["content_hash"] is None


def test_yield_matching_pinned_content_hash_renders_met(tmp_path):
    samples_path = tmp_path / "mc-samples.json"
    samples_path.write_text(json.dumps({"measurements": []}))
    expected_hash = "sha256:" + hashlib.sha256(samples_path.read_bytes()).hexdigest()
    envelope = {**YIELD_PASS_ENVELOPE, "samples": str(samples_path)}
    yield_path = _write(tmp_path, "yield.json", envelope)

    result = build_tier_report(
        _manifest(evidence={"6": {"file": yield_path, "content_hash": expected_hash}})
    )

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "met"


def test_yield_stale_pinned_content_hash_renders_unmet(tmp_path):
    samples_path = tmp_path / "mc-samples.json"
    samples_path.write_text(json.dumps({"measurements": []}))
    envelope = {**YIELD_PASS_ENVELOPE, "samples": str(samples_path)}
    yield_path = _write(tmp_path, "yield.json", envelope)

    result = build_tier_report(
        _manifest(
            evidence={
                "6": {"file": yield_path, "content_hash": "sha256:stale-revision"}
            }
        )
    )

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "unmet"
    assert item_6["reason"] == "stale_evidence"
    assert item_6["citation"] is None


def test_command_evidence_yield_hashes_samples_relative_to_command_cwd(
    tmp_path, monkeypatch
):
    """A command-backed yield entry's referenced ``samples`` path is
    resolved relative to the subprocess's own ``cwd`` -- the same directory
    a relative argument in ``<argv>`` would have resolved against."""
    samples_path = tmp_path / "mc-samples.json"
    samples_path.write_text(json.dumps({"measurements": []}))
    envelope = {**YIELD_PASS_ENVELOPE, "samples": "mc-samples.json"}

    def fake_run(command, **kwargs):
        assert kwargs.get("cwd") == str(tmp_path)
        return _FakeCompletedProcess(0, stdout=json.dumps(envelope))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(
            evidence={
                "6": {
                    "command": [
                        "klt",
                        "yield",
                        "mc-samples.json",
                        "--limits",
                        "spec-limits.json",
                        "--format",
                        "json",
                    ],
                    "cwd": str(tmp_path),
                }
            }
        )
    )

    item_6 = next(item for item in result["items"] if item["id"] == 6)
    assert item_6["status"] == "met"
    assert item_6["citation"]["command"] is not None
    expected_hash = "sha256:" + hashlib.sha256(samples_path.read_bytes()).hexdigest()
    assert item_6["citation"]["content_hash"] == expected_hash


def test_mixed_signal_manifest_shares_bare_item_6_yield_evidence_across_partitions(
    tmp_path,
):
    """Item 6 is kind-independent, so a mixed-signal manifest can cite the
    same `klt yield` evidence for both partitions via the bare `"6"` key --
    same convention every other kind-independent item already follows."""
    yield_path = _write(tmp_path, "yield.json", YIELD_PASS_ENVELOPE)

    result = build_tier_report(
        _manifest(kind="mixed-signal", evidence={"6": yield_path})
    )

    item_6_rows = [item for item in result["items"] if item["id"] == 6]
    assert len(item_6_rows) == 2
    assert {row["partition"] for row in item_6_rows} == {"analog", "digital"}
    assert all(row["status"] == "met" for row in item_6_rows)


# --------------------------------------------------------------------------- #
# Post-layout binding: item 7 <- `klt pex` (issue #871, Phase 2b of epic
# #706). These tests exercise the Curator-proposed, provisional envelope
# shape (PEX_PASS_ENVELOPE/PEX_FAIL_ENVELOPE above) that predates `klt pex`
# itself -- issue #801 ("Define `klt pex`", `src/klayout_tools/pex.py`) has
# since shipped the real command matching this shape exactly (see
# `tests/test_pex.py` for coverage of the real command's own output, and its
# compatibility with `_classify`/`_check_passed` here).
# --------------------------------------------------------------------------- #


def test_pex_evidence_satisfies_item_7(tmp_path):
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"7": pex_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "met"
    assert item_7["reason"] is None
    assert item_7["citation"] == {
        "file": pex_path,
        "command": None,
        "kind": "pex",
        "check_status": "pass",
        "content_hash": "sha256:extractedpex",
        "exit_status": 0,
    }


def test_pex_fail_status_renders_unmet_check_failed(tmp_path):
    pex_path = _write(tmp_path, "pex.json", PEX_FAIL_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"7": pex_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "check_failed"
    assert item_7["citation"] is None


def test_non_pex_evidence_for_item_7_renders_unmet(tmp_path):
    """The concrete gap this issue closes: before this phase, item 7 was
    kind-agnostic and would render "met" on citing any recognised, passing
    envelope -- even a bare `klt drc` report with nothing to do with
    post-layout re-simulation. A genuinely clean, passing DRC citation must
    now render item 7 "unmet" with reason "wrong_kind", never a borrowed
    pass."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"7": drc_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "wrong_kind"
    assert item_7["citation"] is None


def test_no_backing_pex_evidence_renders_unmet_never_assumed_met():
    """AC: "An item with no backing `klt pex` run renders unmet, never
    assumed met." -- item 7 goes through the exact same `_REASON_NO_EVIDENCE`
    machinery as every other item."""
    result = build_tier_report(_manifest(evidence={}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "no_evidence"
    assert item_7["citation"] is None


def test_command_evidence_pex_wrong_kind_renders_unmet(monkeypatch):
    """The kind restriction applies to command-backed evidence exactly like
    file-backed evidence -- a `klt sim` invocation cited for item 7 still
    renders "unmet", even though `klt sim` itself passed."""

    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(SIM_PASS_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(evidence={"7": {"command": ["klt", "sim", "request.json"]}})
    )

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "wrong_kind"
    assert item_7["citation"] is None


def test_items_other_than_7_are_unaffected_by_the_kind_restriction(tmp_path):
    """Regression: items 1-6 and 8-10 still accept any recognised, passing
    envelope kind -- only item 7 is kind-restricted."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    evidence = {str(i): drc_path for i in range(1, 11) if i != 7}
    result = build_tier_report(_manifest(evidence=evidence))

    for item in result["items"]:
        if item["tier"] == "T1" and item["id"] != 7:
            assert item["status"] == "met", item["id"]
            assert item["citation"]["kind"] == "drc"


# --------------------------------------------------------------------------- #
# `reason`: distinguishing missing evidence from a check that ran and failed
# (issue #826, Phase 1b of epic #706)
# --------------------------------------------------------------------------- #


def test_unrecognized_envelope_evidence_renders_unmet_with_that_reason(tmp_path):
    # Valid JSON, but not a recognized klt envelope shape at all (no
    # 'schema_version') -- distinct from an unreadable file and from a
    # malformed manifest entry.
    bogus_path = _write(tmp_path, "bogus.json", {"not": "an envelope"})

    result = build_tier_report(_manifest(evidence={"3": bogus_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "unrecognized_envelope"
    assert item_3["citation"] is None


def test_error_envelope_evidence_renders_unmet_with_check_errored_reason(tmp_path):
    error_path = _write(tmp_path, "error.json", DRC_ERROR_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"3": error_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "check_errored"
    assert item_3["citation"] is None


def test_ladder_rows_render_tier_not_supported_reason():
    result = build_tier_report(_manifest(evidence={}))

    ladder_items = [item for item in result["items"] if item["tier"] != "T1"]
    assert ladder_items  # sanity: T2-T4 rows exist
    assert all(item["reason"] == "tier_not_supported" for item in ladder_items)
    assert all(item["status"] == "unmet" for item in ladder_items)


def test_missing_evidence_reason_is_never_the_same_as_a_failed_check_reason(tmp_path):
    """The exact ambiguity issue #826 exists to kill: a JSON reader must be
    able to tell "nobody ever checked this" apart from "somebody checked
    this and it failed" without any information outside the item itself."""
    drc_violations_path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    result = build_tier_report(
        _manifest(evidence={"3": drc_violations_path})  # item 4 deliberately skipped
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)  # ran, failed
    item_4 = next(item for item in result["items"] if item["id"] == 4)  # skipped

    assert item_3["status"] == "unmet"
    assert item_4["status"] == "unmet"
    assert item_3["reason"] != item_4["reason"]
    assert item_3["reason"] == "check_failed"
    assert item_4["reason"] == "no_evidence"


def test_deliberately_skipped_check_is_caught_amid_otherwise_full_evidence(tmp_path):
    """AC: "A deliberately-skipped check on a test fixture is caught and
    reported as unmet." Every T1 item except #1 gets real, passing
    evidence; #1's evidence is deliberately omitted from the manifest, as
    if that check was simply never run. The aggregator must still catch
    it, render it unmet with a "no runnable check" reason, and must not
    let the block reach tier T1 despite every other item being genuinely
    met."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    evidence = {str(i): drc_path for i in range(2, 11)}  # 2-10, never 1
    evidence["4"] = lvs_path
    # Item 7 is kind-restricted (issue #871) -- give it real `pex` evidence
    # so this test's "every other item is genuinely met" claim still holds.
    evidence["7"] = pex_path

    result = build_tier_report(_manifest(evidence=evidence))

    item_1 = next(item for item in result["items"] if item["id"] == 1)
    assert item_1["status"] == "unmet"
    assert item_1["reason"] == "no_evidence"
    assert item_1["citation"] is None

    # Every other T1 item is genuinely met, with a real citation.
    other_t1_items = [
        item for item in result["items"] if item["tier"] == "T1" and item["id"] != 1
    ]
    assert other_t1_items
    assert all(item["status"] == "met" for item in other_t1_items)
    assert all(item["citation"] is not None for item in other_t1_items)

    # The skipped item is exactly what stops the tier from being T1 -- a
    # skipped check must never silently pass through as "assumed met".
    assert result["t1_met_count"] == 9
    assert result["t1_item_count"] == 10
    assert result["tier"] is None


def test_all_ten_t1_items_met_yields_tier_t1(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    # Item 7 is kind-restricted (issue #871) -- a `pex`-shaped citation is
    # required there; every other item still accepts the shared DRC fixture.
    evidence = {str(i): drc_path for i in range(1, 11)}
    evidence["4"] = lvs_path
    evidence["7"] = pex_path

    result = build_tier_report(_manifest(evidence=evidence))

    assert result["t1_met_count"] == 10
    assert result["tier"] == "T1"


# --------------------------------------------------------------------------- #
# Command-backed evidence: gate binding (issue #825, Phase 1 of epic #706)
#
# Mirrors tests/test_equiv.py's two-tier structure: mocked-subprocess unit
# tests for every resolution branch (fast, no external tool needed), plus a
# real-subprocess integration test at the bottom that actually invokes `klt
# drc`/`klt lvs`/`klt extract`/`klt sim` against genuine, already-passing
# artifacts checked into examples/design-pipeline/ -- proving gate binding
# end to end against real evidence, not a canned fixture.
# --------------------------------------------------------------------------- #


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_command_evidence_runs_and_grades_met(monkeypatch):
    calls: list[dict] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return _FakeCompletedProcess(0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(
            evidence={
                "3": {"command": ["klt", "drc", "design.gds", "--deck", "sky130"]}
            }
        )
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["reason"] is None
    assert item_3["citation"] == {
        "file": None,
        "command": "klt drc design.gds --deck sky130",
        "kind": "drc",
        "check_status": "clean",
        "content_hash": "sha256:layoutA",
        "exit_status": 0,
    }
    assert calls == [
        {
            "command": ["klt", "drc", "design.gds", "--deck", "sky130"],
            "cwd": None,
            "capture_output": True,
            "text": True,
            "timeout": signoff_module._COMMAND_EVIDENCE_TIMEOUT_S,
        }
    ]


def test_command_evidence_passes_cwd_through_to_subprocess(monkeypatch):
    seen_cwd = []

    def fake_run(command, **kwargs):
        seen_cwd.append(kwargs.get("cwd"))
        return _FakeCompletedProcess(0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    build_tier_report(
        _manifest(
            evidence={
                "3": {"command": ["klt", "drc", "design.gds"], "cwd": "/tmp/block"}
            }
        )
    )

    assert seen_cwd == ["/tmp/block"]


def test_command_evidence_nonzero_exit_renders_unmet_command_failed(monkeypatch):
    # Mirrors the real contract (docs/json-contract.md): an application-level
    # error (exit 1) writes its error envelope to stderr and leaves stdout
    # empty -- there is genuinely no evidence on stdout to grade.
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(1, stdout="")

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "command_failed"
    assert item_3["citation"] is None


def test_command_evidence_launch_failure_renders_command_failed(monkeypatch):
    def fake_run(command, **kwargs):
        raise OSError("No such file or directory: 'klt'")

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "command_failed"
    assert item_3["citation"] is None


def test_command_evidence_timeout_renders_command_failed(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "sim"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "command_failed"
    assert item_3["citation"] is None


def test_command_evidence_unparsable_stdout_renders_unreadable_evidence(monkeypatch):
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(0, stdout="not valid json")

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "unreadable_evidence"
    assert item_3["citation"] is None


def test_command_evidence_error_envelope_renders_check_errored(monkeypatch):
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(DRC_ERROR_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "check_errored"
    assert item_3["citation"] is None


def test_command_evidence_failing_check_renders_check_failed(monkeypatch):
    # Real `klt drc` exits 3 (EXIT_VIOLATIONS), never 0, when it finds
    # violations -- exercise that real exit code, not an unrealistic zero.
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(3, stdout=json.dumps(DRC_VIOLATIONS_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "check_failed"
    assert item_3["citation"] is None


def test_command_evidence_stale_content_hash_renders_unmet(monkeypatch):
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(
            evidence={
                "3": {
                    "command": ["klt", "drc"],
                    "content_hash": "sha256:stale-revision",
                }
            }
        )
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "stale_evidence"
    assert item_3["citation"] is None


def test_command_evidence_matching_content_hash_renders_met(monkeypatch):
    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(
            evidence={
                "3": {"command": ["klt", "drc"], "content_hash": "sha256:layoutA"}
            }
        )
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["citation"]["exit_status"] == 0


def test_empty_command_list_renders_invalid_evidence():
    result = build_tier_report(_manifest(evidence={"3": {"command": []}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "invalid_evidence"


def test_non_string_command_element_renders_invalid_evidence():
    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", 5]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "invalid_evidence"


def test_command_takes_precedence_over_file_when_both_given(monkeypatch, tmp_path):
    # The manifest schema does not ask a caller to send both keys, but
    # _normalize_evidence_entry() documents that it checks "command" first
    # -- confirm that contract holds.
    drc_path = _write(
        tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE
    )  # would render unmet

    def fake_run(command, **kwargs):
        return _FakeCompletedProcess(
            0, stdout=json.dumps(DRC_CLEAN_ENVELOPE)
        )  # would render met

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(evidence={"3": {"command": ["klt", "drc"], "file": drc_path}})
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"  # graded via the command, not the file
    assert item_3["citation"]["file"] is None
    assert item_3["citation"]["command"] == "klt drc"


# --------------------------------------------------------------------------- #
# Real-subprocess gate binding: reproduces an already-bronze canary's claim
# against genuine evidence (issue #825 AC: "reproduces the hand-assembled
# tier claim of at least one already-bronze canary, measured against real
# evidence, not synthetic"). examples/design-pipeline/ is this repo's own
# worked example (a sky130 5T OTA) with a real, checked-in DRC-clean layout,
# a matching LVS reference, a real extraction, and a passing post-extraction
# corner sim -- exactly the T1 "DRC clean"/"LVS clean"/"post-layout
# verification" claims examples/design-pipeline/README.md already asserts
# by hand. These tests actually run `klt drc`/`klt lvs`/`klt extract`/
# `klt sim` as subprocesses (via `sys.executable -m klayout_tools.cli`, not
# relying on a `klt` console script being on PATH) against that real
# artifact set and confirm the tier report reproduces the same verdict from
# a genuine, freshly-observed run -- not a pre-existing envelope file.
# --------------------------------------------------------------------------- #

_DESIGN_PIPELINE_DIR = _REPO_ROOT / "examples" / "design-pipeline"

#: `examples/yield/` (issue #816) -- reused below by the fleet roll-up's
#: "measured, not asserted" regression (issue #872) to run a genuine `klt
#: yield` subprocess, the same convention as the drc/lvs/extract/sim real-gate
#: tests above.
_YIELD_EXAMPLES_DIR = _REPO_ROOT / "examples" / "yield"

#: `klt yield`'s statistics core needs the `klt_yield_native` Rust extension
#: built (`uv sync --group yield` or `maturin develop --release` in
#: `native/yield/`) -- skip gracefully, exactly like tests/test_yield.py's own
#: `requires_native`, rather than failing in an environment without a Rust
#: toolchain.
requires_native_yield = pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason=(
        "klt_yield_native is not built -- run `maturin develop --release` in "
        "native/yield/ (or `uv sync --group yield`); see "
        "docs/cli/yield.md#building-the-native-extension"
    ),
)


def _klt_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "klayout_tools.cli", *args]


@pytest.mark.skipif(
    not (_DESIGN_PIPELINE_DIR / "06-layout.gds").exists(),
    reason="examples/design-pipeline/06-layout.gds not present in this checkout",
)
def test_real_drc_gate_reproduces_the_canarys_drc_clean_claim():
    result = build_tier_report(
        _manifest(
            evidence={
                "3": {
                    "command": _klt_command(
                        "drc", "06-layout.gds", "--deck", "sky130", "--format", "json"
                    ),
                    "cwd": str(_DESIGN_PIPELINE_DIR),
                }
            }
        )
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["reason"] is None
    citation = item_3["citation"]
    assert citation["kind"] == "drc"
    assert citation["check_status"] == "clean"
    assert citation["exit_status"] == 0
    assert citation["command"] is not None
    assert citation["file"] is None
    # A real content hash was actually observed, not fabricated.
    assert citation["content_hash"] is not None
    assert citation["content_hash"].startswith("sha256:")


@pytest.mark.skipif(
    not (_DESIGN_PIPELINE_DIR / "08-lvs.request.json").exists(),
    reason="examples/design-pipeline/08-lvs.request.json not present in this checkout",
)
def test_real_lvs_gate_reproduces_the_canarys_lvs_match_claim():
    result = build_tier_report(
        _manifest(
            evidence={
                "4": {
                    "command": _klt_command(
                        "lvs", "08-lvs.request.json", "--format", "json"
                    ),
                    "cwd": str(_DESIGN_PIPELINE_DIR),
                }
            }
        )
    )

    item_4 = next(item for item in result["items"] if item["id"] == 4)
    assert item_4["status"] == "met"
    citation = item_4["citation"]
    assert citation["kind"] == "lvs"
    assert citation["check_status"] == "match"
    assert citation["exit_status"] == 0


@pytest.mark.skipif(
    not (_DESIGN_PIPELINE_DIR / "06-layout.gds").exists(),
    reason="examples/design-pipeline/06-layout.gds not present in this checkout",
)
def test_real_extract_gate_reproduces_the_canarys_netlist_regeneration(tmp_path):
    # "netlist regeneration" (issue #825's phrasing, module docstring): a
    # freshly re-run `klt extract`, not a checked-in .spice file read off
    # disk -- proves the extracted netlist is reproducible from the layout,
    # not merely present. `-o` redirects the written netlist to tmp_path so
    # this test never leaves a generated .spice file behind in the checked-
    # in examples/design-pipeline/ directory.
    #
    # Cited against item 9 ("Testbenches shipped"), not item 7: item 7
    # ("Post-layout verification") is kind-restricted to `pex`-kind evidence
    # only as of issue #871 -- a bare `klt extract` citation there now
    # renders "unmet" (reason "wrong_kind"). Item 9 is kind-independent and
    # unrestricted, so it still proves the same thing this test exists to
    # prove: the command-backed gate actually re-runs `klt extract` and
    # grades that run's own output, not a pre-existing file's say-so.
    output_netlist = tmp_path / "regenerated.spice"
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "9": {
                    "command": _klt_command(
                        "extract",
                        "06-layout.gds",
                        "--deck",
                        "sky130",
                        "--top",
                        "ota_5t_layout_0",
                        "-o",
                        str(output_netlist),
                        "--format",
                        "json",
                    ),
                    "cwd": str(_DESIGN_PIPELINE_DIR),
                }
            },
        )
    )

    assert output_netlist.exists()

    # Item 9 is kind-independent (unlike per-kind items 1/2/5/7), so a plain
    # "analog" manifest's bare "9" evidence key is what every manifest kind
    # looks up (_lookup_evidence) -- no per-partition column to select here.
    item_9 = next(item for item in result["items"] if item["id"] == 9)
    assert item_9["partition"] is None
    assert item_9["status"] == "met"
    citation = item_9["citation"]
    assert citation["kind"] == "extract"
    assert citation["check_status"] == "extracted"
    assert citation["exit_status"] == 0


@pytest.mark.skipif(
    not (_DESIGN_PIPELINE_DIR / "09-sim.request.json").exists(),
    reason="examples/design-pipeline/09-sim.request.json not present in this checkout",
)
def test_real_sim_gate_reproduces_the_canarys_corner_sim_pass():
    # "corner sim" (issue #825's phrasing): the post-extraction corner
    # sweep -- a real, multi-corner `klt sim` run against the layout's own
    # extracted netlist, not a canned pass/fail JSON fixture.
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "5": {
                    "command": _klt_command(
                        "sim", "09-sim.request.json", "--format", "json"
                    ),
                    "cwd": str(_DESIGN_PIPELINE_DIR),
                }
            },
        )
    )

    # See test_real_extract_gate_...'s comment: a plain "analog" manifest
    # looks up the bare "5" key, not "5.analog".
    item_5 = next(item for item in result["items"] if item["id"] == 5)
    assert item_5["partition"] is None
    assert item_5["status"] == "met"
    citation = item_5["citation"]
    assert citation["kind"] == "sim"
    assert citation["check_status"] == "pass"
    assert citation["exit_status"] == 0


@pytest.mark.skipif(
    not (_DESIGN_PIPELINE_DIR / "06-layout.gds").exists(),
    reason="examples/design-pipeline/06-layout.gds not present in this checkout",
)
def test_real_pex_gate_reproduces_the_canarys_post_layout_delta_pass(tmp_path):
    # Epic #709 Phase 1c (#803): item 7 ("Post-layout verification") is
    # kind-restricted to `pex`-kind evidence only (issue #871) -- this is
    # that item's own real-subprocess gate-binding test, the same
    # `klt <verb> ... --format json` real-subprocess convention as items
    # 3/4/5/9 above, run against `10-pex.request.json` (Epic #709 Phase 1c,
    # `examples/design-pipeline/README.md`'s "S10 pex delta proof" section).
    # `-o` redirects the extracted netlist `klt pex` writes to tmp_path so
    # this test never leaves a generated .spice file behind in the checked-
    # in examples/design-pipeline/ directory.
    output_netlist = tmp_path / "regenerated-pex-extracted.spice"
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "7": {
                    "command": _klt_command(
                        "pex",
                        "06-layout.gds",
                        "10-pex.request.json",
                        "--deck",
                        "sky130",
                        "-o",
                        str(output_netlist),
                        "--format",
                        "json",
                    ),
                    "cwd": str(_DESIGN_PIPELINE_DIR),
                }
            },
        )
    )

    assert output_netlist.exists()

    # See test_real_extract_gate_...'s comment: a plain "analog" manifest
    # looks up the bare "7" key, not "7.analog".
    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["partition"] is None
    assert item_7["status"] == "met"
    citation = item_7["citation"]
    assert citation["kind"] == "pex"
    assert citation["check_status"] == "pass"
    assert citation["exit_status"] == 0


def test_non_object_manifest_raises():
    with pytest.raises(SignoffError, match="must be a JSON object"):
        build_tier_report(["not", "a", "dict"])  # type: ignore[arg-type]


def test_missing_kind_raises():
    with pytest.raises(SignoffError, match="kind"):
        build_tier_report({"block": "demo"})


def test_invalid_kind_raises():
    with pytest.raises(SignoffError, match="kind"):
        build_tier_report(_manifest(kind="bogus"))


def test_non_object_evidence_raises():
    with pytest.raises(SignoffError, match="evidence"):
        build_tier_report(_manifest(evidence=["not", "a", "dict"]))


def test_design_evidence_tiers_error_is_importable_alongside_signoff_error():
    # A quick sanity check that both exception types are importable from
    # `klayout_tools.signoff` (SignoffError natively, DesignEvidenceTiersError
    # re-exported) for callers that want one `except (...)` clause -- the
    # actual doc-parse-failure paths are covered in
    # test_design_evidence_tiers.py.
    assert issubclass(DesignEvidenceTiersError, Exception)


# --------------------------------------------------------------------------- #
# CLI (`klt signoff --manifest`)
# --------------------------------------------------------------------------- #


def test_cli_manifest_json_output(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest_path = _write(
        tmp_path, "manifest.json", _manifest(evidence={"3": drc_path})
    )

    exit_code = main(["signoff", "--manifest", manifest_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert out["t1_met_count"] == 1
    assert exit_code == 3  # not every T1 item is met yet


def test_cli_manifest_all_met_exits_zero(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)
    evidence = {str(i): drc_path for i in range(1, 11)}
    evidence["4"] = lvs_path
    # Item 7 is kind-restricted to `pex` evidence (issue #871).
    evidence["7"] = pex_path
    manifest_path = _write(tmp_path, "manifest.json", _manifest(evidence=evidence))

    exit_code = main(["signoff", "--manifest", manifest_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert out["tier"] == "T1"
    assert exit_code == 0


def test_cli_manifest_text_format_colors_unmet_items_red(tmp_path, capsys):
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    exit_code = main(["signoff", "--manifest", manifest_path])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "\033[31m" in out  # unmet items render red
    assert "UNMET" in out


def test_cli_manifest_text_format_colors_met_items_green(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest_path = _write(
        tmp_path, "manifest.json", _manifest(evidence={"3": drc_path})
    )

    main(["signoff", "--manifest", manifest_path])

    out = capsys.readouterr().out
    assert "\033[32m" in out  # met items render green
    assert "MET" in out


def test_cli_manifest_json_output_distinguishes_skipped_from_failed_check(
    tmp_path, capsys
):
    """AC: JSON output makes the missing-evidence case unambiguous (distinct
    from a check that ran and failed)."""
    drc_violations_path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)
    manifest_path = _write(
        tmp_path,
        "manifest.json",
        _manifest(evidence={"3": drc_violations_path}),  # item 4 deliberately skipped
    )

    exit_code = main(["signoff", "--manifest", manifest_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    items_by_id = {item["id"]: item for item in out["items"]}
    assert items_by_id[3]["status"] == "unmet"
    assert items_by_id[3]["reason"] == "check_failed"
    assert items_by_id[4]["status"] == "unmet"
    assert items_by_id[4]["reason"] == "no_evidence"
    assert items_by_id[3]["reason"] != items_by_id[4]["reason"]


def test_cli_manifest_text_format_shows_reason_for_unmet_items_in_red(tmp_path, capsys):
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    main(["signoff", "--manifest", manifest_path])

    out = capsys.readouterr().out
    assert "reason: no_evidence" in out
    # The reason line itself is rendered red, not just the UNMET marker --
    # a skipped check must read as loudly as a failed one, not blend in.
    assert "\033[31mreason: no_evidence\033[0m" in out


def test_cli_manifest_and_files_together_is_an_error(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    exit_code = main(
        ["signoff", "--manifest", manifest_path, drc_path, "--format", "json"]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "manifest" in err["error"]["message"]


def test_cli_manifest_invalid_kind_exits_one(tmp_path, capsys):
    manifest_path = _write(tmp_path, "manifest.json", _manifest(kind="bogus"))

    exit_code = main(["signoff", "--manifest", manifest_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "signoff"


def test_cli_manifest_missing_file_exits_one(tmp_path, capsys):
    exit_code = main(
        ["signoff", "--manifest", str(tmp_path / "nope.json"), "--format", "json"]
    )

    assert exit_code == 1


# --------------------------------------------------------------------------- #
# build_fleet_report(): fleet-wide tier roll-up (issue #827, Phase 1c of
# epic #706)
# --------------------------------------------------------------------------- #


def _fleet_block_manifest(block: str, kind: str = "analog", evidence=None) -> dict:
    return {"block": block, "kind": kind, "evidence": evidence or {}}


def _fleet_write(tmp_path, blocks: list) -> str:
    return _write(tmp_path, "fleet.json", {"blocks": blocks})


def test_fleet_report_covers_a_mixed_fleet_with_different_blockers(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    # canary-a: every T1 item met -> T1, no blocking item. Item 7 is
    # kind-restricted to `pex` evidence (issue #871).
    full_evidence = {str(i): drc_path for i in range(1, 11)}
    full_evidence["4"] = lvs_path
    full_evidence["7"] = pex_path
    block_a = _fleet_block_manifest("canary-a", evidence=full_evidence)

    # canary-b: everything but item 4 (LVS clean) met -> blocked on #4.
    partial_evidence = {str(i): drc_path for i in range(1, 11) if i != 4}
    block_b = _fleet_block_manifest("canary-b", evidence=partial_evidence)

    # canary-c: nothing met -> blocked on item 1 (the first T1 item).
    block_c = _fleet_block_manifest("canary-c", evidence={})

    result = build_fleet_report({"blocks": [block_a, block_b, block_c]})

    assert result["schema_version"] == 1
    assert result["block_count"] == 3
    assert result["t1_count"] == 1
    assert result["not_t1_count"] == 2
    assert result["source_doc"] == "docs/design-evidence-tiers.md"

    by_name = {block["block"]: block for block in result["blocks"]}

    assert by_name["canary-a"]["tier"] == "T1"
    assert by_name["canary-a"]["t1_met_count"] == 10
    assert by_name["canary-a"]["blocking_item"] is None

    assert by_name["canary-b"]["tier"] is None
    assert by_name["canary-b"]["blocking_item"] == {
        "id": 4,
        "title": "LVS clean",
        "partition": None,
        "reason": "no_evidence",
    }

    assert by_name["canary-c"]["tier"] is None
    assert by_name["canary-c"]["blocking_item"]["id"] == 1
    assert by_name["canary-c"]["blocking_item"]["title"] == "Design sources"
    assert by_name["canary-c"]["blocking_item"]["reason"] == "no_evidence"


def test_fleet_report_never_reparses_evidence_itself(tmp_path):
    # The roll-up's blocking_item must be exactly the first unmet T1 item
    # build_tier_report() already computed -- no independent re-grading.
    drc_path = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)
    manifest = _fleet_block_manifest("canary-a", evidence={"3": drc_path})

    tier_result = build_tier_report(manifest)
    fleet_result = build_fleet_report({"blocks": [manifest]})

    first_unmet = next(
        item
        for item in tier_result["items"]
        if item["tier"] == "T1" and item["status"] != "met"
    )
    blocking_item = fleet_result["blocks"][0]["blocking_item"]
    assert blocking_item["id"] == first_unmet["id"]
    assert blocking_item["reason"] == first_unmet["reason"]


# --------------------------------------------------------------------------- #
# Fleet roll-up picks up the statistical (#870) and post-layout (#871) items
# -- issue #872, Phase 2c of epic #706.
#
# build_fleet_report() needed no code change to pick these two items up: it
# already reduces whatever items[] build_tier_report() renders (see its own
# "Fleet roll-up" docstring section), and build_tier_report() has rendered
# all 10 T1 items -- including item 6 (statistical) and item 7 (post-layout)
# -- since Phase 0 (#722). What changed under #870/#871 is which evidence
# shapes those two items can now be *satisfied by* (a `klt yield` report for
# item 6, a `klt pex` report -- and only that kind -- for item 7); before
# those phases landed, a manifest citing genuine yield/pex evidence for them
# rendered "unrecognized_envelope", so a canary with real statistical/
# post-layout evidence already assembled was still reported blocked "on
# statistical/post-layout evidence" by the roll-up, same as one with no
# evidence at all. These tests are the regression proving the roll-up's
# per-canary blocking-item determination -- not just build_tier_report()'s
# own item grading -- now resolves both items to their real verdict.
# --------------------------------------------------------------------------- #


def test_fleet_blocking_item_walks_through_statistical_then_post_layout_items(
    tmp_path,
):
    """A block manifest missing only items 6 and 7 is blocked on item 6
    first (doc order); providing item 6's `klt yield` evidence advances the
    blocker to item 7; citing a wrong-kind (non-`pex`) envelope for item 7
    renders it `"wrong_kind"`, never a borrowed pass; and providing real
    `klt pex` evidence for item 7 finally reaches `tier: "T1"`. Every step
    is read back through the fleet roll-up, not `build_tier_report()`
    directly -- this is `build_fleet_report()`'s own blocking-item
    determination being exercised end to end."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    yield_path = _write(tmp_path, "yield.json", YIELD_PASS_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    base_evidence = {str(i): drc_path for i in range(1, 11) if i not in (6, 7)}
    base_evidence["4"] = lvs_path

    def _row(evidence: dict[str, str]) -> dict[str, object]:
        block = _fleet_block_manifest("stat-postlayout-canary", evidence=evidence)
        result = build_fleet_report({"blocks": [block]})
        return result["blocks"][0]

    # Step 1: items 6 and 7 both missing -> blocked on item 6 (doc order).
    row = _row(base_evidence)
    assert row["tier"] is None
    assert row["blocking_item"] == {
        "id": 6,
        "title": "Statistical claims carry Monte Carlo evidence",
        "partition": None,
        "reason": "no_evidence",
    }

    # Step 2: bind item 6's `klt yield` evidence -> blocker advances to
    # item 7, still missing.
    with_item_6 = dict(base_evidence, **{"6": yield_path})
    row = _row(with_item_6)
    assert row["tier"] is None
    assert row["blocking_item"] == {
        "id": 7,
        "title": "Post-layout verification",
        "partition": None,
        "reason": "no_evidence",
    }

    # Step 3: cite a non-`pex` envelope (a clean DRC report) for item 7 --
    # kind-restricted since #871, so this must never borrow a pass.
    wrong_kind = dict(with_item_6, **{"7": drc_path})
    row = _row(wrong_kind)
    assert row["tier"] is None
    assert row["blocking_item"] == {
        "id": 7,
        "title": "Post-layout verification",
        "partition": None,
        "reason": "wrong_kind",
    }

    # Step 4: bind real `klt pex` evidence for item 7 -> the block reaches
    # T1, and the roll-up's t1_met_count/tier both reflect it.
    fully_met = dict(with_item_6, **{"7": pex_path})
    row = _row(fully_met)
    assert row["tier"] == "T1"
    assert row["t1_met_count"] == 10
    assert row["blocking_item"] is None


@requires_native_yield
def test_fleet_tier_verdict_changes_once_real_yield_evidence_is_bound(tmp_path):
    """AC (issue #872): "re-run against at least one canary whose tier
    verdict changes ... once the new items are bound, measured not
    asserted." Measured here by genuinely running `klt yield` as a
    subprocess against this repo's own worked `examples/yield/` Monte Carlo
    campaign -- the same "real gate" convention the drc/lvs/extract/sim
    tests above use -- not by asserting a canned fixture.

    BEFORE: item 6 has no evidence -> not T1, blocked on item 6.
    AFTER: the same block, now citing a real command-backed `klt yield` run
    over that campaign -> item 6 "met" on a freshly-observed exit status
    and envelope, block reaches T1.
    """
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    base_evidence = {str(i): drc_path for i in range(1, 11) if i != 6}
    base_evidence["4"] = lvs_path
    base_evidence["7"] = pex_path

    before_block = _fleet_block_manifest("gf180-sar-adc-canary", evidence=base_evidence)
    before = build_fleet_report({"blocks": [before_block]})
    before_row = before["blocks"][0]
    assert before_row["tier"] is None
    assert before_row["blocking_item"] == {
        "id": 6,
        "title": "Statistical claims carry Monte Carlo evidence",
        "partition": None,
        "reason": "no_evidence",
    }

    # Deliberately generous limits -- this test proves the roll-up wires a
    # genuine `klt yield` run through to the tier verdict, it is not meant
    # to exercise `klt yield`'s own statistics engine (covered separately by
    # tests/test_yield.py).
    limits_path = _write(
        tmp_path,
        "wide-limits.json",
        {
            "confidence": 0.95,
            "target_ci_halfwidth": 0.05,
            "measurements": {
                "vref": {"min": 0.0, "max": 10.0, "target_yield": 0.5},
                "iq_ua": {"max": 100.0, "target_yield": 0.5},
            },
        },
    )
    after_evidence = dict(
        base_evidence,
        **{
            "6": {
                "command": _klt_command(
                    "yield",
                    "mc-samples.json",
                    "--limits",
                    limits_path,
                    "--format",
                    "json",
                ),
                "cwd": str(_YIELD_EXAMPLES_DIR),
            }
        },
    )
    after_block = _fleet_block_manifest("gf180-sar-adc-canary", evidence=after_evidence)
    after = build_fleet_report({"blocks": [after_block]})
    after_row = after["blocks"][0]

    assert after_row["tier"] == "T1"
    assert after_row["blocking_item"] is None

    # The tier changed because a real klt yield subprocess actually ran and
    # was graded, not by coincidence -- confirm item 6's own citation.
    tier_report = build_tier_report(
        _manifest(block="gf180-sar-adc-canary", evidence=after_evidence)
    )
    item_6 = next(item for item in tier_report["items"] if item["id"] == 6)
    assert item_6["status"] == "met"
    assert item_6["citation"]["kind"] == "yield"
    assert item_6["citation"]["check_status"] == "pass"
    assert item_6["citation"]["exit_status"] == 0
    assert item_6["citation"]["command"] is not None
    assert item_6["citation"]["content_hash"] is not None


def test_fleet_manifest_blocks_accept_inline_or_file_path(tmp_path):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    inline_block = _fleet_block_manifest("inline-block", evidence={"3": drc_path})
    file_block_path = _write(
        tmp_path, "block-manifest.json", _fleet_block_manifest("file-block")
    )

    result = build_fleet_report({"blocks": [inline_block, file_block_path]})

    by_name = {block["block"]: block for block in result["blocks"]}
    assert by_name["inline-block"]["source"] is None
    assert by_name["file-block"]["source"] == file_block_path


def test_fleet_manifest_not_object_raises():
    with pytest.raises(SignoffError, match="must be a JSON object"):
        build_fleet_report(["not", "a", "dict"])  # type: ignore[arg-type]


def test_fleet_manifest_missing_blocks_raises():
    with pytest.raises(SignoffError, match="non-empty JSON array"):
        build_fleet_report({})


def test_fleet_manifest_empty_blocks_raises():
    with pytest.raises(SignoffError, match="non-empty JSON array"):
        build_fleet_report({"blocks": []})


def test_fleet_manifest_non_list_blocks_raises():
    with pytest.raises(SignoffError, match="non-empty JSON array"):
        build_fleet_report({"blocks": "nope"})


def test_fleet_block_entry_wrong_type_raises():
    with pytest.raises(SignoffError, match=r"blocks\[0\]"):
        build_fleet_report({"blocks": [123]})


def test_fleet_block_manifest_missing_block_name_raises():
    with pytest.raises(SignoffError, match="no non-empty 'block' name"):
        build_fleet_report({"blocks": [{"kind": "analog", "evidence": {}}]})


def test_fleet_block_manifest_empty_block_name_raises():
    with pytest.raises(SignoffError, match="no non-empty 'block' name"):
        build_fleet_report({"blocks": [{"block": "", "kind": "analog"}]})


def test_fleet_block_manifest_invalid_kind_propagates():
    with pytest.raises(SignoffError, match="kind"):
        build_fleet_report({"blocks": [{"block": "bad", "kind": "bogus"}]})


def test_fleet_block_manifest_missing_file_raises(tmp_path):
    with pytest.raises(SignoffError, match="file not found"):
        build_fleet_report({"blocks": [str(tmp_path / "nope.json")]})


def test_fleet_block_manifest_non_object_raises(tmp_path):
    path = _write(tmp_path, "list.json", ["not", "a", "dict"])
    with pytest.raises(
        SignoffError, match=r"blocks\[0\] must resolve to a JSON object"
    ):
        build_fleet_report({"blocks": [path]})


# --------------------------------------------------------------------------- #
# CLI (`klt signoff --fleet`)
# --------------------------------------------------------------------------- #


def test_cli_fleet_json_output(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)
    full_evidence = {str(i): drc_path for i in range(1, 11)}
    full_evidence["4"] = lvs_path
    full_evidence["7"] = pex_path  # item 7 is kind-restricted (issue #871)
    fleet_path = _fleet_write(
        tmp_path,
        [
            _fleet_block_manifest("canary-a", evidence=full_evidence),
            _fleet_block_manifest("canary-b", evidence={}),
        ],
    )

    exit_code = main(["signoff", "--fleet", fleet_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert out["block_count"] == 2
    assert out["t1_count"] == 1
    assert exit_code == 3  # not every block is T1 yet


def test_cli_fleet_all_t1_exits_zero(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)
    full_evidence = {str(i): drc_path for i in range(1, 11)}
    full_evidence["4"] = lvs_path
    full_evidence["7"] = pex_path  # item 7 is kind-restricted (issue #871)
    fleet_path = _fleet_write(
        tmp_path, [_fleet_block_manifest("canary-a", evidence=full_evidence)]
    )

    exit_code = main(["signoff", "--fleet", fleet_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert out["not_t1_count"] == 0
    assert exit_code == 0


def test_cli_fleet_text_format_names_blocking_item(tmp_path, capsys):
    fleet_path = _fleet_write(tmp_path, [_fleet_block_manifest("canary-a")])

    exit_code = main(["signoff", "--fleet", fleet_path])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "canary-a" in out
    assert "blocking:" in out
    assert "no_evidence" in out


def test_cli_fleet_and_manifest_together_is_an_error(tmp_path, capsys):
    fleet_path = _fleet_write(tmp_path, [_fleet_block_manifest("canary-a")])
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    exit_code = main(
        [
            "signoff",
            "--fleet",
            fleet_path,
            "--manifest",
            manifest_path,
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "mutually exclusive" in err["error"]["message"]


def test_cli_fleet_and_files_together_is_an_error(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    fleet_path = _fleet_write(tmp_path, [_fleet_block_manifest("canary-a")])

    exit_code = main(["signoff", "--fleet", fleet_path, drc_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "fleet" in err["error"]["message"]


def test_cli_fleet_missing_file_exits_one(tmp_path, capsys):
    exit_code = main(
        ["signoff", "--fleet", str(tmp_path / "nope.json"), "--format", "json"]
    )

    assert exit_code == 1


def test_cli_fleet_invalid_block_entry_exits_one(tmp_path, capsys):
    fleet_path = _fleet_write(tmp_path, [{"kind": "analog"}])  # no 'block' name

    exit_code = main(["signoff", "--fleet", fleet_path, "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "block" in err["error"]["message"]
