"""Item 8 (characterization report) envelope recognition (issue #5688).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet). Item 8
documents ``citations`` as paths to *evidence envelopes*, so each cited file is
checked to be one: it must parse as JSON and be accepted by the same
recogniser ``--manifest`` grading applies to a citation
(:func:`klayout_tools.signoff._classify`), so the two agree on what "evidence
envelope" means. Recognition only -- the envelope's own verdict and any kind
restriction stay with the caller's ``envelope_checks`` command.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .signoff import (
    SignoffError,
    _classify,
    _classify_failure_reason,
    _read_envelope,
)
from .signoff_drc_crosscheck import crosscheck_project


def _recognition(root: str, path: str) -> tuple[str | None, str | None]:
    """``(kind, reason)`` for ``root/path``.

    ``kind`` is the envelope kind, or ``None`` when the file cannot be read
    (missing, directory, bad gzip), is not valid JSON or carries nonfinite
    literals, is not a JSON object, or is not a recognised envelope. The read
    goes through :func:`klayout_tools.signoff._read_envelope` -- the same
    gzip-transparent, nonfinite-refusing reader ``--manifest`` grading applies
    to a citation -- so the two agree on which committed forms are evidence
    (issue #5452).

    ``reason`` (issue #5986) is the diagnosis ``--manifest`` grading gives a
    document that *did* parse as JSON and was refused: ``"unrecognized_envelope"``
    (not an object, matches no shape, or malformed for one) or
    ``"envelope_version_skew"`` (a response from an older klt -- the remedy is
    to re-run the check, not to cite another file), via the same
    :func:`klayout_tools.signoff._classify_failure_reason`. It is ``None`` for
    a recognised envelope and for an unreadable one, which stays reasonless.

    Issue #6809: a cross-check envelope is projected to its single-engine
    equivalent before classification, exactly as ``--manifest`` grading does
    (issue #5815), so a whole ``--engine both`` citation is recognised ``drc``
    here too. Anything the projector refuses stays as-is and fails
    recognition below, still carrying the appropriate reason."""
    try:
        envelope = _read_envelope(os.path.join(root, path))
    except SignoffError:
        return None, None
    try:
        if not isinstance(envelope, Mapping):
            raise SignoffError(f"envelope '{path}' is not a JSON object")
        envelope = crosscheck_project(envelope) or envelope
        return _classify(envelope, path), None
    except SignoffError as exc:
        return None, _classify_failure_reason(exc)


def evidence_checks(
    root: str,
    citations: Mapping[str, Sequence[str]],
    file_check: Callable[[str, str, str], dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(cited, present, envelope)``: the sorted cited paths, and per path the
    existence check and the recognition check (role ``evidence``, ``kind``
    ``None`` on failure, plus the additive ``reason`` when a parsed document
    was refused). A passing recognition check implies the file exists, so the
    spec-row check needs only it."""
    cited = sorted({path for paths in citations.values() for path in paths})
    present = {path: file_check(root, path, "evidence") for path in cited}
    envelopes = {}
    for path in cited:
        kind, reason = _recognition(root, path)
        envelopes[path] = {
            "check": "envelope",
            "role": "evidence",
            "path": path,
            "kind": kind,
            "passed": kind is not None,
            **({"reason": reason} if reason is not None else {}),
        }
    return cited, present, envelopes
