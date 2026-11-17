"""Issue #5666: the ``klayout-runset`` report's ``layout`` field is the
committable ``{path, scope}`` envelope built from the *resolved* layout path
(the file `environment.layout_sha256` hashes) -- the shape
`schema_version: 3` documents unconditionally (docs/cli/lvs.md), the same
normalization the curated-deck report applies (`lvs_report.py`). The old
bare request echo crashed `--format text` (`render_path_field` expects the
envelope) and left `--check` nothing to resolve. `reference` stays the
documented bare-string echo (docs/cli/lvs.md), which the text renderer has
always special-cased.

Split from ``test_lvs_klayout_runset`` (file-size ratchet); reuses its
fake-klayout rig and the shared `.git`-seeding `tmp_path` override
(`lvs_tmp_repo`) that puts the layout in the realistic inside-a-repo
position -- the only position a committed report is ever in."""

from __future__ import annotations

import lvs_tmp_repo
import test_lvs_klayout_runset as base
from klayout_tools.lvs import run_lvs

fake_klayout = base.fake_klayout  # pytest collects the fixture by module-level name
_request = base._request
tmp_path = lvs_tmp_repo.tmp_path  # pytest collects the fixture by module-level name


def test_layout_field_is_the_path_scope_envelope(tmp_path, fake_klayout):
    report = run_lvs(_request(tmp_path, 1))
    assert isinstance(report["layout"], dict)
    assert report["layout"] == {"path": "l.gds", "scope": "repo"}
    # `reference` stays the documented bare-string echo (docs/cli/lvs.md).
    assert isinstance(report["reference"], str)


def test_text_renderer_renders_a_runset_report(tmp_path, fake_klayout, capsys):
    """`--format text` (the default) renders a complete runset report
    instead of raising `AttributeError` inside `render_path_field`."""
    from klayout_tools.cli.lvs_cmd import _print_text

    report = run_lvs(_request(tmp_path, 1))
    _print_text(report)
    out = capsys.readouterr().out
    # The `layout:` line is the envelope rendering (repo-relative), never the
    # absolute host path; `reference:` stays the request's verbatim string.
    assert out.startswith("layout: l.gds\n")
    assert f"layout: {tmp_path / 'l.gds'}" not in out
    assert f"reference: {report['reference']}" in out
    assert f"engine: {report['engine']}" in out
    assert f"status: {report['status']}" in out
    assert "mismatches: " in out
    assert "errors: " in out
