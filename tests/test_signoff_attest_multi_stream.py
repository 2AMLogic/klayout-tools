"""``klt signoff --attest 2`` with repeatable ``--input`` (issue #4935)."""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import AttestationError, render_envelope
from klayout_tools.signoff_attest_stream import (
    build_stream_attestation,
    check_stream_attestation,
)


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "layout").mkdir()
    (repo / "evidence").mkdir()
    return repo


def _write_gds(path: Path, *, dbu: float = 0.001, empty: bool = False) -> str:
    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("top")
    if not empty:
        top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 2000))
    layout.write(str(path))
    return str(path)


def _stream(tmp_path, **kw) -> tuple[Path, str]:
    repo = _repo(tmp_path)
    return repo, _write_gds(repo / "layout" / "top.gds", **kw)


def _hash(envelope: dict) -> str:
    return envelope["provenance"]["input"]["content_hash"]


def _bind(repo: Path, envelope: dict) -> dict:
    env_path = repo / "evidence" / "layout.json"
    env_path.write_text(render_envelope(envelope))
    manifest = {
        "block": "demo",
        "kind": "analog",
        "evidence": {"2": {"file": str(env_path), "content_hash": _hash(envelope)}},
    }
    report = build_tier_report(manifest)
    return next(i for i in report["items"] if i["id"] == 2)


def _two(tmp_path) -> tuple[Path, str, str]:
    repo = _repo(tmp_path)
    (repo / "layout" / "sub").mkdir()
    a = _write_gds(repo / "layout" / "a.gds")
    b = _write_gds(repo / "layout" / "sub" / "a.gds", dbu=0.0005)
    return repo, a, b


def test_multi_stream_binds_set_and_checks_each(tmp_path):
    from klayout_tools.signoff import input_set_digest

    repo, a, b = _two(tmp_path)

    envelope = build_stream_attestation([b, a])

    assert envelope["status"] == "pass"
    binding = envelope["provenance"]["input"]
    assert "path" not in binding
    assert [m["path"]["path"] for m in binding["paths"]] == [
        "layout/a.gds",
        "layout/sub/a.gds",
    ]
    assert binding["content_hash"] == input_set_digest(
        (m["path"]["path"], m["content_hash"]) for m in binding["paths"]
    )
    checks = envelope["attestation"]["checks"]
    assert len(checks) == 6
    assert {c["path"] for c in checks} == {"layout/a.gds", "layout/sub/a.gds"}
    assert _bind(repo, envelope)["status"] == "met"


def test_multi_stream_order_independent(tmp_path):
    _repo_, a, b = _two(tmp_path)
    assert build_stream_attestation([a, b]) == build_stream_attestation([b, a])


def test_single_stream_list_equals_string(tmp_path):
    _repo_, stream = _stream(tmp_path)
    assert build_stream_attestation([stream]) == build_stream_attestation(stream)


def test_editing_one_member_is_stale_and_check_drifts(tmp_path):
    repo, a, b = _two(tmp_path)
    envelope = build_stream_attestation([a, b])
    env_path = repo / "evidence" / "layout.json"
    env_path.write_text(render_envelope(envelope))
    assert _bind(repo, envelope)["status"] == "met"

    _write_gds(Path(b), dbu=0.0005, empty=False)
    layout = kdb.Layout()
    layout.dbu = 0.0005
    layout.create_cell("top").shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 50, 50))
    layout.write(b)

    item = _bind(repo, envelope)
    assert item["status"] != "met"
    assert "stale" in json.dumps(item)
    assert check_stream_attestation([a, b], str(env_path))["status"] != "match"


def test_one_bad_stream_fails_and_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    a = _write_gds(repo / "layout" / "a.gds")
    b = _write_gds(repo / "layout" / "b.gds", empty=True)
    out = repo / "evidence" / "layout.json"

    code = main(
        [
            "signoff",
            "--attest",
            "2",
            "--input",
            a,
            "--input",
            b,
            "-o",
            str(out),
            "--format",
            "json",
        ]
    )

    assert code == 3
    assert not out.exists()


def test_multi_stream_cli_refusals(tmp_path, capsys):
    _repo_, a, b = _two(tmp_path)
    base = ["signoff", "--attest", "2", "--format", "json"]
    assert main([*base, "--input", a, "--input", a]) == 1
    assert "duplicate" in capsys.readouterr().err
    claim = ["--generator", "x", "--regenerated-hash", "sha256:" + "0" * 64]
    assert main([*base, "--input", a, "--input", b, *claim]) == 1
    assert "more than one --input" in capsys.readouterr().err
    assert main([*base, "--input", a, "--input", b]) == 0
    capsys.readouterr()
    assert (
        main(
            ["signoff", "--attest", "2", "--input", a, "--input", b, "--format", "text"]
        )
        == 0
    )
    assert "input set: 2 files" in capsys.readouterr().out


def test_multi_stream_different_repos_refused(tmp_path):
    repo = _repo(tmp_path)
    a = _write_gds(repo / "layout" / "a.gds")
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    b = _write_gds(other / "b.gds")
    with pytest.raises(AttestationError, match="same git repository"):
        build_stream_attestation([a, b])
