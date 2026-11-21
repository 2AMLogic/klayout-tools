"""``klt signoff`` grading of a PDK-runset LVS report (issue #2870).

The runset fixture installs its hash in the signoff-side pin allowlist.
"""

from __future__ import annotations

import copy
import hashlib
from unittest.mock import Mock

import pytest

from klayout_tools import signoff_runset
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    LVS_MATCH_ENVELOPE,
    LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE,
    LVS_MISMATCH_ENVELOPE,
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


def test_one_unrecognised_device_class_fails(tmp_path, runset):
    envelope = _runset_envelope(
        LVS_MATCH_ENVELOPE,
        runset,
        deck_file=str(tmp_path / "missing.lvs"),
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


# --- Issue #6903: the mirrored-inductor2 terminal-order named reason ----


def _mirrored_inductor_envelope(base: dict, deck) -> dict:
    """A failing ``klayout-runset`` report whose only extra finding is the
    mirrored-``inductor2`` terminal-order known limitation."""
    envelope = _runset_envelope(base, deck)
    limit = {
        "category": "topology.known_limitation",
        "severity": "warning",
        "description": "inductor '1' extracts with its LB terminal first",
        "side": "layout",
        "details": {
            "limitation": signoff_runset.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER,
            "layout_terminal_1_net": "LB",
            "layout_terminal_2_net": "LA",
            "reference_terminal_1_net": "LA",
        },
    }
    port = {
        "category": "port.name_mismatch",
        "severity": "error",
        "description": "Port mismatch 'LA' vs. 'LB'",
        "side": "both",
        "net": {"layout": "LA", "reference": "LB"},
        "device": None,
        "property": None,
    }
    envelope["status"] = "mismatch"
    envelope["mismatches"] = [port, limit]
    envelope["mismatch_count"] = 2
    envelope["category_counts"] = {
        "port.name_mismatch": 1,
        "topology.known_limitation": 1,
    }
    return envelope


def test_limit_token_is_pinned_between_engine_and_signoff():
    from klayout_tools import lvs_runset_ports as ports

    assert (
        signoff_runset.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER
        == ports.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER
    )


def test_mirrored_inductor_limitation_reader(tmp_path):
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    envelope = _mirrored_inductor_envelope(LVS_MISMATCH_ENVELOPE, deck)
    found = signoff_runset.mirrored_inductor_limitation(envelope)
    assert found is not None
    assert found["details"]["limitation"] == (
        signoff_runset.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER
    )
    # Without the entry (the negative controls' shape) there is nothing to
    # name, and a non-runset report never carries it.
    plain = _runset_envelope(LVS_MISMATCH_ENVELOPE, deck)
    plain["mismatches"] = LVS_MISMATCH_ENVELOPE["mismatches"]
    assert signoff_runset.mirrored_inductor_limitation(plain) is None
    curated = {**LVS_MISMATCH_ENVELOPE, "engine": "klayout"}
    curated["mismatches"] = envelope["mismatches"]
    assert signoff_runset.mirrored_inductor_limitation(curated) is None


def test_item_4_names_the_mirrored_inductor_limit(tmp_path, runset):
    item = _item_4(tmp_path, _mirrored_inductor_envelope(LVS_MATCH_ENVELOPE, runset))
    assert item["status"] == "unmet", item
    assert item["reason"] == "runset_inductor_terminal_order"
    assert item["citation"] is None
    assert item["detail"]["cited_evidence"]["check_status"] == "mismatch"


def test_item_4_plain_runset_mismatch_stays_check_failed(tmp_path, runset):
    envelope = _runset_envelope(LVS_MISMATCH_ENVELOPE, runset)
    envelope["mismatches"] = list(LVS_MISMATCH_ENVELOPE["mismatches"])
    item = _item_4(tmp_path, envelope)
    assert item["status"] == "unmet", item
    assert item["reason"] == "check_failed"


def test_item_11_lvs_half_names_the_limit(tmp_path):
    from klayout_tools.signoff_power_delivery import _failed_lvs_verdict

    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    envelope = _mirrored_inductor_envelope(LVS_MATCH_ENVELOPE, deck)
    # No supply evidence resolved: was a bare check_failed, now the name.
    reason, detail = _failed_lvs_verdict(envelope, None)
    assert reason == "runset_inductor_terminal_order"
    assert detail["inductor_terminal_order"]["layout_terminal_1_net"] == "LB"
    # ERC half complete: was lvs_did_not_pass, now the name, and the
    # power_delivery block the reason carried is kept.
    reason, detail = _failed_lvs_verdict(envelope, {"supply_nets": ["VDD"], "ties": []})
    assert reason == "runset_inductor_terminal_order"
    assert detail["power_delivery"]["supply_nets"] == ["VDD"]
    # Without the limit entry both branches render exactly as before.
    plain = _runset_envelope(LVS_MISMATCH_ENVELOPE, deck)
    assert _failed_lvs_verdict(plain, None)[0] == "check_failed"
    assert _failed_lvs_verdict(plain, {"supply_nets": ["VDD"]})[0] == (
        "lvs_did_not_pass"
    )


def test_item_11_renders_the_named_reason_end_to_end(tmp_path, runset):
    envelope = _mirrored_inductor_envelope(
        LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE, runset
    )
    parts = _power_delivery_evidence(tmp_path, lvs_envelope=envelope)
    result = build_tier_report(_manifest(evidence={"11": parts}))
    item = _item_11(result)
    assert item["status"] == "unmet", item
    assert item["reason"] == "runset_inductor_terminal_order"
