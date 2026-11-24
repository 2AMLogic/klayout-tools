"""Shared discovery of a fetched IHP-Open-PDK ``ihp-sg13g2`` variant root.

``find_ihp_sg13g2()`` is the canonical answer to "where is the fetched IHP
variant on this host" for the test suite (issue #7327). The previous inline
copy in ``tests/test_characterize_vendor_comparison.py`` matched parent
directory names to find the main checkout from a Loom worktree, which misses
nested layouts (``.loom/worktrees/<repo>/pr-<N>``), and kept a relative
``$KLT_IHP_SG13G2_ROOT`` relative. Here the shared checkout is resolved from
git's own worktree metadata (``git rev-parse --git-common-dir``, the parent
of the resolved common ``.git``), so it holds wherever the worktree lives,
and the override is ``.resolve()``d before use.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: The fetched variant's path relative to a checkout root.
VARIANT = Path("pdks/ihp-open-pdk/ihp-sg13g2")

#: Files whose presence makes a candidate root a complete install: the
#: compiled OSDI model, ngspice corner card and optional vendor Liberty tree.
OSDI_MODEL = Path("libs.tech/ngspice/osdi/psp103.osdi")
CORNER_MOS_LIB = Path("libs.tech/ngspice/models/cornerMOSlv.lib")
STDCELL_LIB = Path("libs.ref/sg13g2_stdcell/lib")


def _shared_checkout(repo: Path) -> Path | None:
    """Root of the main checkout when ``repo`` is a linked git worktree.

    Git's own metadata names the main checkout, so this holds wherever the
    worktree lives (``.loom/worktrees/<name>``, ``.loom/worktrees/<repo>/pr-<N>``).
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


def _complete(root: Path, require_stdcell: bool) -> bool:
    return (
        (root / OSDI_MODEL).is_file()
        and (root / CORNER_MOS_LIB).is_file()
        and (not require_stdcell or (root / STDCELL_LIB).is_dir())
    )


def find_ihp_sg13g2(
    repo: Path | None = None, *, require_stdcell: bool = True
) -> Path | None:
    """The fetched IHP-Open-PDK ``ihp-sg13g2`` variant: ``$KLT_IHP_SG13G2_ROOT``
    (the variant directory itself, resolved to an absolute path and additive
    to the other candidates), else ``pdks/ihp-open-pdk/ihp-sg13g2`` in the
    active checkout (``repo``, this repo by default), else in the checkout
    this worktree hangs off (a Loom worktree under ``.loom/worktrees/``
    shares the main checkout's gitignored ``pdks/``).

    Device simulations can use a model-only install with ``require_stdcell=False``.
    The environment is read at call time so ``monkeypatch.setenv`` works.
    """
    candidates: list[Path] = []
    override = os.environ.get("KLT_IHP_SG13G2_ROOT")
    if override:
        candidates.append(Path(override).resolve())
    if repo is None:
        repo = Path(__file__).resolve().parents[2]
    candidates.append(repo / VARIANT)
    shared = _shared_checkout(repo)
    if shared is not None and shared != repo.resolve():
        candidates.append(shared / VARIANT)
    for root in candidates:
        if _complete(root, require_stdcell):
            return root
    return None
