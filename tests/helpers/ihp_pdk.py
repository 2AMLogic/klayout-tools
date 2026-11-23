"""Resolve the fetched IHP-Open-PDK ``ihp-sg13g2`` variant for live tiers.

The single canonical home for the "where does the real IHP install live"
question (issue #7255). Before this module existed, two test files open-coded
slightly different resolvers -- one with a ``.loom/worktrees`` path-name
heuristic, one whose env override replaced the whole candidate list -- and
they validated different files, so the two live suites could drift apart
(and skip for different reasons). Import the resolver instead:

    from helpers.ihp_pdk import find_ihp_sg13g2

Candidates, in order: ``$KLT_IHP_SG13G2_ROOT`` (the variant directory
itself, relative paths resolve against the cwd), ``<repo>/pdks/…`` in the
active checkout, then the main checkout's equivalent when the active
checkout is a linked git worktree (a Loom worktree under
``.loom/worktrees/`` shares the main checkout's gitignored ``pdks/``).
A root is accepted only when it carries both ngspice assets the live
suites need; each test file adds whatever else it requires on top.

The resolver evaluates lazily (on call, not at import) on purpose: callers
that gate with module-level ``skipif`` marks bind the result once at
import, while fixture-time callers stay sensitive to a ``monkeypatch``ed
environment.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: The variant directory, relative to a checkout's gitignored ``pdks/``.
VARIANT = Path("pdks/ihp-open-pdk/ihp-sg13g2")

#: The compiled PSP103 OSDI model every accepted root must carry.
PSP103_OSDI = Path("libs.tech/ngspice/osdi/psp103.osdi")

#: The ngspice corner card that instantiates it.
CORNER_MOS_LIB = Path("libs.tech/ngspice/models/cornerMOSlv.lib")

#: Files whose presence makes a candidate root a real, sim-ready install.
REQUIRED_FILES = (PSP103_OSDI, CORNER_MOS_LIB)


def _shared_checkout(repo: Path) -> Path | None:
    """Root of the main checkout when ``repo`` is a linked git worktree.

    Git's own metadata names the main checkout, so this holds wherever the
    worktree lives (``.loom/worktrees/<name>``, ``.loom/worktrees/repo/pr-<N>``).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    common = (repo / out).resolve()
    return common.parent if out and common.name == ".git" else None


def _is_sim_ready(root: Path) -> bool:
    return all((root / file).is_file() for file in REQUIRED_FILES)


def find_ihp_sg13g2(repo: Path | None = None) -> Path | None:
    """The fetched IHP-Open-PDK ``ihp-sg13g2`` variant root, or ``None``.

    Invalid candidates (including a ``$KLT_IHP_SG13G2_ROOT`` that points
    nowhere, or at an install missing a required file) are skipped, never
    raised: the caller sees ``None`` and skips its live tier.
    """
    if repo is None:
        repo = Path(__file__).resolve().parents[2]
    candidates: list[Path] = []
    override = os.environ.get("KLT_IHP_SG13G2_ROOT")
    if override:
        candidates.append(Path(override).resolve())
    candidates.append(repo / VARIANT)
    shared = _shared_checkout(repo)
    if shared is not None and shared != repo.resolve():
        candidates.append(shared / VARIANT)
    for root in candidates:
        if _is_sim_ready(root):
            return root
    return None
