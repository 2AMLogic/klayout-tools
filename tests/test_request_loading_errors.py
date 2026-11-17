"""Regressions for `klt eval`'s and `klt yield-sensitivity`'s JSON input
loading (issue #5485).

Both verbs used to carry a hand-rolled "read a JSON file" helper instead of
calling the shared `_paths._load_request_json` the other request-taking verbs
use, and both copies had drifted from it: `eval._load_json_arg` caught only
`json.JSONDecodeError`, `yield_sensitivity._load_json` omitted
`UnicodeDecodeError`, and neither passed an explicit `encoding`. An unreadable
or non-UTF-8 input therefore escaped as a raw `PermissionError` /
`UnicodeDecodeError` traceback, breaking the rule in `docs/json-contract.md`
that every verb reports through the shared error envelope.

These tests pin the envelope, not the message wording -- `error.message` is
not a contracted field. They assert what the contract does promise: an
`EvalError`/`SensitivityError` from the library surface, and exit 1 plus a
parseable envelope naming the command from the CLI surface.
"""

from __future__ import annotations

import json
import os

import pytest

from klayout_tools.cli import main
from klayout_tools.eval import EvalError, run_eval
from klayout_tools.yield_sensitivity import SensitivityError, run_sensitivity

#: `\xff\xfe` is a UTF-16 BOM: valid JSON once decoded as UTF-16, and not
#: decodable as UTF-8 at all -- so it reaches the decode error rather than
#: the JSON parse error.
_NON_UTF8 = b'\xff\xfe{"gates":[]}'

_MINIMAL_DESCRIPTOR = {
    "gates": [{"check": "drc", "args": {"file": "${layout}", "deck": "sky130"}}],
    "objective": {
        "check": "layout-metrics",
        "metric": "cell_count",
        "polarity": "minimize",
        "args": {"block": "${block}"},
    },
}

_skip_as_root = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root reads unreadable files",
)


def _error_envelope(capsys) -> dict:
    """Parse the JSON error envelope the CLI writes to stderr."""
    return json.loads(capsys.readouterr().err)


# --------------------------------------------------------------------------- #
# klt eval -- descriptor and --candidate both resolve through one loader
# --------------------------------------------------------------------------- #


def test_eval_non_utf8_descriptor_raises_evalerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(EvalError, match="could not read"):
        run_eval(str(path))


@_skip_as_root
def test_eval_unreadable_descriptor_raises_evalerror(tmp_path):
    path = tmp_path / "noread.json"
    path.write_text(json.dumps(_MINIMAL_DESCRIPTOR), encoding="utf-8")
    path.chmod(0o000)
    with pytest.raises(EvalError, match="could not read"):
        run_eval(str(path))


def test_eval_non_utf8_candidate_raises_evalerror(tmp_path):
    """`--candidate` goes through the same loader, so it gains the same
    coverage -- it used to traceback identically."""
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(json.dumps(_MINIMAL_DESCRIPTOR), encoding="utf-8")
    candidate = tmp_path / "bad_candidate.json"
    candidate.write_bytes(b'\xff\xfe{"layout":"x.gds"}')
    with pytest.raises(EvalError, match="could not read"):
        run_eval(str(descriptor), str(candidate))


def test_eval_cli_non_utf8_descriptor_emits_an_error_envelope(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)

    exit_code = main(["eval", str(path), "--format", "json"])

    assert exit_code == 1
    error = _error_envelope(capsys)["error"]
    assert error["command"] == "eval"
    assert "could not read" in error["message"]


@_skip_as_root
def test_eval_cli_unreadable_descriptor_emits_an_error_envelope(tmp_path, capsys):
    path = tmp_path / "noread.json"
    path.write_text(json.dumps(_MINIMAL_DESCRIPTOR), encoding="utf-8")
    path.chmod(0o000)

    exit_code = main(["eval", str(path), "--format", "json"])

    assert exit_code == 1
    error = _error_envelope(capsys)["error"]
    assert error["command"] == "eval"
    assert "could not read" in error["message"]


# --------------------------------------------------------------------------- #
# klt yield-sensitivity -- the samples document
# --------------------------------------------------------------------------- #


def test_yield_sensitivity_non_utf8_samples_raises_sensitivityerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b'\xff\xfe{"measurements":[]}')
    with pytest.raises(SensitivityError, match="could not read"):
        run_sensitivity(str(path))


@_skip_as_root
def test_yield_sensitivity_unreadable_samples_raises_sensitivityerror(tmp_path):
    """Already an error before issue #5485 (the copy did catch `OSError`) --
    pinned here so routing through the shared loader cannot regress it."""
    path = tmp_path / "noread.json"
    path.write_text(json.dumps({"measurements": []}), encoding="utf-8")
    path.chmod(0o000)
    with pytest.raises(SensitivityError, match="could not read"):
        run_sensitivity(str(path))


def test_yield_sensitivity_cli_non_utf8_samples_emits_an_error_envelope(
    tmp_path, capsys
):
    path = tmp_path / "bad.json"
    path.write_bytes(b'\xff\xfe{"measurements":[]}')

    exit_code = main(["yield-sensitivity", str(path), "--format", "json"])

    assert exit_code == 1
    error = _error_envelope(capsys)["error"]
    assert error["command"] == "yield-sensitivity"
    assert "could not read" in error["message"]


def test_yield_sensitivity_still_names_a_missing_samples_file(tmp_path):
    """The verb-specific "samples file not found" wording is kept locally --
    the shared loader's own message does not name the document."""
    with pytest.raises(SensitivityError, match="samples file not found"):
        run_sensitivity(str(tmp_path / "nope.json"))
