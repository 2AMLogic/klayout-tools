"""Tests for `scripts/generate_deck_history.py` (issue #623): the
release-tooling generator behind `klt deck resolve`'s lookup table
(`src/klayout_tools/decks/_history.json`).

Runs entirely against a synthetic, throwaway git repo built under `tmp_path`
-- never against this repo's own git history -- mirroring
`test_ingest_canary.py`'s dynamic-import-by-path pattern (the script lives
under `scripts/`, not an importable package). `tests/test_deck_history.py`
has its own sanity check that the *checked-in* `_history.json` is consistent
with `git tag` on this repo; this module instead exercises the generator's
own logic (one entry per deck per tagged release, hash-changes-are-recorded,
untagged commits excluded, idempotency) under full control.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load_generate_deck_history():
    spec = importlib.util.spec_from_file_location(
        "generate_deck_history", SCRIPTS_DIR / "generate_deck_history.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_deck_history"] = module
    spec.loader.exec_module(module)
    return module


gdh = _load_generate_deck_history()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "src" / "klayout_tools" / "decks").mkdir(parents=True)
    (repo / "src" / "klayout_tools" / "decks" / "__init__.py").write_text(
        "# package init -- must be skipped, never treated as a deck module\n"
    )
    return repo


def _write_pyproject(repo: Path, version: str) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "klayout-tools"\nversion = "{version}"\n'
    )


def _commit(repo: Path, tag: str | None, message: str = "commit") -> None:
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    if tag:
        _git("tag", tag, cwd=repo)


@pytest.fixture()
def _tagged_repo(tmp_path: Path) -> Path:
    """Two tagged releases plus one untagged follow-up commit:

    - v0.1.0: only `sky130.py` exists.
    - v0.2.0: `sky130.py`'s content changed (new hash), and `gf180mcu.py`
      is added for the first time.
    - (untagged) `sky130.py` changes again -- must not appear anywhere in
      the generated table, since the tag doesn't exist yet.
    """
    repo = _make_repo(tmp_path)
    decks_dir = repo / "src" / "klayout_tools" / "decks"

    _write_pyproject(repo, "0.1.0")
    (decks_dir / "sky130.py").write_text("DECK_A = 1\n")
    _commit(repo, "v0.1.0")

    _write_pyproject(repo, "0.2.0")
    (decks_dir / "sky130.py").write_text("DECK_B = 2\n")
    (decks_dir / "gf180mcu.py").write_text("NEW_DECK = 1\n")
    _commit(repo, "v0.2.0")

    (decks_dir / "sky130.py").write_text("DECK_C = 3\n")
    _commit(repo, None, message="untagged wip -- must not be recorded")

    return repo


# --------------------------------------------------------------------------- #
# build_entries()
# --------------------------------------------------------------------------- #


def test_build_entries_one_row_per_deck_per_tagged_release(_tagged_repo, monkeypatch):
    monkeypatch.setattr(gdh, "REPO_ROOT", _tagged_repo)

    entries = gdh.build_entries()

    by_tag_deck = {(e["git_tag"], e["deck"]): e for e in entries}
    # sky130 changed between releases -> an entry at both tags. gf180mcu only
    # existed starting at v0.2.0 -> no v0.1.0 entry. The untagged follow-up
    # commit's sky130.py content never appears anywhere.
    assert set(by_tag_deck) == {
        ("v0.1.0", "sky130"),
        ("v0.2.0", "sky130"),
        ("v0.2.0", "gf180mcu"),
    }
    assert by_tag_deck[("v0.1.0", "sky130")]["package_version"] == "0.1.0"
    assert by_tag_deck[("v0.2.0", "sky130")]["package_version"] == "0.2.0"
    assert (
        by_tag_deck[("v0.1.0", "sky130")]["content_hash"]
        != by_tag_deck[("v0.2.0", "sky130")]["content_hash"]
    )


def test_build_entries_hash_matches_streamed_sha256_of_raw_bytes(
    _tagged_repo, monkeypatch
):
    """The recorded hash must match exactly how
    `klayout_tools._provenance.sha256_file` hashes the deck file at runtime
    (a plain SHA-256 of the raw file bytes) -- otherwise a real
    `provenance.deck.content_hash` would never resolve."""
    monkeypatch.setattr(gdh, "REPO_ROOT", _tagged_repo)

    entries = gdh.build_entries()
    (entry,) = [
        e for e in entries if e["git_tag"] == "v0.1.0" and e["deck"] == "sky130"
    ]

    expected = "sha256:" + hashlib.sha256(b"DECK_A = 1\n").hexdigest()
    assert entry["content_hash"] == expected


def test_build_entries_records_commit_and_tag(_tagged_repo, monkeypatch):
    monkeypatch.setattr(gdh, "REPO_ROOT", _tagged_repo)

    entries = gdh.build_entries()

    expected_commit = subprocess.run(
        ["git", "rev-list", "-n", "1", "v0.2.0"],
        cwd=_tagged_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (entry,) = [
        e for e in entries if e["git_tag"] == "v0.2.0" and e["deck"] == "gf180mcu"
    ]
    assert entry["git_commit"] == expected_commit


# --------------------------------------------------------------------------- #
# main() -- writes _history.json, idempotent, fails loudly with no tags
# --------------------------------------------------------------------------- #


def test_main_writes_history_json_and_is_idempotent(
    _tagged_repo, monkeypatch, tmp_path
):
    history_path = tmp_path / "_history.json"
    monkeypatch.setattr(gdh, "REPO_ROOT", _tagged_repo)
    monkeypatch.setattr(gdh, "HISTORY_PATH", history_path)

    assert gdh.main() == 0
    first = history_path.read_text()
    payload = json.loads(first)
    assert len(payload["entries"]) == 3

    assert gdh.main() == 0
    assert history_path.read_text() == first  # byte-identical re-run


def test_main_no_tags_fails_loudly(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    _write_pyproject(repo, "0.1.0")
    (repo / "src" / "klayout_tools" / "decks" / "sky130.py").write_text("X = 1\n")
    _commit(repo, tag=None)

    monkeypatch.setattr(gdh, "REPO_ROOT", repo)
    monkeypatch.setattr(gdh, "HISTORY_PATH", tmp_path / "_history.json")

    exit_code = gdh.main()

    assert exit_code == 1
    assert "no v* git tags" in capsys.readouterr().err
    assert not (tmp_path / "_history.json").exists()
