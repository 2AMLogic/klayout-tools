"""Regressions for `klt` verbs' JSON input loading (issues #5485, #5540).

A family of hand-rolled "open a JSON file, `json.load` it, wrap the failure"
readers kept drifting from the shared `_paths._load_request_json` by dropping
one of its `except` clauses -- usually `UnicodeDecodeError`, which is a
`ValueError` but neither an `OSError` nor a `json.JSONDecodeError`, so
`except (OSError, json.JSONDecodeError)` lets it through. A non-UTF-8 input
file then escaped as a raw `UnicodeDecodeError` traceback, breaking the rule
in `docs/json-contract.md` that every verb reports through the shared error
envelope. #5485 fixed the two `eval`/`yield-sensitivity` copies; #5540
routed the remaining readers onto the shared loader (or, where a
flag-specific message or a path-or-inline shape matters, widened their
`except` in place).

These tests pin the envelope, not the message wording -- `error.message` is
not a contracted field. They assert what the contract does promise: the
caller's own error class from the library surface, and exit 1 plus a
parseable envelope naming the command from the CLI surface.
"""

from __future__ import annotations

import json
import os

import pytest

from klayout_tools import (
    _report_verify,
    arith_gen,
    draw,
    env_provenance,
    gen,
    gen_compose,
    socket_check,
    tap_distance,
)
from klayout_tools._paths import _load_spec_json
from klayout_tools.cli import main
from klayout_tools.cli._parsing import (
    RequestFileError,
    load_json_path_or_inline,
    load_request_file,
)
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


# --------------------------------------------------------------------------- #
# issue #5540 -- every remaining reader resolves a non-UTF-8 file into the
# caller's own error class instead of a UnicodeDecodeError traceback
# --------------------------------------------------------------------------- #


def test_load_spec_json_non_utf8_raises_the_callers_error(tmp_path):
    """`klt erc`/`power`/`mom` read their spec through `_load_spec_json` --
    the one shared helper that still opened without `encoding=` and caught
    only `(OSError, json.JSONDecodeError)`."""
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(ValueError, match="could not read spec"):
        _load_spec_json(str(path), ValueError)


def test_load_json_path_or_inline_non_utf8_raises_the_callers_error(tmp_path):
    """Shared `--layers`/`--conductors`/... reader for the flag-taking verbs
    (`klt components`, `ring-check`, `keepout-check`, `array-check`, ...)."""
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(ValueError, match="could not read"):
        load_json_path_or_inline(str(path), "--layers", ValueError)


def test_draw_params_non_utf8_raises_drawerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(draw.DrawError, match="could not read params file"):
        draw.load_params_arg(str(path))


def test_gen_params_non_utf8_raises_generror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(gen.GenError, match="could not read params file"):
        gen.load_params_arg(str(path))


def test_socket_descriptor_non_utf8_raises_socketcheckerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(socket_check.SocketCheckError, match="could not read"):
        socket_check._load_descriptor(str(path))


def test_tap_distance_net_spec_non_utf8_raises_tapdistanceerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(tap_distance.TapDistanceError, match="could not read"):
        tap_distance._load_net_spec(str(path))


def test_arith_gen_cell_map_non_utf8_raises_arithgenerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(arith_gen.ArithGenError, match="could not read"):
        arith_gen._load_cell_map(str(path))


def test_env_provenance_envelope_non_utf8_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(
        env_provenance.EnvironmentProvenanceError, match="could not read"
    ):
        env_provenance._load_json_envelope(str(path))


def test_committed_report_non_utf8_raises_the_callers_error(tmp_path):
    """`klt drc`/`lvs` `--check REPORT` goes through
    `_report_verify.load_committed_report`, which used to catch the two
    exception types in the *wrong* order for this -- either way,
    `UnicodeDecodeError` escaped."""
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(ValueError, match="could not read"):
        _report_verify.load_committed_report(str(path), ValueError)


def test_generator_report_non_utf8_raises_gencomposeerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(gen_compose.GenComposeError, match="could not read"):
        gen_compose.load_generator_report_arg(str(path))


def test_cli_load_request_file_non_utf8_raises_requestfileerror(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)
    with pytest.raises(RequestFileError, match="could not read"):
        load_request_file(str(path))


# --------------------------------------------------------------------------- #
# issue #5540 -- the CLI request reads route through the shared loader, so a
# non-UTF-8 request file is an error envelope, not a traceback. These need no
# layout: the request is read before anything else.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["route"], id="route"),
        pytest.param(["gen-compose"], id="gen-compose"),
        pytest.param(["layout-plan", "execute"], id="layout-plan-execute"),
        pytest.param(
            ["layout-plan", "check", "--layout", "whatever.gds"],
            id="layout-plan-check",
        ),
    ],
)
def test_cli_non_utf8_request_emits_an_error_envelope(argv, tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_bytes(_NON_UTF8)

    exit_code = main([*argv, str(path), "--format", "json"])

    assert exit_code == 1
    error = _error_envelope(capsys)["error"]
    assert error["command"] == argv[0]
    assert "could not read" in error["message"]
