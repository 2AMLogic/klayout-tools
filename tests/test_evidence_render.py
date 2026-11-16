"""``klt evidence render`` (issue #5274): fill, idempotence, stale detection,
fail-closed behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import evidence_render as er
from klayout_tools.cli import main

OPEN = "<!-- klt:table id=census -->"
CLOSE = "<!-- /klt:table -->"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _spec(rows=None) -> dict:
    return {
        "census": {
            "columns": [
                {"header": "Name"},
                {"header": "Count", "format": "int"},
                {"header": "Margin", "format": "{:.2f}"},
            ],
            "rows": rows
            or [
                [
                    "a",
                    {"record": "evidence/a.json", "pointer": "/n"},
                    {"record": "evidence/a.json", "pointer": "/m"},
                ],
                ["b", {"record": "evidence/b.json", "pointer": "/n"}, 1.5],
            ],
        }
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write_json(tmp_path / "evidence" / "a.json", {"n": 3, "m": 0.123})
    _write_json(tmp_path / "evidence" / "b.json", {"n": 7})
    _write_json(tmp_path / "spec.json", _spec())
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "d.md").write_text(f"intro\n{OPEN}\nold\n{CLOSE}\noutro\n")
    return tmp_path


def _render(repo: Path, check: bool = False, docs=("docs/*.md",)):
    return er.render(str(repo), list(docs), str(repo / "spec.json"), check=check)


def test_fill_and_outside_untouched(repo):
    payload = _render(repo)
    assert payload["status"] == "updated"
    assert payload["updated"] == ["docs/d.md#census"]
    text = (repo / "docs" / "d.md").read_text()
    assert text.startswith("intro\n" + OPEN + "\n| Name | Count | Margin |\n")
    assert "| a | 3 | 0.12 |" in text
    assert "| b | 7 | 1.50 |" in text
    assert text.endswith(CLOSE + "\noutro\n")


def test_idempotent_and_check_clean(repo):
    _render(repo)
    before = (repo / "docs" / "d.md").read_bytes()
    assert _render(repo)["status"] == "clean"
    assert _render(repo, check=True)["status"] == "clean"
    assert (repo / "docs" / "d.md").read_bytes() == before


def test_check_detects_stale_and_writes_nothing(repo):
    before = (repo / "docs" / "d.md").read_bytes()
    payload = _render(repo, check=True)
    assert payload["status"] == "stale"
    assert payload["stale"] == ["docs/d.md#census"]
    assert (repo / "docs" / "d.md").read_bytes() == before
    _render(repo)
    _write_json(repo / "evidence" / "b.json", {"n": 8})
    assert _render(repo, check=True)["stale"] == ["docs/d.md#census"]


def test_crlf_and_empty_body_and_multiple_blocks(repo):
    (repo / "docs" / "d.md").write_bytes(f"x\r\n{OPEN}{CLOSE}\r\ny\r\n".encode())
    _render(repo)
    data = (repo / "docs" / "d.md").read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert b"| a | 3 | 0.12 |" in data
    assert _render(repo, check=True)["status"] == "clean"


def test_multiple_blocks_per_doc(repo):
    spec = _spec()
    spec["two"] = _spec()["census"]
    _write_json(repo / "spec.json", spec)
    (repo / "docs" / "d.md").write_text(
        f"{OPEN}\n{CLOSE}\n<!-- klt:table id=two -->\n{CLOSE}\n"
    )
    payload = _render(repo)
    assert [b["id"] for b in payload["docs"][0]["blocks"]] == ["census", "two"]


def _fails(repo: Path, match: str, **kw) -> None:
    snapshot = {p: p.read_bytes() for p in repo.rglob("*.md")}
    with pytest.raises(er.EvidenceRenderError, match=match):
        _render(repo, **kw)
    assert {p: p.read_bytes() for p in repo.rglob("*.md")} == snapshot


def test_unknown_block_id(repo):
    (repo / "docs" / "d.md").write_text(
        f"{OPEN}\n{CLOSE}\n<!-- klt:table id=nope -->\n{CLOSE}\n"
    )
    _fails(repo, "no spec entry")


def test_spec_id_without_block(repo):
    (repo / "docs" / "d.md").write_text("nothing here\n")
    _fails(repo, "no block in any doc")


def test_unclosed_duplicate_and_stray(repo):
    d = repo / "docs" / "d.md"
    d.write_text(f"{OPEN}\nbody\n")
    _fails(repo, "not closed")
    d.write_text(f"{OPEN}\n{CLOSE}\n{OPEN}\n{CLOSE}\n")
    _fails(repo, "duplicate block id")
    d.write_text(f"{CLOSE}\n")
    _fails(repo, "without an open")


def test_missing_record_and_bad_pointer(repo):
    _write_json(
        repo / "spec.json",
        _spec([["a", {"record": "evidence/zzz.json", "pointer": "/n"}, 1]]),
    )
    _fails(repo, "missing record")
    _write_json(
        repo / "spec.json",
        _spec([["a", {"record": "evidence/a.json", "pointer": "/nope"}, 1]]),
    )
    _fails(repo, "does not resolve")


def test_record_outside_root_rejected(repo):
    _write_json(
        repo / "spec.json",
        _spec([["a", {"record": "../x.json", "pointer": "/n"}, 1]]),
    )
    _fails(repo, "escapes root")


def test_empty_glob_and_unreadable_spec(repo):
    _fails(repo, "matched no files", docs=("nothing/*.md",))
    (repo / "spec.json").write_text("{not json")
    _fails(repo, "cannot read spec")


def test_failure_in_second_doc_writes_nothing(repo):
    (repo / "docs" / "e.md").write_text("<!-- klt:table id=bad -->\n" + CLOSE + "\n")
    _fails(repo, "no spec entry")


def test_bad_format_and_type(repo):
    spec = _spec()
    spec["census"]["columns"][1]["format"] = "hex"
    _write_json(repo / "spec.json", spec)
    _fails(repo, "unknown format")
    _write_json(
        repo / "spec.json",
        _spec([["a", {"record": "evidence/a.json", "pointer": "/m"}, 1]]),
    )
    _fails(repo, "not integral")


def test_pipe_escaped(repo):
    _write_json(repo / "spec.json", _spec([["a|b", 1, 2]]))
    _render(repo)
    assert "| a\\|b | 1 | 2.00 |" in (repo / "docs" / "d.md").read_text()


def test_cli_exit_codes(repo, capsys):
    base = [
        "evidence",
        "render",
        str(repo),
        "--docs",
        "docs/*.md",
        "--spec",
        str(repo / "spec.json"),
        "--format",
        "json",
    ]
    assert main([*base, "--check"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "stale"
    assert main(base) == 0
    assert main([*base, "--check"]) == 0
    capsys.readouterr()
    assert main([*base[:4], "nope/*.md", *base[5:]]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["error"]["command"] == "evidence render"
