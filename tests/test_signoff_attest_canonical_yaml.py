"""Canonical-YAML ``hygiene`` entries (issue #5862).

A ``{"path": ..., "attest": "yaml", "select"?}`` entry binds its file by the
named ``sha256-canonical-yaml-v1`` digest of its canonical parse, so a
comment-only or reformat edit is neither ``--check`` drift nor
``stale_evidence`` -- while a ``continue-on-error: true`` on the signoff job
still is. Item 10 ``hygiene`` only, like ``presence`` (#5615).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import (
    AttestationError,
    build_attestation,
    check_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_canonical_yaml import (
    HASH_ALGORITHM,
    content_digest,
)
from klayout_tools.signoff_attest_text import note_lines

_OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
_CI = ".github/workflows/ci.yml"

_WORKFLOW = """\
name: ci
on:
  push:
    branches: [main]
jobs:
  signoff:
    runs-on: ubuntu-latest
    steps:
      - name: attest-check
        run: klt signoff --attest 10 --inventory inv.json --check evidence/item10.json
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: make lint
"""

# Same parsed document as _WORKFLOW: comment added, keys reordered,
# whitespace reflowed, flow style, anchors/aliases for the repeated runner.
_WORKFLOW_RESPelled = """\
# pinned by canonical parse (issue #5862)
definitions:
  runner: &runner ubuntu-latest
jobs:
  lint:
    steps: [{run: make lint}]
    runs-on: *runner
  signoff:
    steps:
      - name: attest-check
        run: klt signoff --attest 10 --inventory inv.json --check evidence/item10.json
    runs-on: *runner
name: ci
on: {push: {branches: [main]}}
"""


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "evidence").mkdir()
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(_WORKFLOW)
    (repo / "README.md").write_text("# readme\n")
    return repo


def _inv(repo: Path, hygiene=None, item: int = 10, **fields) -> str:
    inventory: dict = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": item,
        **fields,
    }
    if item == 10:
        inventory["hygiene"] = hygiene
        inventory.setdefault("checks", [{"name": "ok", "argv": _OK}])
    path = repo / "inv.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _yaml_inv(repo: Path, select: str | None = None) -> str:
    entry: dict = {"path": _CI, "attest": "yaml"}
    if select is not None:
        entry["select"] = select
    return _inv(repo, ["README.md", entry])


def _checks(envelope: dict) -> dict[str, dict]:
    return {
        c["path"]: c
        for c in envelope["attestation"]["checks"]
        if c["check"] == "present"
    }


def _edit_workflow(repo: Path, edit) -> None:
    path = repo / ".github" / "workflows" / "ci.yml"
    path.write_text(edit(path.read_text()))


def _committed(repo: Path, inventory: str) -> str:
    path = repo / "evidence" / "item10.json"
    path.write_text(render_envelope(build_attestation(10, inventory)))
    return str(path)


# --- the recorded check ------------------------------------------------------ #


def test_yaml_entry_records_mode_selector_and_named_algorithm(tmp_path):
    """AC: the check records ``attest``, ``select`` and its own
    ``hash_algorithm``; the envelope-level algorithm stays raw-bytes."""
    repo = _repo(tmp_path)
    envelope = build_attestation(10, _yaml_inv(repo, "jobs.signoff"))
    check = _checks(envelope)[_CI]
    assert check["check"] == "present"
    assert check["role"] == "hygiene"
    assert check["passed"] is True
    assert check["attest"] == "yaml"
    assert check["select"] == "jobs.signoff"
    assert check["hash_algorithm"] == HASH_ALGORITHM
    assert check["raw_content_hash"] == (
        f"sha256:{hashlib.sha256(_WORKFLOW.encode()).hexdigest()}"
    )
    assert envelope["attestation"]["hash_algorithm"] == "sha256-raw-bytes"
    # the canonical hash is of the selected subtree's parse, not the bytes
    assert check["content_hash"] == content_digest(
        _WORKFLOW.encode(), "jobs.signoff", "<where>"
    )


def test_canonical_digest_is_select_scoped(tmp_path):
    """AC: ``select`` hashes only the named subtree."""
    repo = _repo(tmp_path)
    envelope = build_attestation(10, _yaml_inv(repo, "jobs.signoff"))
    check = _checks(envelope)[_CI]
    assert check["content_hash"] == content_digest(
        _WORKFLOW.encode(), "jobs.signoff", "<where>"
    )
    assert check["content_hash"] != content_digest(
        _WORKFLOW.encode(), "jobs.lint", "<where>"
    )
    assert check["content_hash"] != content_digest(_WORKFLOW.encode(), None, "<where>")


def test_yaml_entry_without_select_hashes_the_whole_document(tmp_path):
    repo = _repo(tmp_path)
    envelope = build_attestation(10, _yaml_inv(repo))
    check = _checks(envelope)[_CI]
    assert "select" not in check
    assert check["content_hash"] == content_digest(_WORKFLOW.encode(), None, "<where>")


def test_missing_yaml_file_fails_presence_like_raw_mode(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".github" / "workflows" / "ci.yml").unlink()
    envelope = build_attestation(10, _yaml_inv(repo, "jobs.signoff"))
    check = _checks(envelope)[_CI]
    assert check["passed"] is False
    assert check["content_hash"] is None  # the missing-file marker
    assert "raw_content_hash" not in check
    assert envelope["status"] == "fail"


# --- --check: the relief and the pin ----------------------------------------- #


def test_comment_reorder_and_reflow_edits_stay_at_match_with_a_note(tmp_path):
    """AC: comment-only, whitespace-only, key-reorder and anchor/alias
    refactors leave ``--check`` at ``match``, exit-path 0, with the raw-byte
    difference reported as an informational note."""
    repo = _repo(tmp_path)
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow.write_text(
        _WORKFLOW.replace(
            "jobs:", "definitions:\n  runner: &runner ubuntu-latest\njobs:"
        ).replace("runs-on: ubuntu-latest", "runs-on: *runner")
    )
    inventory = _yaml_inv(repo, "jobs.signoff")
    committed = _committed(repo, inventory)
    workflow.write_text(_WORKFLOW_RESPelled)

    result = check_attestation(10, inventory, committed)

    assert result["status"] == "match", result.get("drift")
    assert result["fresh_status"] == "pass"
    assert result["notes"] == [
        {
            "check": {"role": "hygiene", "path": _CI},
            "path": _CI,
            "note": 'raw bytes differ; canonical yaml hash unchanged (attest: "yaml")',
        }
    ]
    assert note_lines(result) == [
        f"[NOTE] {_CI}: raw bytes differ; canonical yaml hash unchanged "
        '(attest: "yaml")'
    ]


def test_check_text_format_prints_the_note_line(tmp_path, capsys):
    """AC: the raw-bytes-differ note reaches the live ``--format text``
    output -- the text-path counterpart of the JSON ``notes`` assertions,
    driven through ``_print_attestation_check_text`` (#5862)."""
    repo = _repo(tmp_path)
    inventory = _yaml_inv(repo, "jobs.signoff")
    committed = _committed(repo, inventory)
    _edit_workflow(repo, lambda text: "# comment-only edit\n" + text)

    argv = ["signoff", "--attest", "10", "--inventory", inventory]
    code = main([*argv, "--check", committed, "--format", "text"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "fresh_status: pass" in lines
    assert (
        f"[NOTE] {_CI}: raw bytes differ; canonical yaml hash unchanged "
        '(attest: "yaml")'
    ) in lines


_STEP_ORIGINAL = (
    "      - name: attest-check\n"
    "        run: klt signoff --attest 10 --inventory inv.json"
    " --check evidence/item10.json\n"
)
_STEP_KEYS_SWAPPED = (
    "      - run: klt signoff --attest 10 --inventory inv.json"
    " --check evidence/item10.json\n"
    "        name: attest-check\n"
)


def test_key_reorder_inside_a_list_element_stays_at_match(tmp_path):
    """AC: reordering keys *inside a list element* (the step's ``run:``
    before its ``name:``) is not drift -- the canonicaliser recurses into
    sequences, so mapping key order inside ``steps:`` is not load-bearing.
    The axis the reflow fixture above never exercised."""
    for select in ("jobs.signoff", None):
        repo = _repo(tmp_path / (select or "whole-document"))
        inventory = _yaml_inv(repo, select)
        committed = _committed(repo, inventory)
        _edit_workflow(repo, lambda s: s.replace(_STEP_ORIGINAL, _STEP_KEYS_SWAPPED))
        result = check_attestation(10, inventory, committed)
        assert result["status"] == "match", (select, result.get("drift"))
        assert result["fresh_status"] == "pass"
        assert result["notes"] == [
            {
                "check": {"role": "hygiene", "path": _CI},
                "path": _CI,
                "note": "raw bytes differ; canonical yaml hash unchanged "
                '(attest: "yaml")',
            }
        ]


def test_safe_dump_reformat_of_the_real_workflow_stays_at_match(tmp_path):
    """AC: a ``yaml.safe_dump(sort_keys=True)`` reformat of this repo's
    real workflow -- key order sorted at every level, *including inside
    list items* -- is semantics-preserving (parse-equal), so ``match``
    with the informational note, never drift."""
    real = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    original = real.read_bytes()
    reformatted = yaml.safe_dump(
        yaml.safe_load(original.decode("utf-8")), sort_keys=True
    ).encode("utf-8")
    assert yaml.safe_load(reformatted.decode("utf-8")) == yaml.safe_load(
        original.decode("utf-8")
    )
    repo = _repo(tmp_path)
    workflow = repo / ".github/workflows/ci.yml"
    workflow.write_bytes(original)
    inventory = _yaml_inv(repo, "jobs.signoff-example")
    committed = _committed(repo, inventory)
    workflow.write_bytes(reformatted)
    result = check_attestation(10, inventory, committed)
    assert result["status"] == "match", result.get("drift")
    assert note_lines(result) == [
        f"[NOTE] {_CI}: raw bytes differ; canonical yaml hash unchanged "
        '(attest: "yaml")'
    ]


def test_continue_on_error_added_fails_check(tmp_path):
    """AC: adding ``continue-on-error: true`` to the selected job fails
    ``--check`` -- the exact edit the raw-byte pin exists for."""
    repo = _repo(tmp_path)
    inventory = _yaml_inv(repo, "jobs.signoff")
    committed = _committed(repo, inventory)
    _edit_workflow(
        repo,
        lambda s: s.replace("  signoff:", "  signoff:\n    continue-on-error: true"),
    )
    result = check_attestation(10, inventory, committed)
    assert result["status"] != "match"
    assert any(e["field"].endswith(".content_hash") for e in result["drift"])


def test_step_removed_or_run_changed_fails_check(tmp_path):
    """AC: removing a step, or changing a ``run:`` command inside the
    selected subtree, fails ``--check``."""
    repo = _repo(tmp_path)
    inventory = _yaml_inv(repo, "jobs.signoff")
    committed = _committed(repo, inventory)
    _edit_workflow(repo, lambda s: s.replace("      - name: attest-check\n", ""))
    assert check_attestation(10, inventory, committed)["status"] != "match"

    committed = _committed(repo, inventory)  # re-attest the new content
    _edit_workflow(
        repo,
        lambda s: s.replace(
            "--check evidence/item10.json", "--check evidence/item10.json || true"
        ),
    )
    assert check_attestation(10, inventory, committed)["status"] != "match"


def test_edit_to_an_unrelated_job_stays_at_match(tmp_path):
    """AC: an edit to a job outside ``select`` leaves ``--check`` matching
    (an edit inside the named job is the continue-on-error / step case)."""
    repo = _repo(tmp_path)
    inventory = _yaml_inv(repo, "jobs.signoff")
    committed = _committed(repo, inventory)
    _edit_workflow(
        repo,
        lambda s: s.replace("  lint:", "  newlint:\n    steps: [{run: x}]\n  lint:"),
    )
    assert check_attestation(10, inventory, committed)["status"] == "match"


def test_mode_flip_and_select_change_are_drift(tmp_path):
    """AC: flipping an entry between ``content`` and ``yaml``, or changing
    its ``select``, fails ``--check`` until re-attested."""
    repo = _repo(tmp_path)
    committed = _committed(repo, _inv(repo, [_CI]))
    assert check_attestation(10, _yaml_inv(repo), committed)["status"] != "match"
    committed = _committed(repo, _yaml_inv(repo, "jobs.signoff"))
    for changed in ("jobs.lint", None):
        assert (
            check_attestation(10, _yaml_inv(repo, changed), committed)["status"]
            != "match"
        )


# --- manifest grading (mechanism 3: #4197 re-hash) --------------------------- #


def _manifest(repo: Path, inventory: str) -> dict:
    envelope_path = repo / "evidence" / "item10.json"
    envelope_path.write_text(json.dumps(build_attestation(10, inventory), indent=2))
    digest = hashlib.sha256(Path(inventory).read_bytes()).hexdigest()
    return {
        "block": "demo",
        "kind": "analog",
        "evidence": {
            "10": {"file": str(envelope_path), "content_hash": f"sha256:{digest}"}
        },
    }


def test_manifest_row_relief_and_pin(tmp_path):
    """AC: comment-only and unrelated-job edits leave the ``--manifest`` row
    ``met`` (the canonical re-hash); a semantic edit inside ``select`` makes
    it ``unmet`` with reason ``stale_evidence``."""
    repo = _repo(tmp_path)
    manifest = _manifest(repo, _yaml_inv(repo, "jobs.signoff"))

    def row() -> dict:
        report = build_tier_report(manifest, repo_root=str(repo))
        return next(i for i in report["items"] if i["id"] == 10)

    assert row()["status"] == "met"
    _edit_workflow(repo, lambda s: "# a comment\n" + s)
    assert row()["status"] == "met"
    _edit_workflow(
        repo,
        lambda s: s.replace("  lint:", "  newlint:\n    steps: [{run: x}]\n  lint:"),
    )
    assert row()["status"] == "met"
    _edit_workflow(
        repo,
        lambda s: s.replace("  signoff:", "  signoff:\n    continue-on-error: true"),
    )
    graded = row()
    assert graded["status"] == "unmet"
    assert graded["reason"] == "stale_evidence"


# --- bare-string inventories are untouched (fleet regression) ---------------- #


def test_bare_string_inventory_reattests_byte_identically(tmp_path):
    """AC: a bare-string inventory re-attests against an envelope in the
    exact pre-#5862 shape -- no new keys leak into raw checks, so the
    fleet's committed item-10 envelopes stay reproducible."""
    repo = _repo(tmp_path)
    inventory = _inv(repo, ["README.md", _CI])
    envelope = build_attestation(10, inventory)
    assert set(_checks(envelope)["README.md"]) == set(
        ("check", "role", "path", "passed", "content_hash")
    )
    # a committed envelope exactly as the pre-#5862 producer wrote it
    # (version strings are volatile for --check, so placeholders are fine)
    pre = json.loads(render_envelope(envelope))
    pre["provenance"]["klt_version"] = "0.9.0"
    committed = repo / "evidence" / "pre.json"
    committed.write_text(json.dumps(pre, indent=2) + "\n")
    result = check_attestation(10, inventory, str(committed))
    assert result["status"] == "match", result.get("drift")


# --- attest-time refusals ---------------------------------------------------- #


def test_unparseable_yaml_is_an_attest_time_error(tmp_path):
    """AC: a parse error is an ``AttestationError`` naming the path --
    never a silent byte-hash fallback."""
    repo = _repo(tmp_path)
    _edit_workflow(repo, lambda s: "key: [unclosed\n")
    with pytest.raises(AttestationError, match=r"ci\.yml.*not parseable"):
        build_attestation(10, _yaml_inv(repo))


def test_multi_document_file_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _edit_workflow(repo, lambda s: s + "\n---\nname: other\n")
    with pytest.raises(AttestationError, match=r"ci\.yml"):
        build_attestation(10, _yaml_inv(repo))


def test_set_node_is_an_attest_time_error_naming_the_path(tmp_path):
    """AC: a node with no deterministic canonical form (``!!set`` -- its
    ``repr`` order varies with ``PYTHONHASHSEED``, so a hashed digest could
    never reproduce) is an ``AttestationError`` naming the path, never a
    silently hashed fallback."""
    repo = _repo(tmp_path)
    _edit_workflow(repo, lambda s: s + "\nmatrix: !!set\n  ? a\n  ? b\n")
    with pytest.raises(AttestationError, match=r"ci\.yml.*unsupported node type 'set'"):
        build_attestation(10, _yaml_inv(repo))


def test_absent_select_path_is_an_attest_time_error(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match=r"jobs\.nope.*does not resolve"):
        build_attestation(10, _yaml_inv(repo, "jobs.nope"))


def test_select_through_a_scalar_and_through_a_list_are_errors(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match="does not resolve"):
        build_attestation(10, _yaml_inv(repo, "name.inside"))
    with pytest.raises(AttestationError, match="does not resolve"):
        build_attestation(10, _yaml_inv(repo, "jobs.signoff.steps.run"))


def test_select_may_name_a_scalar_subtree(tmp_path):
    """A ``select`` naming a scalar is legal: the subtree is the scalar."""
    repo = _repo(tmp_path)
    envelope = build_attestation(10, _yaml_inv(repo, "jobs.signoff.runs-on"))
    assert _checks(envelope)[_CI]["content_hash"] == content_digest(
        _WORKFLOW.encode(), "jobs.signoff.runs-on", "<where>"
    )


@pytest.mark.parametrize(
    ("entry", "needle"),
    [
        ({"path": _CI, "select": "jobs"}, "select"),
        ({"path": _CI, "attest": "presence", "select": "j"}, "select"),
        ({"path": _CI, "attest": "yaml", "select": "a..b"}, "empty segment"),
        ({"path": _CI, "attest": "yaml", "select": ""}, "non-empty"),
        ({"path": _CI, "attest": "yml"}, "must be one of"),
        ({"path": _CI, "attest": "yaml", "extra": 1}, "extra"),
    ],
)
def test_malformed_yaml_entries_are_refused(tmp_path, entry, needle):
    repo = _repo(tmp_path)
    with pytest.raises(AttestationError, match=needle):
        build_attestation(10, _inv(repo, [entry]))
