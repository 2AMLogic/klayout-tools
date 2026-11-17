"""Issue #5789: the manifest's opt-in ``require_coverage_disclosure``
ratchet.

T1 item 3's citation reports the cited `klt drc` envelope's three
``coverage`` fields (issue #2002), but by default a claim that hides every
disclosed gap grades exactly like a fully-disclosing one -- a manifest had
no field to state its disclosure in. These cover the top-level boolean and
the per-entry ``coverage_disclosed`` declaration that close that loop: the
declaration is compared against the coverage the citation reports as
per-field set equality. The reporting itself is covered beside the other
item-3 cases in ``test_signoff.py``.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report, check_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    DRC_CLEAN_NO_COVERAGE_ENVELOPE,
    DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE,
    DRC_VIOLATIONS_ENVELOPE,
)

_GAPS = {
    "layers_in_stream_without_rules": ["70/20", "71/20"],
    "rules_skipped": ["met5.4", "met5.5"],
    "deck_scope": ["5.x", "6.x"],
}


def _write(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _manifest(**overrides) -> dict:
    return {"block": "demo-block", "kind": "analog", "evidence": {}, **overrides}


def _item3(evidence, **overrides) -> dict:
    manifest = _manifest(evidence={"3": evidence}, **overrides)
    return next(i for i in build_tier_report(manifest)["items"] if i["id"] == 3)


def _declared(**fields) -> dict:
    return {name: list(entries) for name, entries in fields.items()}


def test_ratchet_off_grades_byte_identically(tmp_path):
    """AC: absent, `null` and `false` all render the report a manifest
    without the key always rendered -- the hard requirement."""
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    evidence = {"3": {"file": drc, "coverage_disclosed": _declared(**_GAPS)}}

    absent = build_tier_report(_manifest(evidence=evidence))
    for value in (False, None):
        explicit = build_tier_report(
            _manifest(evidence=evidence, require_coverage_disclosure=value)
        )
        assert explicit == absent
    assert next(i for i in absent["items"] if i["id"] == 3)["status"] == "met"


def test_matching_declaration_grades_met_with_no_new_detail_key(tmp_path):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    # Shuffled order: the comparison is per-field set equality.
    declared = _declared(
        rules_skipped=["met5.5", "met5.4"],
        deck_scope=["6.x", "5.x"],
        layers_in_stream_without_rules=["71/20", "70/20"],
    )

    item = _item3(
        {"file": drc, "coverage_disclosed": declared},
        require_coverage_disclosure=True,
    )

    assert item["status"] == "met"
    assert item["reason"] is None
    assert "detail" not in item
    assert item["citation"]["coverage"] == _GAPS


def test_disagreeing_field_grades_unmet_naming_missing_and_extra(tmp_path):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    declared = _declared(
        layers_in_stream_without_rules=["70/20"],
        rules_skipped=["met5.4", "met5.5", "met9.1"],
        deck_scope=["5.x", "6.x"],
    )

    item = _item3(
        {"file": drc, "coverage_disclosed": declared},
        require_coverage_disclosure=True,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "coverage_undisclosed"
    assert item["citation"] is None
    assert item["detail"]["coverage_undisclosed"] == {
        "layers_in_stream_without_rules": {
            "missing": ["71/20"],
            "extra": [],
        },
        "rules_skipped": {"extra": ["met9.1"], "missing": []},
    }
    # The unmet item still names what was read (#5479's additive pattern:
    # `citation` is documented as present only on a `met` item).
    assert item["detail"]["cited_evidence"]["file"] == drc
    assert item["detail"]["cited_evidence"]["coverage"] == _GAPS


def test_absent_declaration_with_disclosed_gaps_is_unmet(tmp_path):
    """The issue's observed failure mode: a pointer claim, not an
    enumeration, never grades `met` under the ratchet."""
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    item = _item3(drc, require_coverage_disclosure=True)

    assert item["status"] == "unmet"
    assert item["reason"] == "coverage_undisclosed"
    assert item["detail"]["coverage_undisclosed"] == {
        field: {"missing": sorted(entries), "extra": []}
        for field, entries in _GAPS.items()
    }


def test_no_coverage_block_is_refused_with_that_reason(tmp_path):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_NO_COVERAGE_ENVELOPE)

    item = _item3(drc, require_coverage_disclosure=True)

    assert item["status"] == "unmet"
    assert item["reason"] == "coverage_undisclosed"
    assert item["detail"]["coverage_undisclosed"] == {"reason": "no_coverage_block"}
    assert "coverage" not in item["detail"]["cited_evidence"]


def test_all_empty_fields_with_no_declaration_stay_met(tmp_path):
    """Nothing was disclosed because there is nothing to disclose."""
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_ENVELOPE)

    item = _item3(drc, require_coverage_disclosure=True)

    assert item["status"] == "met"
    assert "detail" not in item


@pytest.mark.parametrize("value", ["true", 1, 0, {}, []])
def test_non_boolean_ratchet_degrades_only_item_3(tmp_path, value):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    def report(**overrides):
        return build_tier_report(_manifest(evidence={"3": drc}, **overrides))

    bad = report(require_coverage_disclosure=value)

    item = next(i for i in bad["items"] if i["id"] == 3)
    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert "require_coverage_disclosure" in item["detail"]["problem"]
    # Item 3 alone: every other item grades exactly as with the key absent.
    baseline = report()
    assert [i for i in bad["items"] if i["id"] != 3] == [
        i for i in baseline["items"] if i["id"] != 3
    ]


@pytest.mark.parametrize("value", ["true", 1, 0, {}, []])
def test_non_boolean_ratchet_degrades_with_no_item_3_evidence(tmp_path, value):
    """A mistyped key is item 3's `invalid_evidence` even when item 3 cites
    nothing at all -- reporting `no_evidence` would never surface it."""
    item = _item3(None, require_coverage_disclosure=value)

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert "require_coverage_disclosure" in item["detail"]["problem"]


@pytest.mark.parametrize(
    "declared",
    [
        ["70/20"],
        {"layers_in_stream_without_rules": "70/20"},
        {"layers_in_stream_without_rules": [70]},
        {"deck_scope": ["5.x"], "unknown_field": ["x"]},
    ],
)
def test_malformed_declaration_is_invalid_evidence(tmp_path, declared):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    item = _item3(
        {"file": drc, "coverage_disclosed": declared},
        require_coverage_disclosure=True,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "invalid_evidence"
    assert "coverage_disclosed" in item["detail"]["problem"]


def test_malformed_declaration_with_ratchet_off_is_ignored(tmp_path):
    """Edge-table row 1 is absolute: with the ratchet off no verdict
    changes, so the lazy validation never fires."""
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    evidence = {"3": {"file": drc, "coverage_disclosed": ["not-an-object"]}}

    assert build_tier_report(_manifest(evidence=evidence)) == build_tier_report(
        _manifest(evidence={"3": drc})
    )


def test_deck_scope_entry_compared_verbatim(tmp_path):
    """A non-layer `deck_scope` token (the curated sg13g2 `"x"`, #5723)
    compares exactly like a chapter name -- the ratchet is correct
    whichever way that deck issue lands."""
    envelope = {
        **DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE,
        "coverage": {
            **DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE["coverage"],
            "deck_scope": ["x"],
        },
    }
    drc = _write(tmp_path, "drc.json", envelope)
    declared = _declared(**{**_GAPS, "deck_scope": ["x"]})

    assert (
        _item3(
            {"file": drc, "coverage_disclosed": declared},
            require_coverage_disclosure=True,
        )["status"]
        == "met"
    )
    assert _item3(
        {"file": drc, "coverage_disclosed": _declared(**_GAPS)},
        require_coverage_disclosure=True,
    )["detail"]["coverage_undisclosed"]["deck_scope"] == {
        "extra": ["5.x", "6.x"],
        "missing": ["x"],
    }


def test_ratchet_leaves_a_failing_envelope_reason_untouched(tmp_path):
    """The ratchet only narrows what already passes: a `violations`
    envelope still reports its own reason first."""
    drc = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    item = _item3(drc, require_coverage_disclosure=True)

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"


def test_report_list_member_refusal_names_the_citation(tmp_path):
    """Item 3 may cite one `klt drc` report per cell (#5031): the gate
    applies per member, and the aggregate names the failing one."""
    disclosed = _write(tmp_path, "a.json", DRC_CLEAN_ENVELOPE)
    hidden = _write(tmp_path, "b.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)

    item = _item3(
        [
            {
                "file": disclosed,
                "coverage_disclosed": _declared(
                    layers_in_stream_without_rules=[],
                    rules_skipped=[],
                    deck_scope=[],
                ),
            },
            {"file": hidden},
        ],
        require_coverage_disclosure=True,
    )

    assert item["status"] == "unmet"
    assert item["reason"] == "coverage_undisclosed"
    assert item["detail"]["failed_citations"] == [hidden]
    assert item["detail"]["coverage_undisclosed"]["rules_skipped"]["missing"] == [
        "met5.4",
        "met5.5",
    ]


def test_check_reproduces_a_report_from_the_same_ratcheted_manifest(tmp_path, capsys):
    """AC: the verdict depends on no CLI state -- `--check` re-grades the
    manifest and matches a report committed from it."""
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    manifest = _manifest(
        evidence={"3": {"file": drc, "coverage_disclosed": _declared(**_GAPS)}},
        require_coverage_disclosure=True,
    )
    manifest_path = _write(tmp_path, "manifest.json", manifest)
    committed = _write(tmp_path, "committed.json", build_tier_report(manifest))

    result = check_tier_report(committed, manifest)
    assert result["status"] == "match"

    exit_code = main(["signoff", "--manifest", manifest_path, "--check", committed])
    assert exit_code == 0
    assert "status: match" in capsys.readouterr().out


def test_text_prints_the_undisclosed_entries_under_the_cite_line(tmp_path, capsys):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_WITH_COVERAGE_GAPS_ENVELOPE)
    manifest_path = _write(
        tmp_path,
        "manifest.json",
        _manifest(evidence={"3": {"file": drc}}, require_coverage_disclosure=True),
    )

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    cite = next(i for i, line in enumerate(lines) if line.startswith("cite:"))
    coverage = next(i for i, line in enumerate(lines) if line.startswith("coverage:"))
    undisclosed = next(
        i for i, line in enumerate(lines) if line.startswith("coverage undisclosed:")
    )
    assert cite < coverage < undisclosed
    assert "layers_in_stream_without_rules=2 (70/20, 71/20)" in lines[coverage]
    assert "missing 70/20, 71/20" in lines[undisclosed]
    assert "rules_skipped: missing met5.4, met5.5" in lines[undisclosed]


def test_text_names_a_missing_coverage_block(tmp_path, capsys):
    drc = _write(tmp_path, "drc.json", DRC_CLEAN_NO_COVERAGE_ENVELOPE)
    manifest_path = _write(
        tmp_path,
        "manifest.json",
        _manifest(evidence={"3": drc}, require_coverage_disclosure=True),
    )

    main(["signoff", "--manifest", manifest_path, "--format", "text"])

    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    assert any(
        line.startswith("coverage undisclosed: cited evidence reports no coverage")
        for line in lines
    )
