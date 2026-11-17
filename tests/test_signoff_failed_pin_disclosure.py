"""A non-passing envelope discloses its manifest ``content_hash`` pin
(issue #5598).

``_grade_check_outcome`` returns before ``_grade_evidence``'s staleness gate,
so until this fix a failing envelope rendered ``unmet``/``check_failed`` with
no ``detail`` at all -- byte-identical whether the manifest's pin matched, was
stale, or could never be compared. The pin is now carried beside the verdict
(the issue #3063 shape), never changing it.
"""

from __future__ import annotations

import hashlib
import json

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_evidence_pin import pin_disclosure
from test_signoff import DRC_VIOLATIONS_ENVELOPE, _manifest, _write

#: The pinned-input hash :data:`DRC_VIOLATIONS_ENVELOPE` records.
LIVE_DRC_PIN = "sha256:layoutA"

STALE_PIN = "sha256:" + "0" * 64


def _anchored_generic(tmp_path, status="fail"):
    """An artifact-anchored item-8 ``generic`` envelope and its input's real
    hash -- the shape whose pin *is* comparable, which the issue's own
    bare-generic reproduction is a special case of."""
    artifact = tmp_path / "characterization.md"
    artifact.write_text("| spec | measured |\n")
    live = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    envelope = {
        "schema_version": 1,
        "kind": "generic",
        "status": status,
        "t1_item": 8,
        "provenance": {"input": {"path": str(artifact), "content_hash": live}},
    }
    return _write(tmp_path, "item8.json", envelope), live


def _bare_generic(tmp_path, status="fail"):
    """The envelope the issue was filed against: no ``provenance`` block, so
    there is no live hash for a pin to be compared against."""
    return _write(
        tmp_path,
        "bare8.json",
        {"schema_version": 1, "kind": "generic", "status": status, "t1_item": 8},
    )


def _item(evidence, item_id, **manifest):
    result = build_tier_report(_manifest(evidence={str(item_id): evidence}, **manifest))
    return next(item for item in result["items"] if item["id"] == item_id)


def _item8(path, pin=None, **extra):
    spec = {"file": path, **extra}
    if pin is not None:
        spec["content_hash"] = pin
    return _item(spec, 8)


# --------------------------------------------------------------------------- #
# The four pin states against a non-passing envelope
# --------------------------------------------------------------------------- #


def test_failing_envelope_discloses_stale_comparable_pin(tmp_path):
    """The case the issue could not see at all: a pin that is both comparable
    and genuinely stale, hidden behind the envelope's own failure."""
    path, _ = _anchored_generic(tmp_path)

    item = _item8(path, STALE_PIN)

    assert item["status"] == "unmet"
    assert item["reason"] == "check_failed"
    assert item["detail"]["content_hash_pin"] == "stale"


def test_stale_pin_on_a_failing_envelope_names_the_failed_comparison(tmp_path):
    """The staleness verdict's own diagnostic payload, so the author can tell
    which input drifted and which hash ``content_hash`` is meant to be."""
    path, live = _anchored_generic(tmp_path)

    item = _item8(path, STALE_PIN)

    assert item["detail"]["hash_check"] == {
        "field": "provenance.input.content_hash",
        "expected": STALE_PIN,
        "actual": live,
        "match": False,
    }
    assert "not the hash of the evidence envelope file" in item["detail"]["hint"]


def test_failing_envelope_discloses_verified_pin(tmp_path):
    path, live = _anchored_generic(tmp_path)

    item = _item8(path, live)

    assert item["reason"] == "check_failed"
    assert item["detail"]["content_hash_pin"] == "verified"
    assert "hash_check" not in item["detail"]


def test_failing_envelope_discloses_unverifiable_pin_with_note(tmp_path):
    """A bare generic envelope has no live hash, so the pin is *uncomparable*
    rather than mismatched -- and the output now says so."""
    item = _item8(_bare_generic(tmp_path), STALE_PIN)

    assert item["reason"] == "check_failed"
    assert item["detail"]["content_hash_pin"] == "unverifiable"
    assert item["detail"]["content_hash_pin_note"] == (
        "generic envelopes carry no provenance.input.content_hash; "
        "the pin cannot be compared"
    )


def test_failing_envelope_without_a_pin_claims_nothing(tmp_path):
    """No staleness claim was made, so none is answered."""
    path, _ = _anchored_generic(tmp_path)

    item = _item8(path)

    assert item["reason"] == "check_failed"
    assert "content_hash_pin" not in (item.get("detail") or {})


# --------------------------------------------------------------------------- #
# Disclosure only: no verdict moves
# --------------------------------------------------------------------------- #


def test_pin_state_never_changes_status_or_reason(tmp_path):
    """Every pin state grades to the same ``unmet``/``check_failed`` the
    unpinned entry does -- a manifest defect is disclosed, never conflated
    with the evidence shortfall."""
    path, live = _anchored_generic(tmp_path)

    verdicts = {
        (item["status"], item["reason"])
        for item in (
            _item8(path),
            _item8(path, live),
            _item8(path, STALE_PIN),
            _item8(_bare_generic(tmp_path), STALE_PIN),
        )
    }

    assert verdicts == {("unmet", "check_failed")}


def test_passing_envelope_keeps_a_clean_met_item(tmp_path):
    """The `met` path is untouched: a verified pin discloses nothing."""
    path, live = _anchored_generic(tmp_path, status="pass")

    item = _item8(path, live)

    assert item["status"] == "met"
    assert "content_hash_pin" not in (item.get("detail") or {})
    assert item["citation"]["content_hash"] == live


def test_originally_reported_runs_now_disclose_the_unenforceable_pin(tmp_path):
    """The issue's reproduction: a bare generic envelope, `status: fail`, cited
    with a correct and then a wrong `content_hash`. Both pins are structurally
    uncomparable, so the two runs still match each other -- but neither is
    silent any more, which is what the report asked for. A *comparable* pin
    is covered by the stale/verified tests above."""
    bare = _bare_generic(tmp_path)
    with open(bare, "rb") as handle:
        true_hash = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    good = _item8(bare, true_hash)
    bad = _item8(bare, STALE_PIN)

    assert good["detail"] == bad["detail"]
    assert good["detail"]["content_hash_pin"] == "unverifiable"
    assert "content_hash_pin_note" in good["detail"]


# --------------------------------------------------------------------------- #
# Every citation kind, not only `generic`
# --------------------------------------------------------------------------- #


def test_failing_drc_envelope_discloses_a_stale_pin(tmp_path):
    report = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    item = _item({"file": report, "content_hash": STALE_PIN}, 3)

    assert item["reason"] == "check_failed"
    assert item["detail"]["content_hash_pin"] == "stale"


def test_failing_drc_envelope_discloses_a_verified_pin(tmp_path):
    report = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    item = _item({"file": report, "content_hash": LIVE_DRC_PIN}, 3)

    assert item["detail"]["content_hash_pin"] == "verified"


def test_pointer_selected_failing_envelope_discloses_its_pin(tmp_path):
    """Issue #2342: the pin binds the envelope the pointer selected, and the
    disclosure follows it there."""
    composed = _write(
        tmp_path, "composed.json", {"drc": DRC_VIOLATIONS_ENVELOPE, "extra": []}
    )

    item = _item({"file": composed, "pointer": "/drc", "content_hash": STALE_PIN}, 3)

    assert item["reason"] == "check_failed"
    assert item["detail"]["content_hash_pin"] == "stale"


def test_command_backed_failing_envelope_discloses_its_pin(tmp_path):
    """A command-backed citation has no file to re-hash, but the envelope it
    prints still carries a comparable `provenance.input.content_hash`."""
    report = _write(tmp_path, "drc.json", DRC_VIOLATIONS_ENVELOPE)

    item = _item(
        {
            "command": ["python3", "-c", f"print(open({report!r}).read())"],
            "content_hash": STALE_PIN,
        },
        3,
    )

    assert item["reason"] == "check_failed"
    assert item["detail"]["content_hash_pin"] == "stale"


def test_critical_metric_detail_is_preserved_beside_the_pin(tmp_path):
    """The disclosure is additive -- it does not displace the detail the
    refusal already carried."""
    envelope = {
        **DRC_VIOLATIONS_ENVELOPE,
        "metrics": {"drc__error__count": 1},
        "critical_metrics": {"drc__error__count": {"max": 0}},
    }
    report = _write(tmp_path, "drc.json", envelope)

    item = _item({"file": report, "content_hash": STALE_PIN}, 3)

    assert item["detail"]["content_hash_pin"] == "stale"
    assert item["detail"]["critical_metric_blockers"][0]["metric"] == (
        "drc__error__count"
    )


# --------------------------------------------------------------------------- #
# The shared helper itself
# --------------------------------------------------------------------------- #


def test_pin_disclosure_omits_the_note_without_a_check_kind():
    """The issue #3063 `yield` call site asks for the bare pin state, so its
    payload is unchanged by the new, explanatory call sites."""
    bare = pin_disclosure({"content_hash": STALE_PIN}, {"content_hash": None})
    explained = pin_disclosure(
        {"content_hash": STALE_PIN}, {"content_hash": None}, check_kind="generic"
    )

    assert bare == {"content_hash_pin": "unverifiable"}
    assert set(explained) == {"content_hash_pin", "content_hash_pin_note"}


def test_pin_disclosure_is_empty_when_nothing_is_pinned():
    assert pin_disclosure({}, {"content_hash": LIVE_DRC_PIN}) == {}
    assert pin_disclosure({}, {"content_hash": None}, check_kind="generic") == {}


def test_pin_disclosure_round_trips_the_manifest_json_shape(tmp_path):
    """The whole point is that a repo byte-comparing `--format json` output
    sees a drifted pin; serialising the item must therefore differ."""
    path, live = _anchored_generic(tmp_path)

    assert json.dumps(_item8(path, live)) != json.dumps(_item8(path, STALE_PIN))
