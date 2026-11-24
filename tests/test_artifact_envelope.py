"""``hashed_path_envelope`` (issue #4931) and ``source_defines_envelope``
(issue #5784)."""

from __future__ import annotations

import hashlib

from klayout_tools._artifact_envelope import (
    hashed_path_envelope,
    source_defines_envelope,
)


def test_sha256_matches_file_bytes(tmp_path):
    f = tmp_path / "a.log"
    f.write_bytes(b"hello\n")
    expected = "sha256:" + hashlib.sha256(b"hello\n").hexdigest()
    assert hashed_path_envelope(str(f))["sha256"] == expected


def test_absent_path_has_null_digest():
    assert hashed_path_envelope(None) == {
        "path": None,
        "scope": "absent",
        "sha256": None,
    }


def test_missing_file_has_null_digest(tmp_path):
    assert hashed_path_envelope(str(tmp_path / "nope"))["sha256"] is None


def test_source_defines_mapping_becomes_a_list_of_envelopes(tmp_path):
    """Issue #5784: a path-keyed mapping cannot carry envelopes in its keys,
    and an external source has no path at all -- so the echo is a list, each
    entry pinning its source by `{path, scope, sha256}`."""
    outside = tmp_path / "macro.v"
    outside.write_bytes(b"module m; endmodule\n")
    resolved = {str(outside): {"FUNCTIONAL": None}, "/no/such/file.v": {"K": "1"}}

    echo = source_defines_envelope(resolved)

    assert echo == [
        {
            "source": {
                "path": None,
                "scope": "external",
                "sha256": "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest(),
            },
            "defines": {"FUNCTIONAL": None},
        },
        {
            "source": {"path": None, "scope": "external", "sha256": None},
            "defines": {"K": "1"},
        },
    ]


def test_source_defines_empty_mapping_echoes_empty_list():
    assert source_defines_envelope({}) == []
