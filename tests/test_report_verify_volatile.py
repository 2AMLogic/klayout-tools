"""Issue #5182: `pdk.ambiguous_sources` is run-specific, not rerun drift."""

import pytest


def _ambiguous_sources_report(device_count, ambiguous):
    pdk = {"name": "sky130A", "source": "search root: ~/.ciel"}
    if ambiguous is not None:
        pdk["ambiguous_sources"] = ambiguous
    return {"device_count": device_count, "provenance": {"pdk": pdk}}


@pytest.mark.parametrize(
    ("committed_amb", "fresh_amb"),
    [
        (None, ["search root: ~/.volare"]),
        (["search root: ~/.volare"], None),
        (None, None),
    ],
)
def test_rerun_ignores_pdk_ambiguous_sources(committed_amb, fresh_amb):
    """Issue #5182: a machine-local `ambiguous_sources` never causes drift."""
    from klayout_tools import _report_verify as rv

    result = rv.build_rerun_result(
        report_path="r.json",
        committed=_ambiguous_sources_report(4, committed_amb),
        fresh=_ambiguous_sources_report(4, fresh_amb),
    )
    assert result["status"] == "match"
    assert result["drift"] == []


def test_rerun_still_detects_content_drift_with_ambiguous_sources():
    from klayout_tools import _report_verify as rv

    result = rv.build_rerun_result(
        report_path="r.json",
        committed=_ambiguous_sources_report(4, None),
        fresh=_ambiguous_sources_report(5, ["search root: ~/.volare"]),
    )
    assert result["status"] == "drifted"


def test_documented_run_specific_fields_equal_volatile_provenance_paths():
    """Issue #5182: docs/json-contract.md's table and the code constant agree."""
    import re
    from pathlib import Path

    from klayout_tools import _report_verify as rv

    doc = (
        Path(__file__).resolve().parent.parent / "docs" / "json-contract.md"
    ).read_text()
    block = doc.split("<!-- run-specific-fields:begin -->")[1].split(
        "<!-- run-specific-fields:end -->"
    )[0]
    documented = {
        tuple(m.split("."))
        for m in re.findall(r"^\| `([a-z_.]+)` \|", block, flags=re.MULTILINE)
    }
    assert documented == set(rv.VOLATILE_PROVENANCE_PATHS)


# --------------------------------------------------------------------------- #
# Issue #5586: a LIST_ITEM wildcard in an exclude= set is silently ignored by
# diff_verdict_fields' exact-path match while the same set is honoured by
# strip_path_patterns -- two canonical views compare equal while --rerun
# reports drift. diff_verdict_fields now rejects such a set outright.
# --------------------------------------------------------------------------- #


def test_diff_verdict_fields_rejects_list_item_exclude():
    """Issue #5586: the issue's own reproduction must now be impossible --
    strip_path_patterns still filters the wildcard, but diff_verdict_fields
    raises instead of silently comparing the excluded leaf anyway."""
    from klayout_tools._report_verify import (
        LIST_ITEM,
        diff_verdict_fields,
        strip_path_patterns,
    )

    pat = frozenset({("abstracted_cells", LIST_ITEM, "lef_path")})
    committed = {"abstracted_cells": [{"lef_path": "/tmp/run-1/x.lef"}]}
    fresh = {"abstracted_cells": [{"lef_path": "/tmp/run-2/x.lef"}]}

    # The strip side honours the wildcard: both filtered views compare equal.
    assert strip_path_patterns(committed, pat) == strip_path_patterns(fresh, pat)

    # The exclude side must refuse it loudly, naming the offending path.
    with pytest.raises(ValueError) as excinfo:
        diff_verdict_fields(committed, fresh, exclude=pat)
    assert "abstracted_cells.*.lef_path" in str(excinfo.value)
    assert "strip_path_patterns" in str(excinfo.value)


def test_diff_verdict_fields_rejects_mixed_exclude_as_a_whole():
    """Issue #5586 edge case: a set mixing a LIST_ITEM entry with plain paths
    is rejected as a whole -- not partially honoured."""
    from klayout_tools._report_verify import LIST_ITEM, diff_verdict_fields

    pat = frozenset({("provenance", "pdk"), ("items", LIST_ITEM, "run_path")})
    committed = {
        "provenance": {"pdk": {"version": "a"}},
        "items": [{"run_path": "/tmp/run-1/x"}],
    }
    fresh = {
        "provenance": {"pdk": {"version": "b"}},
        "items": [{"run_path": "/tmp/run-2/x"}],
    }
    with pytest.raises(ValueError) as excinfo:
        diff_verdict_fields(committed, fresh, exclude=pat)
    assert "items.*.run_path" in str(excinfo.value)


def test_diff_verdict_fields_still_excludes_by_exact_path():
    """Issue #5586: no behaviour change for a LIST_ITEM-free exclude -- the
    literal index path a --rerun caller actually passes still matches."""
    from klayout_tools._report_verify import diff_verdict_fields

    pat = frozenset({("items", "0", "run_path")})
    committed = {"items": [{"run_path": "/tmp/run-1/x", "value": 1}]}
    fresh = {"items": [{"run_path": "/tmp/run-2/x", "value": 1}]}
    assert diff_verdict_fields(committed, fresh, exclude=pat) == []
    assert diff_verdict_fields(committed, fresh) == [
        {
            "field": "items.0.run_path",
            "committed": "/tmp/run-1/x",
            "fresh": "/tmp/run-2/x",
        }
    ]


def test_build_rerun_result_propagates_list_item_exclude_rejection():
    """Issue #5586: build_rerun_result forwards exclude into
    diff_verdict_fields, so it inherits the rejection with no check of its
    own."""
    from klayout_tools._report_verify import LIST_ITEM, build_rerun_result

    pat = frozenset({("abstracted_cells", LIST_ITEM, "lef_path")})
    with pytest.raises(ValueError, match="abstracted_cells"):
        build_rerun_result(
            report_path="r.json",
            committed={"abstracted_cells": [{"lef_path": "/tmp/a.lef"}]},
            fresh={"abstracted_cells": [{"lef_path": "/tmp/b.lef"}]},
            exclude=pat,
        )


def test_exclude_destined_constants_are_list_item_free():
    """Issue #5586: the codebase's unwritten invariant, now asserted -- every
    constant that reaches diff_verdict_fields(exclude=) / build_rerun_result
    (exclude=) carries no LIST_ITEM, so the wildcard rejection can never fire
    on a legitimate caller. A new exclusion set destined for exclude= belongs
    in this list; one needing a per-list-entry exclusion belongs on the
    strip_path_patterns side instead."""
    from klayout_tools import _report_verify as rv
    from klayout_tools import equiv, lvs, signoff, signoff_attest

    exclude_destined = {
        "_report_verify.VOLATILE_PROVENANCE_PATHS": rv.VOLATILE_PROVENANCE_PATHS,
        "_report_verify.VOLATILE_FLOW_PATHS": rv.VOLATILE_FLOW_PATHS,
        "signoff.VOLATILE_REPORT_PATHS": signoff.VOLATILE_REPORT_PATHS,
        "equiv._EQUIV_RERUN_EXCLUDE_PATHS": equiv._EQUIV_RERUN_EXCLUDE_PATHS,
        "lvs._LVS_RERUN_EXCLUDE_PATHS": lvs._LVS_RERUN_EXCLUDE_PATHS,
        "signoff_attest.VOLATILE_ATTESTATION_PATHS": (
            signoff_attest.VOLATILE_ATTESTATION_PATHS
        ),
    }
    for name, paths in exclude_destined.items():
        offenders = [".".join(p) for p in paths if rv.LIST_ITEM in p]
        assert not offenders, (
            f"{name} carries LIST_ITEM path(s) {offenders}: exclude= matches "
            "by exact path only, so these would be rejected at diff time -- "
            "move them to a strip_path_patterns side set"
        )
