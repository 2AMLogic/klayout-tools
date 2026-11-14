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

from ._provenance import sha256_file
from ._report_verify import hash_check

#: ``hash_check.field`` for a yield citation (issue #5392): its report has no
#: ``provenance.input``; the compared value is the samples document's sha256.
_YIELD_HASH_FIELD = "samples (sha256 of the named samples document)"


def input_not_found_entry(
    path: str, report_path: str, found_relative_to_report: str | None
) -> dict[str, Any]:
    """The ``input_not_found`` diagnostic for an echoed input path that does
    not name an existing file (issues #2595, #4795).

    - ``path`` -- the path verbatim, as the report recorded it.
    - ``resolved`` -- the absolute path that was actually looked for.
    - ``found_relative_to_report`` -- where the input *does* live relative to
      the committed report's directory, or ``None``.
    - ``resolved_against`` -- the directory a relative ``path`` was anchored
      to (the cwd); ``None`` for an absolute path, which has no anchor.
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
    return {
        "path": path,
        "resolved": os.path.abspath(path),
        "found_relative_to_report": found_relative_to_report,
        "resolved_against": None if os.path.isabs(path) else os.getcwd(),
        "hint": hint,
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
