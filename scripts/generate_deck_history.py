#!/usr/bin/env python3
"""Regenerate ``src/klayout_tools/decks/_history.json`` (issue #623).

`` klt deck resolve`` needs a hash/version -> shippable-revision lookup table
so a consumer that pinned ``provenance.deck.content_hash`` (see
``docs/json-contract.md``) can find out which klayout-tools git tag / PyPI
version to install to reproduce a report against a specific past revision of
a built-in rule deck, without hand-bisecting this repo's own git history.

This script is the "generated automatically ... not hand-maintained" half of
that feature (issue #623's recommended Option A): it walks every ``v*`` git
tag reachable from the current branch and, for every deck module under
``src/klayout_tools/decks/`` present at that tag, records its content hash
(computed exactly the way ``klayout_tools._provenance.sha256_file`` hashes a
deck file at runtime -- a straight streamed SHA-256 of the raw file bytes),
the tag, its commit, and the package version from that tag's
``pyproject.toml``. One entry is recorded **per deck per release** (not only
when the hash changes) so that:

- ``klt deck resolve --deck X --version Y`` is a direct, always-present
  lookup for any real release -- no "nearest earlier version" fallback logic
  needed at query time.
- ``klt deck resolve --content-hash H`` can report the *newest* release
  shipping that exact byte content, so resolving the currently-running
  build's own deck hash against a from-git-tag ``pip install`` returns that
  same, currently-running version (issue #623's acceptance criterion) even
  when the deck hasn't changed since an earlier release.

Usage::

    python scripts/generate_deck_history.py

Run this once a release has been tagged and pushed (the tag must already
exist for this script to see it) and commit the regenerated
``_history.json`` in a follow-up commit -- see the "Regenerating" section of
``docs/cli/deck.md``. Idempotent: re-running it against unchanged tag history
reproduces byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DECKS_DIR = REPO_ROOT / "src" / "klayout_tools" / "decks"
HISTORY_PATH = DECKS_DIR / "_history.json"

#: Deck modules are plain per-PDK sibling files under decks/ -- anything
#: that isn't the package init, a non-deck support module colocated in the
#: same directory, or an underscore-prefixed data/private file (like this
#: script's own output, `_history.json`) is a deck module. `history.py` is
#: this feature's own resolver module (see its docstring), not a per-PDK
#: rule table, so it must be excluded the same way `decks/__init__.py`'s
#: `deck_names()` excludes it from the *live* registry -- otherwise it gets
#: hashed and recorded as a bogus "history" deck (see issue #1338).
_SKIP_MODULES = {"__init__.py", "history.py"}

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _run(*args: str) -> str:
    return subprocess.run(
        args, cwd=REPO_ROOT, check=True, capture_output=True, text=True
    ).stdout


def _tags() -> list[str]:
    """Every ``v*`` tag, oldest first (matches ``docs/cli/deck.md``'s
    documented ordering)."""
    out = _run("git", "tag", "--sort=v:refname", "--list", "v*")
    return [line for line in out.splitlines() if line.strip()]


def _deck_module_names_at(tag: str) -> list[str]:
    out = _run("git", "ls-tree", "--name-only", f"{tag}:src/klayout_tools/decks")
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line in _SKIP_MODULES or not line.endswith(".py"):
            continue
        names.append(line[: -len(".py")])
    return sorted(names)


def _blob_sha256(tag: str, deck_name: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{tag}:src/klayout_tools/decks/{deck_name}.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def _package_version_at(tag: str) -> str:
    text = _run("git", "show", f"{tag}:pyproject.toml")
    match = _VERSION_RE.search(text)
    if not match:
        raise SystemExit(
            f'could not find a `version = "..."` line in {tag}:pyproject.toml'
        )
    return match.group(1)


def _commit_sha(tag: str) -> str:
    return _run("git", "rev-list", "-n", "1", tag).strip()


def build_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for tag in _tags():
        package_version = _package_version_at(tag)
        commit = _commit_sha(tag)
        for deck_name in _deck_module_names_at(tag):
            digest = _blob_sha256(tag, deck_name)
            entries.append(
                {
                    "deck": deck_name,
                    "content_hash": f"sha256:{digest}",
                    "git_tag": tag,
                    "git_commit": commit,
                    "package_version": package_version,
                }
            )
    return entries


def main() -> int:
    tags = _tags()
    if not tags:
        print("no v* git tags found -- nothing to record", file=sys.stderr)
        return 1

    entries = build_entries()
    payload = {
        "note": (
            "Generated by scripts/generate_deck_history.py from git tag "
            "history (issue #623). Do not hand-edit -- rerun the generator "
            "after tagging a release and commit the result. See "
            "docs/cli/deck.md."
        ),
        "entries": entries,
    }
    HISTORY_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"wrote {len(entries)} entries covering {tags[0]}..{tags[-1]} to {HISTORY_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
