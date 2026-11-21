"""``hashed_path_envelope`` (issue #4931)."""

from __future__ import annotations

import hashlib

from klayout_tools._artifact_envelope import hashed_path_envelope


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
