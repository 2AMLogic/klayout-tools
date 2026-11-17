"""``klt signoff --attest 2`` inventory ``inputs`` (issue #5724)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools.signoff import input_set_digest
from klayout_tools.signoff_attest import AttestationError, render_envelope
from klayout_tools.signoff_attest_stream import (
    build_stream_attestation,
    check_stream_attestation,
)
from test_signoff_attest_item2 import _GENERATOR, _PASS, _bind, _hash, _stream


def _inputs_repo(tmp_path) -> tuple[Path, str]:
    repo, stream = _stream(tmp_path)
    (repo / "tools").mkdir()
    (repo / "tools" / "gen.py").write_text("print(1)\n")
    (repo / "tools" / "cells.json").write_text("{}\n")
    return repo, stream


def _inv(repo: Path, **fields) -> str:
    path = repo / "evidence" / "item2-inv.json"
    doc = {"schema_version": 1, "kind": "t1-attestation-inventory", "t1_item": 2}
    path.write_text(json.dumps({**doc, **fields}))
    return str(path)


def test_inputs_pinned_as_sorted_input_checks_after_builtins_before_audit(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    inv = _inv(repo, inputs=["tools/gen.py", "tools/cells.json"], checks=[_PASS])
    env = build_stream_attestation(stream, inventory_path=inv)
    checks = env["attestation"]["checks"]
    assert env["status"] == "pass"
    assert [c["role"] for c in checks] == ["layout"] * 3 + ["input"] * 2 + ["audit"]
    pins = [c for c in checks if c["role"] == "input"]
    assert [c["path"] for c in pins] == ["tools/cells.json", "tools/gen.py"]
    assert all(c["passed"] and c["content_hash"].startswith("sha256:") for c in pins)
    assert env["attestation"]["check_count"] == 6


def test_inputs_without_checks_accepted_neither_refused(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    env = build_stream_attestation(
        stream, inventory_path=_inv(repo, inputs=["tools/gen.py"])
    )
    assert env["status"] == "pass" and env["attestation"]["check_count"] == 4
    with pytest.raises(AttestationError, match="'checks'.*'inputs'"):
        build_stream_attestation(stream, inventory_path=_inv(repo))


def test_missing_input_fails_the_envelope_and_exit_3(tmp_path):
    from klayout_tools.cli import main

    repo, stream = _inputs_repo(tmp_path)
    inv = _inv(repo, inputs=["tools/gone.py"])
    env = build_stream_attestation(stream, inventory_path=inv)
    assert env["status"] == "fail"
    pin = env["attestation"]["checks"][-1]
    assert pin["passed"] is False and pin["content_hash"] is None
    code = main(
        ["signoff", "--attest", "2", "--input", stream, "--inventory", inv]
        + ["--format", "json"]
    )
    assert code == 3


def test_binding_is_role_tagged_set_digest_over_stream_and_inputs(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    inv = _inv(repo, inputs=["tools/gen.py", "tools/cells.json"])
    env = build_stream_attestation(stream, inventory_path=inv)
    binding = env["provenance"]["input"]
    assert "path" not in binding
    members = binding["paths"]
    assert [(m["path"]["path"], m["role"]) for m in members] == [
        ("layout/top.gds", "stream"),
        ("tools/cells.json", "input"),
        ("tools/gen.py", "input"),
    ]
    assert binding["content_hash"] == input_set_digest(
        (m["path"]["path"], m["content_hash"]) for m in members
    )
    assert _bind(repo, env)["status"] == "met"


@pytest.mark.parametrize(
    "inputs",
    [[], ["tools/gen.py", "tools/gen.py"], ["tools/./gen.py", "tools/gen.py"]]
    + [["layout/top.gds"], ["/etc/passwd"], ["../outside.py"], "tools/gen.py"],
)
def test_malformed_inputs_refused(tmp_path, inputs):
    repo, stream = _inputs_repo(tmp_path)
    with pytest.raises(AttestationError):
        build_stream_attestation(stream, inventory_path=_inv(repo, inputs=inputs))


def test_symlinked_input_refused(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    (repo / "tools" / "link.py").symlink_to(repo / "tools" / "gen.py")
    with pytest.raises(AttestationError, match="symlink"):
        build_stream_attestation(
            stream, inventory_path=_inv(repo, inputs=["tools/link.py"])
        )


def test_editing_only_an_input_grades_the_item_stale(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    env = build_stream_attestation(
        stream, inventory_path=_inv(repo, inputs=["tools/gen.py"])
    )
    assert _bind(repo, env)["status"] == "met"
    (repo / "tools" / "gen.py").write_text("print(2)\n")
    item = _bind(repo, env)
    assert item["status"] == "unmet" and item["reason"] == "stale_evidence"
    members = item["detail"]["artifact_binding"]["members"]
    assert {m["path"]["path"]: m["status"] for m in members} == {
        "layout/top.gds": "verified",
        "tools/gen.py": "stale",
    }


def test_check_reports_drift_on_edited_input_even_when_commands_local_only(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    inv = _inv(repo, inputs=["tools/gen.py"], checks=[{**_PASS, "local_only": True}])
    committed = repo / "evidence" / "item2.json"
    committed.write_text(
        render_envelope(build_stream_attestation(stream, inventory_path=inv))
    )
    assert (
        check_stream_attestation(stream, str(committed), inventory_path=inv)["status"]
        == "match"
    )
    (repo / "tools" / "gen.py").write_text("print(2)\n")
    drift = check_stream_attestation(stream, str(committed), inventory_path=inv)
    assert drift["status"] != "match"


def test_inputs_compose_with_multi_stream(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    other = Path(stream).with_name("other.gds")
    other.write_bytes(Path(stream).read_bytes())
    inv = _inv(repo, inputs=["tools/gen.py"])
    env = build_stream_attestation([stream, str(other)], inventory_path=inv)
    roles = [
        (m["path"]["path"], m["role"]) for m in env["provenance"]["input"]["paths"]
    ]
    assert roles == [
        ("layout/other.gds", "stream"),
        ("layout/top.gds", "stream"),
        ("tools/gen.py", "input"),
    ]
    assert _bind(repo, env)["status"] == "met"
    with pytest.raises(AttestationError):  # a second stream may not be an input
        build_stream_attestation(
            [stream, str(other)],
            inventory_path=_inv(repo, inputs=["layout/other.gds"]),
        )


def test_inputs_compose_with_regeneration_claim_against_the_set_digest(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    inv = _inv(repo, inputs=["tools/gen.py"])
    digest = _hash(build_stream_attestation(stream, inventory_path=inv))
    env = build_stream_attestation(
        stream, inventory_path=inv, generator=_GENERATOR, regenerated_hash=digest
    )
    assert env["provenance"]["regeneration"]["regenerated_hash"] == digest
    assert _bind(repo, env)["status"] == "met"
    committed = repo / "evidence" / "item2.json"
    committed.write_text(render_envelope(env))
    result = check_stream_attestation(stream, str(committed), inventory_path=inv)
    assert result["status"] == "match"


def test_no_inputs_envelope_is_unchanged(tmp_path):
    repo, stream = _inputs_repo(tmp_path)
    plain = build_stream_attestation(stream)
    with_inv = build_stream_attestation(
        stream, inventory_path=_inv(repo, checks=[_PASS])
    )
    assert plain["provenance"]["input"]["path"] == {
        "path": "layout/top.gds",
        "scope": "repo",
    }
    assert with_inv["provenance"]["input"] == plain["provenance"]["input"]
    assert all(c["role"] != "input" for c in with_inv["attestation"]["checks"])
