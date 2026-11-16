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

import json
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .signoff import SignoffError, _classify


def _recognised_kind(root: str, path: str) -> str | None:
    """The envelope kind of ``root/path``, or ``None`` when the file is
    missing, is not a JSON object, or is not a recognised envelope."""
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(envelope, Mapping):
        return None
    try:
        return _classify(envelope, path)
    except SignoffError:
        return None


def evidence_checks(
    root: str,
    citations: Mapping[str, Sequence[str]],
    file_check: Callable[[str, str, str], dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(cited, present, envelope)``: the sorted cited paths, and per path the
    existence check and the recognition check (role ``evidence``, ``kind``
    ``None`` on failure). A passing recognition check implies the file exists,
    so the spec-row check needs only it."""
    cited = sorted({path for paths in citations.values() for path in paths})
    present = {path: file_check(root, path, "evidence") for path in cited}
    envelopes = {}
    for path in cited:
        kind = _recognised_kind(root, path)
        envelopes[path] = {
            "check": "envelope",
            "role": "evidence",
            "path": path,
            "kind": kind,
            "passed": kind is not None,
        }
    return cited, present, envelopes
