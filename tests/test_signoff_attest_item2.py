"""``klt signoff --attest 2 --input STREAM`` (issue #4188)."""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools import signoff_attest as attest_module
from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_stream import (
    build_stream_attestation,
    check_stream_attestation,
)

_GENERATOR = "python gen.py -o layout/top.gds"


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


def test_pass_envelope_shape_and_binds_met(tmp_path):
    repo, stream = _stream(tmp_path)

    envelope = build_stream_attestation(stream)

    assert envelope["kind"] == "generic"
    assert envelope["status"] == "pass"
    assert envelope["t1_item"] == 2
    assert envelope["attestation"]["producer"] == "klt signoff --attest"
    assert envelope["provenance"]["input"]["path"] == {
        "path": "layout/top.gds",
        "scope": "repo",
    }
    assert envelope["provenance"]["klayout_version"]
    assert _hash(envelope).startswith("sha256:")
    assert [c["check"] for c in envelope["attestation"]["checks"]] == [
        "stream_readable",
        "top_cell_non_empty",
        "dbu_on_grid",
    ]
    item = _bind(repo, envelope)
    assert item["status"] == "met"
    binding = item["citation"]["artifact_binding"]
    assert binding["attestation"]["producer"] == "klt signoff --attest"
    assert binding["attestation"]["check_count"] == 3


def test_unreadable_stream_fails_all_checks(tmp_path):
    repo = _repo(tmp_path)
    bad = repo / "layout" / "bad.gds"
    bad.write_bytes(b"not a gds stream")

    envelope = build_stream_attestation(str(bad))

    assert envelope["status"] == "fail"
    assert envelope["attestation"]["passed_count"] == 0


def test_empty_layout_fails(tmp_path):
    _, stream = _stream(tmp_path, empty=True)

    envelope = build_stream_attestation(stream)

    failed = [c["check"] for c in envelope["attestation"]["checks"] if not c["passed"]]
    assert envelope["status"] == "fail"
    assert failed == ["top_cell_non_empty"]


def test_off_grid_dbu_fails(tmp_path):
    _, stream = _stream(tmp_path, dbu=0.0003)

    envelope = build_stream_attestation(stream)

    failed = [c["check"] for c in envelope["attestation"]["checks"] if not c["passed"]]
    assert failed == ["dbu_on_grid"]


def test_matching_regeneration_claim_is_met(tmp_path):
    repo, stream = _stream(tmp_path)
    digest = _hash(build_stream_attestation(stream))

    envelope = build_stream_attestation(
        stream, generator=_GENERATOR, regenerated_hash=digest
    )

    assert envelope["provenance"]["regeneration"] == {
        "generator": _GENERATOR,
        "regenerated_hash": digest,
    }
    item = _bind(repo, envelope)
    assert item["status"] == "met"
    assert item["citation"]["artifact_binding"]["regeneration"] == {
        "generator": _GENERATOR,
        "regenerated_hash": digest,
        "executed_by_klt": False,
    }


def test_mismatched_regeneration_claim_is_stale(tmp_path):
    repo, stream = _stream(tmp_path)

    envelope = build_stream_attestation(
        stream, generator=_GENERATOR, regenerated_hash="sha256:" + "0" * 64
    )

    item = _bind(repo, envelope)
    assert item["status"] != "met"
    assert item["reason"] == "stale_artifact"


def test_geometry_hash_kind_is_recorded(tmp_path):
    from klayout_tools._provenance import layout_geometry_digest

    repo, stream = _stream(tmp_path)
    geometry = layout_geometry_digest(stream)

    envelope = build_stream_attestation(
        stream, generator=_GENERATOR, regenerated_hash=geometry, hash_kind="geometry"
    )

    assert envelope["provenance"]["regeneration"]["hash_kind"] == "geometry"
    assert _bind(repo, envelope)["status"] == "met"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"generator": _GENERATOR},
        {"regenerated_hash": "sha256:" + "a" * 64},
        {"generator": _GENERATOR, "regenerated_hash": "sha256:xyz"},
    ],
)
def test_bad_regeneration_claims_are_refused(tmp_path, kwargs):
    _, stream = _stream(tmp_path)

    with pytest.raises(AttestationError):
        build_stream_attestation(stream, **kwargs)


def test_symlink_and_non_repo_streams_are_refused(tmp_path):
    repo, stream = _stream(tmp_path)
    link = repo / "layout" / "link.gds"
    link.symlink_to(stream)
    with pytest.raises(AttestationError, match="symlink"):
        build_stream_attestation(str(link))

    outside = tmp_path / "outside"
    outside.mkdir()
    loose = _write_gds(outside / "top.gds")
    with pytest.raises(AttestationError, match="git repository"):
        build_stream_attestation(loose)


def test_check_detects_drift(tmp_path):
    repo, stream = _stream(tmp_path)
    committed = repo / "evidence" / "layout.json"
    committed.write_text(render_envelope(build_stream_attestation(stream)))

    assert check_stream_attestation(stream, str(committed))["status"] == "match"

    _write_gds(Path(stream), dbu=0.002)
    result = check_stream_attestation(stream, str(committed))
    assert result["status"] != "match"


def test_cli_item2_flow_and_flag_refusals(tmp_path, capsys):
    repo, stream = _stream(tmp_path)
    out = repo / "evidence" / "layout.json"

    code = main(
        [
            "signoff",
            "--attest",
            "2",
            "--input",
            stream,
            "-o",
            str(out),
            "--format",
            "json",
        ]
    )
    assert code == 0
    assert json.loads(out.read_text())["t1_item"] == 2
    assert (
        main(
            [
                "signoff",
                "--attest",
                "2",
                "--input",
                stream,
                "--check",
                str(out),
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(["signoff", "--attest", "2", "--inventory", stream, "--format", "json"])
        == 1
    )
    assert main(["signoff", "--attest", "2", "--format", "json"]) == 1
    assert (
        main(["signoff", "--attest", "1", "--input", stream, "--format", "json"]) == 1
    )
    assert (
        main(
            [
                "signoff",
                "--attest",
                "2",
                "--input",
                stream,
                "--generator",
                "x",
                "--format",
                "json",
            ]
        )
        == 1
    )
    assert main(["signoff", "--input", stream, "--format", "json"]) == 1
    code = main(["signoff", "--attest", "2", "--input", stream, "--format", "text"])
    assert code == 0
    assert "stream:" in capsys.readouterr().out


def test_failing_cli_attest_writes_nothing(tmp_path):
    repo, stream = _stream(tmp_path, empty=True)
    out = repo / "evidence" / "layout.json"

    code = main(
        [
            "signoff",
            "--attest",
            "2",
            "--input",
            stream,
            "-o",
            str(out),
            "--format",
            "json",
        ]
    )

    assert code == 3
    assert not out.exists()


def test_inventory_items_unchanged_still_reject_item2():
    with pytest.raises(AttestationError):
        build_attestation(2, "nonexistent.json")
    assert attest_module.ATTESTABLE_ITEMS == (1, 8, 9, 10)


# --- declared checks via optional --inventory (issue #4767) ---------------- #


def _inv(repo: Path, checks: list, **extra) -> str:
    path = repo / "evidence" / "item2-inv.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "t1-attestation-inventory",
                "t1_item": 2,
                "checks": checks,
                **extra,
            }
        )
    )
    return str(path)


_PASS = {"name": "ok", "argv": ["python3", "-c", "pass"]}
_FAIL = {"name": "bad", "argv": ["python3", "-c", "import sys; sys.exit(4)"]}


def test_declared_checks_recorded_and_pass(tmp_path):
    repo, stream = _stream(tmp_path)
    inv = _inv(repo, [_PASS])
    env = build_stream_attestation(stream, inventory_path=inv)
    assert env["status"] == "pass"
    last = env["attestation"]["checks"][-1]
    assert last["role"] == "audit" and last["name"] == "ok"
    assert last["argv"] == _PASS["argv"] and last["exit_status"] == 0
    assert env["attestation"]["check_count"] == 4
    assert env["attestation"]["inventory"]["path"]["path"] == "evidence/item2-inv.json"


def test_declared_check_failure_fails_envelope(tmp_path):
    repo, stream = _stream(tmp_path)
    inv = _inv(repo, [_PASS, _FAIL])
    env = build_stream_attestation(stream, inventory_path=inv)
    assert env["status"] == "fail"
    assert env["attestation"]["checks"][-1]["exit_status"] == 4
    code = main(
        [
            "signoff",
            "--attest",
            "2",
            "--input",
            stream,
            "--inventory",
            inv,
            "--format",
            "json",
        ]
    )
    assert code == 3


def test_no_inventory_envelope_unchanged(tmp_path):
    repo, stream = _stream(tmp_path)
    env = build_stream_attestation(stream)
    assert "inventory" not in env["attestation"]
    assert env["attestation"]["check_count"] == 3


def test_declared_check_timeout(tmp_path):
    repo, stream = _stream(tmp_path)
    slow = {
        "name": "slow",
        "argv": ["python3", "-c", "import time; time.sleep(5)"],
        "timeout_s": 0.5,
    }
    env = build_stream_attestation(stream, inventory_path=_inv(repo, [slow]))
    last = env["attestation"]["checks"][-1]
    assert env["status"] == "fail" and last["error"] == "timeout"


def test_declared_checks_check_mode_drift_and_local_only(tmp_path):
    repo, stream = _stream(tmp_path)
    local = {**_FAIL, "name": "local", "local_only": True}
    inv = _inv(repo, [_PASS, {**_PASS, "name": "lo", "local_only": True}])
    env = build_stream_attestation(stream, inventory_path=inv)
    committed = repo / "evidence" / "item2.json"
    committed.write_text(render_envelope(env))
    result = check_stream_attestation(stream, str(committed), inventory_path=inv)
    assert result["status"] == "match"
    assert [c["name"] for c in result["skipped_checks"]] == ["lo"]
    # Editing the inventory is drift.
    inv2 = _inv(repo, [_PASS, local])
    drift = check_stream_attestation(stream, str(committed), inventory_path=inv2)
    assert drift["status"] != "match"


@pytest.mark.parametrize(
    "inventory",
    [
        {"checks": []},
        {"checks": [_PASS, _PASS]},
        {"checks": [_PASS], "t1_item": 9},
        {"checks": [_PASS], "stream": "layout/other.gds"},
        {"checks": [{"name": "x"}]},
    ],
)
def test_malformed_item2_inventory_refused(tmp_path, inventory):
    repo, stream = _stream(tmp_path)
    doc = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 2,
        **inventory,
    }
    path = repo / "evidence" / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(AttestationError):
        build_stream_attestation(stream, inventory_path=str(path))
