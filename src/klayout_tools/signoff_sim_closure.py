"""Re-hash a ``klt sim`` citation's recorded ``.include`` closure (issue #7860).

``provenance.input.content_hash`` of a ``klt sim`` report digests the top deck
only (it is pinned equal to ``environment.netlist_sha256``, issue #5569), so a
thin wrapper that ``.include``-s its real sources keeps a matching hash after
an included file changes -- and ``klt signoff --manifest`` used to read such a
citation as fresh: ``input_verified: true`` over a circuit that no longer is
the one simulated.

The closure itself is already recorded: ``environment.netlist_closure`` lists
every resolved ``.include``/``.inc`` file with its raw ``sha256`` (issue
#2799), and ``klt sim --check`` (issue #5821, :mod:`.sim_report_check`)
re-hashes it against the working tree. This module is the signoff-side reader
of the same data -- it reuses that module's path resolution and the shared
``sha256_file`` rather than hashing a second way, adds no ``klt sim`` report
field, and leaves the meaning of ``provenance.input.content_hash`` alone.

**Grading.** ``input_verified`` is disclosure-only for every native kind
(``docs/cli/signoff.md``, "No verdict changes"), so a drifted include never
turns a ``met`` item ``unmet`` here either. It does what a changed top deck
already does: ``input_verified`` becomes ``false``, and the citation gains a
``sim_closure`` block naming each file that moved.

**What each outcome means** (``sim_closure.status``):

* ``verified`` -- every recorded include re-hashed to its recorded digest.
* ``stale`` -- at least one include was read and differs. A raw-byte mismatch
  is stale even when only a comment moved, exactly as for the top deck; run
  ``klt sim --check`` for its ``cosmetic``/``electrical`` diagnosis.
* ``partial`` -- nothing drifted, but some include could not be judged here
  (host-resolved ``$VAR`` or ``$PDK_ROOT`` target, ``scope: external``, not
  readable from this grading context, or untracked in a git checkout). The
  gap is named in ``unverifiable``.
* ``not_recorded`` -- the report carries no closure (it predates #2799, or
  ``netlist_closure_error`` is set). Top-deck-only behaviour, now said aloud.

Only ``stale`` changes ``input_verified`` (to ``false``), mirroring the
documented contract that ``false`` requires genuinely reading a named file and
finding it different. Every other status leaves it as the top-deck check set
it, so existing citations grade exactly as before; the block is what tells a
reader how far the include check reached.

The closure's first entry is the top deck itself, which
:func:`~klayout_tools.signoff._verify_input_artifact` already re-hashes, so it
is not counted again here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._git_tracked import is_untracked_working_tree_file
from ._provenance import sha256_file
from .sim_report_check import entry_path

_KIND = "sim"

_VERIFIED = "verified"
_STALE = "stale"
_PARTIAL = "partial"
_NOT_RECORDED = "not_recorded"


def _closure(envelope: Mapping[str, Any]) -> list[Any] | None:
    environment = envelope.get("environment")
    closure = (
        environment.get("netlist_closure") if isinstance(environment, Mapping) else None
    )
    return closure if isinstance(closure, list) and closure else None


def _not_recorded(envelope: Mapping[str, Any]) -> dict[str, Any]:
    environment = envelope.get("environment")
    errored = isinstance(environment, Mapping) and environment.get(
        "netlist_closure_error"
    )
    return {
        "status": _NOT_RECORDED,
        "reason": "netlist_closure_error" if errored else "no_netlist_closure",
    }


def _unverifiable(entry: Any, reason: str) -> dict[str, Any]:
    named = entry if isinstance(entry, Mapping) else {}
    label = named.get("path") or named.get("target")
    return {"path": label, "reason": named.get("unhashed_reason") or reason}


def _check_entry(
    entry: Any, evidence_file: str | None, repo_root: str | Path | None
) -> tuple[str, dict[str, Any]]:
    """``(outcome, row)`` for one include entry; ``outcome`` is ``verified``,
    ``stale`` or ``unverifiable``."""
    recorded = entry.get("sha256") if isinstance(entry, Mapping) else None
    if not isinstance(recorded, str) or not recorded:
        return "unverifiable", _unverifiable(entry, "not_hashed")
    path = entry_path(
        entry,
        report_path=evidence_file or "",
        root=str(repo_root) if repo_root is not None else None,
    )
    if path is None:
        return "unverifiable", _unverifiable(entry, "unresolvable_scope")
    # Same git-index basis as the top deck (#6139): a file git does not track
    # is not evidence a clean checkout would reproduce.
    if is_untracked_working_tree_file(path):
        return "unverifiable", _unverifiable(entry, "untracked")
    actual = sha256_file(path)
    if actual is None:
        return "unverifiable", _unverifiable(entry, "not_found")
    if actual == recorded:
        return _VERIFIED, {}
    return _STALE, {
        "path": entry.get("path"),
        "recorded": recorded,
        "actual": actual,
    }


def check_sim_closure(
    envelope: Mapping[str, Any],
    evidence_file: str | None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """The ``sim_closure`` block for a ``klt sim`` envelope -- never raises,
    never reads anything the envelope did not itself record."""
    closure = _closure(envelope)
    if closure is None:
        return _not_recorded(envelope)
    includes = closure[1:]  # entry 0 is the top deck; see the module docstring
    stale: list[dict[str, Any]] = []
    unverifiable: list[dict[str, Any]] = []
    for entry in includes:
        outcome, row = _check_entry(entry, evidence_file, repo_root)
        if outcome == _STALE:
            stale.append(row)
        elif outcome == "unverifiable":
            unverifiable.append(row)
    status = _STALE if stale else _PARTIAL if unverifiable else _VERIFIED
    return {
        "status": status,
        "includes": len(includes),
        "stale": stale,
        "unverifiable": unverifiable,
    }


def with_sim_closure(
    resolution: dict[str, Any],
    evidence_file: str | None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """``resolution`` with a ``sim`` envelope's closure checked: gains
    ``sim_closure`` and, on a stale include, ``input_verified: False``. Any
    other kind is returned untouched, with no new key."""
    if resolution.get("kind") != _KIND:
        return resolution
    block = check_sim_closure(resolution["envelope"], evidence_file, repo_root)
    resolution["sim_closure"] = block
    if block["status"] == _STALE:
        resolution["input_verified"] = False
    return resolution


def sim_closure_disclosure(resolution: Mapping[str, Any]) -> dict[str, Any]:
    """The citation fragment for :func:`with_sim_closure`'s block: ``{}`` for
    any resolution that has none, so non-``sim`` citations are unchanged."""
    block = resolution.get("sim_closure")
    return {"sim_closure": block} if block is not None else {}
