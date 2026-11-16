"""``klt signoff --attest 2 --check`` and the committed regeneration claim.

Issue #5572: ``provenance.regeneration`` is part of what ``--check`` compares,
so a flag-less ``--check`` adopts the committed claim as recorded instead of
reporting drift against a claim the caller did not retype. Passing any of
``--generator`` / ``--regenerated-hash`` / ``--hash-kind`` keeps the previous
behaviour, which makes those flags an explicit cross-check.

(Split from ``test_signoff_attest_item2.py`` for the file-size ratchet; the
stream/envelope helpers are imported from it.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import AttestationError, render_envelope
from klayout_tools.signoff_attest_stream import (
    build_stream_attestation,
    check_stream_attestation,
)
from test_signoff_attest_item2 import _GENERATOR, _hash, _stream, _write_gds


def _drift_fields(result: dict) -> list[str]:
    """The ``field`` names a ``--check`` result reports drift on."""
    return [entry["field"] for entry in result["drift"]]


def _drifts_on_claim(result: dict) -> bool:
    """Whether the drift names the regeneration claim at all. The differ
    reports the whole ``provenance.regeneration`` when one side is absent and
    recurses to ``provenance.regeneration.<field>`` when both are present, so
    match on the prefix rather than one spelling."""
    return any(
        field == "provenance.regeneration"
        or field.startswith("provenance.regeneration.")
        for field in _drift_fields(result)
    )


def _committed_with_claim(tmp_path, **claim_kwargs) -> tuple[Path, str, Path]:
    """A repo whose committed item-2 envelope carries a regeneration claim."""
    repo, stream = _stream(tmp_path)
    digest = claim_kwargs.pop("regenerated_hash", None) or _hash(
        build_stream_attestation(stream)
    )
    envelope = build_stream_attestation(
        stream, generator=_GENERATOR, regenerated_hash=digest, **claim_kwargs
    )
    committed = repo / "evidence" / "layout.json"
    committed.write_text(render_envelope(envelope))
    return repo, stream, committed


def test_flagless_check_adopts_the_committed_regeneration_claim(tmp_path):
    """Issue #5572: the claim is re-checked from the envelope, not retyped."""
    _, stream, committed = _committed_with_claim(tmp_path)

    result = check_stream_attestation(stream, str(committed))

    assert result["status"] == "match"
    assert _drift_fields(result) == []
    assert result["fresh"]["provenance"]["regeneration"]["generator"] == _GENERATOR


def test_flagless_check_adopts_a_geometry_hash_kind_claim(tmp_path):
    from klayout_tools._provenance import layout_geometry_digest

    repo, stream = _stream(tmp_path)
    envelope = build_stream_attestation(
        stream,
        generator=_GENERATOR,
        regenerated_hash=layout_geometry_digest(stream),
        hash_kind="geometry",
    )
    committed = repo / "evidence" / "layout.json"
    committed.write_text(render_envelope(envelope))

    result = check_stream_attestation(stream, str(committed))

    assert result["status"] == "match"
    assert result["fresh"]["provenance"]["regeneration"]["hash_kind"] == "geometry"


def test_flagless_check_still_reports_real_drift_without_naming_the_claim(tmp_path):
    _, stream, committed = _committed_with_claim(tmp_path)

    _write_gds(Path(stream), dbu=0.002)
    result = check_stream_attestation(stream, str(committed))

    assert result["status"] == "drifted"
    assert not _drifts_on_claim(result)
    assert _drift_fields(result)


def test_flags_that_differ_from_the_committed_claim_still_drift(tmp_path):
    _, stream, committed = _committed_with_claim(tmp_path)

    result = check_stream_attestation(
        stream,
        str(committed),
        generator="python other.py",
        regenerated_hash="sha256:" + "0" * 64,
    )

    assert result["status"] == "drifted"
    assert _drifts_on_claim(result)


def test_flags_against_a_claimless_committed_envelope_still_drift(tmp_path):
    repo, stream = _stream(tmp_path)
    committed = repo / "evidence" / "layout.json"
    committed.write_text(render_envelope(build_stream_attestation(stream)))

    result = check_stream_attestation(
        stream,
        str(committed),
        generator=_GENERATOR,
        regenerated_hash=_hash(build_stream_attestation(stream)),
    )

    assert result["status"] == "drifted"
    assert _drifts_on_claim(result)


def test_explicit_hash_kind_bytes_alone_is_not_an_adoption(tmp_path):
    """``--hash-kind`` given is a flag, so the committed claim is not adopted."""
    _, stream, committed = _committed_with_claim(tmp_path)

    result = check_stream_attestation(stream, str(committed), hash_kind="bytes")

    assert result["status"] == "drifted"
    assert _drifts_on_claim(result)


@pytest.mark.parametrize(
    "claim",
    [
        ["not", "an", "object"],
        "a string",
        {"regenerated_hash": "sha256:" + "a" * 64},
        {"generator": _GENERATOR},
        {"generator": "   ", "regenerated_hash": "sha256:" + "a" * 64},
        {"generator": _GENERATOR, "regenerated_hash": "sha256:xyz"},
        {"generator": _GENERATOR, "regenerated_hash": None},
        {
            "generator": _GENERATOR,
            "regenerated_hash": "sha256:" + "a" * 64,
            "hash_kind": "nonsense",
        },
        {
            "generator": _GENERATOR,
            "regenerated_hash": "sha256:" + "a" * 64,
            "hash_kind": 7,
        },
    ],
)
def test_malformed_committed_claim_is_refused_not_matched(tmp_path, claim):
    repo, stream = _stream(tmp_path)
    envelope = build_stream_attestation(stream)
    envelope["provenance"]["regeneration"] = claim
    committed = repo / "evidence" / "layout.json"
    committed.write_text(json.dumps(envelope))

    with pytest.raises(AttestationError):
        check_stream_attestation(stream, str(committed))


def test_claimless_envelope_without_flags_is_unchanged(tmp_path):
    repo, stream = _stream(tmp_path)
    committed = repo / "evidence" / "layout.json"
    committed.write_text(render_envelope(build_stream_attestation(stream)))

    result = check_stream_attestation(stream, str(committed))

    assert result["status"] == "match"
    assert "regeneration" not in result["fresh"]["provenance"]


def test_cli_flagless_check_of_a_committed_claim_exits_zero(tmp_path, capsys):
    """The CLI path: ``_compute()`` must not collapse an absent --hash-kind."""
    repo, stream = _stream(tmp_path)
    out = repo / "evidence" / "layout.json"
    digest = _hash(build_stream_attestation(stream))

    assert (
        main(
            [
                "signoff",
                "--attest",
                "2",
                "--input",
                stream,
                "--generator",
                _GENERATOR,
                "--regenerated-hash",
                digest,
                "-o",
                str(out),
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "signoff",
                "--attest",
                "2",
                "--input",
                stream,
                "--check",
                str(out),
                "--format",
                "json",
            ]
        )
        == 0
    )
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "match"

    _write_gds(Path(stream), dbu=0.002)
    assert (
        main(
            [
                "signoff",
                "--attest",
                "2",
                "--input",
                stream,
                "--check",
                str(out),
                "--format",
                "json",
            ]
        )
        == 3
    )
    drifted = json.loads(capsys.readouterr().out)
    assert drifted["status"] == "drifted"
    assert not _drifts_on_claim(drifted)
