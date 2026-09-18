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

from helpers.subprocess_fakes import fake_completed
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

#: Issue #2002: a `status: "clean"` DRC run whose deck did *not* cover
#: everything the stream draws -- two drawn layers with no rule at all, two
#: rules the deck carries but this run skipped, and the DRM chapters the
#: deck transcribes. This is the case docs/design-evidence-tiers.md item 3
#: requires a claim to disclose, and the case `klt signoff` previously
#: rendered identically to a fully-covering deck.
DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE = {
    **DRC_CLEAN_ENVELOPE,
    "coverage": {
        "deck_layers": ["65/20", "68/20"],
        "layers_checked": ["65/20"],
        "layers_in_stream_without_rules": ["70/20", "71/20"],
        "rules_skipped": ["met5.4", "met5.5"],
        "voltage_domain_warnings": [],
        "deck_scope": ["5.x", "6.x"],
    },
}

#: Issue #2002 back-compat: DRC evidence committed before `klt drc` reported
#: a `coverage` block at all. It must grade exactly as it always did, and
#: must not be rendered as a deck that reported zero gaps.
DRC_CLEAN_NO_COVERAGE_ENVELOPE = {
    key: value for key, value in DRC_CLEAN_ENVELOPE.items() if key != "coverage"
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
        # Issue #1969: `klt lvs` populates `provenance.input` too, pinning the
        # layout side of the compare. Same hash as DRC_CLEAN_ENVELOPE /
        # EXTRACT_ENVELOPE's input for the same reason their decks agree: a
        # real T1 package signs off DRC, extraction, and LVS against one
        # layout, so the cross-check (`input.content_hash`) is expected to
        # find them identical.
        "input": {"content_hash": "sha256:layoutA"},
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

#: Issue #1965: `power_connectivity` block shapes, mirroring
#: `_power_connectivity_report`/`_power_connectivity_unchecked`
#: (`src/klayout_tools/lvs.py`) for a `reference.form:
#: "gate-level-verilog"` compare -- exercised against `_check_passed`'s
#: `lvs` rule below, which must fail on a `"mismatch"` verdict even when
#: the envelope's own top-level `status` is `"match"`.
POWER_CONNECTIVITY_MATCH = {
    "status": "match",
    "reason": None,
    "power_pins": ["VGND", "VPWR"],
    "instance_count": 4,
    "expected_nets": None,
    "findings": [],
    "finding_count": 0,
}

POWER_CONNECTIVITY_MISMATCH = {
    "status": "mismatch",
    "reason": None,
    "power_pins": ["VGND", "VPWR"],
    "instance_count": 4,
    "expected_nets": None,
    "findings": [
        {
            "rule": "power.inconsistent_pin_net",
            "severity": "error",
            "pin": "VGND",
            "expected_net": None,
            "description": (
                "standard-cell power/ground pin 'VGND' reaches 2 distinct "
                "nets across 2 instance(s)"
            ),
            "instance_count": 2,
            "nets": [
                {
                    "net": "VGND",
                    "instance_count": 1,
                    "instances": [
                        {"circuit": "TOP", "instance": "1", "cell": "MYLIB__INV_1"}
                    ],
                    "instances_truncated": False,
                },
                {
                    "net": "VPWR",
                    "instance_count": 1,
                    "instances": [
                        {"circuit": "TOP", "instance": "2", "cell": "MYLIB__BUF_1"}
                    ],
                    "instances_truncated": False,
                },
            ],
        }
    ],
    "finding_count": 1,
}

POWER_CONNECTIVITY_UNCHECKED = {
    "status": "unchecked",
    "reason": "reference.form is 'plain-element', whose reference netlist "
    "carries its own power/ground pins and nets",
    "power_pins": [],
    "instance_count": 0,
    "expected_nets": None,
    "findings": [],
    "finding_count": 0,
}

#: Issue #1965 test plan: `options.power_connectivity: false` (explicitly
#: disabled) must produce the same `"unchecked"` treatment as a reference
#: form the check does not apply to at all -- not a mismatch.
POWER_CONNECTIVITY_UNCHECKED_DISABLED = {
    "status": "unchecked",
    "reason": "power_connectivity check disabled by options.power_connectivity",
    "power_pins": [],
    "instance_count": 0,
    "expected_nets": None,
    "findings": [],
    "finding_count": 0,
}

#: A `reference.form: "gate-level-verilog"` LVS envelope whose ordinary
#: signal compare is clean (`status: "match"`) but whose power/ground
#: connectivity check found a mismatch -- the exact shape #1952/#1964 added
#: and #1965's `_check_passed` gate must catch.
LVS_MATCH_POWER_MISMATCH_ENVELOPE = {
    **LVS_MATCH_ENVELOPE,
    "power_connectivity": POWER_CONNECTIVITY_MISMATCH,
}

LVS_MATCH_POWER_MATCH_ENVELOPE = {
    **LVS_MATCH_ENVELOPE,
    "power_connectivity": POWER_CONNECTIVITY_MATCH,
}

LVS_MATCH_POWER_UNCHECKED_ENVELOPE = {
    **LVS_MATCH_ENVELOPE,
    "power_connectivity": POWER_CONNECTIVITY_UNCHECKED,
}

LVS_MATCH_POWER_UNCHECKED_DISABLED_ENVELOPE = {
    **LVS_MATCH_ENVELOPE,
    "power_connectivity": POWER_CONNECTIVITY_UNCHECKED_DISABLED,
}

#: Pre-#1964 committed evidence: no `power_connectivity` key at all --
#: `LVS_MATCH_ENVELOPE` itself already has this shape (defined above,
#: before #1964 landed the key). Named separately here purely so the
#: intent ("this is deliberately testing the missing-key case") is visible
#: at each call site below.
LVS_MATCH_NO_POWER_CONNECTIVITY_KEY_ENVELOPE = LVS_MATCH_ENVELOPE

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

#: `klt power` (issues #844/#845/#846, Epic #712 Phase 1) JSON report shape
#: -- hand-built here exactly like every other kind's fixture (see
#: docs/cli/power.md's JSON schema). Unlike every kind above, it carries no
#: top-level `status` field and no `provenance` block at all; `klt signoff`
#: (issue #1321, Phase 2 of epic #712) derives pass/fail from `em_verdict`
#: instead -- see signoff.py's "`klt power` (IR-drop/EM) evidence ingestion"
#: docstring section.
POWER_PASS_ENVELOPE = {
    "schema_version": 1,
    "file": "routed.gds",
    "spec": "power.json",
    "power_nets": ["VPWR", "VGND"],
    "networks": [
        {
            "net": "VPWR",
            "island_count": 1,
            "node_count": 2,
            "edge_count": 1,
            "islands": [],
        }
    ],
    "node_count": 2,
    "edge_count": 1,
    "island_count": 1,
    "ir_drop_map": {"worst_case": {"droop_mv": 12.5}, "nets": []},
    "worst_case_droop_mv": 12.5,
    "em_verdict": {
        "status": "pass",
        "checked_edge_count": 1,
        "unchecked_edge_count": 0,
        "fail_count": 0,
        "worst_case": None,
        "nets": [],
    },
    "warnings": [],
}

#: A checked edge exceeded its declared current-density limit -- the
#: rolled-up `em_verdict.status` is `"fail"`.
POWER_EM_FAIL_ENVELOPE = {
    **POWER_PASS_ENVELOPE,
    "em_verdict": {
        **POWER_PASS_ENVELOPE["em_verdict"],
        "status": "fail",
        "fail_count": 1,
    },
}

#: At least one edge was checked and none failed, but some other edge in
#: the design was never checked at all (issue #1997) -- rolled-up
#: `"pass_partial"`, distinct from a genuinely complete `"pass"`.
POWER_EM_PASS_PARTIAL_ENVELOPE = {
    **POWER_PASS_ENVELOPE,
    "em_verdict": {
        **POWER_PASS_ENVELOPE["em_verdict"],
        "status": "pass_partial",
        "unchecked_edge_count": 3,
    },
}

#: No edge in the whole spec had both a declared current limit and a solved
#: current, so nothing was actually EM-verified -- rolled-up `"not_checked"`.
POWER_EM_NOT_CHECKED_ENVELOPE = {
    **POWER_PASS_ENVELOPE,
    "em_verdict": {
        **POWER_PASS_ENVELOPE["em_verdict"],
        "status": "not_checked",
    },
}

#: A spec declaring neither `pads` nor `current_model` runs extraction only
#: -- no IR-drop solve at all, so both `ir_drop_map`/`worst_case_droop_mv`
#: and `em_verdict` are `null` (docs/cli/power.md).
POWER_NO_SOLVE_ENVELOPE = {
    **POWER_PASS_ENVELOPE,
    "ir_drop_map": None,
    "worst_case_droop_mv": None,
    "em_verdict": None,
}

#: "generic" (issue #1152) -- an opt-in, non-`klt`-native evidence envelope:
#: the ingestion path for T1 item 8 ("Characterization report"), the one
#: item whose own checklist text names no specific `klt` verb. Classified
#: purely by its own literal ``"kind": "generic"`` self-declaration -- see
#: signoff.py's "Generic evidence ingestion" docstring section.
GENERIC_PASS_ENVELOPE = {
    "schema_version": 1,
    "kind": "generic",
    "status": "pass",
    "summary": "Q3 characterization sweep: all spec rows within limits",
    "source": "docs/characterization/2026-q3-report.md",
}

GENERIC_FAIL_ENVELOPE = {**GENERIC_PASS_ENVELOPE, "status": "fail"}

#: Same generic shape, but with an optional `provenance` block populated --
#: proves the staleness/consistency machinery picks it up for free, with no
#: generic-specific code path (unlike `klt yield`'s samples-hash fallback).
GENERIC_PASS_ENVELOPE_WITH_PROVENANCE = {
    **GENERIC_PASS_ENVELOPE,
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": None,
        "deck": None,
        "input": {"content_hash": "sha256:characterization-input"},
    },
}

#: `klt sta` (docs/cli/sta.md) multi-corner response shape -- the digital
#: item-5 artifact (issue #1959). `status` is always `"ok"` (this verb has
#: no pass/fail concept of its own), so `klt signoff` derives a verdict from
#: the per-corner timing instead: every corner `timing_status:
#: "constrained"` with non-negative setup and hold slack.
STA_MULTI_CORNER_CLEAN_ENVELOPE = {
    "schema_version": 1,
    "engine": "openroad",
    "engine_version": "26Q3-771-g7cfb2105c9",
    "hdl_toplevel": "gcd",
    "status": "ok",
    "def_path": "/abs/path/gcd.def",
    "verilog_path": None,
    "geometry_source": "routed",
    "wire_load_model": None,
    "wire_load_mode": None,
    "spef_path": "/abs/path/gcd_route.spef",
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": None,
        "input": {"content_hash": "sha256:routeddef"},
    },
    "corners": [
        {
            "corner": "ss_100C_1v60",
            "worst_slack_ns": 0.11842,
            "total_negative_slack_ns": 0.0,
            "worst_hold_slack_ns": 0.02011,
            "total_negative_hold_slack_ns": 0.0,
            "timing_status": "constrained",
            "fmax_mhz": 401.552,
            "setup_violation_count": 0,
            "hold_violation_count": 0,
            "clock_skew_ns": 0.0512,
            "estimated_power_mw": 13.2,
            "spef_annotation": None,
            "deck": {
                "name": "sky130_fd_sc_hd__ss_100C_1v60",
                "content_hash": "sha256:libss",
                "released": True,
            },
        },
        {
            "corner": "tt_025C_1v80",
            "worst_slack_ns": 0.41233,
            "total_negative_slack_ns": 0.0,
            "worst_hold_slack_ns": 0.03812,
            "total_negative_hold_slack_ns": 0.0,
            "timing_status": "constrained",
            "fmax_mhz": 512.345,
            "setup_violation_count": 0,
            "hold_violation_count": 0,
            "clock_skew_ns": 0.0421,
            "estimated_power_mw": 11.6,
            "spef_annotation": None,
            "deck": {
                "name": "sky130_fd_sc_hd__tt_025C_1v80",
                "content_hash": "sha256:libtt",
                "released": True,
            },
        },
    ],
}

#: One corner of the same characterization misses setup timing -- the
#: multi-corner run as a whole did not close timing.
STA_MULTI_CORNER_SETUP_VIOLATION_ENVELOPE = {
    **STA_MULTI_CORNER_CLEAN_ENVELOPE,
    "corners": [
        {
            **STA_MULTI_CORNER_CLEAN_ENVELOPE["corners"][0],
            "worst_slack_ns": -0.31842,
            "total_negative_slack_ns": -2.41112,
            "setup_violation_count": 5,
        },
        STA_MULTI_CORNER_CLEAN_ENVELOPE["corners"][1],
    ],
}

#: Single-corner (`pdk.corner`) `klt sta` response -- the flat shape, with
#: the same per-corner fields hoisted to the top level.
STA_SINGLE_CORNER_CLEAN_ENVELOPE = {
    "schema_version": 1,
    "engine": "openroad",
    "engine_version": "26Q3-771-g7cfb2105c9",
    "hdl_toplevel": "gcd",
    "status": "ok",
    "def_path": "/abs/path/gcd.def",
    "verilog_path": None,
    "geometry_source": "routed",
    "wire_load_model": None,
    "wire_load_mode": None,
    "spef_path": None,
    "worst_slack_ns": 0.41233,
    "total_negative_slack_ns": 0.0,
    "worst_hold_slack_ns": 0.03812,
    "total_negative_hold_slack_ns": 0.0,
    "timing_status": "constrained",
    "fmax_mhz": 512.345,
    "setup_violation_count": 0,
    "hold_violation_count": 0,
    "clock_skew_ns": 0.0421,
    "estimated_power_mw": 11.6,
    "spef_annotation": None,
    "provenance": {
        "klt_version": "0.2.0",
        "klayout_version": "0.30.10",
        "pdk": {"name": "sky130A", "source": "volare", "version": "20240101"},
        "deck": {
            "name": "sky130_fd_sc_hd__tt_025C_1v80",
            "content_hash": "sha256:libtt",
        },
        "input": {"content_hash": "sha256:routeddef"},
    },
}

#: OpenSTA's unconstrained-design sentinel (`1e+39`) restated -- a *positive*
#: number, so a naive `worst_slack_ns >= 0` rule would report "timing closed"
#: on a design that was never timed (docs/cli/sta.md's `timing_status`).
STA_UNCONSTRAINED_ENVELOPE = {
    **STA_SINGLE_CORNER_CLEAN_ENVELOPE,
    "worst_slack_ns": 1e39,
    "worst_hold_slack_ns": 0.0,
    "timing_status": "unconstrained",
}

#: `klt functional-verification` (docs/cli/functional-verification.md)
#: response shape -- an ordinary (zero-delay, pre-layout) regression:
#: `environment.sdf` is `null`.
FUNCTIONAL_VERIFICATION_PASS_ENVELOPE = {
    "schema_version": 1,
    "engine": "icarus",
    "hdl_toplevel": "gcd",
    "testbench": "test_gcd",
    "status": "pass",
    "test_count": 3,
    "passed_count": 3,
    "failed_count": 0,
    "skipped_count": 0,
    "tests": [
        {
            "name": "test_gcd_known_pairs",
            "status": "passed",
            "sim_time_ns": 520.0,
            "real_time_s": 0.0051,
        }
    ],
    "coverage": None,
    "trace": None,
    "environment": {
        "engine": "icarus",
        "engine_version": "13.0",
        "cocotb_version": "2.0.1",
        "results_xml": "/abs/path/results_icarus.xml",
        "random_seed": 1785780800,
        "sdf": None,
    },
}

FUNCTIONAL_VERIFICATION_FAIL_ENVELOPE = {
    **FUNCTIONAL_VERIFICATION_PASS_ENVELOPE,
    "status": "fail",
    "passed_count": 2,
    "failed_count": 1,
}

#: The digital item-7 artifact (issue #1959): the same regression re-run
#: against the post-route gate-level netlist with back-annotated SDF timing
#: -- `environment.sdf` is an object, `annotated: true`.
FUNCTIONAL_VERIFICATION_SDF_ENVELOPE = {
    **FUNCTIONAL_VERIFICATION_PASS_ENVELOPE,
    "environment": {
        **FUNCTIONAL_VERIFICATION_PASS_ENVELOPE["environment"],
        "sdf": {
            "file": "gcd_route.sdf",
            "corner": "typ",
            "annotated": True,
            "partial": True,
            "dropped": {
                "timingcheck": {
                    "count": 708,
                    "reason": (
                        "Icarus implements SDF delay annotation but not TIMINGCHECK"
                    ),
                }
            },
        },
    },
}

#: A `klt place-and-route` response (docs/cli/place-and-route.md) -- trimmed
#: to the fields that overlap `klt sta`'s own shape. Not a recognised kind,
#: and adding `sta`/`functional-verification` recognition must not
#: accidentally start classifying it as one (issue #1959).
PLACE_AND_ROUTE_ENVELOPE = {
    "schema_version": 1,
    "engine": "openroad",
    "hdl_toplevel": "gcd",
    "status": "ok",
    "stage_reached": "route",
    "worst_slack_ns": -2.18828,
    "total_negative_slack_ns": -82.8171,
    "timing_status": "constrained",
    "worst_setup_slack_ns": -4.02163,
    "worst_hold_slack_ns": 0.08421,
    "corners": [
        {
            "name": "tt_025C_1v80",
            "setup_slack_ns": -2.18828,
            "hold_slack_ns": 0.42011,
            "timing_status": "constrained",
        }
    ],
    "stages": [{"name": "route"}],
    "def_path": "/abs/path/gcd.def",
    "gds_path": "/abs/path/gcd.gds",
    "verilog_path": "/abs/path/gcd.v",
}

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


def test_lvs_match_with_power_connectivity_mismatch_fails(tmp_path):
    """Issue #1965: a `status: "match"` LVS envelope whose
    `power_connectivity.status` is `"mismatch"` must no longer count as
    passing -- closing the gap #1952/#1964 left open in `klt signoff`."""
    path = _write(tmp_path, "lvs.json", LVS_MATCH_POWER_MISMATCH_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["kind"] == "lvs"
    assert check["passed"] is False
    # The underlying compare (`status`) is still reported as `"match"` --
    # only the aggregated `passed` verdict changes, exactly as
    # `docs/cli/signoff.md`'s "`klt lvs` power/ground connectivity" section
    # documents.
    assert check["status"] == "match"
    assert check["detail"]["power_connectivity_status"] == "mismatch"


def test_cli_text_format_names_power_connectivity_on_lvs_fail(tmp_path, capsys):
    """Issue #1978: `--format text` (the default) must not print a bare
    `[FAIL] lvs ... status=match` for a check that only failed because of
    `power_connectivity.status == "mismatch"` -- the line's own `status=`
    field is the *signal*-connectivity verdict, which stays `"match"`
    (see `test_lvs_match_with_power_connectivity_mismatch_fails` above),
    so without naming the real reason the printed line reads as a
    contradiction."""
    path = _write(tmp_path, "lvs.json", LVS_MATCH_POWER_MISMATCH_ENVELOPE)

    exit_code = main(["signoff", path])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "status: fail" in out
    assert "[FAIL] lvs" in out
    assert "status=match" in out
    assert "(power_connectivity: mismatch)" in out
    # The suffix belongs on the FAIL line itself, not floating free.
    fail_line = next(line for line in out.splitlines() if line.startswith("[FAIL] lvs"))
    assert fail_line.endswith("(power_connectivity: mismatch)")


def test_cli_text_format_omits_power_connectivity_suffix_on_ordinary_mismatch(
    tmp_path, capsys
):
    """Issue #1978: an ordinary signal mismatch (no power-connectivity
    involvement at all) must not gain the new suffix -- it is additive
    only for the specific `power_connectivity: "mismatch"` cause."""
    path = _write(tmp_path, "lvs.json", LVS_MISMATCH_ENVELOPE)

    exit_code = main(["signoff", path])

    assert exit_code == 3
    out = capsys.readouterr().out
    assert "[FAIL] lvs" in out
    assert "power_connectivity" not in out


def test_lvs_match_with_power_connectivity_match_passes(tmp_path):
    path = _write(tmp_path, "lvs.json", LVS_MATCH_POWER_MATCH_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert check["detail"]["power_connectivity_status"] == "match"


def test_lvs_match_with_power_connectivity_unchecked_passes(tmp_path):
    """`"unchecked"` means "does not apply to this reference form", not
    "not verified" -- it must still count as passing, matching today's
    behavior for every non-`gate-level-verilog` reference."""
    path = _write(tmp_path, "lvs.json", LVS_MATCH_POWER_UNCHECKED_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert check["detail"]["power_connectivity_status"] == "unchecked"


def test_lvs_match_with_power_connectivity_disabled_by_options_passes(tmp_path):
    """`options.power_connectivity: false` produces `status: "unchecked"`
    with a disabled-by-options reason -- confirm it is treated identically
    to the "doesn't apply to this form" unchecked case, not as a
    mismatch."""
    path = _write(tmp_path, "lvs.json", LVS_MATCH_POWER_UNCHECKED_DISABLED_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert check["detail"]["power_connectivity_status"] == "unchecked"


def test_lvs_match_with_no_power_connectivity_key_passes(tmp_path):
    """Pre-#1964 committed evidence has no `power_connectivity` key at
    all -- `_check_passed` must not raise on it, and must not
    retroactively fail signoff for evidence that predates the check."""
    path = _write(tmp_path, "lvs.json", LVS_MATCH_NO_POWER_CONNECTIVITY_KEY_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert check["detail"]["power_connectivity_status"] is None


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


# --------------------------------------------------------------------------- #
# DRC coverage surfacing (issue #2002): the three `coverage` fields
# docs/design-evidence-tiers.md item 3 requires a claim to disclose are
# reported by `klt signoff` -- in the aggregation mode's `checks[].detail`,
# in a tier report's item citation, and in the fleet roll-up -- while
# changing no verdict anywhere. Report before enforce: the four design
# questions in #2002 (hard-fail semantics, what "disclosed" is compared
# against, and whether `klt lvs`'s own disclosures get the same treatment
# for item 4) are deliberately left open.
# --------------------------------------------------------------------------- #


def test_drc_detail_surfaces_the_three_coverage_fields(tmp_path):
    path = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    result = build_signoff([path])

    check = result["checks"][0]
    assert check["detail"]["coverage"] == {
        "layers_in_stream_without_rules": ["70/20", "71/20"],
        "rules_skipped": ["met5.4", "met5.5"],
        "deck_scope": ["5.x", "6.x"],
    }


def test_drc_coverage_gaps_do_not_change_the_verdict(tmp_path):
    """The whole point of this increment: a deck with rule-free drawn layers
    and skipped rules still grades exactly as a fully-covering one does. A
    claim that was `met` before #2002 is still `met` after it -- surfacing
    the gaps is not enforcing their disclosure."""
    gappy = _write(tmp_path, "gappy.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    full = _write(tmp_path, "full.json", DRC_CLEAN_ENVELOPE)

    gappy_result = build_signoff([gappy])
    full_result = build_signoff([full])

    assert gappy_result["status"] == full_result["status"] == "pass"
    assert gappy_result["checks"][0]["passed"] is True
    assert full_result["checks"][0]["passed"] is True


def test_drc_detail_omits_coverage_for_a_pre_coverage_envelope(tmp_path):
    """Back-compat: evidence committed before `klt drc` reported coverage
    renders exactly as before -- no crash, and no `coverage` key claiming
    zero gaps for a run that never measured any."""
    path = _write(tmp_path, "drc.json", DRC_CLEAN_NO_COVERAGE_ENVELOPE)

    result = build_signoff([path])

    check = result["checks"][0]
    assert check["passed"] is True
    assert "coverage" not in check["detail"]
    assert check["detail"] == {
        "file": "design.gds",
        "deck": "sky130",
        "violation_count": 0,
    }


def test_non_drc_kinds_never_carry_a_coverage_disclosure(tmp_path):
    """`klt lvs`'s own coverage-shaped disclosures (item 4) are explicitly
    out of scope for #2002 -- nothing here fabricates a `coverage` key for a
    kind whose envelope does not report one."""
    lvs = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    extract = _write(tmp_path, "extract.json", EXTRACT_ENVELOPE)

    result = build_signoff([lvs, extract])

    assert all("coverage" not in check["detail"] for check in result["checks"])


def test_tier_report_item_3_citation_surfaces_coverage_gaps(tmp_path):
    """Item 3's citation quotes the gaps verbatim, and the item is still
    `met` -- the artifact a reviewer reads now carries both halves of the
    claim doc item 3 asks for."""
    path = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"3": path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["reason"] is None
    assert item_3["citation"]["coverage"] == {
        "layers_in_stream_without_rules": ["70/20", "71/20"],
        "rules_skipped": ["met5.4", "met5.5"],
        "deck_scope": ["5.x", "6.x"],
    }


def test_tier_report_item_3_citation_omits_coverage_for_legacy_evidence(tmp_path):
    """Back-compat on the tier-report path: a pre-`coverage` envelope still
    renders item 3 `met`, with a citation shaped exactly as before."""
    path = _write(tmp_path, "drc.json", DRC_CLEAN_NO_COVERAGE_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"3": path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["citation"] == {
        "file": path,
        "command": None,
        "kind": "drc",
        "check_status": "clean",
        "content_hash": "sha256:layoutA",
        "exit_status": 0,
    }


def test_command_backed_drc_evidence_surfaces_coverage(monkeypatch):
    """The coverage disclosure is read off the envelope the command printed,
    not off a file -- both evidence bindings behave identically."""

    def fake_run(command, **kwargs):
        return fake_completed(
            returncode=0,
            stdout=json.dumps(DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE),
        )

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(evidence={"3": {"command": ["klt", "drc", "design.gds"]}})
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "met"
    assert item_3["citation"]["coverage"]["rules_skipped"] == ["met5.4", "met5.5"]


def test_malformed_coverage_block_is_normalised_not_crashed(tmp_path):
    """A `coverage` block whose fields are the wrong type (a hand-edited or
    future-shaped envelope) renders empty lists rather than raising -- and
    still does not change the verdict."""
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "coverage": {
            "layers_in_stream_without_rules": "70/20",
            "rules_skipped": None,
        },
    }
    path = _write(tmp_path, "drc.json", envelope)

    result = build_signoff([path])

    check = result["checks"][0]
    assert check["passed"] is True
    assert check["detail"]["coverage"] == {
        "layers_in_stream_without_rules": [],
        "rules_skipped": [],
        "deck_scope": [],
    }


def test_fleet_rollup_reports_each_blocks_drc_coverage(tmp_path):
    """The `--fleet` path surfaces the same disclosure, reduced from each
    block's own tier report -- and a block whose deck has gaps still rolls up
    at exactly the tier it did before."""
    gappy = _write(tmp_path, "gappy.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    fleet = {
        "blocks": [
            _fleet_block_manifest("gappy-block", evidence={"3": gappy}),
        ]
    }
    result = build_fleet_report(fleet)

    block = result["blocks"][0]
    assert block["drc_coverage"] == [
        {
            "item": 3,
            "partition": None,
            "layers_in_stream_without_rules": ["70/20", "71/20"],
            "rules_skipped": ["met5.4", "met5.5"],
            "deck_scope": ["5.x", "6.x"],
        }
    ]
    # Unchanged verdict: item 3 is met, the block is simply not T1 yet for
    # the usual reason (items 1-2 have no evidence), never because of a gap.
    assert block["t1_met_count"] == 1
    assert block["blocking_item"]["id"] == 1


def test_fleet_rollup_mixes_pre_and_post_coverage_evidence(tmp_path):
    """A fleet spanning old (no `coverage`) and new (coverage-bearing)
    evidence renders both without crashing: the legacy block simply
    contributes no `drc_coverage` row, which reads as "reported nothing",
    not "reported no gaps"."""
    legacy = _write(tmp_path, "legacy.json", DRC_CLEAN_NO_COVERAGE_ENVELOPE)
    modern = _write(tmp_path, "modern.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    fleet = {
        "blocks": [
            _fleet_block_manifest("legacy-block", evidence={"3": legacy}),
            _fleet_block_manifest("modern-block", evidence={"3": modern}),
        ]
    }
    result = build_fleet_report(fleet)

    by_block = {block["block"]: block for block in result["blocks"]}
    assert by_block["legacy-block"]["drc_coverage"] == []
    assert by_block["modern-block"]["drc_coverage"][0]["rules_skipped"] == [
        "met5.4",
        "met5.5",
    ]
    assert by_block["legacy-block"]["t1_met_count"] == 1
    assert by_block["modern-block"]["t1_met_count"] == 1


def test_fleet_rollup_drc_coverage_is_empty_when_item_3_is_unmet(tmp_path):
    """No citation, no disclosure: a failing DRC check contributes no
    `drc_coverage` row even though its envelope carries a coverage block --
    the roll-up only reduces what the tier report actually cited."""
    failing = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    fleet = {"blocks": [_fleet_block_manifest("failing", evidence={"3": failing})]}
    result = build_fleet_report(fleet)

    assert result["blocks"][0]["drc_coverage"] == []


def test_cli_manifest_text_shows_coverage_beside_the_citation(tmp_path, capsys):
    """The terminal rendering a reviewer actually reads names the gaps --
    counts first, then the entries -- right under the `cite:` line whose
    "clean" they qualify."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    manifest_path = _write(
        tmp_path,
        "manifest.json",
        _manifest(evidence={"3": drc_path}),
    )

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    out = capsys.readouterr().out
    coverage_line = next(
        line for line in out.splitlines() if line.strip().startswith("coverage:")
    )
    assert "layers_in_stream_without_rules=2 (70/20, 71/20)" in coverage_line
    assert "rules_skipped=2 (met5.4, met5.5)" in coverage_line
    assert "deck_scope=2 (5.x, 6.x)" in coverage_line


def test_cli_manifest_text_omits_coverage_for_legacy_evidence(tmp_path, capsys):
    """A pre-`coverage` envelope prints no coverage line at all -- an absent
    statement must not read as "this deck reported no gaps"."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_NO_COVERAGE_ENVELOPE)
    manifest_path = _write(
        tmp_path,
        "manifest.json",
        _manifest(evidence={"3": drc_path}),
    )

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    out = capsys.readouterr().out
    assert "cite:" in out
    assert "coverage:" not in out


def test_cli_fleet_text_flags_blocks_whose_deck_left_gaps(tmp_path, capsys):
    """Fleet text names a gap-bearing block's coverage beside its tier, and
    stays quiet for a block that reported none."""
    gappy = _write(tmp_path, "gappy.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    full = _write(tmp_path, "full.json", DRC_CLEAN_ENVELOPE)
    fleet_path = _fleet_write(
        tmp_path,
        [
            _fleet_block_manifest("gappy-block", evidence={"3": gappy}),
            _fleet_block_manifest("full-block", evidence={"3": full}),
        ],
    )

    main(["signoff", "--fleet", fleet_path, "--format", "text"])

    out = capsys.readouterr().out
    coverage_lines = [
        line for line in out.splitlines() if line.strip().startswith("coverage:")
    ]
    assert len(coverage_lines) == 1
    assert "#3 layers_in_stream_without_rules=2" in coverage_lines[0]


# --------------------------------------------------------------------------- #
# Critical-metric consumption (issue #1850): a declared `critical: true`
# metric (klayout_tools.metrics.REGISTRY) mechanically blocks signoff
# independent of the envelope's own `status`, per its own declared
# `higher_is_better` polarity -- never hard-coded per-verb knowledge.
# --------------------------------------------------------------------------- #


def test_critical_metric_blocks_signoff_despite_passing_status(tmp_path):
    """A `drc` envelope whose own `status` is `"clean"` (would otherwise
    pass) is mechanically blocked when its `metrics` block carries a
    nonzero value for the registered `critical: true`, `higher_is_better:
    False` metric `drc__error__count` -- e.g. a caller that hand-edited
    `violation_count` without updating `status`, or any other producer
    whose `metrics` block disagrees with its own `status`."""
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "metrics": {"drc__error__count": 3},
    }
    path = _write(tmp_path, "drc.json", envelope)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["status"] == "clean"  # the envelope's own status still passes
    assert check["passed"] is False  # but the critical metric blocks it
    assert check["detail"]["critical_metric_blockers"] == [
        {"metric": "drc__error__count", "value": 3, "higher_is_better": False}
    ]


def test_critical_metric_at_passing_value_does_not_block(tmp_path):
    """The same critical metric at its passing value (`0`) never blocks --
    a critical metric present is not, by itself, a signoff blocker."""
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "metrics": {"drc__error__count": 0},
    }
    path = _write(tmp_path, "drc.json", envelope)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert "critical_metric_blockers" not in check["detail"]


def test_unregistered_metric_name_in_metrics_block_is_ignored(tmp_path):
    """A `metrics` block naming an unregistered/unknown metric is ignored,
    not raised -- registry lookups are read-only and defensive."""
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "metrics": {"not__a__registered__metric": 999},
    }
    path = _write(tmp_path, "drc.json", envelope)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert "critical_metric_blockers" not in check["detail"]


def test_envelope_with_no_metrics_block_behaves_as_before(tmp_path):
    """An envelope with no `metrics` block at all (e.g. `klt lvs`, which
    has not adopted the registry) is completely unaffected -- pre-existing
    `status`-only behavior."""
    path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    assert "metrics" not in LVS_MATCH_ENVELOPE

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["passed"] is True
    assert "critical_metric_blockers" not in check["detail"]


def test_non_critical_registered_metric_never_blocks(tmp_path):
    """A registered metric that is *not* `critical` (e.g.
    `design__instance__count`) never blocks signoff, no matter its value --
    only `critical: True` metrics are mechanical blockers."""
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "metrics": {"design__instance__count": 0},
    }
    path = _write(tmp_path, "drc.json", envelope)

    result = build_signoff([path])

    assert result["status"] == "pass"
    assert result["checks"][0]["passed"] is True


def test_multiple_critical_metrics_all_named_in_blockers(tmp_path):
    """A `sim` envelope with two failing critical metrics
    (`sim__corner__failed_count`, `sim__corner__errored_count`) names both
    in `critical_metric_blockers`, even though its own `status` already
    fails independently -- the mechanism is additive, not just a fallback
    for a status that would otherwise pass."""
    envelope = {
        **SIM_FAIL_ENVELOPE,
        "metrics": {
            "sim__corner__failed_count": 1,
            "sim__corner__errored_count": 2,
        },
    }
    path = _write(tmp_path, "sim.json", envelope)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["passed"] is False
    blockers = check["detail"]["critical_metric_blockers"]
    blocker_metrics = {b["metric"] for b in blockers}
    assert blocker_metrics == {
        "sim__corner__failed_count",
        "sim__corner__errored_count",
    }


def test_critical_metric_blocks_signoff_in_tier_report(tmp_path):
    """`build_tier_report` inherits the same critical-metric mechanism via
    `_grade_evidence`'s delegation to `_check_passed` -- a critical metric
    renders a T1 item `"unmet"` with `check_failed`, exactly like a failing
    `status` would, with no separate wiring needed."""
    envelope = {
        **DRC_CLEAN_ENVELOPE,
        "metrics": {"drc__error__count": 5},
    }
    drc_path = _write(tmp_path, "drc.json", envelope)

    result = build_tier_report(_manifest(evidence={"3": drc_path}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "check_failed"
    assert item_3["citation"] is None


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
# "power" evidence kind (issue #1321, Phase 2 of epic #712)
# --------------------------------------------------------------------------- #


def test_power_em_pass_check_passes(tmp_path):
    """`em_verdict.status == "pass"` -- a static IR-drop solve ran and every
    EM-checked edge stayed under its declared current-density limit."""
    path = _write(tmp_path, "power.json", POWER_PASS_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "power"
    assert check["status"] is None  # klt power has no top-level status field
    assert check["passed"] is True
    assert check["detail"]["worst_case_droop_mv"] == 12.5
    assert check["detail"]["em_verdict_status"] == "pass"
    assert check["detail"]["em_verdict_fail_count"] == 0
    assert check["detail"]["em_verdict_checked_edge_count"] == 1
    assert check["provenance"] is None


def test_power_em_fail_check_fails(tmp_path):
    """`em_verdict.status == "fail"` -- a checked edge exceeded its declared
    current-density limit."""
    path = _write(tmp_path, "power.json", POWER_EM_FAIL_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["kind"] == "power"
    assert check["passed"] is False
    assert check["detail"]["em_verdict_status"] == "fail"
    assert check["detail"]["em_verdict_fail_count"] == 1


def test_power_em_pass_partial_does_not_pass(tmp_path):
    """`em_verdict.status == "pass_partial"` (issue #1997) -- every checked
    edge stayed under its limit, but some other edge in the design was
    never checked at all; this must not be treated as more passing than a
    missing verdict, i.e. it must not silently pass."""
    path = _write(tmp_path, "power.json", POWER_EM_PASS_PARTIAL_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["kind"] == "power"
    assert check["passed"] is False
    assert check["detail"]["em_verdict_status"] == "pass_partial"
    assert check["detail"]["em_verdict_fail_count"] == 0


def test_power_em_not_checked_does_not_pass(tmp_path):
    """`em_verdict.status == "not_checked"` -- nothing in the spec had both a
    declared current limit and a solved current, so nothing was actually
    verified; this must not be treated as a pass."""
    path = _write(tmp_path, "power.json", POWER_EM_NOT_CHECKED_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False


def test_power_no_solve_does_not_pass(tmp_path):
    """A spec declaring neither `pads` nor `current_model` runs extraction
    only -- `em_verdict` is `null`, and a missing verdict must not be
    fabricated into a pass."""
    path = _write(tmp_path, "power.json", POWER_NO_SOLVE_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["kind"] == "power"
    assert check["passed"] is False
    assert check["detail"]["worst_case_droop_mv"] is None
    assert check["detail"]["em_verdict_status"] is None


def test_power_evidence_excluded_from_provenance_consistency(tmp_path):
    """A `klt power` envelope carries no `provenance` block at all -- it
    must never participate in (or be able to trigger) the cross-check, the
    same way an unprovenanced `klt yield`/`generic` envelope doesn't."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    power_path = _write(tmp_path, "power.json", POWER_PASS_ENVELOPE)

    result = build_signoff([drc_path, power_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["status"] == "pass"


# --------------------------------------------------------------------------- #
# "generic" evidence kind (issue #1152)
# --------------------------------------------------------------------------- #


def test_generic_pass_check_passes(tmp_path):
    path = _write(tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "generic"
    assert check["status"] == "pass"
    assert check["passed"] is True
    assert check["detail"]["summary"] == GENERIC_PASS_ENVELOPE["summary"]
    assert check["detail"]["source"] == GENERIC_PASS_ENVELOPE["source"]
    assert check["provenance"] is None


def test_generic_fail_check_fails(tmp_path):
    path = _write(tmp_path, "characterization.json", GENERIC_FAIL_ENVELOPE)

    result = build_signoff([path])

    assert result["status"] == "fail"
    check = result["checks"][0]
    assert check["kind"] == "generic"
    assert check["passed"] is False


def test_generic_envelope_with_provenance_participates_in_consistency_check(tmp_path):
    """An optional `provenance` block on a generic envelope is picked up by
    the same machinery every native kind uses -- no generic-specific code
    path (contrast with `klt yield`'s samples-hash fallback)."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    generic_path = _write(
        tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE_WITH_PROVENANCE
    )

    result = build_signoff([drc_path, generic_path])

    # DRC's input.content_hash is "sha256:layoutA"; the generic envelope
    # names a different one -- provenance consistency must catch it exactly
    # like a mismatch between two native kinds would.
    assert result["status"] == "refused"
    assert result["provenance_consistency"]["ok"] is False


def test_generic_envelope_with_matching_provenance_stays_consistent(tmp_path):
    matching_generic = {
        **GENERIC_PASS_ENVELOPE_WITH_PROVENANCE,
        "provenance": {
            **GENERIC_PASS_ENVELOPE_WITH_PROVENANCE["provenance"],
            "input": {"content_hash": "sha256:layoutA"},
        },
    }
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    generic_path = _write(tmp_path, "characterization.json", matching_generic)

    result = build_signoff([drc_path, generic_path])

    assert result["status"] == "pass"
    assert result["provenance_consistency"]["ok"] is True


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


def test_lvs_joins_the_input_hash_cross_check(tmp_path):
    """Issue #1969: now that `klt lvs` populates `provenance.input`, it
    participates in the `input.content_hash` cross-check like every other
    populating verb -- an LVS report signed off against a *different* layout
    than its DRC sibling is refused rather than passing unnoticed.

    This is a second, independent benefit of populating the field, distinct
    from the item-4 staleness gate: the cross-check is envelope-aggregation
    mode, and previously had no LVS value to compare at all.
    """
    other_layout_lvs = {
        **LVS_MATCH_ENVELOPE,
        "provenance": {
            **LVS_MATCH_ENVELOPE["provenance"],
            "input": {"content_hash": "sha256:layoutB"},
        },
    }
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", other_layout_lvs)

    result = build_signoff([drc_path, lvs_path])

    assert result["status"] == "refused"
    mismatches = result["provenance_consistency"]["mismatches"]
    assert [entry["field"] for entry in mismatches] == ["input.content_hash"]
    values = {entry["source"]: entry["value"] for entry in mismatches[0]["values"]}
    assert values[drc_path] == "sha256:layoutA"
    assert values[lvs_path] == "sha256:layoutB"


def test_drc_and_lvs_against_the_same_layout_stay_consistent(tmp_path):
    """Issue #1987, the paired passing case for
    `test_lvs_joins_the_input_hash_cross_check` above: binding LVS into the
    `input.content_hash` comparison must only refuse a *genuinely* stale
    pairing. A DRC and an LVS envelope naming the same layout hash are the
    normal T1 combination and must still render `ok: true` with no
    mismatches -- otherwise the fix would trade a silent false pass for a
    blanket false refusal, which is just as useless a grade.
    """
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    # Precondition: both fixtures really do pin the same layout, so the
    # assertion below is about the comparison and not about the fixtures
    # accidentally having drifted apart.
    assert (
        DRC_CLEAN_ENVELOPE["provenance"]["input"]
        == LVS_MATCH_ENVELOPE["provenance"]["input"]
    )

    result = build_signoff([drc_path, lvs_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["provenance_consistency"]["mismatches"] == []
    assert result["status"] == "pass"


@pytest.mark.parametrize("shape", ["absent", "null"])
def test_pre_1969_lvs_envelope_is_excluded_not_a_forced_mismatch(tmp_path, shape):
    """Issue #1987 edge case: an `lvs` envelope committed *before* `klt lvs`
    started populating `provenance.input` (issue #1969) carries no
    `input.content_hash` at all -- either the key is absent entirely or it
    is an explicit `null`.

    Such an envelope must stay non-participating, exactly as it was before
    the field existed: `_check_scalar_field` collects only non-`None`
    values, so a single populating check (the DRC sibling) leaves one
    distinct value and no mismatch. The failure mode this guards against is
    treating "nothing to say" as "disagrees with everyone", which would
    retroactively refuse every archived report the moment the field shipped.
    """
    provenance = {**LVS_MATCH_ENVELOPE["provenance"]}
    if shape == "absent":
        del provenance["input"]
    else:
        provenance["input"] = None
    old_shape_lvs = {**LVS_MATCH_ENVELOPE, "provenance": provenance}

    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", old_shape_lvs)

    result = build_signoff([drc_path, lvs_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["provenance_consistency"]["mismatches"] == []
    assert result["status"] == "pass"
    lvs_check = next(check for check in result["checks"] if check["kind"] == "lvs")
    assert lvs_check["provenance"].get("input") is None


def test_sim_does_not_participate_in_the_input_hash_cross_check(tmp_path):
    """Issue #1987's scope note, pinned as behaviour: `klt sim` deliberately
    leaves `provenance.input` `None` (it simulates a netlist against a model
    library -- there is no input *layout* stream to pin), so it never
    contributes a value to the `input.content_hash` comparison.

    This is the reason `klt sim` did *not* get `klt lvs`'s issue-#1969
    treatment: a netlist digest is not comparable with the layout digests
    `drc`/`extract`/`lvs`/`pex` contribute, so populating the shared field
    with one would turn every legitimate layout-plus-simulation manifest
    into a permanent `provenance_consistency` refusal.
    """
    assert SIM_PASS_ENVELOPE["provenance"]["input"] is None

    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    sim_path = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)

    result = build_signoff([drc_path, lvs_path, sim_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["provenance_consistency"]["mismatches"] == []
    assert result["status"] == "pass"


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


def test_valid_json_matching_no_recognized_kind_still_raises(tmp_path):
    """AC: a file that is valid JSON but matches none of the eight kinds
    (seven native + generic) still raises SignoffError naming the mismatch --
    unchanged behavior."""
    path = _write(
        tmp_path,
        "unknown.json",
        {"schema_version": 1, "kind": "not-a-real-kind", "status": "pass"},
    )

    with pytest.raises(SignoffError, match="unrecognized shape"):
        build_signoff([path])


def test_generic_kind_marker_takes_priority_over_native_structural_shape(tmp_path):
    """The explicit "kind": "generic" self-declaration is checked ahead of
    every native structural check, so an incidental field collision (e.g. a
    generic envelope that happens to also carry a `violations` list) is
    never misclassified as a native kind instead."""
    collision_envelope = {
        **GENERIC_PASS_ENVELOPE,
        "violations": ["not actually a DRC violations list"],
    }
    path = _write(tmp_path, "characterization.json", collision_envelope)

    result = build_signoff([path])

    assert result["checks"][0]["kind"] == "generic"


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
    # The digital column's RTL/synthesis guidance stays primary; the doc's
    # full-custom (no RTL, no synthesis) sub-case documents its substitute
    # artifact as an explicit, named cross-reference to the Analog column's
    # own artifact -- not by silently blending the two flows (issue #1190).
    assert "schematic" in item_1["text"]
    assert "full-custom digital partition" in item_1["text"]
    assert "Analog column's artifact" in item_1["text"]


def test_analog_manifest_uses_the_analog_column():
    result = build_tier_report(_manifest(kind="analog"))

    item_1 = next(item for item in result["items"] if item["id"] == 1)
    assert "schematic" in item_1["text"]


def test_digital_column_documents_the_full_custom_sub_case_for_items_1_2_5():
    """Issue #1190: a hand-captured full-custom digital partition (no RTL,
    no synthesis) has no RTL/synthesis artifact to cite -- the doc's Digital
    column for items 1, 2, and 5 documents a named substitute inline, as a
    sub-case of the existing column rather than a new column or manifest
    `kind` (grading is unaffected either way: these three items already
    accept any recognised evidence kind, regardless of what the column text
    says).

    Issue #1959 extended the same inline-sub-case treatment to item 7, whose
    grading stopped being column-independent once it became per-partition-kind
    -- see the item-7 assertions at the end."""
    result = build_tier_report(_manifest(kind="digital"))
    by_id = {item["id"]: item for item in result["items"] if item["tier"] == "T1"}

    # Item 1 (Design sources): RTL/synthesis stays the primary artifact, but
    # the full-custom substitute (schematic sources + regenerated netlist,
    # the Analog column's own artifact) is documented too.
    assert "RTL" in by_id[1]["text"]
    assert "full-custom digital partition" in by_id[1]["text"]
    assert "Analog column's artifact" in by_id[1]["text"]

    # Item 2 (Layout): P&R stays primary; the full-custom substitute is
    # hand-drawn GDS/OASIS with documented provenance.
    assert "place-and-route" in by_id[2]["text"]
    assert "full-custom" in by_id[2]["text"]
    assert "hand-drawn layout" in by_id[2]["text"]

    # Item 5 (Full corner verification): STA stays primary; the full-custom
    # substitute is PVT-corner SPICE sim plus an explicit per-corner timing
    # margin metric standing in for the STA setup/hold requirement.
    assert "static timing analysis" in by_id[5]["text"]
    assert "full-custom" in by_id[5]["text"]
    assert "timing-margin metric" in by_id[5]["text"]
    assert "PVT" in by_id[5]["text"]

    # Item 7 (Post-layout verification): until issue #1959 this item's
    # grading was globally pex-restricted regardless of column text, so the
    # Digital column needed no full-custom wording at all. Item 7 is now
    # graded per-partition-kind (`pex` for analog; `pex` *or* an
    # SDF-annotated `klt functional-verification` run for digital), so the
    # Digital column now has to say which of the two artifacts a given
    # digital partition cites -- the RTL-flow SDF re-simulation, or the
    # full-custom sub-case's own `klt pex` report. This is still an inline
    # sub-case of the Digital column, not a new column or manifest `kind`,
    # exactly like items 1/2/5 above.
    assert "SDF" in by_id[7]["text"]
    assert "functional-verification" in by_id[7]["text"]
    assert "full-custom sub-case" in by_id[7]["text"]
    assert "pex" in by_id[7]["text"]


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
        # Issue #2002: item 3's citation also quotes the three `coverage`
        # fields docs/design-evidence-tiers.md requires the claim to
        # disclose. `deck_scope` is `[]` because this fixture's coverage
        # block predates that field, not because the deck declares no scope.
        "coverage": {
            "layers_in_stream_without_rules": [],
            "rules_skipped": [],
            "deck_scope": [],
        },
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
    # Issue #1969: `klt lvs` now populates `provenance.input.content_hash`
    # (it was always null before, which is exactly what made a pinned item-4
    # citation ungradeable -- see the two tests below).
    assert item_4["citation"]["content_hash"] == "sha256:layoutA"


def test_lvs_matching_pinned_content_hash_renders_met(tmp_path):
    """Issue #1969: a T1 item-4 citation pinning an expected `content_hash`
    against a `klt lvs` report grades `met` when the hash matches.

    Before #1969 this was unreachable: `klt lvs`'s `provenance.input` was
    always `null`, so `_grade_evidence` found `actual_hash: None` and *no*
    pinned hash could ever match -- every pinned "LVS clean" citation graded
    `stale_evidence` regardless of whether the layout had actually moved.
    """
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    result = build_tier_report(
        _manifest(evidence={"4": {"file": lvs_path, "content_hash": "sha256:layoutA"}})
    )

    item_4 = next(item for item in result["items"] if item["id"] == 4)
    assert item_4["status"] == "met"
    assert item_4["reason"] is None
    assert item_4["citation"]["content_hash"] == "sha256:layoutA"


def test_lvs_stale_pinned_content_hash_renders_unmet(tmp_path):
    """The other half of #1969: populating the hash must not turn the
    staleness gate into a rubber stamp. A pinned hash that does *not* match
    the report's own still grades `stale_evidence` -- the fix removes a false
    negative, it does not introduce a false pass."""
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    result = build_tier_report(
        _manifest(
            evidence={"4": {"file": lvs_path, "content_hash": "sha256:stale-revision"}}
        )
    )

    item_4 = next(item for item in result["items"] if item["id"] == 4)
    assert item_4["status"] == "unmet"
    assert item_4["reason"] == "stale_evidence"
    assert item_4["citation"] is None


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
        return fake_completed(returncode=0, stdout=json.dumps(envelope))

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
        return fake_completed(returncode=0, stdout=json.dumps(SIM_PASS_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(evidence={"7": {"command": ["klt", "sim", "request.json"]}})
    )

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "wrong_kind"
    assert item_7["citation"] is None


def test_items_other_than_3_4_and_7_are_unaffected_by_the_kind_restriction(
    tmp_path,
):
    """Regression: items 1, 2, 5, 6 and 8-10 still accept any recognised,
    passing envelope kind -- only items 3, 4 (issue #1987) and 7 are
    kind-restricted."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    evidence = {str(i): drc_path for i in range(1, 11) if i not in (4, 7)}
    result = build_tier_report(_manifest(evidence=evidence))

    for item in result["items"]:
        if item["tier"] == "T1" and item["id"] not in (4, 7):
            assert item["status"] == "met", item["id"]
            assert item["citation"]["kind"] == "drc"


@pytest.mark.parametrize("kind", ["analog", "digital"])
def test_items_3_and_4_reject_a_passing_extract_envelope(tmp_path, kind):
    """Issue #1987: `klt extract` cannot fail (`_check_passed` is always True
    for it), so it must never stand in for "DRC clean" or "LVS clean"."""
    extract_path = _write(tmp_path, "extract.json", EXTRACT_ENVELOPE)
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)

    result = build_tier_report(
        _manifest(kind=kind, evidence={"3": extract_path, "4": extract_path})
    )
    for item_id in (3, 4):
        item = next(i for i in result["items"] if i["id"] == item_id)
        assert item["status"] == "unmet", item_id
        assert item["reason"] == "wrong_kind", item_id
        assert item["citation"] is None

    # The right kind still satisfies each item, and a drc report does not
    # satisfy item 4 (nor an lvs report item 3).
    result = build_tier_report(
        _manifest(kind=kind, evidence={"3": lvs_path, "4": drc_path})
    )
    for item_id in (3, 4):
        item = next(i for i in result["items"] if i["id"] == item_id)
        assert item["reason"] == "wrong_kind", item_id
    result = build_tier_report(
        _manifest(kind=kind, evidence={"3": drc_path, "4": lvs_path})
    )
    for item_id in (3, 4):
        item = next(i for i in result["items"] if i["id"] == item_id)
        assert item["status"] == "met", item_id


# --------------------------------------------------------------------------- #
# Generic evidence binding: item 8 only (issue #1152)
# --------------------------------------------------------------------------- #


def test_generic_evidence_satisfies_item_8(tmp_path):
    generic_path = _write(tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"8": generic_path}))

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "met"
    assert item_8["reason"] is None
    assert item_8["citation"] == {
        "file": generic_path,
        "command": None,
        "kind": "generic",
        "check_status": "pass",
        "content_hash": None,
        "exit_status": 0,
    }


def test_generic_fail_status_for_item_8_renders_unmet_check_failed(tmp_path):
    generic_path = _write(tmp_path, "characterization.json", GENERIC_FAIL_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"8": generic_path}))

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "unmet"
    assert item_8["reason"] == "check_failed"
    assert item_8["citation"] is None


def test_item_8_still_accepts_a_native_kind_too(tmp_path):
    """Item 8 was, and remains, otherwise unrestricted: "generic" is an
    *additional* accepted kind for it, not a replacement for the existing
    any-native-kind permissiveness (issue #871's docstring, "every item
    except item 7 accepts any recognised, passing envelope kind")."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    result = build_tier_report(_manifest(evidence={"8": drc_path}))

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "met"
    assert item_8["citation"]["kind"] == "drc"


@pytest.mark.parametrize("item_id", [3, 4, 5, 6, 7])
def test_generic_evidence_for_items_3_through_7_renders_unmet_wrong_kind(
    tmp_path, item_id
):
    """The concrete regression this issue guards against: a passing generic
    citation must never satisfy DRC/LVS/corner-verification/Monte-Carlo/
    post-layout items -- even though its own `status` genuinely is "pass".
    This preserves the strict-kind guarantee for the `klt`-verb-backed T1
    items; only item 8 (no named verb) may accept "generic" evidence."""
    generic_path = _write(tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={str(item_id): generic_path}))

    item = next(item for item in result["items"] if item["id"] == item_id)
    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["citation"] is None


@pytest.mark.parametrize("item_id", [1, 2, 9, 10])
def test_generic_evidence_for_unrestricted_non_item_8_items_still_rejected(
    tmp_path, item_id
):
    """Items 1, 2, 9, and 10 are otherwise unrestricted (any native kind
    satisfies them, like items 3-6) but do not opt in to "generic" either --
    only item 8 does."""
    generic_path = _write(tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={str(item_id): generic_path}))

    item = next(item for item in result["items"] if item["id"] == item_id)
    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["citation"] is None


def test_command_evidence_generic_wrong_kind_renders_unmet(monkeypatch):
    """The kind restriction applies to command-backed evidence exactly like
    file-backed evidence."""

    def fake_run(command, **kwargs):
        return fake_completed(returncode=0, stdout=json.dumps(GENERIC_PASS_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(evidence={"3": {"command": ["klt", "some-generic-check"]}})
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "wrong_kind"
    assert item_3["citation"] is None


def test_no_backing_generic_evidence_for_item_8_renders_unmet_never_assumed_met():
    result = build_tier_report(_manifest(evidence={}))

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "unmet"
    assert item_8["reason"] == "no_evidence"
    assert item_8["citation"] is None


def test_unprovenanced_generic_evidence_cannot_satisfy_a_pinned_content_hash(tmp_path):
    """A generic envelope with no `provenance` block (the documented
    caveat) can never match a manifest's pinned `content_hash` -- it renders
    "unmet"/"stale_evidence" rather than a false pass."""
    generic_path = _write(tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE)

    result = build_tier_report(
        _manifest(
            evidence={"8": {"file": generic_path, "content_hash": "sha256:expected"}}
        )
    )

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "unmet"
    assert item_8["reason"] == "stale_evidence"
    assert item_8["citation"] is None


# --------------------------------------------------------------------------- #
# "power" evidence never satisfies any tier-report item (issue #1321)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("item_id", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_power_evidence_never_satisfies_any_tier_item(tmp_path, item_id):
    """`docs/design-evidence-tiers.md`'s T1 checklist has no item for
    power-grid IR-drop/EM evidence -- unlike "generic" (scoped to item 8), a
    genuinely passing `klt power` citation must render "unmet"/"wrong_kind"
    for every item, including item 8, never a borrowed pass."""
    power_path = _write(tmp_path, "power.json", POWER_PASS_ENVELOPE)

    result = build_tier_report(_manifest(evidence={str(item_id): power_path}))

    item = next(item for item in result["items"] if item["id"] == item_id)
    assert item["status"] == "unmet"
    assert item["reason"] == "wrong_kind"
    assert item["citation"] is None


def test_command_evidence_power_wrong_kind_renders_unmet(monkeypatch):
    """The restriction applies to command-backed evidence exactly like
    file-backed evidence -- a `klt power` invocation cited for any item
    still renders "unmet", even though the run itself passed."""

    def fake_run(command, **kwargs):
        return fake_completed(returncode=0, stdout=json.dumps(POWER_PASS_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(
            evidence={"3": {"command": ["klt", "power", "routed.gds", "power.json"]}}
        )
    )

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "wrong_kind"
    assert item_3["citation"] is None


def test_no_backing_power_evidence_renders_unmet_never_assumed_met():
    result = build_tier_report(_manifest(evidence={}))

    for item_id in range(1, 11):
        item = next(item for item in result["items"] if item["id"] == item_id)
        assert item["status"] == "unmet"
        assert item["citation"] is None


def test_generic_evidence_with_matching_provenance_content_hash_renders_met(tmp_path):
    generic_path = _write(
        tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE_WITH_PROVENANCE
    )

    result = build_tier_report(
        _manifest(
            evidence={
                "8": {
                    "file": generic_path,
                    "content_hash": "sha256:characterization-input",
                }
            }
        )
    )

    item_8 = next(item for item in result["items"] if item["id"] == 8)
    assert item_8["status"] == "met"
    assert item_8["citation"]["content_hash"] == "sha256:characterization-input"


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


def test_command_evidence_runs_and_grades_met(monkeypatch):
    calls: list[dict] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return fake_completed(returncode=0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

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
        # Issue #2002: a command-backed `drc` citation quotes the coverage
        # block off the envelope the command actually printed, exactly like
        # the file-backed path -- `deck_scope` is `[]` here because
        # DRC_CLEAN_ENVELOPE's fixture coverage block predates that field.
        "coverage": {
            "layers_in_stream_without_rules": [],
            "rules_skipped": [],
            "deck_scope": [],
        },
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
        return fake_completed(returncode=0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

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
        return fake_completed(returncode=1, stdout="")

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
        return fake_completed(returncode=0, stdout="not valid json")

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "unreadable_evidence"
    assert item_3["citation"] is None


def test_command_evidence_error_envelope_renders_check_errored(monkeypatch):
    def fake_run(command, **kwargs):
        return fake_completed(returncode=0, stdout=json.dumps(DRC_ERROR_ENVELOPE))

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
        return fake_completed(returncode=3, stdout=json.dumps(DRC_VIOLATIONS_ENVELOPE))

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(_manifest(evidence={"3": {"command": ["klt", "drc"]}}))

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "check_failed"
    assert item_3["citation"] is None


def test_command_evidence_stale_content_hash_renders_unmet(monkeypatch):
    def fake_run(command, **kwargs):
        return fake_completed(returncode=0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

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
        return fake_completed(returncode=0, stdout=json.dumps(DRC_CLEAN_ENVELOPE))

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
        return fake_completed(
            returncode=0, stdout=json.dumps(DRC_CLEAN_ENVELOPE)
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
# `--tiers-doc` / `$KLT_TIERS_DOC` overrides (issue #1050)
# --------------------------------------------------------------------------- #

# A structurally valid but deliberately tiny tier doc: three T1 items, so a
# report built from it is unmistakably *not* built from the real doc's ten.
_VENDORED_TIERS_DOC = """\
# Design-evidence tiers

## The ladder

| Tier | Claim | Demonstrated by |
|---|---|---|
| **T1 — sim-validated** | Designed and simulation-validated | Open-source evidence |
| **T2 — signoff-validated** | Validated on commercial tools | T1, plus commercial |
| **T3 — silicon-validated** | Fabricated and measured | T2, plus a tapeout |
| **T4 — production-validated** | Proven in silicon | An external project |

## T1 checklist — what "sim-validated" requires

1. **Vendored item one**
   - *Analog* — committed schematic sources.
   - *Digital* — committed RTL sources.
2. **Vendored item two** — latest `klt drc` JSON report: `status: clean`.
3. **Vendored item three** — some other gate.

## Verification rules

- **Staleness is failure.**
"""


def _write_tiers_doc(tmp_path) -> str:
    path = tmp_path / "vendored-tiers.md"
    path.write_text(_VENDORED_TIERS_DOC, encoding="utf-8")
    return str(path)


def test_cli_manifest_tiers_doc_overrides_the_parsed_doc(tmp_path, capsys):
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    tiers_doc = _write_tiers_doc(tmp_path)

    exit_code = main(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--tiers-doc",
            tiers_doc,
            "--format",
            "json",
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert out["t1_item_count"] == 3
    assert out["items"][0]["title"] == "Vendored item one"
    # The report names the doc it was actually built from, not the canonical one.
    assert out["source_doc"] == tiers_doc


def test_cli_fleet_tiers_doc_overrides_the_parsed_doc(tmp_path, capsys):
    fleet_path = _write(
        tmp_path,
        "fleet.json",
        {"blocks": [{"block": "b1", "kind": "analog", "evidence": {}}]},
    )
    tiers_doc = _write_tiers_doc(tmp_path)

    exit_code = main(
        ["signoff", "--fleet", fleet_path, "--tiers-doc", tiers_doc, "--format", "json"]
    )

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert out["source_doc"] == tiers_doc
    assert out["blocks"][0]["t1_item_count"] == 3


def test_cli_manifest_env_var_overrides_the_parsed_doc(tmp_path, capsys, monkeypatch):
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    tiers_doc = _write_tiers_doc(tmp_path)
    monkeypatch.setenv("KLT_TIERS_DOC", tiers_doc)

    exit_code = main(["signoff", "--manifest", manifest_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert out["t1_item_count"] == 3
    assert out["source_doc"] == tiers_doc


def test_cli_manifest_tiers_doc_beats_the_env_var(tmp_path, capsys, monkeypatch):
    manifest_path = _write(tmp_path, "manifest.json", _manifest())
    tiers_doc = _write_tiers_doc(tmp_path)
    monkeypatch.setenv("KLT_TIERS_DOC", str(tmp_path / "does-not-exist.md"))

    exit_code = main(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--tiers-doc",
            tiers_doc,
            "--format",
            "json",
        ]
    )

    assert exit_code == 3
    assert json.loads(capsys.readouterr().out)["source_doc"] == tiers_doc


def test_cli_manifest_unreadable_tiers_doc_emits_the_error_envelope(tmp_path, capsys):
    manifest_path = _write(tmp_path, "manifest.json", _manifest())

    exit_code = main(
        [
            "signoff",
            "--manifest",
            manifest_path,
            "--tiers-doc",
            str(tmp_path / "nope.md"),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "signoff"
    assert "could not read design-evidence-tiers doc" in err["error"]["message"]


def test_cli_tiers_doc_without_manifest_or_fleet_is_an_error(tmp_path, capsys):
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    tiers_doc = _write_tiers_doc(tmp_path)

    exit_code = main(
        ["signoff", drc_path, "--tiers-doc", tiers_doc, "--format", "json"]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "signoff"
    assert "--tiers-doc" in err["error"]["message"]


def test_cli_envelope_aggregation_is_unaffected_by_a_missing_tier_doc(
    tmp_path, capsys, monkeypatch
):
    """AC: envelope-aggregation mode never reads the tier doc, so it keeps
    working even where the doc cannot be resolved at all (issue #1050)."""
    monkeypatch.setenv("KLT_TIERS_DOC", str(tmp_path / "does-not-exist.md"))
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    exit_code = main(["signoff", drc_path, "--format", "json"])

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["status"] == "pass"


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


# --------------------------------------------------------------------------- #
# The committed worked example (`examples/signoff/`, issue #1954).
#
# `examples/signoff/` is the copy-paste starting point a block author adapts:
# a block manifest citing T1 items 3 and 4 against real, committed `klt
# drc`/`klt lvs` envelopes, with every other item deliberately uncited. These
# tests pin the mixed `met`/`unmet` shape its README documents, so the example
# cannot silently rot into "everything unmet" (or, worse, "everything met").
# --------------------------------------------------------------------------- #

_SIGNOFF_EXAMPLES_DIR = _REPO_ROOT / "examples" / "signoff"


@pytest.mark.skipif(
    not (_SIGNOFF_EXAMPLES_DIR / "manifest.json").exists(),
    reason="examples/signoff/manifest.json not present in this checkout",
)
def test_committed_example_manifest_grades_items_3_and_4_met(monkeypatch):
    # The manifest's file-backed evidence paths are repo-root-relative,
    # because a manifest resolves them against the invoking process's cwd.
    monkeypatch.chdir(_REPO_ROOT)
    manifest = json.loads(
        (_SIGNOFF_EXAMPLES_DIR / "manifest.json").read_text(encoding="utf-8")
    )

    result = build_tier_report(manifest)

    assert result["kind"] == "analog"
    assert result["t1_met_count"] == 2
    met = {item["id"] for item in result["items"] if item["status"] == "met"}
    assert met == {3, 4}

    item_3 = next(item for item in result["items"] if item["id"] == 3)
    assert item_3["citation"]["kind"] == "drc"
    assert item_3["citation"]["check_status"] == "clean"
    # The manifest pins the layout revision it is claiming against, and the
    # committed envelope still matches it -- a stale pin would render unmet.
    assert (
        item_3["citation"]["content_hash"] == manifest["evidence"]["3"]["content_hash"]
    )

    item_4 = next(item for item in result["items"] if item["id"] == 4)
    assert item_4["citation"]["kind"] == "lvs"
    assert item_4["citation"]["check_status"] == "match"

    # Every other T1 item is visibly uncited, never silently assumed met.
    for item in result["items"]:
        if item["tier"] == "T1" and item["id"] not in {3, 4}:
            assert item["status"] == "unmet"
            assert item["reason"] == "no_evidence"


@pytest.mark.skipif(
    not (_SIGNOFF_EXAMPLES_DIR / "fleet.json").exists(),
    reason="examples/signoff/fleet.json not present in this checkout",
)
def test_committed_example_fleet_manifest_rolls_up_the_one_block(monkeypatch):
    monkeypatch.chdir(_REPO_ROOT)
    fleet = json.loads(
        (_SIGNOFF_EXAMPLES_DIR / "fleet.json").read_text(encoding="utf-8")
    )

    result = build_fleet_report(fleet)

    assert result["block_count"] == 1
    block = result["blocks"][0]
    assert block["block"] == "example-current-mirror"
    assert block["t1_met_count"] == 2
    assert block["tier"] is None


# --------------------------------------------------------------------------- #
# Digital-flow evidence: `sta` + `functional-verification` (issue #1959)
#
# Before this phase, `_classify` recognised no digital RTL-flow artifact at
# all, and item 7 was globally restricted to `pex` -- so no digital block
# could reach T1 on items 5 and 7 whatever evidence it produced. These tests
# cover the two new kinds, item 7's new per-block-kind restriction, and the
# regression that analog / full-custom-digital grading is unchanged.
# --------------------------------------------------------------------------- #


def test_sta_multi_corner_clean_check_passes(tmp_path):
    """`klt sta`'s `status` is always `"ok"` -- the verdict comes from the
    per-corner timing instead."""
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_CLEAN_ENVELOPE)

    result = build_signoff([sta_path])

    assert result["status"] == "pass"
    check = result["checks"][0]
    assert check["kind"] == "sta"
    assert check["status"] == "ok"
    assert check["passed"] is True
    assert check["detail"]["corner_count"] == 2
    assert check["detail"]["worst_slack_ns"] == 0.11842
    assert check["detail"]["worst_hold_slack_ns"] == 0.02011
    assert check["detail"]["timing_status"] == "constrained"


def test_sta_single_corner_clean_check_passes(tmp_path):
    sta_path = _write(tmp_path, "sta.json", STA_SINGLE_CORNER_CLEAN_ENVELOPE)

    result = build_signoff([sta_path])

    check = result["checks"][0]
    assert check["kind"] == "sta"
    assert check["passed"] is True
    assert check["detail"]["corner_count"] is None


def test_sta_setup_violation_at_one_corner_check_fails(tmp_path):
    """Corner scoping is the cited run's own declared corner set: every
    corner `klt sta` reported must close, not just the nominal one."""
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_SETUP_VIOLATION_ENVELOPE)

    result = build_signoff([sta_path])

    assert result["status"] == "fail"
    assert result["checks"][0]["passed"] is False


def test_sta_unconstrained_timing_never_passes(tmp_path):
    """OpenSTA's unconstrained sentinel (`1e+39`) is a *positive* number --
    a naive `worst_slack_ns >= 0` rule would call an untimed design closed.
    `timing_status` must be `"constrained"` first."""
    sta_path = _write(tmp_path, "sta.json", STA_UNCONSTRAINED_ENVELOPE)

    result = build_signoff([sta_path])

    assert result["checks"][0]["kind"] == "sta"
    assert result["checks"][0]["passed"] is False


def test_sta_empty_corners_list_never_passes(tmp_path):
    """A corners response that characterised zero corners proves nothing."""
    sta_path = _write(
        tmp_path,
        "sta.json",
        {**STA_MULTI_CORNER_CLEAN_ENVELOPE, "corners": []},
    )

    result = build_signoff([sta_path])

    assert result["checks"][0]["passed"] is False


def test_functional_verification_pass_check_passes(tmp_path):
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_PASS_ENVELOPE)

    result = build_signoff([fv_path])

    check = result["checks"][0]
    assert check["kind"] == "functional-verification"
    assert check["passed"] is True
    assert check["detail"]["test_count"] == 3
    assert check["detail"]["failed_count"] == 0
    assert check["detail"]["sdf_annotated"] is False


def test_functional_verification_fail_check_fails(tmp_path):
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_FAIL_ENVELOPE)

    result = build_signoff([fv_path])

    assert result["checks"][0]["kind"] == "functional-verification"
    assert result["checks"][0]["passed"] is False


def test_sdf_annotated_functional_verification_is_marked_in_detail(tmp_path):
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)

    result = build_signoff([fv_path])

    assert result["checks"][0]["detail"]["sdf_annotated"] is True
    assert result["checks"][0]["detail"]["sdf_corner"] == "typ"


def test_place_and_route_envelope_is_still_unrecognized(tmp_path):
    """`klt place-and-route`'s response overlaps `klt sta`'s timing fields
    (`worst_slack_ns`/`timing_status`/`corners`) but is a different verb with
    a different corner-sweep contract -- recognising `sta` must not start
    silently classifying it as one."""
    pnr_path = _write(tmp_path, "pnr.json", PLACE_AND_ROUTE_ENVELOPE)

    with pytest.raises(SignoffError, match="unrecognized shape"):
        build_signoff([pnr_path])


def test_sta_evidence_satisfies_item_5_for_a_digital_manifest(tmp_path):
    """The headline gap: a digital block's corner-verification evidence is a
    multi-corner `klt sta` run, not a `klt sim` corner sweep it has no reason
    to produce."""
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_CLEAN_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"5": sta_path}))

    item_5 = next(item for item in result["items"] if item["id"] == 5)
    assert item_5["status"] == "met"
    assert item_5["reason"] is None
    assert item_5["citation"]["kind"] == "sta"
    assert item_5["citation"]["check_status"] == "ok"
    assert item_5["citation"]["content_hash"] == "sha256:routeddef"


def test_functional_verification_evidence_satisfies_item_5_for_digital(tmp_path):
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_PASS_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"5": fv_path}))

    item_5 = next(item for item in result["items"] if item["id"] == 5)
    assert item_5["status"] == "met"
    assert item_5["citation"]["kind"] == "functional-verification"


def test_failing_sta_evidence_for_item_5_renders_unmet_check_failed(tmp_path):
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_SETUP_VIOLATION_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"5": sta_path}))

    item_5 = next(item for item in result["items"] if item["id"] == 5)
    assert item_5["status"] == "unmet"
    assert item_5["reason"] == "check_failed"
    assert item_5["citation"] is None


def test_sdf_annotated_functional_verification_satisfies_item_7_for_digital(
    tmp_path,
):
    """Item 7's Digital column asks for "the functional test suite re-run
    against the post-route gate-level netlist with back-annotated SDF
    timing" -- an SDF-annotated `klt functional-verification` run is exactly
    that artifact."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"7": fv_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "met"
    assert item_7["reason"] is None
    assert item_7["citation"]["kind"] == "functional-verification"


def test_unannotated_functional_verification_for_item_7_is_not_post_layout(
    tmp_path,
):
    """A zero-delay pre-layout regression is the right *kind* but the wrong
    evidence: `environment.sdf` is `null`, so it proves nothing about
    post-route behaviour. Distinguished from `wrong_kind` so the report says
    exactly what to re-run."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_PASS_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"7": fv_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "not_post_layout"
    assert item_7["citation"] is None


def test_sta_citation_for_item_7_renders_unmet_wrong_kind(tmp_path):
    """Settling the issue's open question: item 7's Digital column asks for
    SDF gate-level re-simulation, not STA-with-parasitics. A `klt sta` run
    (even a SPEF-annotated, timing-clean one) is item-5 evidence."""
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_CLEAN_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"7": sta_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "wrong_kind"
    assert item_7["citation"] is None


def test_pex_still_satisfies_item_7_for_a_full_custom_digital_block(tmp_path):
    """Regression: a full-custom digital partition declares `kind:
    "digital"` and cites `klt pex` exactly like an analog block -- widening
    item 7 for the RTL flow must not take that path away."""
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)

    result = build_tier_report(_manifest(kind="digital", evidence={"7": pex_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "met"
    assert item_7["citation"]["kind"] == "pex"


def test_analog_item_7_still_requires_pex(tmp_path):
    """Regression: the new digital artifact is scoped to the digital
    partition -- an analog block's item 7 is unchanged, `pex` only."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)

    result = build_tier_report(_manifest(kind="analog", evidence={"7": fv_path}))

    item_7 = next(item for item in result["items"] if item["id"] == 7)
    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "wrong_kind"
    assert item_7["citation"] is None


def test_analog_item_5_accepts_any_recognised_kind_as_before(tmp_path):
    """Regression: item 5 stays unrestricted for every block kind -- this
    phase widens what is *recognised*, it does not tighten what item 5
    accepts."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    sim_path = _write(tmp_path, "sim.json", SIM_PASS_ENVELOPE)

    analog = build_tier_report(_manifest(kind="analog", evidence={"5": drc_path}))
    assert next(i for i in analog["items"] if i["id"] == 5)["status"] == "met"

    full_custom_digital = build_tier_report(
        _manifest(kind="digital", evidence={"5": sim_path})
    )
    item_5 = next(i for i in full_custom_digital["items"] if i["id"] == 5)
    assert item_5["status"] == "met"
    assert item_5["citation"]["kind"] == "sim"


def test_mixed_signal_partitions_apply_their_own_item_7_rule(tmp_path):
    """A mixed-signal manifest's digital partition applies the same
    per-block-kind item-7 rule a pure `digital` manifest does, while its
    analog partition still requires `pex`."""
    pex_path = _write(tmp_path, "pex.json", PEX_PASS_ENVELOPE)
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)

    result = build_tier_report(
        _manifest(
            kind="mixed-signal",
            evidence={"7.analog": pex_path, "7.digital": fv_path},
        )
    )

    item_7 = {
        item["partition"]: item
        for item in result["items"]
        if item["id"] == 7 and item["tier"] == "T1"
    }
    assert item_7["analog"]["status"] == "met"
    assert item_7["analog"]["citation"]["kind"] == "pex"
    assert item_7["digital"]["status"] == "met"
    assert item_7["digital"]["citation"]["kind"] == "functional-verification"


def test_mixed_signal_analog_partition_rejects_the_digital_item_7_artifact(
    tmp_path,
):
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)

    result = build_tier_report(_manifest(kind="mixed-signal", evidence={"7": fv_path}))

    item_7 = {
        item["partition"]: item
        for item in result["items"]
        if item["id"] == 7 and item["tier"] == "T1"
    }
    assert item_7["analog"]["status"] == "unmet"
    assert item_7["analog"]["reason"] == "wrong_kind"
    assert item_7["digital"]["status"] == "met"


def test_generic_citation_still_rejected_on_items_5_and_7_for_digital(tmp_path):
    """Direction 3 of the issue -- widening `generic` onto items 5/7 -- is
    explicitly NOT implemented: it would weaken exactly the guarantee #1152
    was careful to preserve."""
    generic_path = _write(tmp_path, "generic.json", GENERIC_PASS_ENVELOPE)

    result = build_tier_report(
        _manifest(kind="digital", evidence={"5": generic_path, "7": generic_path})
    )

    for item_id in (5, 7):
        item = next(i for i in result["items"] if i["id"] == item_id)
        assert item["status"] == "unmet", item_id
        assert item["reason"] == "wrong_kind", item_id
        assert item["citation"] is None


def test_command_backed_sta_evidence_satisfies_item_5(monkeypatch):
    """The new kinds work through the command-backed evidence path exactly
    like the file-backed one -- no separate wiring."""

    def fake_run(command, **kwargs):
        return fake_completed(
            returncode=0, stdout=json.dumps(STA_MULTI_CORNER_CLEAN_ENVELOPE)
        )

    monkeypatch.setattr(signoff_module.subprocess, "run", fake_run)

    result = build_tier_report(
        _manifest(
            kind="digital",
            evidence={"5": {"command": ["klt", "sta", "request.json"]}},
        )
    )

    item_5 = next(item for item in result["items"] if item["id"] == 5)
    assert item_5["status"] == "met"
    assert item_5["citation"]["kind"] == "sta"
    assert item_5["citation"]["command"] == "klt sta request.json"


def test_realistic_digital_envelope_set_reaches_eight_of_ten_items(tmp_path):
    """The issue's "Manifest C" experiment, now with the digital artifacts
    recognised: a digital RTL-flow block citing `sta`, `functional-
    verification` (SDF-annotated for item 7), `drc`, `lvs` and `extract`
    reaches `met` on items 1, 2, 3, 4, 5, 7 and 8 -- the two items it still
    misses (6, 9) and item 10 are evidence it genuinely has not cited."""
    drc_path = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)
    lvs_path = _write(tmp_path, "lvs.json", LVS_MATCH_ENVELOPE)
    extract_path = _write(tmp_path, "extract.json", EXTRACT_ENVELOPE)
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_CLEAN_ENVELOPE)
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_PASS_ENVELOPE)
    fv_sdf_path = _write(tmp_path, "fv-sdf.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    generic_path = _write(tmp_path, "characterization.json", GENERIC_PASS_ENVELOPE)

    result = build_tier_report(
        _manifest(
            kind="digital",
            evidence={
                "1": extract_path,
                "2": drc_path,
                "3": drc_path,
                "4": lvs_path,
                "5": sta_path,
                "7": fv_sdf_path,
                "8": generic_path,
                "9": fv_path,
            },
        )
    )

    met = {item["id"] for item in result["items"] if item["status"] == "met"}
    assert {1, 2, 3, 4, 5, 7, 8}.issubset(met)
    assert result["t1_met_count"] >= 8

    # And the two unmet items report "nobody cited this", never a check that
    # ran and failed.
    for item_id in (6, 10):
        item = next(i for i in result["items"] if i["id"] == item_id)
        assert item["status"] == "unmet"
        assert item["reason"] == "no_evidence"


def test_fleet_rollup_blocking_item_reflects_the_new_digital_kinds(tmp_path):
    """The roll-up is a pure reduction of `build_tier_report`, so a digital
    block's blocking item moves off items 5/7 for free once its evidence is
    recognised."""
    sta_path = _write(tmp_path, "sta.json", STA_MULTI_CORNER_CLEAN_ENVELOPE)
    fv_sdf_path = _write(tmp_path, "fv-sdf.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)

    result = build_fleet_report(
        {
            "blocks": [
                {
                    "block": "digital-canary",
                    "kind": "digital",
                    "evidence": {"5": sta_path, "7": fv_sdf_path},
                }
            ]
        }
    )

    block = result["blocks"][0]
    assert block["blocking_item"]["id"] == 1
    assert block["blocking_item"]["reason"] == "no_evidence"
