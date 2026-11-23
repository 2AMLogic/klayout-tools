"""Issue #5597: T1 item 11 accepts a **grouped** citation -- a list whose
every element is itself a compound ``(erc, lvs)`` citation -- and grades one
group per independently signed-off stream, aggregating to ``met`` only when
every group grades ``met``. Landed outside ``tests/test_signoff.py`` per the
per-file size ratchet (see ``scripts/README.md``), like
``test_signoff_multi_member.py`` before it."""

from __future__ import annotations

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    DRC_CLEAN_ENVELOPE,
    ERC_CLEAN_ENVELOPE,
    ERC_MISSING_TIE_ENVELOPE,
    ERC_SUPPLY_SPEC,
    LVS_MATCH_POWER_MATCH_ENVELOPE,
    _erc_evidence,
    _item_11,
    _manifest,
    _power_delivery_evidence,
    _write,
)

# --------------------------------------------------------------------------- #
# T1 item 11: grouped citations (issue #5597)
#
# A partition whose physical evidence is several independent streams needs
# one `(erc, lvs)` group per stream. The flat form resolves to one entry per
# kind -- silently dropping every but the last (an order-dependent false
# `met`) -- so the grouped form is a list whose every element is itself a
# compound citation, graded independently by exactly item 11's own rules.
# --------------------------------------------------------------------------- #


def test_item_11_grouped_citation_met_when_every_group_passes(tmp_path):
    clean = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")
    digital = _power_delivery_evidence(tmp_path, kind="digital", prefix="d")

    result = build_tier_report(
        _manifest(kind="analog", evidence={"11": [clean, digital]})
    )

    item = _item_11(result)
    assert item["status"] == "met"
    assert item["reason"] is None
    # The leading group keeps the single-citation contract every existing
    # consumer reads: its own parts, its own power_delivery block.
    assert item["citation"]["kind"] == "erc"
    assert [part["file"] for part in item["citation"]["parts"]] == clean
    assert item["citation"]["power_delivery"]["pdn"] is False
    # One compound citation per group, each with its own per-stream facts.
    assert len(item["citations"]) == 2
    assert item["citations"][0]["power_delivery"]["pdn"] is False
    assert item["citations"][1]["power_delivery"]["pdn"] is True
    assert [part["kind"] for part in item["citations"][1]["parts"]] == [
        "erc",
        "lvs",
        "place-and-route",
    ]
    # Per-group grading, named.
    assert item["citations_graded"] == [
        {
            "citation": clean[0],
            "status": "met",
            "reason": None,
            "content_hash": "sha256:layoutA",
            "input_verified": None,
        },
        {
            "citation": digital[0],
            "status": "met",
            "reason": None,
            "content_hash": "sha256:layoutA",
            "input_verified": None,
        },
    ]
    assert item["members_covered"] == item["members_total"] == 2


def test_item_11_grouped_citation_failing_group_is_unmet_in_both_orders(tmp_path):
    """The order-dependent false `met` is the defect this closes: a failing
    group cited *first*, beside a clean group, previously lost to last-wins
    and graded `met`. Either order must now render `unmet` with the failing
    group named."""
    failing = _power_delivery_evidence(
        tmp_path, kind="analog", prefix="b", erc_envelope=ERC_MISSING_TIE_ENVELOPE
    )
    clean = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")

    for groups in ([failing, clean], [clean, failing]):
        result = build_tier_report(_manifest(kind="analog", evidence={"11": groups}))

        item = _item_11(result)
        assert item["status"] == "unmet"
        assert item["reason"] == "supply_not_continuous"
        assert item["citation"] is None
        assert item["detail"]["failed_citations"] == [failing[0]]
        assert item["members_covered"] == 1
        assert item["members_total"] == 2
        failing_graded = {
            "citation": failing[0],
            "status": "unmet",
            "reason": "supply_not_continuous",
            "content_hash": "sha256:layoutA",
            "input_verified": None,
        }
        clean_graded = {
            "citation": clean[0],
            "status": "met",
            "reason": None,
            "content_hash": "sha256:layoutA",
            "input_verified": None,
        }
        expected_graded = (
            [failing_graded, clean_graded]
            if groups[0] is failing
            else [clean_graded, failing_graded]
        )
        assert item["citations_graded"] == expected_graded
        # Every cited part stays accounted for across `citations`: a
        # resolved group carries its parts even when its rules failed, so
        # no cited envelope can silently vanish from the report of record.
        assert len(item["citations"]) == 2
        assert [part["file"] for part in item["citations"][0]["parts"]] == groups[0]
        assert [part["file"] for part in item["citations"][1]["parts"]] == groups[1]


def test_item_11_nested_one_group_matches_flat_one_group(tmp_path):
    """A one-group citation -- nested or flat -- must grade byte-identically
    to today: none of the grouped keys appears, and the rendering is
    unchanged."""
    group = _power_delivery_evidence(tmp_path, kind="digital", prefix="one")

    flat_item = _item_11(
        build_tier_report(_manifest(kind="digital", evidence={"11": group}))
    )
    nested_item = _item_11(
        build_tier_report(_manifest(kind="digital", evidence={"11": [group]}))
    )

    assert nested_item == flat_item
    assert nested_item["status"] == "met"
    assert "citations" not in nested_item
    assert "citations_graded" not in nested_item
    assert "members_covered" not in nested_item
    assert "members_total" not in nested_item


def test_item_11_mixed_outer_list_is_invalid_evidence(tmp_path):
    group = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")

    result = build_tier_report(
        _manifest(kind="analog", evidence={"11": [group, group[0]]})
    )

    assert _item_11(result)["reason"] == "invalid_evidence"


def test_item_11_empty_inner_group_is_invalid_evidence(tmp_path):
    group = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")

    result = build_tier_report(_manifest(kind="analog", evidence={"11": [group, []]}))

    assert _item_11(result)["reason"] == "invalid_evidence"


def test_item_11_duplicate_kind_within_one_group_is_invalid_evidence(tmp_path):
    """Two parts of the same kind in one group is the manifest authoring
    mistake `_resolve_power_delivery_parts`' own docstring names -- and
    last-wins was order-dependent: a failing stream cited first beside a
    clean one graded `met` on the clean one alone. Once the grouped form
    exists the mistake is unambiguous, so it renders `invalid_evidence`
    instead of resolving to the last one named."""
    group = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")
    duplicate_erc = _erc_evidence(
        tmp_path, ERC_CLEAN_ENVELOPE, ERC_SUPPLY_SPEC, prefix="b-erc"
    )

    for entry in (group + [duplicate_erc], [group + [duplicate_erc]]):
        result = build_tier_report(_manifest(kind="analog", evidence={"11": entry}))

        assert _item_11(result)["reason"] == "invalid_evidence"


def test_item_11_grouped_citation_missing_lvs_part_fails_that_group(tmp_path):
    erc_only = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")[:1]
    clean = _power_delivery_evidence(tmp_path, kind="analog", prefix="b")

    result = build_tier_report(
        _manifest(kind="analog", evidence={"11": [erc_only, clean]})
    )

    item = _item_11(result)
    assert item["status"] == "unmet"
    assert item["reason"] == "power_delivery_parts_missing"
    assert item["detail"]["failed_citations"] == [erc_only[0]]


def test_item_11_grouped_citation_stale_part_in_non_leading_group_fails(tmp_path):
    stale = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")
    pinned = [
        {"file": stale[0], "content_hash": "sha256:a-different-layout"},
        stale[1],
    ]
    clean = _power_delivery_evidence(tmp_path, kind="analog", prefix="b")

    result = build_tier_report(
        _manifest(kind="analog", evidence={"11": [clean, pinned]})
    )

    item = _item_11(result)
    assert item["status"] == "unmet"
    assert item["reason"] == "stale_evidence"


def test_cli_item_11_text_output_states_group_coverage(tmp_path, capsys):
    """Issue #5597: a multi-group citation's group count is not JSON-only --
    the text rendering states how many groups were cited and how many
    graded met. A single-group citation renders exactly as before."""
    clean = _power_delivery_evidence(tmp_path, kind="analog", prefix="a")
    failing = _power_delivery_evidence(
        tmp_path, kind="analog", prefix="b", erc_envelope=ERC_MISSING_TIE_ENVELOPE
    )
    manifest_path = _write(
        tmp_path,
        "manifest.json",
        _manifest(kind="analog", evidence={"11": [clean, failing]}),
    )

    main(["signoff", "--manifest", manifest_path])

    out = capsys.readouterr().out
    assert "groups: 1/2 cited groups graded met" in out

    single_path = _write(
        tmp_path,
        "manifest-single.json",
        _manifest(kind="analog", evidence={"11": clean}),
    )
    main(["signoff", "--manifest", single_path])

    out = capsys.readouterr().out
    assert "groups:" not in out


def test_cli_multi_member_item_3_and_4_text_rendering_has_no_groups_line(
    tmp_path, capsys
):
    """Issue #5597 regression pin: the `groups:` coverage line is item 11's
    alone. Items 3 and 4 have carried JSON member counts since issue #5031,
    and their text rendering must not change -- their members are per-cell
    reports, not citation groups, so a "cited groups graded met" line would
    misdescribe what was cited."""
    drc_paths = [_write(tmp_path, f"{n}.json", DRC_CLEAN_ENVELOPE) for n in "abc"]
    lvs_paths = [
        _write(tmp_path, f"{n}-lvs.json", LVS_MATCH_POWER_MATCH_ENVELOPE) for n in "de"
    ]

    for item_id, paths in ((3, drc_paths), (4, lvs_paths)):
        manifest_path = _write(
            tmp_path,
            f"manifest-{item_id}.json",
            _manifest(kind="analog", evidence={str(item_id): paths}),
        )
        main(["signoff", "--manifest", manifest_path])

        out = capsys.readouterr().out
        assert f"T1 #{item_id}" in out
        assert "groups:" not in out
