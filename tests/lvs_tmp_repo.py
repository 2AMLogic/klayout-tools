"""Shared `.git`-seeding `tmp_path` override for ``klayout-runset`` test
modules (issue #5666, ported from `tests/test_lvs.py`'s issue-#2659 fixture).

Import the module and bind the fixture to a module-level name so pytest
collects it in place of the built-in::

    import lvs_tmp_repo
    tmp_path = lvs_tmp_repo.tmp_path  # pytest collects the fixture by name

Seeding a `.git` marker makes `tmp_path` itself this run's repo root
(`env_provenance.find_repo_root` only checks for a `.git` entry). Since
#5666 the runset report's `layout` is the committable `{path, scope}`
envelope built from the *resolved* layout path, the same as the
curated-deck report; a layout under a bare `tmp_path` resolves to
`scope: "external"` with `path: null` -- correct, but `--check`/`--rerun`
cannot verify it (the one case resolution cannot cover). Seeding the marker
puts every test in the *realistic* position of a design inside a repo,
which is the only position a report committed as `klt signoff` evidence is
ever in. The external branch is pinned separately by
`tests/test_env_provenance.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """Seeded repo root; see the module docstring."""
    (tmp_path / ".git").mkdir()
    return tmp_path
