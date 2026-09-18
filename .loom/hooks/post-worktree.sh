#!/bin/bash
# Loom post-worktree hook (repo-owned, klayout-tools-local).
#
# Problem (issue #2020): worktree.sh / pr-worktree.sh never re-run any Python
# install step when creating a new worktree. A `pip install -e .` (or `uv
# sync`) run once against the main checkout records an ABSOLUTE path in its
# editable finder, so `pytest` / `python3 -m klayout_tools...` invoked from
# inside *any* worktree can silently import the MAIN CHECKOUT's src/, not the
# worktree's own modified copy -- a false negative if the worktree fixes a
# bug (old code still runs) or a false positive if it introduces one (the
# broken code is never actually imported).
#
# Fix: give every worktree its OWN uv-managed virtualenv (`.venv/`, already
# gitignored at the repo root) and its own editable install, by running
# `uv sync --extra dev` here. This is idempotent and safe to re-run.
#
# `--extra dev` pulls in pytest/ruff so `uv run pytest` never silently falls
# back to a stray global `~/.local/bin/pytest` that has its own (possibly
# stale, possibly main-checkout-pointing) editable install.
#
# Invoked by worktree.sh after every issue worktree is created, and by
# pr-worktree.sh after every PR review worktree's branch checkout succeeds
# (both call sites pass: $1 worktree path, $2 branch name, $3 issue/PR
# number). Never overwritten by a Loom reinstall/upgrade -- see
# .loom/docs/repo-owned-files.md.
#
# Best-effort by design: a failure here must never abort worktree creation.
# Both call sites already treat a non-zero exit as "warn and continue".
set -uo pipefail

WORKTREE_DIR="${1:-$(pwd)}"

if ! cd "$WORKTREE_DIR" 2>/dev/null; then
    echo "post-worktree: could not cd to $WORKTREE_DIR -- skipping uv sync" >&2
    exit 1
fi

if [[ ! -f "pyproject.toml" ]]; then
    echo "post-worktree: no pyproject.toml at $WORKTREE_DIR -- skipping uv sync" >&2
    exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "post-worktree: 'uv' not found on PATH -- skipping venv sync (tests run here may import the wrong checkout, see issue #2020)" >&2
    exit 1
fi

if uv sync --extra dev --quiet; then
    echo "post-worktree: 'uv sync --extra dev' complete -- $WORKTREE_DIR has its own .venv + editable install"
    exit 0
else
    echo "post-worktree: 'uv sync --extra dev' failed in $WORKTREE_DIR -- worktree still usable, but its own editable install may be stale or missing (issue #2020)" >&2
    exit 1
fi
