"""Diagnostic payloads that make a failed ``--check`` / signoff comparison legible.

Issue #4795. These build *additional, optional* keys only; verdicts and the
``expected``/``actual``/``match`` triple of every comparison are unchanged.
Kept out of :mod:`klayout_tools._report_verify` (and ``lvs``/``signoff``)
because those files are under the per-file size ratchet.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from . import env_provenance
from ._provenance import sha256_file
from ._report_verify import hash_check

#: ``hash_check.field`` for a yield citation (issue #5392): its report has no
#: ``provenance.input``; the compared value is the samples document's sha256.
_YIELD_HASH_FIELD = "samples (sha256 of the named samples document)"


def input_not_found_entry(
    path: str,
    report_path: str,
    found_relative_to_report: str | None,
    *,
    echoed: Any = None,
) -> dict[str, Any]:
    """The ``input_not_found`` diagnostic for an echoed input path that does
    not name an existing file (issues #2595, #4795).

    - ``path`` -- the path verbatim, as the report recorded it.
    - ``resolved`` -- the absolute path that was actually looked for.
    - ``found_relative_to_report`` -- where the input *does* live relative to
      the committed report's directory, or ``None``.
    - ``resolved_against`` -- the directory a relative ``path`` was anchored
      to (the cwd); ``None`` for an absolute path, which has no anchor.
    - ``resolved_repo_root`` -- for a repo-scoped ``{path, scope}`` echo
      (issue #5845), the repo root the recorded path was anchored on --
      discovered from the report's own directory, the identical anchor
      :func:`klayout_tools._report_verify.resolve_committed_path` uses;
      ``None`` for every other echo shape (a bare string resolves against
      the cwd, not a repo root).
    - ``hint`` -- when the input sits next to the report, how to reach it;
      ``None`` otherwise.
    """
    report_dir = os.path.dirname(os.path.abspath(report_path))
    hint = (
        None
        if found_relative_to_report is None
        else "run --check from the report's directory, or pass --layout/"
        "an absolute path; request-file-relative paths resolve against "
        f"{report_dir}"
    )
    repo_root = None
    if isinstance(echoed, Mapping) and echoed.get("scope") == "repo":
        repo_root = env_provenance.find_repo_root(report_dir)
    return {
        "path": path,
        "resolved": os.path.abspath(path),
        "found_relative_to_report": found_relative_to_report,
        "resolved_against": None if os.path.isabs(path) else os.getcwd(),
        "resolved_repo_root": repo_root,
        "hint": hint,
    }


def deck_not_found_entry(
    recorded: str, name: Any, *, override: bool = False
) -> dict[str, Any]:
    """The ``deck_not_found`` diagnostic for a ``--engine klayout`` report
    whose deck resolves by no route (issue #5709). Additive key on the
    still-failing ``provenance.deck.content_hash`` check; never a skip.

    - ``recorded`` -- the ``deck`` path the report recorded; ``resolved`` its
      absolute form (what was looked for first).
    - ``name`` -- ``provenance.deck.name``, the basename looked up among the
      packaged decks (``packaged_dir``); ``None`` when the report has none.
    - ``hint`` -- how to supply the deck.
    - ``override`` -- ``recorded`` is the ``--deck-file`` path itself, the
      thing that was looked for (a miss is the flag's, not the report's);
      ``name`` is ``None`` (no packaged lookup happened) and the hint names
      the flag instead of suggesting it.
    """
    return {
        "recorded": recorded,
        "resolved": os.path.abspath(recorded),
        "name": None if override else name,
        "packaged_dir": os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "decks"
        ),
        "hint": (
            "--deck-file names no existing file; point it at the deck "
            "script to verify against"
            if override
            else "pass --deck-file <path to the deck script> to verify against it"
        ),
    }


def stale_pin(spec: Mapping[str, Any], resolution: Mapping[str, Any]) -> dict[str, Any]:
    """``detail`` for a ``stale_evidence`` verdict: names the failed
    comparison (field / expected / actual, the entry shape ``--check`` emits)
    so the author can tell which input to regenerate or re-pin (issue #4795).

    Issue #4837 adds, additively, ``compared_against`` (what ``actual`` was
    computed over) and ``hint`` (which hash ``content_hash`` must be), plus
    ``pin_is_envelope_file_hash`` when the pin is the cited envelope file's own
    sha256 -- the usual mistake.
    """
    expected = spec.get("content_hash")
    samples_path = resolution.get("yield_samples_path")
    detail: dict[str, Any] = {
        "content_hash_pin": "stale",
        "hash_check": hash_check(
            _YIELD_HASH_FIELD
            if samples_path is not None
            else "provenance.input.content_hash",
            expected,
            resolution["content_hash"],
        ),
    }
    envelope = resolution.get("envelope")
    provenance = envelope.get("provenance") if isinstance(envelope, Mapping) else None
    block = provenance.get("input") if isinstance(provenance, Mapping) else None
    block = block if isinstance(block, Mapping) else {}
    members = block.get("paths")
    if isinstance(members, list) and members:
        names = []
        for member in members:
            path = member.get("path") if isinstance(member, Mapping) else None
            path = path.get("path") if isinstance(path, Mapping) else path
            if isinstance(path, str):
                names.append(path)
        detail["compared_against"] = names
        what = "the input-set digest over the files in compared_against"
    elif isinstance(block.get("path"), str):
        detail["compared_against"] = block["path"]
        what = f"the sha256 of the input artifact {block['path']}"
    elif isinstance(samples_path, str):
        # Issue #5392: a yield report has no provenance.input; the hash is
        # over the samples document the report names.
        detail["compared_against"] = samples_path
        what = f"the sha256 of the samples document {samples_path}"
    else:
        return detail
    field = "samples document" if samples_path else "provenance.input.content_hash"
    hint = (
        f"content_hash must be {what} ({field}), "
        "not the hash of the evidence envelope file"
    )
    evidence_file = resolution.get("file")
    own = sha256_file(evidence_file) if isinstance(evidence_file, str) else None
    if own is not None and expected == f"sha256:{own}":
        hint = (
            "the pinned content_hash is the sha256 of the evidence envelope "
            f"file {evidence_file} itself; " + hint
        )
        detail["pin_is_envelope_file_hash"] = True
    detail["hint"] = hint
    return detail
