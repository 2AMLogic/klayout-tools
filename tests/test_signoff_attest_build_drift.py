"""``klt signoff --attest --check`` build_drift (issue #5875)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from klayout_tools.cli import main
from klayout_tools.signoff_attest import build_attestation, render_envelope

_PINS = {
    "pdk": {"name": "sky130A", "revision": "0fe599b2afb6708d281543108caf8310912f54af"},
    "tools": {"ngspice": "42"},
}
_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]


def _seed(tmp_path, version="__unset__"):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for d in ("tb", "attest", "evidence"):
        (repo / d).mkdir()
    (repo / "tb" / "a.spice").write_text("* a\n")
    inv = repo / "attest" / "item9.json"
    inv.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "t1-attestation-inventory",
                "t1_item": 9,
                "pins": _PINS,
                "testbenches": [{"path": "tb/a.spice", "cold_start": {"argv": _OK}}],
            }
        )
    )
    env = build_attestation(9, str(inv))
    if version is None:
        env.get("provenance", {}).pop("klt_version", None)
    elif version != "__unset__":
        env.setdefault("provenance", {})["klt_version"] = version
    out = repo / "evidence" / "item9.json"
    out.write_text(render_envelope(env))
    return str(inv), out


def _check(inv, out, capsys, *extra):
    capsys.readouterr()
    rc = main(
        ["signoff", "--attest", "9", "--inventory", inv, "--check", str(out)]
        + list(extra)
        + ["--format", "json"]
    )
    return rc, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("flag", [[], ["--require-build-match"]])
def test_current_build_passes(tmp_path, capsys, flag):
    inv, out = _seed(tmp_path)
    rc, res = _check(inv, out, capsys, *flag)
    assert rc == 0
    assert res["build_drift"]["state"] == "current"


def test_stale_is_report_only_without_flag(tmp_path, capsys):
    inv, out = _seed(tmp_path, "0.0.1+gdeadbeef")
    rc, res = _check(inv, out, capsys)
    assert rc == 0
    assert res["status"] == "match"
    assert res["build_drift"]["state"] == "stale"
    assert res["build_drift"]["cited_klt_version"] == "0.0.1+gdeadbeef"


def test_stale_fails_with_flag(tmp_path, capsys):
    inv, out = _seed(tmp_path, "0.0.1+gdeadbeef")
    rc, res = _check(inv, out, capsys, "--require-build-match")
    assert rc == 3
    assert res["status"] == "drifted"
    assert [d["field"] for d in res["drift"]] == ["provenance.klt_version"]


@pytest.mark.parametrize("version", [None, "0.1.0+unknown"])
def test_unverifiable_never_fails(tmp_path, capsys, version):
    inv, out = _seed(tmp_path, version)
    rc, res = _check(inv, out, capsys, "--require-build-match")
    assert rc == 0
    assert res["build_drift"]["state"] == "unverifiable"


def test_flag_with_content_drift_reports_both(tmp_path, capsys):
    inv, out = _seed(tmp_path, "0.0.1+gdeadbeef")
    (Path(inv).parent.parent / "tb" / "a.spice").write_text("* changed\n")
    rc, res = _check(inv, out, capsys, "--require-build-match")
    assert rc == 3
    fields = [d["field"] for d in res["drift"]]
    assert "provenance.klt_version" in fields
    assert len(fields) > 1


def test_flag_refused_without_check(tmp_path, capsys):
    inv, _ = _seed(tmp_path)
    capsys.readouterr()
    rc = main(
        [
            "signoff",
            "--attest",
            "9",
            "--inventory",
            inv,
            "--require-build-match",
            "--format",
            "json",
        ]
    )
    assert rc != 0
    assert "--require-build-match" in capsys.readouterr().err


def test_flag_refused_without_attest(capsys):
    rc = main(["signoff", "--require-build-match", "--format", "json"])
    assert rc != 0


def test_text_line(tmp_path, capsys):
    inv, out = _seed(tmp_path)
    capsys.readouterr()
    main(["signoff", "--attest", "9", "--inventory", inv, "--check", str(out)])
    assert "build_drift: current" in capsys.readouterr().out
