"""Signoff reasons for the mirrored IHP inductor2 terminal-order limitation."""

from klayout_tools import signoff_runset
from klayout_tools.signoff import build_tier_report
from test_signoff import (
    LVS_MATCH_ENVELOPE,
    LVS_MATCH_SUPPLY_CORRESPONDENCE_ENVELOPE,
    LVS_MISMATCH_ENVELOPE,
    _item_11,
    _manifest,
    _power_delivery_evidence,
)
from test_signoff_runset import _item_4, _runset_envelope
from test_signoff_runset import runset as runset


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
    # A malformed sibling must not hide a valid limitation later in the list.
    mixed = {**envelope, "mismatches": [None, *envelope["mismatches"]]}
    assert signoff_runset.mirrored_inductor_limitation(mixed) == found
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
