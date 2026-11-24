"""Composition of a parent block's T1 rows from its sub-block manifests.

Issue #6927: the ``composed_of`` manifest key, the
``met_by_composition`` row status, ``t1_composed_count``, and the rule
that none of it ever counts toward ``tier`` -- the parent's own boundary
is exactly what composed rows do not cover. The canonical fixture and
every variant come from the issue's acceptance criteria; evidence
envelopes and the full-T1 evidence builder are reused from
``test_signoff.py`` so a sub-block grades exactly as it would standalone.
"""

from __future__ import annotations

import pytest

import test_signoff as signoff_fixtures
from klayout_tools.cli import main
from klayout_tools.signoff import (
    _T1_SATISFIED_STATUSES,
    SignoffError,
    build_fleet_report,
    build_tier_report,
    check_tier_report,
)

_write = signoff_fixtures._write
_full_t1_evidence = signoff_fixtures._full_t1_evidence
_DRC_CLEAN = signoff_fixtures.DRC_CLEAN_ENVELOPE
_DRC_VIOLATIONS = signoff_fixtures.DRC_VIOLATIONS_ENVELOPE
_LVS_MATCH = signoff_fixtures.LVS_MATCH_ENVELOPE
_LVS_MISMATCH = signoff_fixtures.LVS_MISMATCH_ENVELOPE
_SIM_PASS = signoff_fixtures.SIM_PASS_ENVELOPE


def _write_sub(tmp_path, name: str, *, drc=_DRC_CLEAN, lvs=_LVS_MATCH, kind="analog"):
    """One sub-block manifest ``name`` citing the given item 3/4 evidence,
    written to a file; the canonical fixture's A and B are two of these."""
    manifest = {
        "block": name,
        "kind": kind,
        "evidence": {
            "3": _write(tmp_path, f"{name}-drc.json", drc),
            "4": _write(tmp_path, f"{name}-lvs.json", lvs),
        },
    }
    return _write(tmp_path, f"sub-{name}.json", manifest)


def _own_evidence_minus(tmp_path, *remove: str) -> dict:
    """The full-T1 evidence fixture with the named items' own citations
    removed -- the parent's "cites own evidence for everything else"."""
    evidence = _full_t1_evidence(tmp_path)
    for item_id in remove:
        del evidence[item_id]
    return evidence


def _parent(composed_of, *, evidence=None, kind="analog") -> dict:
    return {
        "block": "P",
        "kind": kind,
        "evidence": evidence if evidence is not None else {},
        "composed_of": composed_of,
    }


def _item(report, item_id: int, partition=None):
    return next(
        item
        for item in report["items"]
        if item["tier"] == "T1"
        and item["id"] == item_id
        and item.get("partition") == partition
    )


def test_canonical_fixture_composes_items_3_and_4(tmp_path):
    """P cites own evidence for every other applicable T1 item and no own
    item 3/4 evidence; A and B both have items 3 and 4 met."""
    sub_a = _write_sub(tmp_path, "A")
    sub_b = _write_sub(tmp_path, "B")
    parent = _parent([sub_a, sub_b], evidence=_own_evidence_minus(tmp_path, "3", "4"))

    result = build_tier_report(parent)

    for item_id in (3, 4):
        item = _item(result, item_id)
        assert item["status"] == "met_by_composition"
        assert item["reason"] == "composed_from_sub_blocks_only"
        assert item["citation"] is None
        assert item["composed_from"] == [
            {
                "block": "A",
                "source": sub_a,
                "status": "met",
                "content_hash": "sha256:layoutA",
            },
            {
                "block": "B",
                "source": sub_b,
                "status": "met",
                "content_hash": "sha256:layoutA",
            },
        ]
    # Every other T1 item is met by the parent's own evidence...
    assert all(
        _item(result, item_id)["status"] == "met"
        for item_id in (1, 2, 5, 6, 7, 8, 9, 10, 11)
    )
    # ...and none of the composed rows counted toward the tier or the
    # direct-met count (fixture table: tier null, t1_met_count = 11 - 2).
    assert result["tier"] is None
    assert result["t1_met_count"] == 9
    assert result["t1_item_count"] == 11
    assert result["t1_composed_count"] == 2
    # Nothing is unmet, so no unmet-reason rollup renders at all.
    assert "unmet_reason_counts" not in result


def test_canonical_fixture_agrees_across_manifest_and_fleet(tmp_path):
    """One test, both entry points: the same tier, direct count, composed
    count and blocker in `--manifest` JSON and `--fleet` JSON."""
    parent = _parent(
        [_write_sub(tmp_path, "A"), _write_sub(tmp_path, "B")],
        evidence=_own_evidence_minus(tmp_path, "3", "4"),
    )

    tier_report = build_tier_report(parent)
    fleet_row = build_fleet_report({"blocks": [parent]})["blocks"][0]

    assert fleet_row["tier"] == tier_report["tier"] is None
    assert fleet_row["t1_met_count"] == tier_report["t1_met_count"] == 9
    assert fleet_row["t1_composed_count"] == tier_report["t1_composed_count"] == 2
    # The first composed row in render order is the blocker, and its
    # reason says *why* the parent is not T1.
    assert fleet_row["blocking_item"] == {
        "id": 3,
        "title": "DRC clean",
        "partition": None,
        "reason": "composed_from_sub_blocks_only",
    }


def test_canonical_fixture_renders_composed_rows_in_both_text_formats(tmp_path, capsys):
    sub_a = _write_sub(tmp_path, "A")
    sub_b = _write_sub(tmp_path, "B")
    parent_path = _write(
        tmp_path,
        "parent.json",
        _parent([sub_a, sub_b], evidence=_own_evidence_minus(tmp_path, "3", "4")),
    )
    fleet_path = _write(tmp_path, "fleet.json", {"blocks": [parent_path]})

    main(["signoff", "--manifest", parent_path, "--format", "text"])
    out = capsys.readouterr().out
    for item_id in (3, 4):
        assert f"[COMP ] T1 #{item_id}" in out
        assert f"[MET  ] T1 #{item_id}" not in out
    assert f"composed from: A (source={sub_a}, status=met" in out
    assert f"composed from: B (source={sub_b}, status=met" in out
    assert "reason: composed_from_sub_blocks_only" in out
    assert "T1: 9/11 items met, 2 composed (parent boundary uncovered)" in out

    main(["signoff", "--fleet", fleet_path, "--format", "text"])
    fleet_out = capsys.readouterr().out
    assert "[not-T1] P (analog)" in fleet_out
    assert "T1: 9/11 items met, 2 composed (parent boundary uncovered)" in fleet_out
    assert "blocking: #3 DRC clean (reason: composed_from_sub_blocks_only)" in (
        fleet_out
    )


def test_composed_rows_never_count_toward_tier_and_miswired_parent(tmp_path):
    """The case the earlier design would have passed: A and B each pass
    LVS, but the parent's own LVS fails -- the parent stays tier null with
    the failing own citation unmasked and the child results visible."""
    assert _T1_SATISFIED_STATUSES == frozenset({"met", "not_applicable"})

    parent = _parent(
        [_write_sub(tmp_path, "A"), _write_sub(tmp_path, "B")],
        evidence=_own_evidence_minus(tmp_path, "3"),
    )
    parent["evidence"]["4"] = _write(tmp_path, "P-lvs.json", _LVS_MISMATCH)

    result = build_tier_report(parent)

    item_4 = _item(result, 4)
    assert item_4["status"] == "unmet"
    assert item_4["reason"] == "check_failed"
    cited = item_4["detail"]["cited_evidence"]
    assert cited["kind"] == "lvs"
    assert cited["check_status"] == "mismatch"
    assert "composed_from" not in item_4
    item_3 = _item(result, 3)
    assert item_3["status"] == "met_by_composition"
    assert item_3["composed_from"][0]["block"] == "A"
    assert result["tier"] is None
    assert result["t1_composed_count"] == 1


def test_own_passing_item_3_and_4_win_and_reach_tier_t1(tmp_path):
    """A parent that cites its own passing item 3 and 4 composes nothing
    and earns T1 the normal way -- `composed_of` does not override or
    supplement its own evidence."""
    parent = _parent(
        [_write_sub(tmp_path, "A"), _write_sub(tmp_path, "B")],
        evidence=_full_t1_evidence(tmp_path),
    )

    result = build_tier_report(parent)

    for item_id in (3, 4):
        item = _item(result, item_id)
        assert item["status"] == "met"
        assert item["citation"] is not None
        assert "composed_from" not in item
    assert result["tier"] == "T1"
    assert "t1_composed_count" not in result


def test_one_sub_block_unmet_leaves_the_row_unmet_with_detail(tmp_path, capsys):
    """One sub-block unmet at item 3: the parent row stays
    unmet/no_evidence with `detail.composition` naming each sub-block and
    its status there -- and the composed sibling (item 4) is not counted
    as an unmet reason."""
    sub_a = _write_sub(tmp_path, "A")
    sub_b = _write_sub(tmp_path, "B", drc=_DRC_VIOLATIONS)

    parent = _parent([sub_a, sub_b], evidence=_own_evidence_minus(tmp_path, "3", "4"))
    result = build_tier_report(parent)

    item_3 = _item(result, 3)
    assert item_3["status"] == "unmet"
    assert item_3["reason"] == "no_evidence"
    composition = item_3["detail"]["composition"]
    assert composition[0] == {
        "block": "A",
        "source": sub_a,
        "status": "met",
        "content_hash": "sha256:layoutA",
    }
    assert composition[1]["block"] == "B"
    assert composition[1]["status"] == "unmet"
    assert "content_hash" not in composition[1]
    assert _item(result, 4)["status"] == "met_by_composition"
    assert result["tier"] is None
    assert result["t1_composed_count"] == 1
    # Composed rows are not "unmet", so the reason rollup counts only the
    # genuinely-unmet item 3 -- decided and intended (issue #6927 AC).
    assert result["unmet_reason_counts"] == {"no_evidence": 1}

    parent_path = _write(tmp_path, "parent.json", parent)
    main(["signoff", "--manifest", parent_path, "--format", "text"])
    text = capsys.readouterr().out
    item_3_text, item_4_text = text.split("[UNMET] T1 #3 ", 1)[1].split(
        "[COMP ] T1 #4 ", 1
    )
    assert "reason: no_evidence" in item_3_text
    assert [
        line.strip() for line in item_3_text.splitlines() if "sub-block:" in line
    ] == [
        f"sub-block: A (source={sub_a}, status=met, content_hash=sha256:layoutA)",
        f"sub-block: B (source={sub_b}, status=unmet)",
    ]
    assert "composed from:" not in item_3_text
    assert item_4_text.count("composed from:") == 2
    assert text.count("sub-block:") == 2


def test_items_outside_3_and_4_are_never_composed(tmp_path):
    """Both sub-blocks have item 5 met; the parent, citing no own item 5
    evidence, still grades it unmet/no_evidence with no composition
    detail -- v1 composes only items 3 and 4."""
    subs = []
    for name in ("A", "B"):
        manifest = {
            "block": name,
            "kind": "analog",
            "evidence": {
                "3": _write(tmp_path, f"{name}-drc.json", _DRC_CLEAN),
                "4": _write(tmp_path, f"{name}-lvs.json", _LVS_MATCH),
                "5": _write(tmp_path, f"{name}-sim.json", _SIM_PASS),
            },
        }
        subs.append(_write(tmp_path, f"sub-{name}.json", manifest))

    result = build_tier_report(
        _parent(subs, evidence=_own_evidence_minus(tmp_path, "3", "4", "5"))
    )

    item_5 = _item(result, 5)
    assert item_5["status"] == "unmet"
    assert item_5["reason"] == "no_evidence"
    assert "composed_from" not in item_5
    assert "composition" not in (item_5.get("detail") or {})
    assert _item(result, 3)["status"] == "met_by_composition"
    assert result["t1_composed_count"] == 2


def test_inline_sub_block_entry_composes_and_renders_inline(tmp_path, capsys):
    """An inline `composed_of` entry (the `--fleet` blocks shape) composes
    the same way, with `source: null` rendered as `<inline>`."""
    inline = {
        "block": "A",
        "kind": "analog",
        "evidence": {
            "3": _write(tmp_path, "A-drc.json", _DRC_CLEAN),
            "4": _write(tmp_path, "A-lvs.json", _LVS_MATCH),
        },
    }

    result = build_tier_report(_parent([inline]))
    assert _item(result, 3)["composed_from"] == [
        {
            "block": "A",
            "source": None,
            "status": "met",
            "content_hash": "sha256:layoutA",
        }
    ]

    parent_path = _write(tmp_path, "parent.json", _parent([inline]))
    main(["signoff", "--manifest", parent_path, "--format", "text"])
    assert "composed from: A (source=<inline>, status=met" in (capsys.readouterr().out)


def test_reports_without_composition_are_byte_identical(tmp_path):
    """`t1_composed_count` is omitted when zero, so existing manifest and
    fleet output carries no new key a committed report would drift on."""
    plain = {"block": "solo", "kind": "analog", "evidence": {}}

    result = build_tier_report(plain)
    assert "t1_composed_count" not in result
    fleet_row = build_fleet_report({"blocks": [plain]})["blocks"][0]
    assert "t1_composed_count" not in fleet_row


def test_check_matches_and_flips_when_sub_block_evidence_edits(tmp_path):
    """`--check` against a committed composed report returns match; editing
    a sub-block's evidence flips it to drift -- the sub-block manifests are
    re-read and re-graded, so drift reproduces with no new mechanism."""
    sub_a = _write_sub(tmp_path, "A")
    sub_b = _write_sub(tmp_path, "B")
    parent = _parent([sub_a, sub_b], evidence=_own_evidence_minus(tmp_path, "3", "4"))
    committed_path = _write(tmp_path, "tier-report.json", build_tier_report(parent))

    assert check_tier_report(committed_path, parent)["status"] == "match"

    _write(tmp_path, "A-drc.json", _DRC_VIOLATIONS)  # sub A's item 3 fails
    result = check_tier_report(committed_path, parent)

    assert result["status"] == "drifted"
    fields = {entry["field"] for entry in result["drift"]}
    assert "items.2.status" in fields  # item 3: composed -> unmet
    assert "items.2.composed_from" in fields
    assert "t1_composed_count" in fields


@pytest.mark.parametrize(
    ("composed_of", "match"),
    [
        ([], "'composed_of' must be a non-empty JSON array"),
        # An explicit null is a present-but-malformed key, not an absent one.
        (None, "'composed_of' must be a non-empty JSON array"),
        ("manifests/sub-A.json", "'composed_of' must be a non-empty JSON array"),
        ([42], r"composed_of\[0\] must be a JSON object or a file path string"),
        (
            [{"kind": "analog", "evidence": {}}],
            r"composed_of\[0\] resolves to a manifest with no non-empty "
            "'block' name",
        ),
        (
            [{"block": "A", "kind": "digital", "evidence": {}}],
            r"composed_of\[0\] \(block 'A'\) has kind 'digital', which does "
            "not match the parent's kind 'analog'",
        ),
        (
            [{"block": "A", "kind": "analog", "composed_of": []}],
            r"composed_of\[0\] \(block 'A'\) declares its own 'composed_of'",
        ),
        (
            [{"block": "A", "kind": "analog", "composed_of": None}],
            r"composed_of\[0\] \(block 'A'\) declares its own 'composed_of'",
        ),
    ],
)
def test_malformed_composition_aborts_naming_the_entry(composed_of, match):
    with pytest.raises(SignoffError, match=match):
        build_tier_report(_parent(composed_of))


def test_mixed_signal_parent_composes_per_partition(tmp_path):
    """A mixed-signal parent keys composition on (id, partition): a
    sub-block missing one partition's evidence leaves exactly that
    partition row unmet with the per-sub-block detail."""
    full = {
        "block": "A",
        "kind": "mixed-signal",
        "evidence": {
            "3.analog": _write(tmp_path, "a-drc.json", _DRC_CLEAN),
            "3.digital": _write(tmp_path, "a-drc.json", _DRC_CLEAN),
            "4.analog": _write(tmp_path, "a-lvs.json", _LVS_MATCH),
            "4.digital": _write(tmp_path, "a-lvs.json", _LVS_MATCH),
        },
    }
    half = {
        "block": "B",
        "kind": "mixed-signal",
        "evidence": {
            "3.analog": _write(tmp_path, "b-drc.json", _DRC_CLEAN),
            "4.analog": _write(tmp_path, "b-lvs.json", _LVS_MATCH),
        },
    }

    result = build_tier_report(_parent([full, half], kind="mixed-signal"))

    analog_3 = _item(result, 3, "analog")
    assert analog_3["status"] == "met_by_composition"
    assert {entry["block"] for entry in analog_3["composed_from"]} == {"A", "B"}
    digital_3 = _item(result, 3, "digital")
    assert digital_3["status"] == "unmet"
    assert digital_3["reason"] == "no_evidence"
    statuses = {
        entry["block"]: entry["status"] for entry in digital_3["detail"]["composition"]
    }
    assert statuses == {"A": "met", "B": "unmet"}
