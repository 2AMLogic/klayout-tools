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
