"""``klt signoff`` flag-combination validation (issue #5527).

The modes ``klt signoff`` offers are mutually exclusive, and several of its
flags only mean anything in one of them. Deciding that is pure
``args``-inspection with no I/O beyond one ``isdir``, so it lives here
rather than inside :func:`klayout_tools.cli.signoff_cmd.run` -- which keeps
that dispatcher under the complexity ratchet and lets
:mod:`klayout_tools.cli.signoff_repin_cmd` share the ``--repo-root`` check
instead of restating its wording.

Each function returns the one-line refusal message, or ``None`` when the
invocation is fine; the caller owns the envelope and the exit code.
"""

from __future__ import annotations

import argparse
import os


def repo_root_problem(repo_root: str | None) -> str | None:
    """Issue #2878: an explicit ``--repo-root`` that is not an existing
    directory is refused, never silently ignored -- grading that *looks*
    right while resolving against nothing would be worse than the error."""
    if repo_root is not None and not os.path.isdir(repo_root):
        return f"--repo-root '{repo_root}' is not an existing directory"
    return None


def mode_flag_problem(
    args: argparse.Namespace,
    manifest_source: str | None,
    fleet_source: str | None,
) -> str | None:
    """The reason the report modes cannot run as invoked, or ``None``.

    Extracted from :func:`run` (issue #5527) so adding ``--repin``'s own
    dispatch did not push it past the complexity ratchet; the checks, their
    order and their wording are unchanged.
    """
    graded = manifest_source or fleet_source
    if manifest_source and fleet_source:
        return "--manifest and --fleet are mutually exclusive"
    if getattr(args, "tiers_doc", None) and not graded:
        return (
            "--tiers-doc only applies to --manifest/--fleet (envelope "
            "aggregation does not read the design-evidence-tiers doc)"
        )
    if getattr(args, "check", None) and not graded:
        return (
            "--check only applies to --manifest/--fleet (it verifies a "
            "committed tier/fleet report still reproduces from its manifest; "
            "envelope aggregation produces no such report)"
        )
    repo_root = getattr(args, "repo_root", None)
    if repo_root is not None and not graded:
        return (
            "--repo-root only applies to --manifest/--fleet (envelope "
            "aggregation re-hashes no input artifacts)"
        )
    return repo_root_problem(repo_root)
