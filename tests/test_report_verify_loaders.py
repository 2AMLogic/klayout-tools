"""Rejection arms of the shared `--check`/`--rerun` committed-report loaders.

`load_committed_report` (issue #1106's informative-failure contract: missing
file, unparseable JSON, non-object JSON, unreadable file) and
`resolve_committed_path` (issues #2659/#2699's `{path, scope}` resolution)
were previously exercised only through verb-level tests, which never reached
every arm; these pin each rejection directly.
"""

import json
import os

import pytest


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root reads unreadable files",
)
def test_load_committed_report_unreadable_file_is_an_error_not_a_crash(tmp_path):
    """A committed report that exists but cannot be read (permission-denied)
    raises the caller's error with the "could not read" message -- the
    `except OSError` arm, never a traceback."""
    from klayout_tools import _report_verify as rv

    path = tmp_path / "committed.json"
    path.write_text('{"status": "clean"}')
    path.chmod(0o000)

    with pytest.raises(RuntimeError, match="could not read committed report"):
        rv.load_committed_report(str(path), error_cls=RuntimeError)


def test_load_committed_report_non_object_json_is_an_error(tmp_path):
    """Valid JSON that is not an object (an array here) is rejected with the
    "must be a JSON object" message, same as unparseable JSON."""
    from klayout_tools import _report_verify as rv

    path = tmp_path / "committed.json"
    path.write_text(json.dumps(["not", "an", "object"]))

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        rv.load_committed_report(str(path), error_cls=RuntimeError)


def test_resolve_committed_path_repo_scope_with_non_string_path_is_none():
    """A `{path, scope}` object whose `path` is not a non-empty string (or is
    missing) names no resolvable path -- `None`, not a guess."""
    from klayout_tools import _report_verify as rv

    assert (
        rv.resolve_committed_path({"scope": "repo", "path": 123}, report_path="r.json")
        is None
    )
    assert rv.resolve_committed_path({"scope": "repo"}, report_path="r.json") is None


def test_resolve_committed_path_repo_scope_outside_any_repo_is_none(tmp_path):
    """`scope: "repo"` anchored outside any git repository (a report in a
    temp dir) has no repo root to join against -- `None`, reported as
    "cannot verify" upstream."""
    from klayout_tools import _report_verify as rv

    report = tmp_path / "committed.json"
    report.write_text("{}")

    assert (
        rv.resolve_committed_path(
            {"scope": "repo", "path": "layout/top.gds"},
            report_path=str(report),
        )
        is None
    )
