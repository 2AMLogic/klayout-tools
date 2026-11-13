"""KLayout ``# %include`` deck-tree coverage (issue #5443; split from
test_drc_klayout_engine.py for the file-size ratchet).

``# %include`` is how a PDK's native deck is usually built -- a thin driver
script over a directory of rule files -- so following it is what makes
``provenance.deck.tree`` mean anything for such a deck. The two parametrized
tests below are the contract: every line form was cross-checked against the
installed KLayout 0.30.12 by running a probe deck through ``klayout -b -r``
and watching whether the included file executed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import klayout_tools.drc_klayout_engine as engine_module
from helpers.subprocess_fakes import fake_completed
from klayout_tools.cli import main
from klayout_tools.drc import run_drc_klayout_engine
from test_drc_klayout_engine import (
    _CLEAN_WITH_CATEGORIES_RDB,
    _write_deck_file,
    _write_gds,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def _stub_klayout_passing_git_through(monkeypatch):
    """Like test_drc_klayout_engine's ``_stub_klayout_drc_subprocess``,
    but lets the real ``git``
    through -- a tree-bearing deck calls ``git describe`` for
    ``provenance.deck.tree.release``, which the blanket stub cannot answer."""
    real_run = subprocess.run

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[0] == "git":
            return real_run(
                cmd, capture_output=capture_output, text=text, timeout=timeout
            )
        report_path = next(a for a in cmd if a.startswith("report=")).split("=", 1)[1]
        Path(report_path).write_text(_CLEAN_WITH_CATEGORIES_RDB, encoding="utf-8")
        return fake_completed()

    monkeypatch.setattr(engine_module.subprocess, "run", fake_run)


def _include_deck(tmp_path: Path, rule_text: str = "# rule\n") -> str:
    """A deck shaped like a PDK's native driver: a thin entry script whose
    rules all arrive through `# %include`."""
    (tmp_path / "rule_decks").mkdir(exist_ok=True)
    (tmp_path / "rule_decks" / "a.drc").write_text(rule_text, encoding="utf-8")
    return _write_deck_file(tmp_path / "deck.drc", "# %include rule_decks/a.drc\n")


def test_include_deck_records_tree_identity_that_tracks_rule_files(tmp_path):
    deck = _include_deck(tmp_path)

    first = engine_module._deck_tree_identity(deck)
    again = engine_module._deck_tree_identity(deck)
    (tmp_path / "rule_decks" / "a.drc").write_text("# changed\n", encoding="utf-8")
    changed = engine_module._deck_tree_identity(deck)

    assert first == again
    assert first["file_count"] == 2
    assert first["content_hash"].startswith("sha256:")
    assert changed["content_hash"] != first["content_hash"]


def test_nested_includes_are_followed_relative_to_the_including_file(tmp_path):
    # `rule_decks/a.drc` includes `shared/b.drc` as `shared/b.drc`, i.e.
    # relative to itself -- KLayout resolves each include against the file it
    # appears in, not against the entry script.
    deck = _include_deck(tmp_path, "# %include shared/b.drc\n")
    (tmp_path / "rule_decks" / "shared").mkdir()
    (tmp_path / "rule_decks" / "shared" / "b.drc").write_text("# b\n")

    files = engine_module._deck_script_files(deck)

    assert [Path(f).name for f in files] == ["deck.drc", "a.drc", "b.drc"]
    assert engine_module._deck_tree_identity(deck)["file_count"] == 3


def test_include_and_ruby_require_are_both_counted(tmp_path):
    (tmp_path / "rule_decks").mkdir()
    (tmp_path / "rule_decks" / "a.drc").write_text("# rule\n")
    (tmp_path / "util.rb").write_text("# util\n")
    deck = _write_deck_file(
        tmp_path / "deck.drc",
        '# %include rule_decks/a.drc\nrequire_relative "util"\n',
    )

    names = {Path(f).name for f in engine_module._deck_script_files(deck)}

    assert names == {"deck.drc", "a.drc", "util.rb"}


def test_missing_include_target_is_skipped_without_error(tmp_path):
    deck = _write_deck_file(tmp_path / "deck.drc", "# %include rule_decks/gone.drc\n")

    assert engine_module._deck_script_files(deck) == [str(tmp_path / "deck.drc")]
    assert engine_module._deck_tree_identity(deck) is None


def test_include_cycle_terminates(tmp_path):
    # KLayout itself crashes on a cycle, so no real deck has one -- but a
    # static walker must never hang or recurse away on malformed input.
    _write_deck_file(tmp_path / "a.drc", "# %include b.drc\n")
    _write_deck_file(tmp_path / "b.drc", "# %include a.drc\n")

    files = engine_module._deck_script_files(str(tmp_path / "a.drc"))

    assert [Path(f).name for f in files] == ["a.drc", "b.drc"]


# Every form below was cross-checked against the installed KLayout 0.30.12 by
# running a probe deck through `klayout -b -r` and watching whether the
# included file executed: the walker follows exactly what KLayout splices.
@pytest.mark.parametrize(
    "line",
    [
        "# %include rule_decks/a.drc",
        '# %include "rule_decks/a.drc"',
        "# %include 'rule_decks/a.drc'",
        "#%include rule_decks/a.drc",
        "  # %include rule_decks/a.drc",
        "# %include rule_decks/a.drc   ",
        "# %include ./rule_decks/a.drc",
        "# %include  rule_decks/a.drc",
        "# %includerule_decks/a.drc",  # no blank after the keyword
        "#\t%include rule_decks/a.drc",
        "# %include\trule_decks/a.drc",
        # An unterminated quote: KLayout reads to end of line.
        '# %include "rule_decks/a.drc',
    ],
)
def test_include_forms_klayout_follows_are_followed(tmp_path, line):
    (tmp_path / "rule_decks").mkdir()
    (tmp_path / "rule_decks" / "a.drc").write_text("# rule\n")
    deck = _write_deck_file(tmp_path / "deck.drc", line + "\n")

    assert str(tmp_path / "rule_decks" / "a.drc") in engine_module._deck_script_files(
        deck
    )


@pytest.mark.parametrize(
    "line",
    [
        "# %INCLUDE rule_decks/a.drc",  # case-sensitive
        "# % include rule_decks/a.drc",  # no blank inside the keyword
        "## %include rule_decks/a.drc",  # `#` must open the line
        "puts 1 # %include rule_decks/a.drc",  # ... not trail code
        "# %include rule_decks/a.drc # c",  # no trailing-comment syntax
        '# %include rule_decks/a.drc"',  # a trailing quote is part of the path
        "# %include rule_decks/a",  # no `.rb`/`.drc` suffix search
        "# %include",  # no path at all
        "# %%include rule_decks/a.drc",  # `%%include` is not the directive
    ],
)
def test_include_forms_klayout_ignores_are_not_followed(tmp_path, line):
    (tmp_path / "rule_decks").mkdir()
    (tmp_path / "rule_decks" / "a.drc").write_text("# rule\n")
    deck = _write_deck_file(tmp_path / "deck.drc", line + "\n")

    assert engine_module._deck_script_files(deck) == [str(tmp_path / "deck.drc")]


def test_include_deck_provenance_and_check_detect_rule_file_drift(
    tmp_path, monkeypatch, capsys
):
    # In-repo position (issue #2659): outside a repo `file` is `path: null`,
    # which `--check` cannot re-hash and so reports as drift.
    (tmp_path / ".git").mkdir(exist_ok=True)
    gds = _write_gds(tmp_path / "test.gds")
    deck = _include_deck(tmp_path)

    _stub_klayout_passing_git_through(monkeypatch)
    report = run_drc_klayout_engine(gds, deck)
    assert report["provenance"]["deck"]["tree"]["file_count"] == 2

    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(report), encoding="utf-8")
    assert main(["drc", "--check", str(saved), "--format", "json"]) == 0
    capsys.readouterr()

    # Only the included rule file moves -- the entry script is untouched, so
    # `provenance.deck.content_hash` still matches and the tree hash is the
    # only thing that can catch it.
    (tmp_path / "rule_decks" / "a.drc").write_text("# drifted\n", encoding="utf-8")
    assert main(["drc", "--check", str(saved), "--format", "json"]) != 0
    by_field = {c["field"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert by_field["provenance.deck.content_hash"]["match"] is True
    assert by_field["provenance.deck.tree.content_hash"]["match"] is False


def test_crosscheck_check_detects_rule_file_drift_on_the_native_side(
    tmp_path, monkeypatch
):
    """`--engine both` embeds the native report's own `provenance.deck`
    (issue #4986), so the native side gains the same coverage."""
    from klayout_tools.drc import check_drc_report

    _stub_klayout_passing_git_through(monkeypatch)
    gds = _write_gds(tmp_path / "test.gds")
    deck = _include_deck(tmp_path)
    native = run_drc_klayout_engine(gds, deck)

    envelope = tmp_path / "x.crosscheck.json"
    envelope.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "crosscheck",
                "file": gds,
                "reports": {"curated": None, "native": native},
            }
        ),
        encoding="utf-8",
    )
    field = "reports.native.provenance.deck.tree.content_hash"

    before = check_drc_report(str(envelope), input_path=gds)
    assert {c["field"]: c["match"] for c in before["checks"]}[field] is True

    (tmp_path / "rule_decks" / "a.drc").write_text("# drifted\n", encoding="utf-8")
    after = check_drc_report(str(envelope), input_path=gds)
    assert after["status"] == "drifted"
    assert {c["field"]: c["match"] for c in after["checks"]}[field] is False


def test_interpolated_include_path_is_skipped_rather_than_guessed(tmp_path):
    """``$(...)`` interpolation (issue #5443): KLayout evaluates the
    expression before resolving the path, which a static walker cannot do, so
    the include is skipped and the file it names is not covered by the tree
    hash. Skipping is the specified behaviour -- a guess would silently claim
    coverage of a file the deck may not even read -- and `docs/cli/drc.md`
    says so where it documents the tree hash.
    """
    (tmp_path / "rule_decks").mkdir()
    (tmp_path / "rule_decks" / "a.drc").write_text("# rule\n")
    deck = _write_deck_file(tmp_path / "deck.drc", '# %include $("rule_decks")/a.drc\n')

    assert engine_module._deck_script_files(deck) == [str(tmp_path / "deck.drc")]
    assert engine_module._deck_tree_identity(deck) is None


def test_quoted_include_with_trailing_content_over_includes(tmp_path):
    """The walker's one known over-inclusion, pinned so it stays deliberate
    (issue #5443): KLayout parses the quoted path, then rejects the leftover
    ``trailing`` and aborts the whole deck (``Expected end of text here``),
    while the walker drops the leftover and follows the include.
    Over-including for a deck that cannot run at all costs nothing, so it is
    left alone rather than special-cased.
    """
    (tmp_path / "rule_decks").mkdir()
    (tmp_path / "rule_decks" / "a.drc").write_text("# rule\n")
    deck = _write_deck_file(
        tmp_path / "deck.drc", '# %include "rule_decks/a.drc" trailing\n'
    )

    assert str(tmp_path / "rule_decks" / "a.drc") in engine_module._deck_script_files(
        deck
    )
