"""``klt drc --klayout-binary`` / ``$KLT_KLAYOUT_BINARY`` override tests
(issue #4950; split from test_drc_klayout_engine.py for the file-size ratchet)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import klayout
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
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/klayout")
    result = run_drc_klayout_engine(gds, deck)
    assert _deck_launches(calls)[0][0] == "klayout"
    assert result["environment"] == {"klayout_binary": "/usr/bin/klayout"}


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


# --------------------------------------------------------------------------- #
# Binary-derived provenance (issue #5660)
# --------------------------------------------------------------------------- #
#
# `provenance.klayout_version` for `--engine klayout` must name the
# standalone binary that ran the deck -- not the pip `klayout` wheel in
# klt's own env -- and `klayout_version_mismatch`/its warning must follow
# that same value. The wheel's version survives as the additive
# `klayout_module_version`. These live here rather than in
# tests/test_provenance.py because that file is at its file-size-ratchet
# ceiling and this is binary-engine behavior end to end.


def _expect(monkeypatch, version):
    from klayout_tools import build_identity

    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: version
    )


def test_default_run_records_binary_version_in_provenance(
    tmp_path, monkeypatch, capsys
):
    _expect(monkeypatch, "0.30.12")
    _stub_versioned_klayout(monkeypatch, version_stdout="KLayout 0.30.12\n")
    gds, deck = _inputs(tmp_path)

    report = run_drc_klayout_engine(gds, deck)

    assert report["provenance"]["klayout_version"] == "0.30.12"
    assert report["provenance"]["klayout_module_version"] == klayout.__version__
    assert report["provenance"]["klayout_version_mismatch"] is False
    assert "klt: warning" not in capsys.readouterr().err


def test_override_binary_version_tracks_the_flag(tmp_path, monkeypatch):
    """A `--klayout-binary` whose version differs from both the wheel and
    PATH's `klayout` is the version provenance records (the report's own
    reproduction claim: it names the engine that actually ran)."""
    _expect(monkeypatch, "0.30.9")
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    gds, deck = _inputs(tmp_path)
    exe = _fake_klayout_exe(tmp_path / "pinned-klayout")
    _stub_versioned_klayout(monkeypatch, version_stdout="KLayout 0.30.9\n")

    report = run_drc_klayout_engine(gds, deck, klayout_binary=exe)

    assert report["provenance"]["klayout_version"] == "0.30.9"
    assert report["provenance"]["klayout_module_version"] == klayout.__version__


@pytest.mark.parametrize(
    "kwargs",
    [
        {"probe_returncode": 2},
        {"version_stdout": "garbage"},
        {"version_stdout": ""},
        {"probe_side_effect": FileNotFoundError("nope")},
        {"probe_side_effect": subprocess.TimeoutExpired(cmd="klayout", timeout=1)},
    ],
)
def test_soft_failed_probe_records_null_and_completes(tmp_path, monkeypatch, kwargs):
    """No `--min-klayout-version` declared: a failed/unparseable probe must
    not fail the run (no new failure mode on the default path) -- it records
    `klayout_version: null` and never a mismatch."""
    _expect(monkeypatch, "0.30.12")
    calls = _stub_versioned_klayout(monkeypatch, **kwargs)
    gds, deck = _inputs(tmp_path)

    report = run_drc_klayout_engine(gds, deck)

    assert report["provenance"]["klayout_version"] is None
    assert report["provenance"]["klayout_module_version"] == klayout.__version__
    assert report["provenance"]["klayout_version_mismatch"] is False
    assert len(_deck_launches(calls)) == 1


def test_binary_probe_launched_exactly_once_per_run(tmp_path, monkeypatch):
    calls = _stub_versioned_klayout(monkeypatch, version_stdout="KLayout 0.30.12\n")
    gds, deck = _inputs(tmp_path)

    run_drc_klayout_engine(gds, deck)

    assert [c[1:] for c in calls if c[1:] == ["-v"]] == [["-v"]]


def test_no_override_environment_records_which_then_bare_name(tmp_path, monkeypatch):
    monkeypatch.delenv("KLT_KLAYOUT_BINARY", raising=False)
    _stub_versioned_klayout(monkeypatch)
    gds, deck = _inputs(tmp_path)

    monkeypatch.setattr("shutil.which", lambda _: "/opt/klayout/bin/klayout")
    assert run_drc_klayout_engine(gds, deck)["environment"] == {
        "klayout_binary": "/opt/klayout/bin/klayout"
    }

    monkeypatch.setattr("shutil.which", lambda _: None)
    assert run_drc_klayout_engine(gds, deck)["environment"] == {
        "klayout_binary": "klayout"
    }


def test_binary_mismatch_warning_names_the_application_not_the_wheel(
    tmp_path, monkeypatch, capsys
):
    """The mismatch advisory and warning follow the binary-derived value, so
    they fire on a run whose *engine* is untested -- and the remedy pins the
    application, never `--with klayout==` (which cannot reproduce this run)."""
    _expect(monkeypatch, "0.30.12")
    _stub_versioned_klayout(monkeypatch, version_stdout="KLayout 0.28.16\n")
    gds, deck = _inputs(tmp_path)

    report = run_drc_klayout_engine(gds, deck)

    assert report["provenance"]["klayout_version"] == "0.28.16"
    assert report["provenance"]["klayout_version_mismatch"] is True
    err = capsys.readouterr().err
    assert "standalone KLayout application" in err
    assert "0.28.16" in err and "0.30.12" in err
    assert "--klayout-binary" in err
    assert "--with klayout==" not in err


def test_build_provenance_override_pair_semantics(monkeypatch, capsys):
    """Unit contract of the pair: the default caller stays byte-identical
    (wheel version, no `klayout_module_version` key -- the curated engine and
    `klt lvs` pin), the override pair re-points `klayout_version` (including
    to `null` on a soft-failed probe) and moves the mismatch computation."""
    from klayout_tools import _provenance, build_identity

    monkeypatch.setattr(_provenance, "_klayout_version", lambda: "0.30.13")
    monkeypatch.setattr(
        build_identity, "_recorded_klayout_version_expected", lambda: "0.30.12"
    )

    default = _provenance.build_provenance(include_klayout_version_mismatch=True)
    assert default["klayout_version"] == "0.30.13"
    assert "klayout_module_version" not in default
    assert default["klayout_version_mismatch"] is True  # wheel really differs
    assert capsys.readouterr().err.count("klt: warning:") == 1

    overridden = _provenance.build_provenance(
        include_klayout_version_mismatch=True,
        klayout_version_override="0.30.12",
        klayout_version_override_active=True,
    )
    assert overridden["klayout_version"] == "0.30.12"
    assert overridden["klayout_module_version"] == "0.30.13"
    assert overridden["klayout_version_mismatch"] is False  # engine matches
    assert capsys.readouterr().err == ""  # no wheel-worded warning either

    soft = _provenance.build_provenance(
        include_klayout_version_mismatch=True,
        klayout_version_override=None,
        klayout_version_override_active=True,
    )
    assert soft["klayout_version"] is None
    assert soft["klayout_module_version"] == "0.30.13"
    assert soft["klayout_version_mismatch"] is False
