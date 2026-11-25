#!/usr/bin/env python3
"""Build the GitHub Release notes from the CHANGELOG section (issue #5861)
plus the changelog fragments added since the previous release (issue #5005).

``.github/workflows/publish.yml``'s ``github-release`` job calls this after
the PyPI upload succeeds and feeds the output to ``gh release create`` (or
``gh release edit`` on a re-run) as ``--notes-file``.

The notes are:

- the matching ``## X.Y.Z (date)`` section of ``CHANGELOG.md`` (without the
  heading -- the release title is the tag). The bump commit the fleet
  publisher writes only adds that heading under ``## Unreleased`` (issue
  #5005), so the section body is whatever ``## Unreleased`` held;
- plus every ``changelog.d/*.md`` fragment ADDED since the previous ``v*``
  tag (``git diff --diff-filter=A --name-only <prev-tag> HEAD -- changelog.d/``,
  in filename order): fragments are no longer folded into ``CHANGELOG.md`` at
  release time (the publisher's commit is heading-only), so this is where
  they reach the release notes. The window starts at the previous tag, so no
  release repeats an earlier one's fragments;
- with "the first/next release after ``vPREV``" availability placeholders
  resolved for the rendered notes only (anchored on the release this one
  supersedes; the persistent text in ``docs/cli/signoff.md`` and the
  fragments on disk stay as written).

GitHub caps a release body at 125,000 characters, so the combined section is
cut at a line boundary to keep the whole note under ``--max-chars`` (default
120,000); when cut, an italicised truncation note says so. Every note ends
with the same footer the 2026-11-15 backfill used: a link to ``CHANGELOG.md``
at the tag and the PyPI version page.

Usage::

    release_notes.py X.Y.Z [--changelog PATH] [--repo OWNER/NAME]
                         [--max-chars N] [--fragments-dir PATH]
                         [--previous-tag TAG | --no-fragments]

``X.Y.Z`` is the bare version (no ``v``); ``--repo`` defaults to this
repository and only shapes the CHANGELOG link. ``--previous-tag`` defaults to
the newest ``v*`` tag older than ``X.Y.Z`` in the git repository containing
the changelog; pass ``--no-fragments`` (or run outside a git repository) to
skip fragment collection.

Exit codes: 0 ok; 2 bad input (bad version, no such section, empty notes,
absurd ``--max-chars``).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_VERSION = r"\d+\.\d+\.\d+"
DEFAULT_REPO = "2AMLogic/klayout-tools"
# GitHub rejects release bodies over 125,000 characters; stay under it with
# headroom for the footer markup.
DEFAULT_MAX_CHARS = 120_000

TRUNCATION_NOTE = "\n\n_(truncated: the full section is in CHANGELOG.md)_\n"

# The words of an availability note are separated by spaces/tabs or by one
# soft wrap (a single line break plus indentation), as hand-wrapped Markdown
# prose has it. Never a blank line: two paragraphs must not combine into a
# placeholder (carried over from release_prep.py, issue #7493).
_NOTE_GAP = r"(?:[ \t]+|[ \t]*\n[ \t]*)"
_NOTE_PLACEHOLDER_RE = re.compile(
    rf"(?:the{_NOTE_GAP})?(?:first|next){_NOTE_GAP}release{_NOTE_GAP}after{_NOTE_GAP}"
    rf"`?v?({_VERSION})`?"
)


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
    """Return the ``## version (date)`` section body, up to the next heading.

    The body may legitimately be empty (the publisher's bump commit adds the
    heading without moving entries, issue #5005); :func:`build_notes` decides
    whether section plus fragments carry any content at all.
    """
    if not re.fullmatch(_VERSION, version):
        raise NotesError(f"not an X.Y.Z version: {version!r}")
    heading = re.compile(rf"^## {re.escape(version)} \([^\n]*\)[ \t]*$", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        raise NotesError(f"CHANGELOG.md has no '## {version} (…)' section")
    rest = changelog_text[match.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    section = rest[: next_heading.start()] if next_heading else rest
    return section.strip("\n")


def previous_tag(version: str, repo_root: Path) -> str | None:
    """The newest ``v*`` tag whose version is older than ``version``."""
    out = subprocess.run(
        ["git", "-C", str(repo_root), "tag", "--sort=-v:refname", "--list", "v*"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise NotesError(f"could not list tags in {repo_root}: {out.stderr.strip()}")
    target = tuple(int(part) for part in version.split("."))
    for tag in out.stdout.splitlines():
        tag = tag.strip()
        if not re.fullmatch(rf"v{_VERSION}", tag):
            continue
        candidate = tuple(int(part) for part in tag[1:].split("."))
        if candidate < target:
            return tag
    return None


def fragment_paths(repo_root: Path, previous: str) -> list[Path]:
    """``changelog.d/*.md`` fragments added since ``previous``, newest last,
    in the order a release folds them (filename order)."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--diff-filter=A",
            "--name-only",
            previous,
            "HEAD",
            "--",
            "changelog.d/",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise NotesError(
            f"could not diff fragments since {previous}: {out.stderr.strip()}"
        )
    return sorted(
        repo_root / line.strip()
        for line in out.stdout.splitlines()
        if line.strip().endswith(".md") and line.strip() != "changelog.d/README.md"
    )


def collect_fragments(repo_root: Path, version: str) -> list[str]:
    """The fragment bodies for ``version``'s notes, in fold order."""
    previous = previous_tag(version, repo_root)
    if previous is None:
        return []
    bodies = []
    for path in fragment_paths(repo_root, previous):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise NotesError(f"cannot read fragment {path}: {exc}") from exc
        if body.strip():
            bodies.append(body.strip("\n"))
    return bodies


def resolve_notes(text: str, previous: str, version: str) -> str:
    """Resolve "first/next release after ``vPREV``" placeholders anchored on
    the release being superseded; anything else stays as written (an older
    anchor would be wrong to rewrite -- this release is not the first after
    it -- and refusing is pointless once the release is already on PyPI)."""

    def _resolve(match: re.Match[str]) -> str:
        if match.group(1) == previous:
            return f"`klt {version}`"
        return match.group(0)

    return _NOTE_PLACEHOLDER_RE.sub(_resolve, text)


def build_notes(
    version: str,
    changelog_text: str,
    repo: str = DEFAULT_REPO,
    max_chars: int = DEFAULT_MAX_CHARS,
    fragments: list[str] | None = None,
    previous_version: str | None = None,
) -> str:
    """Full release-note body: section + fragments (+ truncation) + footer.

    ``fragments`` are the changelog.d bodies to append (collected by
    :func:`collect_fragments` when not given); ``previous_version`` anchors
    the availability-note resolver. The whole body is guaranteed to stay
    within ``max_chars`` characters; when it alone would not fit, it is cut at
    a line boundary (a hard cut only for a single pathological line longer
    than the budget).
    """
    section = extract_section(changelog_text, version)
    bodies = [section] if section.strip() else []
    bodies.extend(fragments or [])
    if not any(body.strip() for body in bodies):
        raise NotesError(
            f"CHANGELOG.md section '## {version}' and the changelog.d/ fragments"
            " added since the previous release are both empty -- refusing to"
            " publish empty notes (add a fragment, then re-run this job)"
        )
    body = "\n\n".join(bodies)
    if previous_version:
        body = resolve_notes(body, previous_version, version)
    footer = _footer(version, repo)
    if len(body) + len(footer) <= max_chars:
        return body + footer
    budget = max_chars - len(footer) - len(TRUNCATION_NOTE)
    if budget < 1:
        raise NotesError(f"max_chars={max_chars} leaves no room for any notes")
    cut = body.rfind("\n", 0, budget)
    if cut < 1:  # one line longer than the budget: hard-cut, still bounded
        cut = budget
    return body[:cut].rstrip() + TRUNCATION_NOTE + footer


def _repo_root_for(changelog: Path) -> Path | None:
    out = subprocess.run(
        ["git", "-C", str(changelog.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


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
    parser.add_argument(
        "--previous-tag",
        default=None,
        help="collect fragments added since this tag (default: newest v* tag"
        " older than the version)",
    )
    parser.add_argument(
        "--no-fragments",
        action="store_true",
        help="build the notes from the CHANGELOG section alone",
    )
    args = parser.parse_args(argv)
    try:
        fragments: list[str] | None = None
        previous_version: str | None = None
        root = _repo_root_for(args.changelog) if not args.no_fragments else None
        if root is not None:
            previous = args.previous_tag
            if previous is None:
                previous = previous_tag(args.version, root)
            if previous is not None:
                previous_version = previous[1:]
                fragments = []
                for path in fragment_paths(root, previous):
                    try:
                        body = path.read_text(encoding="utf-8")
                    except OSError as exc:
                        raise NotesError(f"cannot read fragment {path}: {exc}") from exc
                    if body.strip():
                        fragments.append(body.strip("\n"))
        notes = build_notes(
            args.version,
            args.changelog.read_text(encoding="utf-8"),
            repo=args.repo,
            max_chars=args.max_chars,
            fragments=fragments,
            previous_version=previous_version,
        )
    except (NotesError, OSError) as exc:
        print(f"release_notes.py: error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
