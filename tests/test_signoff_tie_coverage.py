"""Item 11 per-class tie coverage (issue #6987).

Split out of ``test_signoff.py`` to respect the per-file size ratchet
(same precedent as ``test_signoff_substrate_item11.py``): before #6987 the
item was gated on ``erc.missing_tie`` *firing* but not on it having *run*
for every declared class, so deleting a tie class from
``erc_coverage.checked`` -- with an empty ``skipped`` list and zero
findings -- left the item ``met`` off an envelope that carries no verdict
for one of its required rules at all.
"""

from __future__ import annotations

import pytest

from klayout_tools.coverage import work_id
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    ERC_BOX_SELECTED_WELL_CLASS_SPEC,
    ERC_CLEAN_ENVELOPE,
    _erc_coverage_block,
    _item_11,
    _manifest,
    _power_delivery_evidence,
)

#: The work identities `klt erc` records for the two ties
#: `ERC_BOX_SELECTED_WELL_CLASS_SPEC` declares, spelled once so every test
#: below compares against the exact producer form.
_BOX_SELECTED_TIE_IDS = {
    "device_body_wells": 'erc.missing_tie:["device_body_wells"]',
    "bias_tub": 'erc.missing_tie:["bias_tub"]',
}


@pytest.mark.parametrize("kind", ["analog", "digital"])
def test_item_11_unmet_when_a_declared_tie_class_is_absent_from_checked(tmp_path, kind):
    """Issue #6987: a `met` grade was gated on `erc.missing_tie` *firing*
    but not on it having *run* for every declared class -- deleting a tie
    class from `erc_coverage.checked` (with nothing in `skipped` and zero
    findings) left the item `met`, the exact false pass of record the issue
    measured. Both branches of the item read the same ERC half, so the
    identical citation is exercised through each.

    The partial case keeps `missing_tie_checked: true` and another checked
    identity in the list, so only a check comparing the *exact* per-class
    work identities -- not a count, a summary flag, or "some tie ran" --
    can catch it."""
    unchecked = ["bias_tub"]
    checked = [
        _BOX_SELECTED_TIE_IDS["device_body_wells"],
        'erc.net_connectivity:["VPWR"]',
    ]
    envelope = {
        **ERC_CLEAN_ENVELOPE,
        "erc_coverage": _erc_coverage_block(
            checked=checked,
            missing_tie_checked=True,
        ),
    }
    result = build_tier_report(
        _manifest(
            kind=kind,
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path,
                    kind=kind,
                    erc_envelope=envelope,
                    erc_spec=ERC_BOX_SELECTED_WELL_CLASS_SPEC,
                )
            },
        )
    )

    item = _item_11(result)
    assert item["status"] == "unmet"
    assert item["reason"] == "tie_coverage_incomplete"
    assert item["detail"] == {"ties_unchecked": unchecked}
    assert item["citation"] is None


def test_item_11_unmet_when_every_declared_tie_class_is_absent_from_checked(
    tmp_path,
):
    """The total-removal row of issue #6987's table: no `erc.missing_tie`
    identity anywhere in `checked`, an empty `skipped` list, zero findings
    -- the cited envelope carries no verdict for the item's required rule
    at all, so nothing in it is positive evidence."""
    envelope = {
        **ERC_CLEAN_ENVELOPE,
        "erc_coverage": _erc_coverage_block(checked=[]),
    }
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path,
                    kind="analog",
                    erc_envelope=envelope,
                    erc_spec=ERC_BOX_SELECTED_WELL_CLASS_SPEC,
                )
            },
        )
    )

    item = _item_11(result)
    assert item["status"] == "unmet"
    assert item["reason"] == "tie_coverage_incomplete"
    assert item["detail"] == {"ties_unchecked": ["device_body_wells", "bias_tub"]}
    assert item["citation"] is None


@pytest.mark.parametrize(
    "checked,skipped,assertion_list",
    [
        # A duplicate ID cannot stand in for the second class.
        (
            [
                _BOX_SELECTED_TIE_IDS["device_body_wells"],
                _BOX_SELECTED_TIE_IDS["device_body_wells"],
            ],
            [],
            [],
        ),
        # A different (undeclared) class cannot either.
        (
            [
                _BOX_SELECTED_TIE_IDS["device_body_wells"],
                'erc.missing_tie:["nwell_tie"]',
            ],
            [],
            [],
        ),
        # Neither can an assertion-list entry alone: the producer records
        # an asserted tie in `checked` *and* `checked_by_well_assertion`,
        # so the assertion list is never evidence a class ran.
        (
            [_BOX_SELECTED_TIE_IDS["device_body_wells"]],
            [],
            [_BOX_SELECTED_TIE_IDS["bias_tub"]],
        ),
        # The literal move of the original report -- a bare string in
        # `skipped`, which the explicit-skip gate cannot match (it reads
        # `{id, reason}` records) and the coverage gate must.
        (
            [_BOX_SELECTED_TIE_IDS["device_body_wells"]],
            [_BOX_SELECTED_TIE_IDS["bias_tub"]],
            [],
        ),
    ],
)
def test_item_11_tie_coverage_gate_rejects_weaker_substitutes(
    tmp_path, checked, skipped, assertion_list
):
    """Issue #6987: only the declared class's exact work identity in
    `checked` is positive evidence it ran. A duplicate of another class's
    ID, a different class's ID, an assertion-list entry, or the class moved
    verbatim into `skipped` as a bare string (the form the original report
    literally measured) each leave a declared class with no verdict, and
    the item `unmet` naming which."""
    envelope = {
        **ERC_CLEAN_ENVELOPE,
        "erc_coverage": _erc_coverage_block(
            checked=checked,
            skipped=skipped,
            checked_by_well_assertion=assertion_list,
            missing_tie_checked=bool(
                any(i.startswith("erc.missing_tie:") for i in checked)
            ),
        ),
    }
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path,
                    kind="analog",
                    erc_envelope=envelope,
                    erc_spec=ERC_BOX_SELECTED_WELL_CLASS_SPEC,
                )
            },
        )
    )

    item = _item_11(result)
    assert item["status"] == "unmet"
    assert item["reason"] == "tie_coverage_incomplete"
    assert item["detail"] == {"ties_unchecked": ["bias_tub"]}
    assert item["citation"] is None


def test_item_11_tie_coverage_gate_reads_no_usable_checked_list(tmp_path):
    """A coverage block whose `checked` is not a list supplies no positive
    evidence for any class -- never a default-clean read (issue #6987)."""
    block = {
        key: value for key, value in _erc_coverage_block().items() if key != "checked"
    }
    envelope = {**ERC_CLEAN_ENVELOPE, "erc_coverage": block}
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path,
                    kind="analog",
                    erc_envelope=envelope,
                    erc_spec=ERC_BOX_SELECTED_WELL_CLASS_SPEC,
                )
            },
        )
    )

    item = _item_11(result)
    assert item["status"] == "unmet"
    assert item["reason"] == "tie_coverage_incomplete"
    assert item["detail"] == {"ties_unchecked": ["device_body_wells", "bias_tub"]}
    assert item["citation"] is None


def test_item_11_tie_coverage_gate_preserves_the_legacy_no_coverage_block_path(
    tmp_path,
):
    """The compatibility path of record (issue #6987): an older envelope
    that omits the entire `erc_coverage` block -- every report before
    #2179 -- is graded exactly as it always was, exactly as the
    explicit-skip and disclosure gates before this one already promise."""
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path,
                    kind="analog",
                    erc_envelope=ERC_CLEAN_ENVELOPE,
                    erc_spec=ERC_BOX_SELECTED_WELL_CLASS_SPEC,
                )
            },
        )
    )

    assert _item_11(result)["status"] == "met"


@pytest.mark.parametrize(
    ("name_field", "identity"),
    [({}, "tie1"), ({"name": None}, "None"), ({"name": 3}, "3"), ({"name": ""}, "")],
)
@pytest.mark.parametrize("covered", [False, True])
def test_item_11_checks_the_producers_tie_identity(
    tmp_path, name_field, identity, covered
):
    first, second = ERC_BOX_SELECTED_WELL_CLASS_SPEC["ties"]
    second = {key: value for key, value in second.items() if key != "name"}
    spec = {
        **ERC_BOX_SELECTED_WELL_CLASS_SPEC,
        "ties": [first, {**second, **name_field}],
    }
    checked = [work_id("erc.missing_tie", first["name"])]
    if covered:
        checked.append(work_id("erc.missing_tie", identity))
    envelope = {
        **ERC_CLEAN_ENVELOPE,
        "erc_coverage": _erc_coverage_block(checked=checked, missing_tie_checked=True),
    }
    evidence = _power_delivery_evidence(
        tmp_path, kind="analog", erc_envelope=envelope, erc_spec=spec
    )
    item = _item_11(
        build_tier_report(_manifest(kind="analog", evidence={"11": evidence}))
    )
    if covered:
        assert item["status"] == "met"
    else:
        assert item["reason"] == "tie_coverage_incomplete"
        assert item["detail"] == {"ties_unchecked": [identity]}
