"""Tests for ``klt sim --timeout-s`` (issue #5036)."""

from __future__ import annotations

import pytest

from klayout_tools.cli import main
from klayout_tools.sim import SimError
from klayout_tools.sim_request_overrides import _with_timeout_override
from test_sim import _write_body, _write_request


def test_override_unset_returns_request_unchanged():
    request = {"options": {"timeout_s": 7200}}
    assert _with_timeout_override(request, None) is request


def test_override_wins_without_mutating_the_loaded_request():
    request = {"options": {"timeout_s": 7200, "waveforms": True}}
    out = _with_timeout_override(request, 300)
    assert out["options"] == {"timeout_s": 300, "waveforms": True}
    assert request["options"]["timeout_s"] == 7200


def test_override_creates_options_block():
    assert _with_timeout_override({}, 30)["options"] == {"timeout_s": 30}


@pytest.mark.parametrize("bad", [0, -1, True])
def test_override_rejects_non_positive(bad):
    with pytest.raises(SimError, match="--timeout-s must be a positive"):
        _with_timeout_override({}, bad)


def _request(tmp_path):
    _write_body(tmp_path)
    return _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 7200},
        },
    )


def test_cli_passes_timeout_to_run_sim(tmp_path, monkeypatch, capsys):
    from klayout_tools.cli import sim_cmd

    seen = {}

    def fake(path, **kwargs):
        seen.update(kwargs)
        return {"schema_version": 1, "status": "pass", "corners": []}

    monkeypatch.setattr(sim_cmd, "run_sim", fake)
    main(["sim", str(_request(tmp_path)), "--timeout-s", "300", "--format", "json"])
    capsys.readouterr()
    assert seen["timeout_s"] == 300


def test_cli_rejects_non_positive_timeout(tmp_path, capsys):
    code = main(
        [
            "sim",
            str(_request(tmp_path)),
            "--timeout-s",
            "0",
            "--dry-run",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert code != 0
    assert "--timeout-s must be a positive" in captured.out + captured.err


def test_cli_timeout_override_reaches_the_dry_run(tmp_path, capsys):
    code = main(
        [
            "sim",
            str(_request(tmp_path)),
            "--timeout-s",
            "5",
            "--dry-run",
            "--format",
            "json",
        ]
    )
    assert code == 0
    capsys.readouterr()
