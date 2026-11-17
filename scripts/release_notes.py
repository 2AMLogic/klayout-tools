#!/usr/bin/env python3
"""Build the GitHub Release notes from the CHANGELOG section (issue #5861).

``.github/workflows/publish.yml``'s ``github-release`` job calls this after
the PyPI upload succeeds and feeds the output to ``gh release create`` (or
``gh release edit`` on a re-run) as ``--notes-file``.

The notes are the matching ``## X.Y.Z (date)`` section of ``CHANGELOG.md``,
without the heading (the release title is the tag). GitHub caps a release
body at 125,000 characters, so the section is cut at a line boundary to keep
the whole note under ``--max-chars`` (default 120,000); when cut, an
italicised truncation note says so. Every note ends with the same footer the
2026-11-15 backfill used: a link to ``CHANGELOG.md`` at the tag and the PyPI
version page.

Usage::

    release_notes.py X.Y.Z [--changelog PATH] [--repo OWNER/NAME]
                          [--max-chars N]

``X.Y.Z`` is the bare version (no ``v``); ``--repo`` defaults to this
repository and only shapes the CHANGELOG link.

Exit codes: 0 ok; 2 bad input (bad version, no such section, empty section,
absurd ``--max-chars``).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERSION = r"\d+\.\d+\.\d+"
DEFAULT_REPO = "2AMLogic/klayout-tools"
# GitHub rejects release bodies over 125,000 characters; stay under it with
# headroom for the footer markup.
DEFAULT_MAX_CHARS = 120_000

TRUNCATION_NOTE = "\n\n_(truncated: the full section is in CHANGELOG.md)_\n"


class NotesError(Exception):
    """Input the script cannot act on (maps to exit code 2)."""


def _footer(version: str, repo: str) -> str:
    tag = f"v{version}"
    return (
        "\n\n---\n"
        f"Full notes: [CHANGELOG.md at {tag}]"
        f"(https://github.com/{repo}/blob/{tag}/CHANGELOG.md). "
        f"Published on PyPI as [klayout-tools=={version}]"
        f"(https://pypi.org/project/klayout-tools/{version}/).\n"
    )


def extract_section(changelog_text: str, version: str) -> str:
    """Return the ``## version (date)`` section body, up to the next heading."""
    if not re.fullmatch(_VERSION, version):
        raise NotesError(f"not an X.Y.Z version: {version!r}")
    heading = re.compile(rf"^## {re.escape(version)} \([^\n]*\)[ \t]*$", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        raise NotesError(f"CHANGELOG.md has no '## {version} (…)' section")
    rest = changelog_text[match.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    section = rest[: next_heading.start()] if next_heading else rest
    section = section.strip("\n")
    if not section.strip():
        raise NotesError(f"CHANGELOG.md section '## {version}' is empty")
    return section


def build_notes(
    version: str,
    changelog_text: str,
    repo: str = DEFAULT_REPO,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Full release-note body: section (+ truncation note) + link footer.

    The whole body is guaranteed to stay within ``max_chars`` characters;
    when the section alone would not fit, it is cut at a line boundary (a
    hard cut only for a single pathological line longer than the budget).
    """
    section = extract_section(changelog_text, version)
    footer = _footer(version, repo)
    if len(section) + len(footer) <= max_chars:
        return section + footer
    budget = max_chars - len(footer) - len(TRUNCATION_NOTE)
    if budget < 1:
        raise NotesError(f"max_chars={max_chars} leaves no room for any notes")
    cut = section.rfind("\n", 0, budget)
    if cut < 1:  # one line longer than the budget: hard-cut, still bounded
        cut = budget
    return section[:cut].rstrip() + TRUNCATION_NOTE + footer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="bare X.Y.Z version (no 'v')")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=REPO_ROOT / "CHANGELOG.md",
        help="path to CHANGELOG.md (default: the repo's)",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="OWNER/NAME for the CHANGELOG-at-tag link (default: %(default)s)",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="hard cap on the whole note (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        notes = build_notes(
            args.version,
            args.changelog.read_text(encoding="utf-8"),
            repo=args.repo,
            max_chars=args.max_chars,
        )
    except (NotesError, OSError) as exc:
        print(f"release_notes.py: error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
