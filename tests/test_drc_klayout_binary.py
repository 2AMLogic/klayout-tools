"""``klt drc --klayout-binary`` / ``$KLT_KLAYOUT_BINARY`` override tests
(issue #4950; split from test_drc_klayout_engine.py for the file-size ratchet)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.drc import DrcError, run_drc_klayout_engine
from test_drc_klayout_engine import (
    _deck_launches,
    _inputs,
    _stub_versioned_klayout,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _fake_klayout_exe(path: Path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_klayout_binary_flag_used_for_probe_and_deck(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    gds, deck = _inputs(tmp_path)
    exe = _fake_klayout_exe(tmp_path / "pinned-klayout")
    calls = _stub_versioned_klayout(monkeypatch)

    result = run_drc_klayout_engine(
        gds, deck, min_klayout_version="0.30.9", klayout_binary=exe
    )

    assert [c[0] for c in calls] == [exe, exe]
    assert calls[0][1:] == ["-v"]
    assert result["environment"] == {"klayout_binary": exe}


def test_klayout_binary_precedence_flag_env_path(tmp_path, monkeypatch):
    gds, deck = _inputs(tmp_path)
    flag_exe = _fake_klayout_exe(tmp_path / "flag-klayout")
    env_exe = _fake_klayout_exe(tmp_path / "env-klayout")

    calls = _stub_versioned_klayout(monkeypatch)
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", env_exe)
    run_drc_klayout_engine(gds, deck, klayout_binary=flag_exe)
    assert _deck_launches(calls)[0][0] == flag_exe

    calls.clear()
    run_drc_klayout_engine(gds, deck)
    assert _deck_launches(calls)[0][0] == env_exe

    calls.clear()
    monkeypatch.setenv("KLT_KLAYOUT_BINARY", "")  # empty is ignored
    result = run_drc_klayout_engine(gds, deck)
    assert _deck_launches(calls)[0][0] == "klayout"
    assert "environment" not in result


def test_klayout_binary_unrunnable_override_names_source_no_fallback(
    tmp_path, monkeypatch
):
    gds, deck = _inputs(tmp_path)
    calls = _stub_versioned_klayout(monkeypatch)
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)

    with pytest.raises(DrcError, match="--klayout-binary"):
        run_drc_klayout_engine(gds, deck, klayout_binary=str(tmp_path / "nope"))

    monkeypatch.setenv("KLT_KLAYOUT_BINARY", str(tmp_path / "nope"))
    with pytest.raises(DrcError, match="KLT_KLAYOUT_BINARY"):
        run_drc_klayout_engine(gds, deck)
    assert calls == []


def test_klayout_binary_old_override_still_version_checked(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    gds, deck = _inputs(tmp_path)
    exe = _fake_klayout_exe(tmp_path / "old-klayout")
    calls = _stub_versioned_klayout(monkeypatch, version_stdout="KLayout 0.28.16")

    with pytest.raises(DrcError) as old:
        run_drc_klayout_engine(
            gds, deck, min_klayout_version="0.30.9", klayout_binary=exe
        )
    assert old.value.code == "klayout_version_too_old"
    assert calls == [[exe, "-v"]]


def test_klayout_binary_cli_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    gds, deck = _inputs(tmp_path)
    exe = _fake_klayout_exe(tmp_path / "cli-klayout")
    calls = _stub_versioned_klayout(monkeypatch)

    code = main(
        [
            "drc",
            gds,
            "--engine",
            "klayout",
            "--deck-file",
            deck,
            "--klayout-binary",
            exe,
            "--format",
            "json",
        ]
    )

    assert code == 4  # empty deck: no rule coverage, not a clean verdict
    assert _deck_launches(calls)[0][0] == exe
    assert json.loads(capsys.readouterr().out)["environment"]["klayout_binary"] == exe
