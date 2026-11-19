"""Diagnostic payloads that make a failed ``--check`` / signoff comparison legible.

Issue #4795. These build *additional, optional* keys only; verdicts and the
``expected``/``actual``/``match`` triple of every comparison are unchanged.
Kept out of :mod:`klayout_tools._report_verify` (and ``lvs``/``signoff``)
because those files are under the per-file size ratchet.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from . import env_provenance
from ._provenance import sha256_file
from ._report_verify import hash_check, resolve_committed_path

#: ``hash_check.field`` for a yield citation (issue #5392): its report has no
#: ``provenance.input``; the compared value is the samples document's sha256.
_YIELD_HASH_FIELD = "samples (sha256 of the named samples document)"
_LVS_OVERRIDE_HINT = "--layout/an absolute path"


def input_not_found_entry(
    path: str,
    report_path: str,
    found_relative_to_report: str | None,
    *,
    override_hint: str = _LVS_OVERRIDE_HINT,
    echoed: Any = None,
    override: str | None = None,
) -> dict[str, Any]:
    """The ``input_not_found`` diagnostic for an echoed input path that does
    not name an existing file (issues #2595, #4795).

    - ``path`` -- the path verbatim, as the report recorded it.
    - ``resolved`` -- the absolute path that was actually looked for.
    - ``found_relative_to_report`` -- where the input *does* live relative to
      the committed report's directory, or ``None``.
    - ``resolved_against`` -- the directory a relative ``path`` was anchored
      to (the cwd); ``None`` for an absolute path, which has no anchor.
    - ``resolved_repo_root`` -- the repo root a repo-scoped ``{path,
      scope}`` echo (issue #5845) was anchored on; ``None`` for every
      other echo shape and whenever ``override`` supplied the reported
      ``path`` (issue #6461: anchored on the cwd, not a repo root).
    - ``hint`` -- when the input sits next to the report, how to reach it;
      ``None`` otherwise.
    """
    report_dir = os.path.dirname(os.path.abspath(report_path))
    hint = (
        None
        if found_relative_to_report is None
        else f"run --check from the report's directory, or pass {override_hint}; "
        f"request-file-relative paths resolve against {report_dir}"
    )
    entry = {
        "path": path,
        "resolved": os.path.abspath(path),
        "found_relative_to_report": found_relative_to_report,
        "resolved_against": None if os.path.isabs(path) else os.getcwd(),
        "hint": hint,
    }
    if echoed is not None:
        # Issue #6461: gate on the override *here* -- an override supplied
        # the reported `path` (anchored on the cwd), so the echo's repo
        # root must never name an anchor the path did not use.
        entry["resolved_repo_root"] = (
            env_provenance.find_repo_root(report_dir)
            if not override
            and isinstance(echoed, Mapping)
            and echoed.get("scope") == "repo"
            else None
        )
    return entry


def input_hash_check(
    field: str,
    expected: str | None,
    echoed: Any,
    report_path: str,
    override: str | None = None,
    hasher: Callable[[str | None], str | None] = sha256_file,
    *,
    override_hint: str = _LVS_OVERRIDE_HINT,
    include_repo_root: bool = False,
) -> dict[str, Any]:
    """One :func:`~klayout_tools._report_verify.hash_check` entry for an
    input path the committed report echoes back (``klt lvs``'s ``layout``/
    ``reference``/runset ``deck_file``, ``klt drc``'s ``file``), with issue
    #2595's ``input_not_found`` diagnostic attached when that path does not
    name an existing file.

    Shared by ``klt lvs --check`` (issues #2595, #6223) and ``klt drc --check``
    (issue #6466); ``klt erc --check`` keeps its own smaller copy.

    :func:`~klayout_tools._provenance.sha256_file` returns ``None`` both for
    "the file is not there" and (vacuously) for "no path was recorded", so a
    plain ``hash_check`` renders an unresolvable input as ``actual: null`` --
    indistinguishable, to a reader, from a recorded hash that no longer
    matches. That is exactly the confusing shape issue #2595 reported when a
    committed request/report pair with *request-file-relative*
    ``layout``/``reference`` paths is ``--check``ed from a different
    directory.

    This does **not** change where the path is resolved: ``--check`` still
    anchors a relative echoed path to the current working directory, the
    same convention ``klt drc --check``'s ``file`` field uses (re-anchoring
    one verb and not the other would make the two silently disagree). It
    only makes the failure legible, as an *additional*, optional key on the
    entry -- ``expected``/``actual``/``match`` and the resulting ``status``
    are byte-identical to before:

    - ``path`` -- the echoed path verbatim, as the report recorded it.
    - ``resolved`` -- the absolute path that was actually looked for.
    - ``found_relative_to_report`` -- the absolute path the input *does*
      occupy relative to the committed report's own directory, when it is
      there; ``null`` otherwise (including for an echoed absolute path,
      which has no report-relative candidate to try).

    ``hasher`` is the recorded digest form: ``sha256_file`` (bare hex, default;
    ``klt lvs``'s ``environment.*_sha256``) or ``content_hash``
    (``sha256:``-prefixed; the LVS runset deck, issue #6223, and ``klt drc``'s
    ``provenance.input.content_hash``, issue #6466).

    Issue #2659: ``echoed`` is resolved through
    :func:`klayout_tools._report_verify.resolve_committed_path`, so the
    ``{path, scope}`` object ``layout`` carries today is joined to the repo
    root of the committed report's own directory -- which is precisely the
    case the "known limitation" above describes, now *resolved* for the
    layout side rather than merely diagnosed; ``reference`` too since #4984
    (only a bare-string one keeps the cwd anchoring). An
    entry naming no path at all (``scope: "external"``/``"absent"``, or a
    report with no such field) resolves to ``None`` and renders exactly as
    an absent one always has.
    """
    path = resolve_committed_path(echoed, report_path=report_path, override=override)
    check = hash_check(field, expected, hasher(path))
    if path is None or os.path.isfile(path):
        return check
    found_relative_to_report: str | None = None
    if not os.path.isabs(path):
        candidate = os.path.join(os.path.dirname(os.path.abspath(report_path)), path)
        if os.path.isfile(candidate):
            found_relative_to_report = os.path.abspath(candidate)
    check["input_not_found"] = input_not_found_entry(
        path,
        report_path,
        found_relative_to_report,
        override_hint=override_hint,
        echoed=echoed if include_repo_root else None,
        override=override,
    )
    return check


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

    Issue #5837 extends both to a ``provenance.input`` that carries only
    ``content_hash``/``role`` -- the shape ``build_provenance`` emits, with no
    ``path``/``paths`` to name in ``compared_against``: the hint states the pin
    must be the envelope's recorded input hash, and the envelope-own-hash check
    still flags a pin of the evidence file's own sha256.
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
    what = None
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
    if what is None:
        # Issue #5837: no path to name in compared_against -- either
        # provenance.input carries only content_hash/role (the
        # `build_provenance` shape) or there is no input block at all. The
        # hint and the own-hash check below still apply.
        hint = (
            "content_hash must be the envelope's provenance.input.content_hash "
            "(the recorded input hash), not the hash of the evidence envelope "
            "file"
        )
    else:
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
