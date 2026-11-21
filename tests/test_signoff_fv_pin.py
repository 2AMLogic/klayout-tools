"""File-backed ``functional-verification`` ``content_hash`` pins (issues
#4893, #5550): the legacy evidence-file basis for an envelope from an older
``klt``, and the RTL-digest basis for a provenance-bearing one."""

from __future__ import annotations

import hashlib
from pathlib import Path

from klayout_tools.signoff import build_signoff, build_tier_report
from test_signoff import (
    FUNCTIONAL_VERIFICATION_SDF_ENVELOPE,
    _manifest,
    _synthesize_envelope,
    _write,
)

#: The same SDF envelope from a `klt` that records the shared `provenance`
#: block (issue #5550): `provenance.input.content_hash` digests the RTL the
#: run built (role `source`), not the report's own bytes -- the value a
#: manifest `content_hash` pin binds for this kind.
FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE = {
    **FUNCTIONAL_VERIFICATION_SDF_ENVELOPE,
    "provenance": {
        "klt_version": "0.9.2",
        "klt_git_commit": None,
        "klt_reinstall_spec": None,
        "klayout_version": "0.30.12",
        "pdk": None,
        "deck": None,
        "input": {"content_hash": "sha256:rtl-a", "role": "source"},
    },
}

#: The RTL digest the provenance-bearing fixture records -- what a manifest
#: `content_hash` pin binds for this kind since issue #5550.
RTL_DIGEST = FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE["provenance"]["input"][
    "content_hash"
]


#: An `options.from_synthesize` report from the same `klt`: the block is
#: present but `provenance.input` is `null` *by design* (the design input is
#: the synthesized netlist, pinned by `from_synthesize.netlist_sha256`), unlike
#: the null-digest case (`input` present, `content_hash` null).
FUNCTIONAL_VERIFICATION_SDF_FROM_SYNTHESIZE_ENVELOPE = {
    **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
    "provenance": {
        **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE["provenance"],
        "input": None,
    },
}


def _fv_item7(
    tmp_path, pin, *, command=False, envelope=FUNCTIONAL_VERIFICATION_SDF_ENVELOPE
):
    fv_path = _write(tmp_path, "fv.json", envelope)
    spec = {"file": fv_path}
    if command:
        spec = {
            "command": [
                "python3",
                "-c",
                f"print(open({fv_path!r}).read())",
            ]
        }
    if pin is not None:
        spec["content_hash"] = pin
    result = build_tier_report(_manifest(kind="digital", evidence={"7": spec}))
    return fv_path, next(item for item in result["items"] if item["id"] == 7)


# --------------------------------------------------------------------------- #
# Legacy envelopes (no `provenance` key): the #4893 evidence-file basis
# --------------------------------------------------------------------------- #


def test_legacy_file_backed_pin_matches_evidence_bytes(tmp_path):
    """A report from an older `klt` is graded by the grader re-hashing the
    cited file; a matching pin grades on the normal verdict and the citation
    discloses the hash basis."""
    fv_path = _write(tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_ENVELOPE)
    with open(fv_path, "rb") as handle:
        pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(tmp_path, pin)

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == pin
    assert item_7["citation"]["content_hash_basis"] == "evidence_file"
    assert item_7["citation"]["input_verified"] is True


def test_legacy_file_backed_pin_goes_stale_on_edit(tmp_path):
    """A pin that no longer matches the evidence bytes is stale."""
    other = tmp_path / "other.json"
    other.write_text("{}")
    pin = "sha256:" + hashlib.sha256(other.read_bytes()).hexdigest()
    fv_path, item_7 = _fv_item7(tmp_path, pin)
    digest = hashlib.sha256(Path(fv_path).read_bytes()).hexdigest()

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"
    assert item_7["detail"] == {
        "content_hash_pin": "stale",
        "hash_check": {
            "field": "evidence_file (sha256 of the cited evidence file)",
            "expected": pin,
            "actual": f"sha256:{digest}",
            "match": False,
        },
        "compared_against": fv_path,
        "hint": (
            f"content_hash must be the sha256 of the evidence file itself ({fv_path})"
        ),
    }


def test_legacy_command_backed_pin_stays_unverifiable(tmp_path):
    """A legacy command citation's stdout is not a stable file; the pin stays
    ungradable and the note says why."""
    _, item_7 = _fv_item7(tmp_path, "sha256:expected", command=True)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "unverifiable_provenance"
    assert item_7["citation"] is None
    assert item_7["detail"]["content_hash_pin"] == "unverifiable"
    note = item_7["detail"]["content_hash_pin_note"]
    assert "functional-verification" in note
    assert "provenance.input.content_hash" in note
    assert "command-backed" in note


def test_unpinned_functional_verification_citation_has_no_basis_key(tmp_path):
    """No pin -> no new keys."""
    _, item_7 = _fv_item7(tmp_path, None)

    assert "content_hash_basis" not in (item_7.get("citation") or {})


# --------------------------------------------------------------------------- #
# Provenance-bearing envelopes (issue #5550): the pin binds the RTL digest
# --------------------------------------------------------------------------- #


def test_rtl_digest_pin_grades_met_without_evidence_file_basis(tmp_path):
    _, item_7 = _fv_item7(
        tmp_path, RTL_DIGEST, envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == RTL_DIGEST
    # Not the report's own bytes: the #4893 basis key is absent, so a reader
    # can tell this pin binds a design input.
    assert "content_hash_basis" not in item_7["citation"]


def test_rtl_digest_pin_goes_stale_when_digest_differs(tmp_path):
    """A pin that no longer matches the recorded RTL digest -- the manifest
    was written against a different RTL revision -- is stale evidence."""
    _, item_7 = _fv_item7(
        tmp_path,
        "sha256:rtl-b",
        envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
    )

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_command_backed_rtl_digest_pin_is_gradable(tmp_path):
    """A command-backed citation no longer grades `unverifiable_provenance`
    when the envelope carries the digest: the parsed stdout *is* the envelope,
    so the pin compares against it like a file-backed one."""
    _, item_7 = _fv_item7(
        tmp_path,
        RTL_DIGEST,
        command=True,
        envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
    )

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == RTL_DIGEST


def test_legacy_byte_pin_flips_to_stale_against_provenance_bearing_envelope(
    tmp_path,
):
    """The compat flip (issue #5550): a manifest that pinned the report's own
    bytes under #4893 -- and graded `verified` through the evidence-file
    re-hash -- now grades `stale_evidence` against an unchanged committed
    report, because the pin is compared against the RTL digest instead."""
    fv_path = _write(
        tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )
    with open(fv_path, "rb") as handle:
        byte_pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(
        tmp_path, byte_pin, envelope=FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_null_rtl_digest_pin_is_unverifiable_with_null_note(tmp_path):
    """A source that could not be hashed records `content_hash: null`; a pin
    against that envelope is unverifiable with a note naming the null digest
    -- not the legacy wording, and never the evidence-file basis."""
    envelope = {
        **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
        "provenance": {
            **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE["provenance"],
            "input": {"content_hash": None, "role": "source"},
        },
    }
    _, item_7 = _fv_item7(tmp_path, "sha256:expected", envelope=envelope)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "unverifiable_provenance"
    note = item_7["detail"]["content_hash_pin_note"]
    assert "null" in note
    assert "could not be hashed" in note
    assert "legacy" not in note


def test_null_rtl_digest_never_falls_back_to_the_evidence_file_basis(tmp_path):
    """The designed null-digest case (`input` present, `content_hash` null)
    keeps refusing the #4893 basis even when the pin is the report's own
    bytes -- only an *absent* `provenance.input` re-opens that basis."""
    envelope = {
        **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE,
        "provenance": {
            **FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE["provenance"],
            "input": {"content_hash": None, "role": "source"},
        },
    }
    fv_path = _write(tmp_path, "fv.json", envelope)
    with open(fv_path, "rb") as handle:
        byte_pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(tmp_path, byte_pin, envelope=envelope)

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "unverifiable_provenance"
    assert "content_hash_basis" not in (item_7.get("citation") or {})


# --------------------------------------------------------------------------- #
# `options.from_synthesize` reports (issue #5550): a block with `input: null`
# --------------------------------------------------------------------------- #


def test_from_synthesize_file_backed_pin_keeps_the_evidence_file_basis(tmp_path):
    """A `from_synthesize` report records no digest of its own (its design
    input is the netlist), so a pin on the file keeps the #4893 binding it has
    on `main` -- the report's own bytes, disclosed as `evidence_file`."""
    fv_path = _write(
        tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_FROM_SYNTHESIZE_ENVELOPE
    )
    with open(fv_path, "rb") as handle:
        pin = "sha256:" + hashlib.sha256(handle.read()).hexdigest()

    _, item_7 = _fv_item7(
        tmp_path,
        pin,
        envelope=FUNCTIONAL_VERIFICATION_SDF_FROM_SYNTHESIZE_ENVELOPE,
    )

    assert item_7["status"] == "met"
    assert item_7["citation"]["content_hash"] == pin
    assert item_7["citation"]["content_hash_basis"] == "evidence_file"


def test_from_synthesize_file_backed_pin_goes_stale_on_edit(tmp_path):
    """The evidence-file basis still detects an edited report."""
    _, item_7 = _fv_item7(
        tmp_path,
        "sha256:" + "0" * 64,
        envelope=FUNCTIONAL_VERIFICATION_SDF_FROM_SYNTHESIZE_ENVELOPE,
    )

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "stale_evidence"


def test_from_synthesize_command_backed_pin_note_names_the_real_cause(tmp_path):
    """A command citation's stdout is not a stable file, so the pin stays
    ungradable -- and the note says so, naming the netlist-pinned design input
    rather than an unhashable source or a legacy `klt`."""
    _, item_7 = _fv_item7(
        tmp_path,
        "sha256:expected",
        command=True,
        envelope=FUNCTIONAL_VERIFICATION_SDF_FROM_SYNTHESIZE_ENVELOPE,
    )

    assert item_7["status"] == "unmet"
    assert item_7["reason"] == "unverifiable_provenance"
    assert item_7["detail"]["content_hash_pin"] == "unverifiable"
    note = item_7["detail"]["content_hash_pin_note"]
    assert "from_synthesize" in note
    assert "netlist" in note
    assert "command-backed" in note
    assert "could not be hashed" not in note
    assert "legacy" not in note


def test_functional_verification_source_digest_agrees_with_synthesize(tmp_path):
    """Issue #5550: an FV envelope's `source`-role digest joins
    `_provenance_consistency`'s per-role comparison, so an FV citation and a
    `klt synthesize` citation over the same RTL set agree instead of the FV
    report being invisible to the gate."""
    fv_path = _write(
        tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )
    synth = _synthesize_envelope()
    synth["provenance"] = {"input": {"content_hash": "sha256:rtl-a", "role": "source"}}
    synth_path = _write(tmp_path, "synth.json", synth)

    result = build_signoff([fv_path, synth_path])

    assert result["provenance_consistency"]["ok"] is True
    assert result["status"] == "pass"


def test_functional_verification_source_digest_disagreement_refuses(tmp_path):
    """The same bundle with the RTL edited under one report only -- the
    per-role comparison now catches what a provenance-less FV envelope could
    never participate in (issue #5550, the #2027 per-role comparison)."""
    fv_path = _write(
        tmp_path, "fv.json", FUNCTIONAL_VERIFICATION_SDF_PROVENANCE_ENVELOPE
    )
    synth = _synthesize_envelope()
    synth["provenance"] = {"input": {"content_hash": "sha256:rtl-b", "role": "source"}}
    synth_path = _write(tmp_path, "synth.json", synth)

    result = build_signoff([fv_path, synth_path])

    assert result["provenance_consistency"]["ok"] is False
    mismatches = result["provenance_consistency"]["mismatches"]
    assert {
        "field": "input.content_hash",
        "role": "source",
    }.items() <= mismatches[0].items()
    assert result["status"] == "refused"
