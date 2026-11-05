"""Helper for test_deck_history (issue #4920), kept out of that size-governed file.

The automated release PR records its own version via
`generate_deck_history.py --as-release` before the tag exists, so exactly the
pending `base-version` tag may be ahead of `git tag`.
"""

from __future__ import annotations

import re


def pending_release_tag(repo_root) -> str:
    text = (repo_root / "pyproject.toml").read_text()
    match = re.search(r'^base-version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return f"v{match.group(1)}" if match else ""


def with_pending_tag(
    repo_root, table_tags: list[str], git_tag_output: str
) -> list[str]:
    """Parse `git tag` output, appending the pending tag if the table has it."""
    real_tags = [line for line in git_tag_output.splitlines() if line.strip()]
    pending = pending_release_tag(repo_root)
    if real_tags and table_tags == [*real_tags, pending] and pending not in real_tags:
        real_tags = [*real_tags, pending]
    return real_tags
