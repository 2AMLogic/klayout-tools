"""Item-8 declared metric gaps: ``unmeasured_metrics`` (issue #7259).

A digital characterization that measured some but not all of the required
per-corner metrics used to choose between a red attestation that bound its
measured values (one ``metric`` check per corner x metric, a missing binding
failed) and a green metrics-free one that bound none. ``unmeasured_metrics``
declares each gap -- ``[{corner, metric, reason}]`` -- so the attestation
passes on the measured subset and discloses every unmeasured pair as a
passing ``unmeasured_metric`` check, the way item 9's ``unbenched_rows``
discloses a bench-less row. Split from ``tests/test_signoff_attest_item8.py``,
which sits at the per-file size ratchet's 500-line limit; the inventory
fixtures are imported from it, exactly as
``tests/test_signoff_attest_item8_external.py`` does.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import (
    AttestationError,
    check_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_text import attestation_check_line
from test_signoff_attest_item8 import (
    _FAIL,
    _analog_inventory,
    _attest,
    _digital_inventory,
    _failed,
    _grade,
    _repo,
)

_REASON = "Frequency sweep was not run at ss"
_SS_FMAX = {"corner": "ss", "metric": "fmax", "reason": _REASON}


def _partial_inventory(**overrides) -> dict:
    """The two-corner fixture with ``ss/fmax`` unbound and declared so."""
    inventory = _digital_inventory()
    del inventory["metrics"]["ss"]["fmax"]
    inventory["unmeasured_metrics"] = [dict(_SS_FMAX)]
    inventory.update(overrides)
    return inventory


def _digital(envelope: dict) -> list[tuple]:
    """``(check, corner, metric, value-or-None)`` per role-``digital`` check."""
    return [
        (c["check"], c["corner"], c["metric"], c.get("value"))
        for c in envelope["attestation"]["checks"]
        if c["role"] == "digital"
    ]


def _write_inventory(repo, inventory: dict) -> str:
    path = repo / "inventory.json"
    path.write_text(json.dumps(inventory))
    return str(path)


# --------------------------------------------------------------------------- #
# The mixed inventory: five measured values bound, one gap disclosed, a pass
# --------------------------------------------------------------------------- #


def test_declared_gap_passes_and_binds_the_measured_subset(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _partial_inventory())

    assert envelope["status"] == "pass"
    # The disclosure takes the position its ``metric`` check would have held.
    assert _digital(envelope) == [
        ("metric", "ss", "area", 5),
        ("unmeasured_metric", "ss", "fmax", None),
        ("metric", "ss", "power", 2),
        ("metric", "tt", "area", 5),
        ("metric", "tt", "fmax", 100),
        ("metric", "tt", "power", 2),
    ]


def test_disclosure_check_carries_the_reason_and_no_numeric_value(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _partial_inventory())

    (gap,) = [
        c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "unmeasured_metric"
    ]
    assert gap == {
        "check": "unmeasured_metric",
        "role": "digital",
        "corner": "ss",
        "metric": "fmax",
        "reason": _REASON,
        "passed": True,
    }
    assert [
        c["value"] for c in envelope["attestation"]["checks"] if c["check"] == "metric"
    ] == [
        5,
        2,
        5,
        100,
        2,
    ]


def test_partial_characterization_still_grades_met(tmp_path):
    repo = _repo(tmp_path)

    envelope = _attest(repo, _partial_inventory())

    item = _grade(repo, envelope)
    assert item["status"] == "met"
    attestation = item["citation"]["attestation"]
    assert (
        attestation["check_count"]
        == attestation["passed_count"]
        == len(envelope["attestation"]["checks"])
    )


def test_text_output_names_the_gap_and_shows_the_reason(tmp_path, capsys):
    repo = _repo(tmp_path)
    inventory = _write_inventory(repo, _partial_inventory())
    capsys.readouterr()

    assert main(["signoff", "--attest", "8", "--inventory", inventory]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert f"[PASS] digital ss fmax: unmeasured ({_REASON})" in lines
    assert "[PASS] digital ss area: 5" in lines
    assert not any("fmax: missing" in line for line in lines)


def test_check_line_distinguishes_a_declared_gap_from_a_failed_measurement():
    gap = attestation_check_line(
        {
            "check": "unmeasured_metric",
            "role": "digital",
            "corner": "ss",
            "metric": "fmax",
            "reason": _REASON,
            "passed": True,
        }
    )
    missing = attestation_check_line(
        {
            "check": "metric",
            "role": "digital",
            "corner": "ss",
            "metric": "fmax",
            "value": None,
            "passed": False,
        }
    )

    assert gap == f"[PASS] digital ss fmax: unmeasured ({_REASON})"
    assert missing == "[FAIL] digital ss fmax: missing"


# --------------------------------------------------------------------------- #
# A declaration excuses exactly its own pair
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("corner", "metric"), [("tt", "fmax"), ("ss", "power")])
def test_a_declaration_covers_only_its_own_pair(tmp_path, corner, metric):
    repo = _repo(tmp_path)
    inventory = _partial_inventory()
    del inventory["metrics"][corner][metric]

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    (failed,) = [c for c in envelope["attestation"]["checks"] if not c["passed"]]
    assert _failed(envelope) == [("metric", metric)]
    assert (failed["corner"], failed["value"]) == (corner, None)


def test_a_declaration_does_not_excuse_an_invalid_binding_elsewhere(tmp_path):
    repo = _repo(tmp_path)
    inventory = _partial_inventory()
    inventory["metrics"]["tt"]["power"]["field"] = "status"

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    assert _failed(envelope) == [("metric", "power")]


def test_a_declaration_does_not_excuse_a_missing_cited_envelope(tmp_path):
    repo = _repo(tmp_path)
    (repo / "evidence" / "sim_ss.json").unlink()

    envelope = _attest(repo, _partial_inventory())

    assert envelope["status"] == "fail"
    assert ("citation", "bandwidth") in _failed(envelope)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda inv: inv["envelope_checks"].update({"evidence/sim_ss.json": _FAIL}),
            ("command", "evidence/sim_ss.json"),
        ),
        (
            lambda inv: inv.update(spec_rows=["gain", "bandwidth", "psrr"]),
            ("citation", "psrr"),
        ),
    ],
    ids=["freshness", "citation"],
)
def test_a_declaration_rescues_neither_a_freshness_nor_a_citation_failure(
    tmp_path, mutate, expected
):
    repo = _repo(tmp_path)
    inventory = _partial_inventory()
    mutate(inventory)

    envelope = _attest(repo, inventory)

    assert envelope["status"] == "fail"
    assert expected in _failed(envelope)


# --------------------------------------------------------------------------- #
# Refusals (a malformed inventory: exit 1, nothing attested)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("ss/fmax", "'unmeasured_metrics' must be a list"),
        ([_SS_FMAX, "ss/fmax"], r"unmeasured_metrics\[1\]' must be an object"),
        (
            [{"corner": "ss", "metric": "fmax"}],
            r"unmeasured_metrics\[0\]' must be an object",
        ),
        ([{**_SS_FMAX, "issue": 7259}], r"unmeasured_metrics\[0\]' must be an object"),
        ([{**_SS_FMAX, "reason": ""}], r"\.reason' must be a non-empty string"),
        ([{**_SS_FMAX, "reason": "   "}], r"\.reason' must be a non-empty string"),
        ([{**_SS_FMAX, "reason": None}], r"\.reason' must be a non-empty string"),
        ([{**_SS_FMAX, "corner": "ff"}], r"\.corner' must be one of 'corners'"),
        ([{**_SS_FMAX, "corner": 1}], r"\.corner' must be one of 'corners'"),
        (
            [{**_SS_FMAX, "metric": "jitter"}],
            r"\.metric' must be one of: area, fmax, power",
        ),
        ([{**_SS_FMAX, "metric": None}], r"\.metric' must be one of"),
        ([_SS_FMAX, dict(_SS_FMAX)], "'ss/fmax' is declared more than once"),
    ],
)
def test_malformed_declarations_are_refused(tmp_path, bad, message):
    repo = _repo(tmp_path)

    with pytest.raises(AttestationError, match=message):
        _attest(repo, _partial_inventory(unmeasured_metrics=bad))


def test_a_pair_that_is_both_bound_and_declared_unmeasured_is_refused(tmp_path):
    repo = _repo(tmp_path)

    with pytest.raises(
        AttestationError, match=r"declares ss/fmax unmeasured, but 'metrics' binds it"
    ):
        _attest(repo, _digital_inventory(unmeasured_metrics=[_SS_FMAX]))


@pytest.mark.parametrize("declared", [[_SS_FMAX], []], ids=["entry", "empty-list"])
def test_declarations_outside_the_digital_block_are_refused(tmp_path, declared):
    repo = _repo(tmp_path)

    with pytest.raises(AttestationError, match="'unmeasured_metrics'.*digital"):
        _attest(repo, _analog_inventory(unmeasured_metrics=declared))


# --------------------------------------------------------------------------- #
# Compatibility
# --------------------------------------------------------------------------- #


def test_an_empty_declaration_adds_no_check_and_leaves_the_checks_unchanged(tmp_path):
    repo = _repo(tmp_path)

    plain = _attest(repo, _digital_inventory())
    empty = _attest(repo, _digital_inventory(unmeasured_metrics=[]))

    # Only the pinned inventory hash moves: the bytes of the inventory did.
    assert empty["status"] == plain["status"] == "pass"
    assert empty["attestation"] == plain["attestation"]


def test_inventories_without_declarations_keep_their_checks_and_order(tmp_path):
    repo = _repo(tmp_path)

    digital = _attest(repo, _digital_inventory())
    analog = _attest(repo, _analog_inventory())

    assert digital["status"] == analog["status"] == "pass"
    assert [c["check"] for c in digital["attestation"]["checks"]] == [
        *["present"] * 3,
        *["envelope"] * 2,
        *["citation"] * 2,
        *["command"] * 2,
        *["metric"] * 6,
    ]
    assert [c["check"] for c in analog["attestation"]["checks"]] == [
        *["present"] * 3,
        *["envelope"] * 2,
        *["citation"] * 2,
        *["command"] * 2,
    ]


def test_check_drift_names_a_declared_gap_by_corner_and_metric(tmp_path):
    repo = _repo(tmp_path)
    committed = repo / "evidence" / "item8.json"
    committed.write_text(render_envelope(_attest(repo, _partial_inventory())))
    inventory = _partial_inventory()
    inventory["unmeasured_metrics"][0]["reason"] = "Sweep dropped at ss"

    result = check_attestation(8, _write_inventory(repo, inventory), str(committed))

    (reason,) = [e for e in result["drift"] if e["field"].endswith(".reason")]
    assert reason["check"] == {
        "role": "digital",
        "check": "unmeasured_metric",
        "corner": "ss",
        "metric": "fmax",
    }
    assert (reason["committed"], reason["fresh"]) == (_REASON, "Sweep dropped at ss")
