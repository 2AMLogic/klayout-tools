"""Mutation-proposal loading/validation for the cocotb functional-verification flow.

Split out of ``functional_verification_mutation.py`` (itself split out of
``functional_verification.py``, the #1909 SDF-split precedent): the
``<proposals>`` document loader/validators and the resolution of proposals
against the request's sources. Names that stay in ``functional_verification.py``
are reached as ``_fv.<name>`` at call time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ._vendor import mutation_variants as _mutation_variants

#: `<proposals>` top-level required fields (spike §3b). `schema` is not
#: required or validated -- user-authored input, the same posture every
#: other request-taking verb's own `schema` field already takes.
_MUTATION_PROPOSALS_REQUIRED_FIELDS = ("scope", "proposals")

#: `proposals[]` entry required fields (spike §3b). `detectability_argument`
#: is optional and never validated beyond "string or absent" -- free-text,
#: echoed back unmodified, never acted on programmatically.
_MUTATION_PROPOSAL_REQUIRED_FIELDS = (
    "index",
    "category",
    "file",
    "line",
    "original_code",
    "mutated_code",
)


#: A minimal, complete `<proposals>` document: every required field filled in,
#: ``index`` 1-based (0 is rejected). Rendered into ``klt
#: functional-verification --help`` (issue #5679) so the schema is learnable
#: without a failed run per field; a test pins that it validates.
MUTATION_PROPOSALS_EXAMPLE: dict[str, Any] = {
    "schema": "klt.functional_verification.mutation_proposals/1",
    "scope": ["<a path from request.sources, byte-for-byte>"],
    "proposals": [
        {
            "index": 1,
            "category": "operator",
            "file": "<same string as in scope>",
            "line": 10,
            "original_code": "<the byte-exact source line>",
            "mutated_code": "<the replacement line>",
        }
    ],
}

#: Repo-relative location of the CLI doc, valid in a source checkout.
_CLI_DOC_REPO_PATH = "docs/cli/functional-verification.md"


def functional_verification_doc_location() -> str:
    """Where to read the ``functional-verification`` CLI doc *from this
    install*: the copy bundled in the wheel
    (``klayout_tools/data/cli/functional-verification.md``) when present, else
    the repo-relative path. The wheel ships the doc since v0.9.2 (#5288) but
    nothing pointed at it, so a pinned ``uvx`` install had a dead pointer."""
    bundled = (
        Path(__file__).resolve().parent / "data" / "cli" / "functional-verification.md"
    )
    return str(bundled) if bundled.is_file() else _CLI_DOC_REPO_PATH


@dataclass(frozen=True)
class _ProposalRecord:
    """One validated `<proposals>.proposals[]` entry -- satisfies the ported
    `_mutation_variants.MutationProposal` protocol (`index`/`file`/`line`/
    `original_code`/`mutated_code`) by construction, plus the two fields
    (`category`, `detectability_argument`) that protocol doesn't need but the
    response's `results[]` echoes back."""

    index: int
    category: str
    file: str
    line: int
    original_code: str
    mutated_code: str
    detectability_argument: str | None = None


def _load_mutation_proposals_json(path: str) -> Any:
    """Read ``path`` and decode it as JSON, raising
    :class:`FunctionalVerificationError` for every failure mode a caller
    needs to report -- the `<proposals>`-specific analogue of
    :func:`klayout_tools._paths._load_request_json`, kept separate only so
    the error text says "mutation proposals file", not "request file"."""
    if not os.path.exists(path):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file not found: {path}"
        )
    if os.path.isdir(path):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals path is not a file: {path}"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise _fv.FunctionalVerificationError(
            f"could not read mutation proposals file: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file is not valid JSON: {exc}"
        ) from exc


def _raise_collected(errors: list[str]) -> None:
    """Raise one :class:`FunctionalVerificationError` naming every collected
    validation failure (issue #5679: the author sees the whole schema gap in
    one run, not one field per run). A single failure raises its message
    unchanged; several are joined with ``"; "``, each message's text kept
    intact so substring matchers still find it. No-op when ``errors`` is
    empty."""
    if errors:
        _raise_all_collected(errors)


def _raise_all_collected(errors: list[str]) -> NoReturn:
    """The always-raising half of :func:`_raise_collected`, for call sites
    that know the list is non-empty and read better typed as diverging."""
    if len(errors) == 1:
        raise _fv.FunctionalVerificationError(errors[0])
    raise _fv.FunctionalVerificationError(
        f"--mutations input has {len(errors)} validation errors: " + "; ".join(errors)
    )


def _entry_errors(position: int, entry: dict[str, Any]) -> list[str]:
    """Every shape/type failure of one ``proposals[]`` entry (empty if valid)."""
    prefix = f"mutation proposals.proposals[{position}]"
    errors = [
        f"{prefix} is missing required field: {field}"
        for field in _MUTATION_PROPOSAL_REQUIRED_FIELDS
        if field not in entry
    ]
    for field in ("index", "line"):
        if field in entry:
            value = entry[field]
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{prefix}.{field} must be an integer")
    for field in ("category", "file"):
        if field in entry:
            value = entry[field]
            if not isinstance(value, str) or not value:
                errors.append(f"{prefix}.{field} must be a non-empty string")
    for field in ("original_code", "mutated_code"):
        if field in entry and not isinstance(entry[field], str):
            errors.append(f"{prefix}.{field} must be a string")
    detectability_argument = entry.get("detectability_argument")
    if detectability_argument is not None and not isinstance(
        detectability_argument, str
    ):
        errors.append(f"{prefix}.detectability_argument must be a string when given")
    return errors


def parse_mutation_proposals(
    path: str,
) -> tuple[list[str], list[_ProposalRecord], list[int], list[str]]:
    """Load a ``<proposals>`` document (spike §3b) and collect **every**
    structural/type failure instead of stopping at the first (issue #5679).

    Returns ``(scope, proposals, indices, errors)``: ``scope`` and
    ``proposals`` hold only what validated, ``indices`` every integer
    ``index`` seen (including on entries that failed another check, so a
    duplicate is still reported next to an unrelated missing field), and
    ``errors`` the messages -- empty iff the document is valid. A file that
    is missing, unreadable, not JSON or not an object has nothing to
    accumulate over and raises immediately.

    Per-proposal *anchor* validation is deliberately **not** done here -- that
    needs the request's resolved ``sources`` and belongs to
    :func:`_resolve_mutation_proposals`, whose failures are individual
    ``"rejected"`` results, not whole-document ones.
    """
    document = _load_mutation_proposals_json(path)
    if not isinstance(document, dict):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file must contain a JSON object: {path}"
        )
    errors = [
        f"mutation proposals file is missing required field: {field}"
        for field in _MUTATION_PROPOSALS_REQUIRED_FIELDS
        if field not in document
    ]

    scope: list[str] = []
    if "scope" in document:
        scope_raw = document["scope"]
        if not isinstance(scope_raw, list) or not all(
            isinstance(entry, str) and entry for entry in scope_raw
        ):
            errors.append(
                "mutation proposals 'scope' must be an array of non-empty strings"
            )
        else:
            scope = list(scope_raw)

    proposals: list[_ProposalRecord] = []
    indices: list[int] = []
    if "proposals" in document:
        proposals_raw = document["proposals"]
        if not isinstance(proposals_raw, list):
            errors.append("mutation proposals 'proposals' must be an array")
            proposals_raw = []
        for position, entry in enumerate(proposals_raw):
            if not isinstance(entry, dict):
                errors.append(
                    f"mutation proposals.proposals[{position}] must be a JSON object"
                )
                continue
            index = entry.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                indices.append(index)
            entry_errors = _entry_errors(position, entry)
            errors.extend(entry_errors)
            if entry_errors:
                continue
            proposals.append(
                _ProposalRecord(
                    index=entry["index"],
                    category=entry["category"],
                    file=entry["file"],
                    line=entry["line"],
                    original_code=entry["original_code"],
                    mutated_code=entry["mutated_code"],
                    detectability_argument=entry.get("detectability_argument"),
                )
            )

    return scope, proposals, indices, errors


def load_mutation_proposals(path: str) -> tuple[list[str], list[_ProposalRecord]]:
    """Load and validate a ``<proposals>`` document (spike §3b): ``schema``
    (unvalidated), ``scope`` (array of RTL source paths, matched against
    `<request>.sources` by the caller -- see
    :func:`_validate_mutation_scope`), and ``proposals[]`` (booley's own
    byte-exact shape, §1a).

    Structural/type errors here are always whole-document failures (exit 1)
    -- a malformed `<proposals>` document can produce no trustworthy
    mutation run at all, the same "fail the whole request, don't run half a
    plan" posture ``options.sdf`` validation already takes in the base
    contract. Every failure is reported at once (see
    :func:`parse_mutation_proposals`).

    Returns ``(scope, proposals)``.
    """
    scope, proposals, _indices, errors = parse_mutation_proposals(path)
    _raise_collected(errors)
    return scope, proposals


def _index_errors(indices: list[int]) -> list[str]:
    """One message per duplicate or non-positive ``index`` (spike §3b)."""
    seen: set[int] = set()
    invalid: dict[int, None] = {}
    for index in indices:
        if index <= 0 or index in seen:
            invalid[index] = None
        seen.add(index)
    return [
        f"mutation proposals[].index must be unique and positive (got {index!r})"
        for index in invalid
    ]


def _validate_proposal_indices(proposals: list[_ProposalRecord]) -> None:
    """Reject a duplicate or non-positive ``index`` before any build runs
    (spike §3b: "the same 'fail the whole request, don't run half a plan'
    posture ``options.sdf`` validation already takes"). Whole-document exit
    1, unlike a bad ``file``/anchor mismatch on one proposal, which is a
    per-proposal ``"rejected"`` result instead (see
    :func:`_resolve_mutation_proposals`)."""
    _raise_collected(_index_errors([proposal.index for proposal in proposals]))


def _scope_errors(scope: list[str], request_sources: list[str]) -> list[str]:
    """The scope-subset failure, naming both the unmatched entries and the
    ``request.sources`` strings actually available (matching stays an exact
    string compare -- issue #5679 only improves the diagnostic)."""
    missing = [entry for entry in scope if entry not in request_sources]
    if not missing:
        return []
    available = ", ".join(request_sources) if request_sources else "(none)"
    return [
        "mutation proposals 'scope' must be a subset of request.sources "
        f"-- not present in request.sources: {', '.join(missing)} "
        f"(request.sources has: {available})"
    ]


def _validate_mutation_scope(scope: list[str], request_sources: list[str]) -> None:
    """``<proposals>.scope`` must be a subset of `<request>.sources` (spike
    §3b) -- a proposal targeting a file the baseline build never compiles
    cannot be meaningfully tested. Whole-document exit 1, checked before the
    (expensive) baseline ever runs.
    """
    _raise_collected(_scope_errors(scope, request_sources))


def _load_request_and_proposals(
    request: str, proposals_path: str, base_dir: str | None = None
) -> tuple[dict[str, Any], str, list[str], list[_ProposalRecord]]:
    """Load both ``--mutations`` input documents and validate them as one
    report (issue #5679): cheap, whole-document checks before the expensive
    baseline ever runs -- a malformed proposals document or an out-of-scope
    entry should never cost a full build+test cycle to discover. The
    ``<request>`` is parsed (a cheap JSON read) *before* the proposals are
    judged so the ``scope`` subset check joins the same accumulated report:
    one run names every failure, in both documents at once.

    ``base_dir`` is forwarded to :func:`_fv.load_request_arg` so a request
    archived beside an evidence record can still be re-anchored (issue #5673).

    Returns ``(request_doc, request_dir, scope, proposal_records)``; raises
    :class:`FunctionalVerificationError` naming every failure otherwise.
    """
    scope, proposal_records, indices, errors = parse_mutation_proposals(proposals_path)
    try:
        request_doc, request_dir = _fv.load_request_arg(request, base_dir=base_dir)
    except _fv.FunctionalVerificationError as exc:
        # Both documents malformed: report the proposals failures too rather
        # than hiding them behind the request's own. `_raise_all_collected`
        # always raises here (`str(exc)` keeps the list non-empty).
        _raise_all_collected([*errors, *_index_errors(indices), str(exc)])
    _fv._reject_verilog_top_mutations(request_doc.get("testbench"))
    raw_sources = request_doc.get("sources")
    errors.extend(_index_errors(indices))
    errors.extend(
        _scope_errors(scope, raw_sources if isinstance(raw_sources, list) else [])
    )
    _raise_collected(errors)
    return request_doc, request_dir, scope, proposal_records


def _resolve_mutation_proposals(
    proposals: list[_ProposalRecord], scope: list[str], request_dir: str
) -> tuple[_mutation_variants.MutationVariantPlan, dict[int, str]]:
    """Anchor-validate every proposal against the *pristine* scope source
    (the ported seam, spike §1a), splitting them into a
    :class:`~klayout_tools._vendor.mutation_variants.MutationVariantPlan`
    of the ones that resolved cleanly and a ``{index: reason}`` map of the
    ones that did not.

    Deliberately does **not** call the ported module's own
    ``MutationVariantPlan.resolve()`` classmethod, which aborts the entire
    batch on the *first* proposal that fails ``file``-in-scope or anchor
    validation -- booley's own atomic-batch posture (§1a). This module's own
    contract is per-proposal (spike §3b/§3c): a bad ``file`` or an
    anchor mismatch on one proposal is that proposal's own ``"rejected"``
    result, not a whole-document failure -- "the same 'one bad entry doesn't
    poison the batch' posture ``klt drc``'s per-violation reporting already
    takes." So this function drives the ported module's own building blocks
    (``_read_originals``, ``_resolve_one``) directly, one proposal at a time,
    instead.
    """
    allowed = set(scope)
    try:
        originals = _mutation_variants.MutationVariantPlan._read_originals(
            Path(request_dir), allowed
        )
    except _mutation_variants.MutationVariantError as exc:
        # Every `scope` entry is already a `request.sources` member (see
        # `_validate_mutation_scope`), and every `request.sources` entry was
        # already confirmed readable by `_resolve_sources` -- so this should
        # not fire in practice. Kept as a whole-document exit 1 (not a
        # per-proposal rejection) rather than assumed unreachable: an
        # unreadable *scope* file affects every proposal naming it, not one.
        raise _fv.FunctionalVerificationError(str(exc)) from exc

    resolved: list[_mutation_variants.ResolvedMutation] = []
    rejected: dict[int, str] = {}
    for proposal in proposals:
        if proposal.file not in allowed:
            rejected[proposal.index] = (
                f"file '{proposal.file}' is outside the declared scope"
            )
            continue
        try:
            resolved.append(
                _mutation_variants.MutationVariantPlan._resolve_one(
                    proposal, originals[proposal.file]
                )
            )
        except _mutation_variants.MutationVariantError as exc:
            rejected[proposal.index] = str(exc)

    plan = _mutation_variants.MutationVariantPlan(
        Path(request_dir), originals, tuple(resolved)
    )
    return plan, rejected


# ---------------------------------------------------------------------------
# `--mutations`: isolated per-mutant build+test, bounded by a klt-level
# subprocess timeout (issue #1592, spike "Open questions")
# ---------------------------------------------------------------------------

# Imported last so that whichever module is imported first, the others find
# every name they need already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
