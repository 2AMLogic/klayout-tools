"""``klt signoff`` grading of a PDK-runset LVS report (issue #2870).

The pin allowlist ships empty, so each test installs the fixture runset's hash
the way a reviewed commit to ``signoff_runset.PINNED_RUNSETS`` would.
"""

from __future__ import annotations

import copy
import hashlib

import pytest

from klayout_tools import signoff_runset
from klayout_tools.signoff import build_tier_report
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
            "device_census": {"layout": {"NMOS": 2}, "reference": {"NMOS": 2}},
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


def test_curated_report_citation_is_unchanged(tmp_path):
    item = _item_4(tmp_path, LVS_MATCH_ENVELOPE)

    assert item["status"] == "met"
    assert "engine" not in item["citation"]


def test_unpinned_runset_is_refused(tmp_path, runset, monkeypatch):
    monkeypatch.setattr(signoff_runset, "PINNED_RUNSETS", {})

    item = _item_4(tmp_path, _runset_envelope(LVS_MATCH_ENVELOPE, runset))

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_not_pinned"


def test_runset_file_drift_with_pinned_recorded_hash_is_a_mismatch(tmp_path, runset):
    envelope = _runset_envelope(LVS_MATCH_ENVELOPE, runset)
    runset.write_text("# edited after the run\n")
    # The recorded hash is still the pinned one; only the file moved.
    assert envelope["provenance"]["runset"]["content_hash"] in (
        signoff_runset.PINNED_RUNSETS
    )

    item = _item_4(tmp_path, envelope)

    assert item["reason"] == "runset_hash_mismatch"


def test_one_unrecognised_device_class_fails(tmp_path, runset):
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        coverage={"declared_not_recognized": ["RES"]},
    )

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_unrecognised_device"


def test_census_disagreement_fails_even_with_match_status(tmp_path, runset):
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        device_census={"layout": {"NMOS": 3}, "reference": {"NMOS": 2}},
    )

    item = _item_4(tmp_path, envelope)

    assert item["status"] == "unmet"
    assert item["reason"] == "runset_unrecognised_device"


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


def test_item_11_refuses_unpinned_runset_report(tmp_path, runset, monkeypatch):
    envelope = _runset_envelope(LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset)
    monkeypatch.setattr(signoff_runset, "PINNED_RUNSETS", {})
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
    assert item["status"] == "unmet"
    assert item["reason"] == "runset_not_pinned"


def test_item_11_runset_report_with_empty_net_correspondence_is_unproven(
    tmp_path, runset
):
    envelope = _runset_envelope(
        LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset, net_correspondence=[]
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
        device_census={"layout": {"npn13G2": 1}, "reference": {"npn13G2": 1}},
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
