"""``klt signoff --attest --records``: record format 2 (issue #5945).

One line per covered file in a records file, no committed aggregate, a
judgment-only envelope pinned to the inventory -- and the gate unchanged: a
covered file whose line was not renewed fails ``--check`` and the manifest
grader. Classic-format behaviour is covered by the other
``test_signoff_attest_*`` files, which this change leaves untouched."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from klayout_tools import signoff_records as rec
from klayout_tools.cli import main
from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import AttestationError, build_attestation
from test_signoff import (
    _OK,
    _attest_repo,
    _hash_of,
    _inventory,
    _item,
    _manifest,
    _write_inventory,
)

RECORDS = "attest/item{item}.files.ndjson"


def _item1_inventory(**derived_extra) -> dict:
    derived = {
        "path": "netlist/ota.spice",
        "generator": "klt netlist",
        **(derived_extra or {"check": {"argv": _OK}}),
    }
    return _inventory(1, derived=[derived])


def _attest(repo: Path, item: int, inventory: str, capsys=None) -> tuple[int, Path]:
    envelope = repo / "evidence" / f"item{item}.json"
    records = repo / RECORDS.format(item=item)
    code = main(
        [
            "signoff",
            "--attest",
            str(item),
            "--inventory",
            inventory,
            "--records",
            str(records),
            "-o",
            str(envelope),
        ]
    )
    if capsys is not None:
        capsys.readouterr()
    return code, envelope


def _check(item: int, inventory: str, envelope: Path, capsys) -> tuple[int, dict]:
    code = main(
        [
            "signoff",
            "--attest",
            str(item),
            "--inventory",
            inventory,
            "--check",
            str(envelope),
            "--format",
            "json",
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def _lines(repo: Path, item: int) -> list[dict]:
    return rec.parse_records((repo / RECORDS.format(item=item)).read_text()) or []


@pytest.fixture
def item1(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1, _item1_inventory())
    code, envelope = _attest(repo, 1, inventory, capsys)
    assert code == 0
    return repo, inventory, envelope


def test_records_file_has_one_sorted_line_per_covered_file(item1):
    repo, _inventory_path, _envelope = item1

    lines = _lines(repo, 1)

    assert [line["path"] for line in lines] == [
        "design/bias.sch",
        "design/ota.sch",
        "netlist/ota.spice",
    ]
    assert {line["role"] for line in lines} == {"source", "derived"}
    text = (repo / RECORDS.format(item=1)).read_text()
    assert text == rec.render_records(lines)
    assert text.count("\n") == len(lines)  # one line each


def test_committed_envelope_carries_no_aggregate_or_count(item1):
    repo, inventory, envelope = item1

    doc = json.loads(envelope.read_text())

    assert doc["attestation"]["record_format"] == rec.RECORD_FORMAT
    assert "check_count" not in doc["attestation"]
    assert "passed_count" not in doc["attestation"]
    assert not any(c["check"] == "present" for c in doc["attestation"]["checks"])
    assert not re.search(r"\d+/\d+", doc["summary"])
    assert doc["summary"] == (
        "T1 item 1 (Design sources): attestation checks passed (klt signoff --attest)"
    )
    # The pin is the inventory's hash, not an aggregate of covered files.
    assert doc["provenance"]["input"]["content_hash"] == _hash_of(inventory)
    assert "paths" not in doc["provenance"]["input"]
    assert doc["provenance"]["input"]["records"]["path"] == {
        "path": "attest/item1.files.ndjson",
        "scope": "repo",
    }


def test_live_result_recomputes_the_counts(tmp_path):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1, _item1_inventory())

    result = build_attestation(1, inventory, None, str(repo / RECORDS.format(item=1)))

    attestation = result["attestation"]
    assert attestation["check_count"] == attestation["passed_count"] > 0
    assert (
        f"{attestation['passed_count']}/{attestation['check_count']}"
        in (result["summary"])
    )


def test_untouched_tree_checks_clean_and_rewrite_is_byte_stable(item1, capsys):
    repo, inventory, envelope = item1
    before = (envelope.read_bytes(), (repo / RECORDS.format(item=1)).read_bytes())

    code, result = _check(1, inventory, envelope, capsys)
    again, _ = _attest(repo, 1, inventory)
    capsys.readouterr()

    assert (code, result["status"], result["fresh_status"]) == (0, "match", "pass")
    assert again == 0
    assert before == (
        envelope.read_bytes(),
        (repo / RECORDS.format(item=1)).read_bytes(),
    )


def test_changing_a_covered_file_without_renewing_its_line_fails_check(item1, capsys):
    repo, inventory, envelope = item1
    (repo / "design" / "ota.sch").write_text("edited\n")

    code, result = _check(1, inventory, envelope, capsys)

    assert code == 3
    assert result["status"] == "drifted"
    fields = [d["field"] for d in result["drift"]]
    assert fields == ["records.design/ota.sch.sha256"]
    assert result["drift"][0]["check"] == {"role": "source", "path": "design/ota.sch"}
    # Renewing exactly that line is what makes it pass again.
    assert _attest(repo, 1, inventory)[0] == 0
    capsys.readouterr()
    assert _check(1, inventory, envelope, capsys)[0] == 0


@pytest.mark.parametrize("damage", ["missing", "extra", "unsorted"])
def test_missing_extra_or_noncanonical_lines_fail_check(item1, capsys, damage):
    repo, inventory, envelope = item1
    records = repo / RECORDS.format(item=1)
    lines = records.read_text().splitlines(keepends=True)
    if damage == "missing":
        records.write_text("".join(lines[:-1]))
    elif damage == "extra":
        stray = rec.render_records(
            [{"path": "zz/extra.txt", "role": "source", "sha256": "0" * 64}]
        )
        records.write_text("".join(lines) + stray)
    else:
        records.write_text("".join(reversed(lines)))

    code, result = _check(1, inventory, envelope, capsys)

    assert code == 3
    assert result["status"] == "drifted"
    assert result["drift"]


def test_deleting_the_records_file_fails_check(item1, capsys):
    repo, inventory, envelope = item1
    (repo / RECORDS.format(item=1)).unlink()

    code, result = _check(1, inventory, envelope, capsys)

    assert code == 3
    assert result["drift"][0]["committed"] == "missing"


def test_deleted_covered_file_fails_the_attestation(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1, _item1_inventory())
    (repo / "design" / "bias.sch").unlink()

    code, envelope = _attest(repo, 1, inventory, capsys)

    assert code == 3  # a failing attestation writes nothing
    assert not envelope.exists()
    assert not (repo / RECORDS.format(item=1)).exists()


def _graded(repo: Path, envelope: Path, item: int) -> dict:
    pin = json.loads(envelope.read_text())["provenance"]["input"]["content_hash"]
    manifest = _manifest(
        evidence={str(item): {"file": str(envelope), "content_hash": pin}}
    )
    return _item(build_tier_report(manifest, repo_root=str(repo)), item)


def test_grader_meets_an_untouched_record_format_attestation(item1):
    repo, _inventory_path, envelope = item1

    graded = _graded(repo, envelope, 1)

    assert graded["status"] == "met"
    binding = graded["citation"]["artifact_binding"]
    assert binding["input_verified"] is True
    assert {m["status"] for m in binding["members"]} == {"verified"}
    assert binding["attestation"]["record_format"] == rec.RECORD_FORMAT


def test_grader_marks_an_unrenewed_covered_file_stale(item1):
    repo, _inventory_path, envelope = item1
    (repo / "design" / "ota.sch").write_text("edited after attestation\n")

    graded = _graded(repo, envelope, 1)

    assert graded["status"] == "unmet"
    assert graded["reason"] == "stale_evidence"
    stale = [
        m["path"]["path"]
        for m in graded["detail"]["artifact_binding"]["members"]
        if m["status"] == "stale"
    ]
    assert stale == ["design/ota.sch"]


def test_grader_cannot_verify_a_deleted_covered_file_or_records_file(item1):
    repo, _inventory_path, envelope = item1
    (repo / "design" / "bias.sch").unlink()
    assert _graded(repo, envelope, 1)["reason"] == "unverifiable_provenance"

    (repo / "design" / "bias.sch").write_text("v {xschem bias}\n")
    (repo / RECORDS.format(item=1)).unlink()
    assert _graded(repo, envelope, 1)["status"] == "unmet"


def test_grader_refuses_a_malformed_or_empty_records_file(item1):
    repo, _inventory_path, envelope = item1
    records = repo / RECORDS.format(item=1)
    for text in ("not json\n", ""):
        records.write_text(text)
        assert _graded(repo, envelope, 1)["status"] == "unmet"


def test_changing_the_inventory_still_stales_the_pin(item1):
    repo, inventory, envelope = item1
    Path(inventory).write_text(Path(inventory).read_text() + "\n")

    assert _graded(repo, envelope, 1)["reason"] == "stale_evidence"


@pytest.mark.parametrize("item", [9, 10])
def test_items_9_and_10_round_trip_and_grade(tmp_path, capsys, item):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, item)
    code, envelope = _attest(repo, item, inventory, capsys)
    assert code == 0

    lines = _lines(repo, item)
    assert [line["path"] for line in lines] == sorted(line["path"] for line in lines)
    assert len(lines) >= 2
    assert _check(item, inventory, envelope, capsys)[0] == 0
    assert _graded(repo, envelope, item)["status"] == "met"

    target = repo / lines[0]["path"]
    target.write_text(target.read_text() + "changed\n")
    assert _check(item, inventory, envelope, capsys)[0] == 3
    assert _graded(repo, envelope, item)["reason"] == "stale_evidence"


def test_derived_line_records_its_sources(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(
        repo, 1, _item1_inventory(recorded={"sources": ["design/ota.sch"]})
    )
    code, envelope = _attest(repo, 1, inventory, capsys)
    assert code == 0

    derived = next(line for line in _lines(repo, 1) if line["role"] == "derived")
    ota = next(line for line in _lines(repo, 1) if line["path"] == "design/ota.sch")
    assert derived["sources"] == {"design/ota.sch": ota["sha256"]}
    # Renewing the source renews the derived artifact's line, or --check fails.
    (repo / "design" / "ota.sch").write_text("edited\n")
    assert _check(1, inventory, envelope, capsys)[0] == 3


def test_a_hash_map_in_the_inventory_is_refused_with_records(tmp_path):
    repo = _attest_repo(tmp_path)
    hashed = {"derived_sha256": "0" * 64, "sources": {"design/ota.sch": "1" * 64}}
    inventory = _write_inventory(repo, 1, _item1_inventory(recorded=hashed))

    with pytest.raises(AttestationError, match="live in the records file"):
        build_attestation(1, inventory, None, str(repo / RECORDS.format(item=1)))


def test_records_flag_is_attest_only_and_not_with_check(tmp_path, capsys):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 10)
    code, envelope = _attest(repo, 10, inventory, capsys)
    assert code == 0

    assert main(["signoff", "--records", "r.ndjson", "--manifest", "m.json"]) == 1
    assert (
        main(
            [
                "signoff",
                "--attest",
                "10",
                "--inventory",
                inventory,
                "--check",
                str(envelope),
                "--records",
                "r.ndjson",
            ]
        )
        == 1
    )
    capsys.readouterr()


def test_classic_envelope_is_unchanged_by_this_format(tmp_path):
    repo = _attest_repo(tmp_path)
    inventory = _write_inventory(repo, 1)

    envelope = build_attestation(1, inventory)

    assert "record_format" not in envelope["attestation"]
    assert "records" not in envelope
    assert "paths" in envelope["provenance"]["input"]


# --------------------------------------------------------------------------- #
# The point of the format: git merges PRs that touch disjoint covered files.
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _git_repo(tmp_path) -> tuple[Path, str]:
    repo = _attest_repo(tmp_path)
    (repo / ".git").rmdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    sources = ["design/a.sch", "design/b.sch", "design/c.sch"]
    for name in sources:
        (repo / name).write_text(f"v {{{name}}}\n")
    inventory = _write_inventory(
        repo, 1, _inventory(1, sources=sources, derived=_item1_inventory()["derived"])
    )
    assert _attest(repo, 1, inventory)[0] == 0
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    return repo, inventory


def _renew(repo: Path, inventory: str, branch: str, name: str) -> None:
    assert _git(repo, "checkout", "-q", "-b", branch, "main").returncode == 0
    (repo / name).write_text(f"edited on {branch}\n")
    assert _attest(repo, 1, inventory)[0] == 0
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", branch).returncode == 0


def test_prs_touching_disjoint_files_merge_with_plain_git(tmp_path, capsys):
    repo, inventory = _git_repo(tmp_path)
    _renew(repo, inventory, "pr-a", "design/a.sch")
    _renew(repo, inventory, "pr-c", "design/c.sch")
    envelope = repo / "evidence" / "item1.json"
    # Neither PR changed the envelope: only its own file's line moved.
    assert _git(repo, "diff", "--name-only", "main", "pr-a").stdout.split() == [
        "attest/item1.files.ndjson",
        "design/a.sch",
    ]

    assert _git(repo, "checkout", "-q", "pr-a").returncode == 0
    merged = _git(repo, "merge", "--no-edit", "pr-c")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    capsys.readouterr()
    code, result = _check(1, inventory, envelope, capsys)
    assert (code, result["status"], result["fresh_status"]) == (0, "match", "pass")
    assert _graded(repo, envelope, 1)["status"] == "met"


def test_two_prs_renewing_the_same_file_still_conflict(tmp_path):
    repo, inventory = _git_repo(tmp_path)
    _renew(repo, inventory, "pr-a1", "design/a.sch")
    _renew(repo, inventory, "pr-a2", "design/a.sch")

    assert _git(repo, "checkout", "-q", "pr-a1").returncode == 0

    assert _git(repo, "merge", "--no-edit", "pr-a2").returncode != 0
