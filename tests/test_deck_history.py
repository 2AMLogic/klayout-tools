"""Tests for `klt deck resolve` (issue #623): the library module
(`klayout_tools.decks.history`) and its CLI wiring (`cli/deck_cmd.py` +
`cli/parser.py`).

Fixtures point `history._HISTORY_PATH` at a synthetic table under `tmp_path`
(mirroring `test_kb_cmd.py`'s `DEFAULT_KB_ROOT` monkeypatch pattern) so these
tests never depend on -- or risk being broken by regenerating -- the
checked-in `src/klayout_tools/decks/_history.json` table, which has its own
minimal sanity coverage at the bottom of this module.
"""

from __future__ import annotations

import json

import pytest

from klayout_tools.cli import main
from klayout_tools.decks import history

_ENTRIES = [
    {
        "deck": "sky130",
        "content_hash": "sha256:" + "a" * 64,
        "git_tag": "v0.1.0",
        "git_commit": "0" * 40,
        "package_version": "0.1.0",
    },
    {
        "deck": "gf180mcu",
        "content_hash": "sha256:" + "b" * 64,
        "git_tag": "v0.1.0",
        "git_commit": "0" * 40,
        "package_version": "0.1.0",
    },
    {
        # sky130 unchanged between v0.1.0 and v0.2.0 -- same hash as above,
        # a fresh entry per the "one entry per deck per release" design.
        "deck": "sky130",
        "content_hash": "sha256:" + "a" * 64,
        "git_tag": "v0.2.0",
        "git_commit": "1" * 40,
        "package_version": "0.2.0",
    },
    {
        "deck": "gf180mcu",
        "content_hash": "sha256:" + "c" * 64,
        "git_tag": "v0.2.0",
        "git_commit": "1" * 40,
        "package_version": "0.2.0",
    },
]


@pytest.fixture()
def _history_table(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text(json.dumps({"entries": _ENTRIES}))
    monkeypatch.setattr(history, "_HISTORY_PATH", path)
    return path


# --------------------------------------------------------------------------- #
# library: resolve_deck
# --------------------------------------------------------------------------- #


def test_resolve_by_content_hash(_history_table):
    report = history.resolve_deck(content_hash="sha256:" + "c" * 64)

    assert report["schema_version"] == 1
    assert report["deck"] == "gf180mcu"
    assert report["git_tag"] == "v0.2.0"
    assert report["git_commit"] == "1" * 40
    assert report["package_version"] == "0.2.0"
    assert report["query"] == {
        "content_hash": "sha256:" + "c" * 64,
        "deck": None,
        "version": None,
    }


def test_resolve_by_content_hash_unchanged_across_releases_returns_newest(
    _history_table,
):
    # sky130's hash "a"*64 is identical at v0.1.0 and v0.2.0 -- resolving it
    # must report the *newest* release, so that resolving the
    # currently-installed build's own hash reports its own version (issue
    # #623's acceptance criterion), not a stale earlier release that
    # happened to ship the same bytes first.
    report = history.resolve_deck(content_hash="sha256:" + "a" * 64)

    assert report["package_version"] == "0.2.0"
    assert report["git_tag"] == "v0.2.0"


def test_resolve_by_content_hash_narrowed_by_deck(_history_table):
    # Without --deck this would still resolve unambiguously since no two
    # decks share a hash in the fixture, but --deck must still narrow
    # correctly (and not accidentally exclude a real match).
    report = history.resolve_deck(content_hash="sha256:" + "a" * 64, deck="sky130")

    assert report["deck"] == "sky130"


def test_resolve_by_content_hash_deck_mismatch_not_found(_history_table):
    with pytest.raises(history.DeckHistoryError, match="no known release"):
        history.resolve_deck(content_hash="sha256:" + "a" * 64, deck="gf180mcu")


def test_resolve_by_deck_and_version(_history_table):
    report = history.resolve_deck(deck="gf180mcu", version="0.1.0")

    assert report["content_hash"] == "sha256:" + "b" * 64
    assert report["git_tag"] == "v0.1.0"
    assert report["git_commit"] == "0" * 40


def test_resolve_unreleased_version_not_found(_history_table):
    # Edge case from the curated test plan: a deck/version combo that never
    # shipped must fail loudly, not silently.
    with pytest.raises(history.DeckHistoryError, match="99.0.0"):
        history.resolve_deck(deck="sky130", version="99.0.0")


def test_resolve_unknown_content_hash_not_found(_history_table):
    # A hash predating the table's start (or never released) -- not a crash.
    with pytest.raises(history.DeckHistoryError, match="no known release"):
        history.resolve_deck(content_hash="sha256:" + "f" * 64)


def test_resolve_requires_content_hash_or_deck_and_version(_history_table):
    with pytest.raises(history.DeckHistoryError, match="requires either"):
        history.resolve_deck()


def test_resolve_requires_both_deck_and_version_together(_history_table):
    with pytest.raises(history.DeckHistoryError, match="requires either"):
        history.resolve_deck(deck="sky130")

    with pytest.raises(history.DeckHistoryError, match="requires either"):
        history.resolve_deck(version="0.1.0")


def test_known_deck_names(_history_table):
    assert history.known_deck_names() == ["gf180mcu", "sky130"]


def test_malformed_history_table_raises(tmp_path, monkeypatch):
    path = tmp_path / "_history.json"
    path.write_text("not json")
    monkeypatch.setattr(history, "_HISTORY_PATH", path)

    with pytest.raises(history.DeckHistoryError, match="not valid JSON"):
        history.resolve_deck(content_hash="sha256:" + "a" * 64)


def test_missing_history_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_HISTORY_PATH", tmp_path / "does-not-exist.json")

    with pytest.raises(history.DeckHistoryError, match="not found"):
        history.resolve_deck(content_hash="sha256:" + "a" * 64)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_resolve_json(_history_table, capsys):
    exit_code = main(
        ["deck", "resolve", "--content-hash", "sha256:" + "c" * 64, "--format", "json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deck"] == "gf180mcu"
    assert payload["package_version"] == "0.2.0"


def test_cli_resolve_text(_history_table, capsys):
    exit_code = main(["deck", "resolve", "--deck", "sky130", "--version", "0.1.0"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "deck: sky130" in out
    assert "package_version: 0.1.0" in out


def test_cli_resolve_not_found_error_envelope(_history_table, capsys):
    exit_code = main(
        [
            "deck",
            "resolve",
            "--deck",
            "sky130",
            "--version",
            "99.0.0",
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "deck resolve"
    assert "99.0.0" in err["error"]["message"]


def test_cli_resolve_missing_query_error_envelope(_history_table, capsys):
    exit_code = main(["deck", "resolve", "--format", "json"])

    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert "requires either" in err["error"]["message"]


def test_cli_deck_no_subcommand_prints_help(capsys):
    exit_code = main(["deck"])

    assert exit_code == 2
    assert "usage" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- #
# the checked-in table (src/klayout_tools/decks/_history.json)
# --------------------------------------------------------------------------- #


def test_checked_in_history_table_covers_every_release():
    """Sanity check on the real, generated table -- not the synthetic
    fixture above. Every known deck must have an entry for every known
    release (the "one entry per deck per release" invariant
    `scripts/generate_deck_history.py` and `resolve_deck`'s content-hash
    "newest wins" behavior both depend on)."""
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    real_path = repo_root / "src" / "klayout_tools" / "decks" / "_history.json"
    data = json.loads(real_path.read_text())
    entries = data["entries"]

    tags = sorted({e["git_tag"] for e in entries})
    decks = sorted({e["deck"] for e in entries})
    assert tags, "expected at least one release tag in the checked-in table"
    assert decks, "expected at least one deck in the checked-in table"

    for tag in tags:
        decks_at_tag = {e["deck"] for e in entries if e["git_tag"] == tag}
        assert decks_at_tag == set(decks), (
            f"{tag} is missing an entry for one of {decks} -- rerun "
            "scripts/generate_deck_history.py"
        )

    # The table is a well-formed, up-to-date reflection of *tagged* history
    # -- it need not (and for an unreleased dev checkout, generally won't)
    # include the current working tree's hash, only what git tag --list
    # reports. Confirm the tag set matches reality so the fixture doesn't
    # silently drift from a real `git tag` run.
    result = subprocess.run(
        ["git", "tag", "--sort=v:refname", "--list", "v*"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    real_tags = [line for line in result.stdout.splitlines() if line.strip()]
    if real_tags:
        assert tags == real_tags, (
            "checked-in _history.json is stale relative to `git tag` -- "
            "rerun scripts/generate_deck_history.py and commit the result"
        )
