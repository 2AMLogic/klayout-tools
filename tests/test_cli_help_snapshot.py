"""Snapshot tests pinning ``klt --help`` and ``klt <verb> --help`` (#5146).

Help text and flag defaults are part of the CLI contract. These tests render
the help of the root parser and of every registered subcommand straight from
``create_parser()`` (no subprocess) and compare each against a committed
snapshot in ``tests/data/cli_help/``. That makes refactors of
``cli/parser.py`` (see #3962) provably output-neutral.

Layout: ``_root.txt`` for the top-level parser, ``<verb>.txt`` for every
verb. Nested subcommands are rendered into their parent verb's file after a
``--- <path> ---`` separator.

Regenerate the snapshots after an intentional help-text change with::

    KLT_UPDATE_HELP_SNAPSHOTS=1 KLT_TESTS_ALLOW_DIRTY_TREE=1 \
        uv run pytest tests/test_cli_help_snapshot.py

Help is normalized (see ``_help``) because argparse's rendering varies by
Python version.
The terminal width is pinned to 80 columns (argparse reads ``COLUMNS`` when
it builds a formatter), so output does not depend on the invoking terminal.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
from pathlib import Path

import pytest

from klayout_tools.cli.parser import create_parser

SNAPSHOT_DIR = Path(__file__).parent / "data" / "cli_help"
ROOT_NAME = "_root"
UPDATE_ENV = "KLT_UPDATE_HELP_SNAPSHOTS"
REGEN_COMMAND = (
    f"{UPDATE_ENV}=1 KLT_TESTS_ALLOW_DIRTY_TREE=1 "
    "uv run pytest tests/test_cli_help_snapshot.py"
)
WIDTH = "80"


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _unique_choices(
    action: argparse._SubParsersAction,
) -> dict[str, argparse.ArgumentParser]:
    """Subcommand name -> parser, dropping aliases that share a parser."""
    seen: set[int] = set()
    out: dict[str, argparse.ArgumentParser] = {}
    for name, sub in action.choices.items():
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        out[name] = sub
    return out


_OPTION_METAVAR = re.compile(r"(?<=[ ,])(-{1,2}[\w-]+) [^\s,]+(?=, -)")


def _help(parser: argparse.ArgumentParser) -> str:
    """``format_help()`` normalized to be independent of the Python version.

    argparse varies across versions in two ways that would make a raw snapshot
    depend on the interpreter:

    * the ``usage:`` line wraps differently when a parser has mutually
      exclusive groups (e.g. ``klt drc --check | --list-rules``), so the block
      is dropped;
    * since 3.13 an option with both forms renders ``-o, --outdir OUTDIR``
      instead of ``-o OUTDIR, --outdir OUTDIR``, which also changes where the
      description wraps. Metavars on all but the last option string are
      dropped and each option entry is joined onto one line with single
      spaces.

    Every flag, default and choice is still pinned by the options sections.
    """
    text = parser.format_help()
    head, sep, rest = text.partition("\n\n")
    if sep and head.startswith("usage:"):
        text = rest
    text = _OPTION_METAVAR.sub(r"\1", text)
    out: list[str] = []
    for line in text.splitlines():
        if out and re.match(r"^\s{3,}\S", line):
            out[-1] = f"{out[-1].rstrip()} {line.strip()}"
        else:
            out.append(line)
    out = [
        re.sub(r"(?<=\S) {2,}(?=\S)", " ", ln) if ln.startswith("  ") else ln
        for ln in out
    ]
    return "\n".join(out) + "\n"


def _render_tree(parser: argparse.ArgumentParser, path: str) -> str:
    """Help for ``parser`` followed by every nested subcommand's help."""
    chunks = [_help(parser)]
    action = _subparsers_action(parser)
    if action is not None:
        for name, sub in _unique_choices(action).items():
            child = f"{path} {name}"
            chunks.append(f"--- {child} ---\n")
            chunks.append(_render_tree(sub, child))
    return "\n".join(chunks)


def _render_all() -> dict[str, str]:
    """Map snapshot name -> rendered help text, for the whole CLI."""
    root = create_parser()
    rendered = {ROOT_NAME: _help(root)}
    action = _subparsers_action(root)
    assert action is not None, "klt registers its verbs through add_subparsers"
    for verb, sub in _unique_choices(action).items():
        rendered[verb] = _render_tree(sub, f"klt {verb}")
    return rendered


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("COLUMNS", WIDTH)
    monkeypatch.setenv("LINES", "50")
    return _render_all()


def test_rendering_is_deterministic(
    rendered: dict[str, str], monkeypatch: pytest.MonkeyPatch
):
    # A second render in the same process, and one with a different ambient
    # terminal width, must match: the test pins the width itself.
    assert _render_all() == rendered
    monkeypatch.setenv("COLUMNS", WIDTH)  # explicit: the pin is what matters
    assert _render_all() == rendered


def test_snapshot_set_matches_registered_verbs(rendered: dict[str, str]):
    if os.environ.get(UPDATE_ENV):
        pytest.skip("regenerating snapshots")
    on_disk = {p.stem for p in SNAPSHOT_DIR.glob("*.txt")}
    expected = set(rendered)
    missing = sorted(expected - on_disk)
    stale = sorted(on_disk - expected)
    assert not missing and not stale, (
        f"snapshot files out of sync with registered verbs: missing={missing} "
        f"stale={stale}. Regenerate with: {REGEN_COMMAND}"
    )


def test_help_matches_snapshots(rendered: dict[str, str]):
    if os.environ.get(UPDATE_ENV):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for stale in SNAPSHOT_DIR.glob("*.txt"):
            if stale.stem not in rendered:
                stale.unlink()
        for name, text in rendered.items():
            (SNAPSHOT_DIR / f"{name}.txt").write_text(text, encoding="utf-8")
        return

    failures: list[str] = []
    for name, text in sorted(rendered.items()):
        path = SNAPSHOT_DIR / f"{name}.txt"
        if not path.exists():
            failures.append(f"{name}: no snapshot at {path}")
            continue
        expected = path.read_text(encoding="utf-8")
        if expected != text:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=f"snapshot/{name}.txt",
                    tofile=f"current/{name}",
                )
            )
            failures.append(f"{name}: help text changed\n{diff}")
    assert not failures, (
        "\n\n".join(failures)
        + f"\n\nIf the change is intentional, regenerate with: {REGEN_COMMAND}"
    )
