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

import io
import json

import pytest

from klayout_tools.cli import main
from klayout_tools.design_evidence_tiers import DesignEvidenceTiersError
from klayout_tools.signoff import SignoffError, build_signoff, build_tier_report

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

    evidence = {str(i): drc_path for i in range(2, 11)}  # 2-10, never 1
    evidence["4"] = lvs_path

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

    evidence = {str(i): drc_path for i in range(1, 11)}
    evidence["4"] = lvs_path

    result = build_tier_report(_manifest(evidence=evidence))

    assert result["t1_met_count"] == 10
    assert result["tier"] == "T1"


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
    evidence = {str(i): drc_path for i in range(1, 11)}
    evidence["4"] = lvs_path
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
