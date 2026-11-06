"""Bundled tiers doc vs. an identical vendored override (issue #5041)."""

from __future__ import annotations

import json

import pytest

from klayout_tools import signoff as signoff_module
from klayout_tools.cli import main
from test_signoff import _manifest, _write


def _run_signoff_json(argv, capsys):
    exit_code = main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("mode", ["manifest", "fleet"])
@pytest.mark.parametrize("via", ["flag", "env"])
def test_cli_bundled_doc_and_identical_override_differ_only_in_source_doc(
    tmp_path, capsys, monkeypatch, mode, via
):
    """Chip repos can drop their vendored doc (issue #5041): grading with the
    bundled copy and with a byte-identical vendored copy yields the same
    report, except `source_doc` (bundled -> canonical name, override -> the
    given path)."""
    monkeypatch.delenv("KLT_TIERS_DOC", raising=False)
    if mode == "manifest":
        target = ["--manifest", _write(tmp_path, "manifest.json", _manifest())]
    else:
        fleet = {"blocks": [{"block": "b1", "kind": "analog", "evidence": {}}]}
        target = ["--fleet", _write(tmp_path, "fleet.json", fleet)]
    base = ["signoff", *target, "--format", "json"]

    vendored = tmp_path / "vendored-copy.md"
    vendored.write_bytes(signoff_module.DEFAULT_DOC_PATH.read_bytes())

    bundled_code, bundled = _run_signoff_json(base, capsys)

    if via == "flag":
        override_argv = [*base, "--tiers-doc", str(vendored)]
    else:
        monkeypatch.setenv("KLT_TIERS_DOC", str(vendored))
        override_argv = base
    override_code, override = _run_signoff_json(override_argv, capsys)

    assert override_code == bundled_code
    assert bundled["source_doc"] == "docs/design-evidence-tiers.md"
    assert override["source_doc"] == str(vendored)
    bundled.pop("source_doc")
    override.pop("source_doc")
    assert override == bundled
