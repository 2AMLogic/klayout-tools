"""Issue #6903 part A: the as-drawn and mirrored ``inductor2`` reproduced
through ``klt lvs --engine klayout-runset`` on the **pinned** IHP
``ihp-sg13g2`` runset, with the measured behaviour asserted and both
negative controls run.

Everything here skips cleanly when the standalone ``klayout`` binary or a
matching IHP install is absent -- the hermetic coverage of the same rules
lives in ``tests/test_lvs_klayout_runset_ports.py``. The measured record
this file pins (KLayout 0.30.12, IHP sg13g2 v0.3.0 runset at commit
``5cccb161``, digest ``sha256:e01bcb1e...``):

- **Strict top-port mode is on in klt's invocation.** klt passes only
  ``input``/``schematic``/``report``/``top``/``topcell`` plus the
  request's ``deck_vars``; the runset's own default then is
  ``IGNORE_TOP_PORTS_MISMATCH = false`` -> ``compare && flag_missing_ports``
  (``sg13g2.lvs`` lines 138/492-493). The envelope records the answer
  (``provenance.runset.strict_top_port_mode``), and the single-variable
  control below proves the findings come from that check: with
  ``deck_vars: {"ignore_top_ports_mismatch": true}`` they vanish.
- **A mirrored, correctly wired inductor fails only the runset's own
  checks** (strict-port name crossings from the ``LA``/``LB`` texts naming
  the nets; on a symmetric block the compare itself stays clean and the
  device pair absorbs the reversal into a crossed net pairing), while
  klt's database verdict used to read ``status: "match"`` -- the bug this
  issue closes. It now reads ``mismatch`` with the findings verbatim and
  the known limitation named.
- **Port findings live in the ``.lvsdb``**, not the process log: stdout
  carries only the logger-formatted ``ERROR : Netlists don't match`` line,
  which ``engine_error_lines`` (``^\\s*ERROR``) deliberately does not
  match.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import lvs_runset_ports as ports
from klayout_tools.lvs import run_lvs
from klayout_tools.signoff_runset import PINNED_RUNSETS

FIXTURES = Path(__file__).parent / "fixtures" / "runset_inductor"

#: The pinned ihp-sg13g2 runset entry (signoff_runset.PINNED_RUNSETS), as
#: ``(content_hash, commit)`` -- the test refuses to run an unpinned deck.
_IHP_HASH = next(h for h, e in PINNED_RUNSETS.items() if e.get("pdk") == "ihp-sg13g2")


def _pinned_ihp_deck() -> str:
    from klayout_tools import pdk

    try:
        root = pdk.find_pdk(variant="ihp-sg13g2")["assets"]["klayout"]
    except Exception:  # any resolution failure means "absent"
        pytest.skip("no ihp-sg13g2 PDK install")
    deck = os.path.join(root, "tech", "lvs", "sg13g2.lvs")
    if not os.path.isfile(deck):
        pytest.skip("ihp-sg13g2 install lacks the sg13g2.lvs runset")
    from klayout_tools._provenance import sha256_file

    if sha256_file(deck) != _IHP_HASH.removeprefix("sha256:"):
        pytest.skip(f"installed sg13g2.lvs does not hash to the pin ({_IHP_HASH})")
    return deck


def _request(deck: str, gds: str, cdl: str, deck_vars: dict | None = None):
    return json.dumps(
        {
            "engine": "klayout-runset",
            "deck_file": deck,
            "layout": {"file": str(FIXTURES / gds), "top": "IND2"},
            "reference": {"netlist": str(FIXTURES / cdl), "top": "IND2"},
            **({"deck_vars": deck_vars} if deck_vars else {}),
        }
    )


@pytest.fixture(scope="module")
def deck() -> str:
    return _pinned_ihp_deck()


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_asdrawn_matches(deck):
    report = run_lvs(_request(deck, "ind2_asdrawn.gds", "ind2.cdl"))
    assert report["status"] == "match"
    assert report["device_census"]["layout"] == {"inductor": 1}
    assert report["device_census"]["reference"] == {"INDUCTOR": 1}
    assert not [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    # The strict-mode answer is recorded, and klt passed no override.
    assert report["provenance"]["runset"]["strict_top_port_mode"] is True
    assert report["deck_vars"] == {}


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_mirrored_is_mismatch_with_verbatim_findings_and_named_limit(deck):
    report = run_lvs(_request(deck, "ind2_mirrored.gds", "ind2.cdl"))
    # As-shipped behaviour, now visible: mismatch, not the old bare match.
    assert report["status"] == "mismatch"
    findings = [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert [m["description"] for m in findings] == [
        "Port mismatch 'LA' vs. 'LB'",
        "Port mismatch 'LB' vs. 'LA'",
    ]
    assert all(m["severity"] == "error" for m in findings)
    # The strict-mode answer, and the single-variable control proving the
    # findings come from flag_missing_ports: the runset's own switch turns
    # them (and the failure) off.
    assert report["provenance"]["runset"]["strict_top_port_mode"] is True
    relaxed = run_lvs(
        _request(
            deck, "ind2_mirrored.gds", "ind2.cdl", {"ignore_top_ports_mismatch": True}
        )
    )
    assert not [
        m
        for m in relaxed["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert relaxed["status"] == "match"
    # The known defect is named beside the as-shipped failure.
    limits = [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
    assert len(limits) == 1
    assert limits[0]["severity"] == "warning"
    assert limits[0]["details"]["limitation"] == (
        ports.LIMIT_MIRRORED_INDUCTOR2_TERMINAL_ORDER
    )


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_negative_control_1_reversed_reference_stays_plain(deck):
    """The reference genuinely wired the other way round: the same terminal
    order and the same strict-port findings, but the *reference* is the
    reversed side -- no named limitation."""
    report = run_lvs(_request(deck, "ind2_asdrawn.gds", "ind2_reversed.cdl"))
    assert report["status"] == "mismatch"
    assert [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert not [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_negative_control_2_labels_off_the_ports_is_rejected(deck):
    """LA/LB moved off the port pins: the runset extracts no inductor at
    all, and the report is refused (never `match`), with no port findings
    and no named limitation."""
    report = run_lvs(_request(deck, "ind2_wrongnet_labels.gds", "ind2.cdl"))
    assert report["status"] == "inconclusive"
    assert report["coverage"]["nothing_checked"] is True
    assert not [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert not [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_crossed_labels_documented_equivalence(deck):
    """Measured limit: crossed LA/LB labels (each text on the other port's
    net) are netlist-indistinguishable from the mirrored stream -- same
    terminal order, same names, same findings -- so they carry the same
    named limitation; no .lvsdb-level reader can tell them apart (issue
    #6903's part-C escape hatch, reported here as measured evidence)."""
    report = run_lvs(_request(deck, "ind2_swapped_labels.gds", "ind2.cdl"))
    assert report["status"] == "mismatch"
    findings = [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert [m["description"] for m in findings] == [
        "Port mismatch 'LA' vs. 'LB'",
        "Port mismatch 'LB' vs. 'LA'",
    ]
    limits = [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
    assert len(limits) == 1


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_pinnet_port_names_reach_the_envelope_verbatim(deck):
    """The sg13g2-vco shape: an inductor port on a top-level pin net names
    the layout net ``LA,VDD``, the strict-port check logs it, and the
    envelope carries it verbatim with status mismatch (it used to read
    ``match``). No known limitation: the reference net names carry no
    LA/LB, so the label-to-net relation is unreadable there."""
    report = run_lvs(_request(deck, "ind2_pinnet.gds", "ind2_pinnet.cdl"))
    assert report["status"] == "mismatch"
    findings = [
        m
        for m in report["mismatches"]
        if m["category"] == ports.CATEGORY_PORT_NAME_MISMATCH
    ]
    assert [m["description"] for m in findings] == [
        "Port mismatch 'LA,VDD' vs. 'VDD'",
        "Port mismatch 'LB,OUT' vs. 'OUT'",
    ]
    assert findings[0]["net"] == {"layout": "LA,VDD", "reference": "VDD"}
    assert not [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
