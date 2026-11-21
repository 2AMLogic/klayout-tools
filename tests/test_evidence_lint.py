"""`klt evidence lint` / `klayout_tools.evidence_lint` (issue #4043)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from klayout_tools import evidence_lint as el
from klayout_tools import evidence_record as er
from klayout_tools.cli import main

NOW = datetime(2026, 10, 22, 18, 30, 5, tzinfo=timezone.utc)


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "sim").mkdir()
    (root / "sim" / "req.json").write_text('{"corners": {}}\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _report(root):
    art = root.parent / "arts"
    art.mkdir(exist_ok=True)
    (art / "corner.cir").write_text("* deck\n")
    (art / "ngspice.log").write_text("log\n")
    report = {
        "schema_version": 1,
        "provenance": {
            "pdk": {"name": "sky130A"},
            "deck": {"name": "d", "content_hash": "sha256:abc"},
        },
        "corner_count": 1,
        "corners": [
            {
                "corner_id": "tt_1v8_27",
                "measurements": [],
                "artifacts": {
                    "log": str(art / "ngspice.log"),
                    "deck": str(art / "corner.cir"),
                },
            }
        ],
    }
    path = root.parent / "report.json"
    path.write_text(json.dumps(report))
    return path


def _record(repo, minutes=0, **kw):
    return er.write_record(
        str(_report(repo)),
        str(repo / "sim" / "req.json"),
        block="b",
        scope="s",
        root=str(repo),
        now=NOW + timedelta(minutes=minutes),
        **kw,
    )


def _lint(repo, **kw):
    kw.setdefault("check_append_only", False)
    return el.lint(str(repo), **kw)


def _codes(payload):
    return {f["code"] for f in payload["findings"]}


def _edit(repo, rel, fn):
    path = repo / rel
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def test_empty_evidence_dir_is_clean(repo):
    payload = _lint(repo)
    assert payload["status"] == "clean" and payload["records_checked"] == 0


def test_writer_round_trip_is_clean(repo):
    _record(repo)
    payload = _lint(repo)
    assert payload["findings"] == []
    assert payload["records_checked"] == 1


def test_shape_and_id_mismatch(repo):
    summary = _record(repo)
    _edit(repo, summary["record_path"], lambda d: d.pop("pdk_pin"))
    bad = repo / "evidence" / "sim" / "b" / "s" / "oops.json"
    bad.write_text("{}")
    assert {el.F_SHAPE, el.F_ID_MISMATCH} <= _codes(_lint(repo))


def test_unreadable_record(repo):
    _record(repo)
    (repo / "evidence" / "sim" / "b" / "s" / "20260101-000000-abcdef0.json").write_text(
        "{"
    )
    assert el.F_UNREADABLE in _codes(_lint(repo))


def test_supersession_clean_chain_then_violations(repo):
    first = _record(repo)
    second = _record(repo, minutes=1, supersedes=[first["record_path"]])
    assert _lint(repo)["findings"] == []

    # fork: a third record also supersedes the first
    _record(repo, minutes=2, supersedes=[first["record_path"]])
    assert el.F_SUPERSEDES_FORK in _codes(_lint(repo))

    # missing target
    _edit(
        repo,
        second["record_path"],
        lambda d: d.update(supersedes=["evidence/sim/b/s/nope.json"]),
    )
    assert el.F_SUPERSEDES_MISSING in _codes(_lint(repo))


def test_supersedes_omitted_and_scope_mismatch(repo):
    _record(repo)
    _record(repo, minutes=1)  # existing scope, no supersedes
    assert el.F_SUPERSEDES_OMITTED in _codes(_lint(repo))

    other = er.write_record(
        str(_report(repo)),
        str(repo / "sim" / "req.json"),
        block="b",
        scope="other",
        root=str(repo),
        now=NOW + timedelta(minutes=5),
        supersedes=[next(iter(_paths(repo, "s")))],
    )
    assert other and el.F_SUPERSEDES_SCOPE in _codes(_lint(repo))


def _paths(repo, scope):
    directory = repo / "evidence" / "sim" / "b" / scope
    return sorted(os.path.relpath(p, repo) for p in directory.glob("*.json"))


def test_self_supersession_is_cycle(repo):
    summary = _record(repo)
    _edit(
        repo,
        summary["record_path"],
        lambda d: d.update(supersedes=[summary["record_path"]]),
    )
    assert el.F_SUPERSEDES_CYCLE in _codes(_lint(repo))


def test_head_file(repo):
    first = _record(repo)
    second = _record(repo, minutes=1, supersedes=[first["record_path"]])
    head = repo / "evidence" / "sim" / "b" / "s" / "HEAD"
    head.write_text(os.path.basename(second["record_path"]) + "\n")
    assert _lint(repo)["findings"] == []
    head.write_text(os.path.basename(first["record_path"]) + "\n")
    assert el.F_HEAD in _codes(_lint(repo))


def test_sidecar_missing_and_hash(repo):
    summary = _record(repo)
    sidecar = repo / "evidence" / "sim" / "b" / "s" / f"{summary['record_id']}.d"
    deck = sidecar / "tt_1v8_27" / "corner.cir"
    assert deck.is_file()

    def add_hash(d):
        d["artifacts"]["corners"][0]["deck_sha256"] = "0" * 64

    _edit(repo, summary["record_path"], add_hash)
    assert el.F_SIDECAR_HASH in _codes(_lint(repo))
    deck.unlink()
    assert el.F_SIDECAR_MISSING in _codes(_lint(repo))


def test_verdict_rederivation(repo):
    summary = _record(repo)

    def claim(d):
        d["result"]["corners"][0]["status"] = "fail"
        d["result"]["measurements"] = []
        d["result"]["coverage"] = {
            "checked": ["x"],
            "skipped": [],
            "unknown": [],
            "malformed": [],
        }
        d["result"]["status"] = "pass"

    _edit(repo, summary["record_path"], claim)
    assert el.F_VERDICT in _codes(_lint(repo))

    _edit(repo, summary["record_path"], lambda d: d["result"].update(status="fail"))
    assert el.F_VERDICT not in _codes(_lint(repo))


def test_schema_required_field_and_corner_pattern(repo, tmp_path):
    summary = _record(repo)
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps(
            {
                "required_fields": {"rate": "string"},
                "extension_keys": ["rate"],
                "corner_id_pattern": "^(tt|ss|ff)_\\d+v\\d+_\\d+$",
            }
        )
    )
    schema = el.load_schema(str(schema_file))
    assert _lint(repo)["findings"] == []  # no schema: built-in checks only
    findings = _lint(repo, schema=schema)["findings"]
    assert [f["code"] for f in findings] == [el.F_SCHEMA]
    assert "rate" in findings[0]["message"]

    def conform(d):
        d["rate"] = 5
        d["extensions"] = {"lane": 1}
        d["result"]["corners"][0]["corner_id"] = "weird"

    _edit(repo, summary["record_path"], conform)
    messages = " ".join(f["message"] for f in _lint(repo, schema=schema)["findings"])
    assert "must be a string" in messages
    assert "'lane' not allowed" in messages
    assert "corner_id_pattern" in messages


def test_bad_schema_file(tmp_path):
    bad = tmp_path / "s.json"
    bad.write_text('{"nope": 1}')
    with pytest.raises(el.EvidenceLintError):
        el.load_schema(str(bad))


def test_append_only_git(repo):
    summary = _record(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evidence")
    _git(repo, "branch", "base")
    _git(repo, "checkout", "-q", "-b", "feat")

    # adding a record is fine
    second = _record(repo, minutes=1, supersedes=[summary["record_path"]])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add")
    assert el.lint(str(repo), base="base")["findings"] == []

    # modifying a committed record is reported
    _edit(repo, summary["record_path"], lambda d: d.update(subset_reason="edited"))
    _git(repo, "commit", "-qam", "edit")
    payload = el.lint(str(repo), base="base")
    assert el.F_APPEND_ONLY in _codes(payload)
    assert second["record_path"] not in {f["record"] for f in payload["findings"]}

    # deleting one is reported too
    _git(repo, "rm", "-q", summary["record_path"])
    _git(repo, "commit", "-qm", "rm")
    assert el.F_APPEND_ONLY in _codes(el.lint(str(repo), base="base"))


def test_non_git_root_and_missing_base(tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(el.EvidenceLintError) as exc:
        el.lint(str(plain))
    assert exc.value.code == el.CODE_NOT_GIT
    with pytest.raises(el.EvidenceLintError) as exc:
        el.lint(str(repo), base="no-such-ref")
    assert exc.value.code == el.CODE_BAD_BASE


def test_cli_json_and_exit_codes(repo, capsys):
    summary = _record(repo)
    argv = ["evidence", "lint", str(repo), "--no-append-only", "--format", "json"]
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1 and out["status"] == "clean"

    _edit(repo, summary["record_path"], lambda d: d.pop("scope"))
    assert main(argv) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["findings"][0]["code"] == el.F_SHAPE

    assert (
        main(["evidence", "lint", str(repo), "--base", "nope", "--format", "json"]) == 1
    )
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["code"] == el.CODE_BAD_BASE
