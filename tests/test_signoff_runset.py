"""``klt signoff`` grading of a PDK-runset LVS report (issue #2870).

The runset fixture installs its hash in the signoff-side pin allowlist.
"""

from __future__ import annotations

import copy
import hashlib
from unittest.mock import Mock

import pytest

from klayout_tools import signoff_runset
from klayout_tools.signoff import build_tier_report, describe_grader
from test_signoff import (
    LVS_MATCH_ENVELOPE,
    LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE,
    _item_11,
    _manifest,
    _power_delivery_evidence,
    _write,
)


def _sha(path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def runset(tmp_path, monkeypatch):
    """A real runset file on disk and its hash installed in the pin table."""
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# the PDK's own runset\n")
    monkeypatch.setitem(
        signoff_runset.PINNED_RUNSETS,
        _sha(deck),
        {"pdk": "ihp-sg13g2", "revision": "test-rev"},
    )
    return deck


def _runset_envelope(base: dict, deck, **overrides) -> dict:
    envelope = copy.deepcopy(base)
    envelope.update(
        {
            "engine": "klayout-runset",
            "deck_file": str(deck),
            "coverage": {"declared_not_recognized": []},
            "device_census": {
                "layout": {"NMOS": 2},
                "reference": {"NMOS": 2},
                # Issue #6902: the engine's reference-source evidence.
                "reference_source": {"NMOS": 2},
            },
        }
    )
    envelope["provenance"]["runset"] = {"content_hash": _sha(deck)}
    envelope.update(overrides)
    return envelope


def _item_4(tmp_path, envelope) -> dict:
    path = _write(tmp_path, "lvs.json", envelope)
    result = build_tier_report(_manifest(evidence={"4": path}))
    return next(item for item in result["items"] if item["id"] == 4)


def test_pinned_runset_report_is_met_and_names_its_engine(tmp_path, runset):
    item = _item_4(tmp_path, _runset_envelope(LVS_MATCH_ENVELOPE, runset))

    assert item["status"] == "met", item
    assert item["citation"]["engine"] == "klayout-runset"
    assert item["citation"]["runset_content_hash"] == _sha(runset)
    assert item["citation"]["pdk_pin"]["pdk"] == "ihp-sg13g2"


def _lvs_items(tmp_path, envelope) -> tuple[dict, dict]:
    parts = _power_delivery_evidence(tmp_path, lvs_envelope=envelope)
    result = build_tier_report(_manifest(evidence={"4": parts[1], "11": parts}))
    return next(item for item in result["items"] if item["id"] == 4), _item_11(result)


def _lvs_citations(items: tuple[dict, dict]) -> tuple[dict, dict]:
    item_4, item_11 = items
    assert item_4["status"] == "met", item_4
    assert item_11["status"] == "met", item_11
    compound = item_11["citation"]
    assert "runset_rehash_status" not in compound
    assert "runset_rehash_status" not in compound["parts"][0]
    lvs_part = next(part for part in compound["parts"] if part["kind"] == "lvs")
    return item_4["citation"], lvs_part


def test_relative_runset_is_rehashed_from_signoff_cwd(tmp_path, runset, monkeypatch):
    envelope = _runset_envelope(
        LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset, deck_file=runset.name
    )
    monkeypatch.chdir(runset.parent)
    matching = _lvs_citations(_lvs_items(tmp_path, envelope))
    assert all("runset_rehash_status" not in citation for citation in matching)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    unavailable = _lvs_citations(_lvs_items(tmp_path, envelope))
    assert unavailable == tuple(
        {**citation, "runset_rehash_status": "unverifiable"} for citation in matching
    )

    (other_dir / runset.name).write_text("# unrelated runset\n")
    for item in _lvs_items(tmp_path, envelope):
        assert item["status"] == "unmet", item
        assert item["reason"] == "runset_hash_mismatch"
        assert item["citation"] is None


@pytest.mark.parametrize("path_case", ["missing-absolute", "absent", "non-string"])
def test_unavailable_runset_path_is_disclosed(tmp_path, runset, path_case):
    envelope = _runset_envelope(LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset)
    if path_case == "missing-absolute":
        envelope["deck_file"] = str(tmp_path / "missing.lvs")
    elif path_case == "absent":
        del envelope["deck_file"]
    else:
        envelope["deck_file"] = 42

    for citation in _lvs_citations(_lvs_items(tmp_path, envelope)):
        assert citation["runset_rehash_status"] == "unverifiable"


def test_runset_read_error_is_disclosed_from_one_attempt_per_item(
    tmp_path, runset, monkeypatch
):
    envelope = _runset_envelope(LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset)
    digest = Mock(side_effect=OSError("cannot read runset"))
    monkeypatch.setattr(signoff_runset, "sha256_file", digest)

    for citation in _lvs_citations(_lvs_items(tmp_path, envelope)):
        assert citation["runset_rehash_status"] == "unverifiable"
    assert digest.call_count == 2


def test_curated_report_citation_is_unchanged(tmp_path):
    for citation in _lvs_citations(
        _lvs_items(tmp_path, LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE)
    ):
        assert "engine" not in citation
        assert "runset_rehash_status" not in citation


def test_unpinned_runset_is_refused(tmp_path, runset, monkeypatch):
    monkeypatch.setattr(signoff_runset, "PINNED_RUNSETS", {})
    digest = Mock(side_effect=AssertionError("an unpinned deck must not be read"))
    monkeypatch.setattr(signoff_runset, "sha256_file", digest)

    envelope = _runset_envelope(LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset)
    for item in _lvs_items(tmp_path, envelope):
        assert item["status"] == "unmet", item
        assert item["reason"] == "runset_not_pinned"
        assert item["citation"] is None
    digest.assert_not_called()


def test_runset_file_drift_with_pinned_recorded_hash_is_a_mismatch(tmp_path, runset):
    envelope = _runset_envelope(LVS_MATCH_ENVELOPE, runset)
    runset.write_text("# edited after the run\n")
    # The recorded hash is still the pinned one; only the file moved.
    assert envelope["provenance"]["runset"]["content_hash"] in (
        signoff_runset.PINNED_RUNSETS
    )

    item = _item_4(tmp_path, envelope)

    assert item["reason"] == "runset_hash_mismatch"


def test_zero_instance_declared_classes_do_not_fail_a_matching_report(tmp_path, runset):
    """Issue #6902 corrects the #2870 proxy: ``declared_not_recognized``
    names classes the runset declares with zero instances *anywhere* (the
    layout, the converted reference and the reference source all lack them),
    which no real block can avoid on a PDK runset that declares the whole
    device list. Disclosure, not a failure."""
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        deck_file=str(tmp_path / "missing.lvs"),
        coverage={"declared_not_recognized": ["RES", "sky130_fd_pr__cap_var_hvt"]},
    )

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "met", item


def test_report_without_reference_source_evidence_fails_closed(tmp_path, runset):
    """The both-sides-drop guard needs positive evidence a pre-#6902 report
    does not carry; without it the report cannot prove the runset's reader
    kept every reference device (issue #6902's fail-closed decision)."""
    envelope = _runset_envelope(LVS_MATCH_ENVELOPE, runset)
    del envelope["device_census"]["reference_source"]

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_unrecognised_device"
    assert any(
        "re-run 'klt lvs'" in gap for gap in item["detail"]["runset_device_gaps"]
    ), item["detail"]


@pytest.mark.parametrize(
    ("side", "counts"),
    [
        ("layout", []),
        ("reference", {"NMOS": 2, "CAP": True}),
        ("reference_source", {"NMOS": 2, "CAP": "3"}),
        ("layout", {"NMOS": 2, "CAP": -1}),
    ],
)
def test_invalid_census_cannot_prove_device_recognition(tmp_path, runset, side, counts):
    envelope = _runset_envelope(LVS_MATCH_ENVELOPE, runset)
    envelope["device_census"][side] = counts

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_unrecognised_device"
    assert any(
        f"device_census.{side}" in gap for gap in item["detail"]["runset_device_gaps"]
    )


def test_census_disagreement_fails_even_with_match_status(tmp_path, runset):
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        device_census={
            "layout": {"NMOS": 3},
            "reference": {"NMOS": 2},
            "reference_source": {"NMOS": 2},
        },
    )

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_unrecognised_device"


@pytest.mark.parametrize("converted", [2, 3])
def test_reference_class_counted_only_in_the_source_fails(tmp_path, runset, converted):
    """Issue #6902's both-sides-drop case: the reference source names a
    device the runset's reader dropped, so it survives in neither census and
    layout and converted reference agree. Three converted NMOS also make the
    total equal, disguising the missing resistor."""
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        device_census={
            "layout": {"NMOS": converted},
            "reference": {"NMOS": converted},
            "reference_source": {"NMOS": 2, "RES_XHIGH_PO": 1},
        },
    )

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_unrecognised_device"
    assert any(
        "RES_XHIGH_PO" in gap and "dropped on both sides" in gap
        for gap in item["detail"]["runset_device_gaps"]
    ), item["detail"]


def test_census_compare_is_case_insensitive(tmp_path, runset):
    """A sky130 ``convert_subckts`` report reads its layout census lower and
    its reference census upper; equal counts must agree (issue #6902)."""
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        device_census={
            "layout": {"sky130_fd_pr__pfet_g5v0d10v5": 36},
            "reference": {"SKY130_FD_PR__PFET_G5V0D10V5": 36},
            "reference_source": {"SKY130_FD_PR__PFET_G5V0D10V5": 36},
        },
    )

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "met", item


def test_item_11_accepts_runset_report_with_populated_net_correspondence(
    tmp_path, runset
):
    envelope = _runset_envelope(LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset)
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path, kind="analog", lvs_envelope=envelope
                )
            },
        )
    )

    item = _item_11(result)
    assert item["status"] == "met", item


def test_item_11_runset_report_with_empty_net_correspondence_is_unproven(
    tmp_path, runset
):
    envelope = _runset_envelope(
        LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE,
        runset,
        deck_file=str(tmp_path / "missing.lvs"),
        net_correspondence=[],
    )
    result = build_tier_report(
        _manifest(
            kind="analog",
            evidence={
                "11": _power_delivery_evidence(
                    tmp_path, kind="analog", lvs_envelope=envelope
                )
            },
        )
    )

    assert _item_11(result)["reason"] == "lvs_supply_unproven"


SG13G2_RUNSET_HASH = (
    "sha256:e01bcb1ec5858e8069bade8f7fd7ec28c1026fc2fd0590e8f611d3398a5bbbd7"
)


def _npn_envelope(deck_file) -> dict:
    stand_in = deck_file.parent / "stand-in.lvs"
    stand_in.write_text("# stand-in\n")
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        stand_in,
        device_census={
            "layout": {"npn13G2": 1},
            "reference": {"npn13G2": 1},
            "reference_source": {"npn13G2": 1},
        },
    )
    envelope["deck_file"] = str(deck_file)
    envelope["provenance"]["runset"] = {"content_hash": SG13G2_RUNSET_HASH}
    return envelope


def test_shipped_sg13g2_pin_grades_an_npn13g2_report_met(tmp_path):
    """Uses the real shipped pin (no monkeypatch). The envelope is
    hand-built; the runset itself was not run (needs klayout + the PDK)."""
    pin = signoff_runset.PINNED_RUNSETS[SG13G2_RUNSET_HASH]
    assert pin["commit"] == "5cccb161f7492697cfa52eb14dc03beb00bdca9e"

    item = _item_4(tmp_path, _npn_envelope(tmp_path / "absent.lvs"))

    assert item["status"] == "met", item
    assert item["citation"]["pdk_pin"]["pdk"] == "ihp-sg13g2"


def test_shipped_sg13g2_pin_rejects_a_modified_runset_file(tmp_path):
    edited = tmp_path / "sg13g2.lvs"
    edited.write_text("# modified runset\n")

    item = _item_4(tmp_path, _npn_envelope(edited))

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_hash_mismatch"


GF180MCU_RUNSET_HASH = (
    "sha256:09a4f1f1a6fe1fc1e4e193ae5ae1c2b1c49ba190f9e135ffd72d45c14637fde9"
)


def test_shipped_gf180mcu_pin_is_tied_to_an_upstream_commit():
    pin = signoff_runset.PINNED_RUNSETS[GF180MCU_RUNSET_HASH]
    assert pin["pdk"] == "gf180mcu"
    assert pin["commit"] == "1a4e4b59f5d1ea4dfd7b108a02bc085151609ccb"
    assert pin["path"] == "klayout/lvs/gf180mcu.lvs"


def test_shipped_gf180mcu_pin_rejects_a_modified_runset_file(tmp_path):
    edited = tmp_path / "gf180mcu.lvs"
    edited.write_text("# modified runset\n")
    envelope = _runset_envelope(LVS_MATCH_ENVELOPE, edited)
    envelope["provenance"]["runset"] = {"content_hash": GF180MCU_RUNSET_HASH}

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_hash_mismatch"


SKY130_RUNSET_HASH = (
    "sha256:5b4ede4bdb4744b60e42c9b29baa93f40bb24430fb8f3a48a276a712a492ac38"
)


def _sky130_envelope(deck_file) -> dict:
    """The 2AMLogic/sky130-usb2-phy ``differential_driver`` report shape
    (2026-11-17): a genuine match whose census sides differ only in case and
    whose ``declared_not_recognized`` is 100 zero-instance classes."""
    stand_in = deck_file.parent / "stand-in.lvs"
    stand_in.write_text("# stand-in\n")
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        stand_in,
        coverage={
            "declared_not_recognized": [
                "sky130_fd_pr__cap_var_hvt",
                "sky130_fd_bs_flash__special_sonosfet_star",
            ]
        },
        device_census={
            "layout": {
                "sky130_fd_pr__pfet_g5v0d10v5": 36,
                "sky130_fd_pr__nfet_g5v0d10v5": 37,
                "sky130_fd_pr__res_generic_po": 2,
                "sky130_fd_pr__res_xhigh_po_1p41": 1,
                "sky130_fd_pr__model__cap_mim": 4,
            },
            "reference": {
                "SKY130_FD_PR__PFET_G5V0D10V5": 36,
                "SKY130_FD_PR__NFET_G5V0D10V5": 37,
                "SKY130_FD_PR__RES_GENERIC_PO": 2,
                "SKY130_FD_PR__RES_XHIGH_PO_1P41": 1,
                "SKY130_FD_PR__MODEL__CAP_MIM": 4,
            },
            "reference_source": {
                "SKY130_FD_PR__PFET_G5V0D10V5": 36,
                "SKY130_FD_PR__NFET_G5V0D10V5": 37,
                "SKY130_FD_PR__RES_GENERIC_PO": 2,
                "SKY130_FD_PR__RES_XHIGH_PO_1P41": 1,
                "SKY130_FD_PR__MODEL__CAP_MIM": 4,
            },
        },
    )
    envelope["deck_file"] = str(deck_file)
    envelope["provenance"]["runset"] = {"content_hash": SKY130_RUNSET_HASH}
    envelope["deck_vars"] = {
        "convert_subckts": "true",
        "scale": "true",
        "thr": "2",
    }
    return envelope


def test_shipped_sky130_pin_grades_a_real_report_shape_met(tmp_path):
    """AC 1 of issue #6902: the sky130 shape -- status ``match``, census
    sides differing only in case, 100 zero-instance declared classes, the
    pinned runset hash -- grades item 4 ``met``. Uses the real shipped pin
    (no monkeypatch); the runset itself was not run here (needs klayout +
    the PDK)."""
    pin = signoff_runset.PINNED_RUNSETS[SKY130_RUNSET_HASH]
    assert pin["pdk"] == "sky130"
    assert pin["commit"] == "13d2572144450daba6a9f3508ae4c6e93e852466"
    assert pin["path"] == "tech/sky130/lvs/sky130.lvs"

    item = _item_4(tmp_path, _sky130_envelope(tmp_path / "absent.lvs"))

    assert item["status"] == "met", item
    assert item["citation"]["pdk_pin"]["pdk"] == "sky130"


def test_sky130_pin_digest_is_the_one_committed_reports_record():
    """The committed 2AMLogic/sky130-usb2-phy reports record this digest for
    a ciel-installed ``sky130A/libs.tech/klayout/lvs/sky130.lvs``; the pin
    entry must carry exactly it so those reports need no re-run for the pin
    (they do need one for the evidence field, which they predate)."""
    assert SKY130_RUNSET_HASH in signoff_runset.PINNED_RUNSETS


def test_describe_grader_lists_pinned_runsets():
    result = describe_grader()
    assert {p["pdk"] for p in result["pinned_runsets"].values()} == {
        "ihp-sg13g2",
        "ihp-sg13cmos5l",
        "gf180mcu",
        "sky130",
    }
