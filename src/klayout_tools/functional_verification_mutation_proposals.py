"""Mutation-proposal loading/validation for the cocotb functional-verification flow.

Split out of ``functional_verification_mutation.py`` (itself split out of
``functional_verification.py``, the #1909 SDF-split precedent): the
``<proposals>`` document loader/validators and the resolution of proposals
against the request's sources. Names that stay in ``functional_verification.py``
are reached as ``_fv.<name>`` at call time; every name is re-exported from there.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    contract. Per-proposal *anchor* validation (does ``original_code``
    actually occur once on the declared line of the *pristine* source) is
    deliberately **not** done here -- that needs the request's own resolved
    ``sources``, and belongs to :func:`_resolve_mutation_proposals`, whose
    failures are individual ``"rejected"`` results, not whole-document ones.

    Returns ``(scope, proposals)``.
    """
    document = _load_mutation_proposals_json(path)
    if not isinstance(document, dict):
        raise _fv.FunctionalVerificationError(
            f"mutation proposals file must contain a JSON object: {path}"
        )
    for field in _MUTATION_PROPOSALS_REQUIRED_FIELDS:
        if field not in document:
            raise _fv.FunctionalVerificationError(
                f"mutation proposals file is missing required field: {field}"
            )

    scope_raw = document["scope"]
    if not isinstance(scope_raw, list) or not all(
        isinstance(entry, str) and entry for entry in scope_raw
    ):
        raise _fv.FunctionalVerificationError(
            "mutation proposals 'scope' must be an array of non-empty strings"
        )
    scope = list(scope_raw)

    proposals_raw = document["proposals"]
    if not isinstance(proposals_raw, list):
        raise _fv.FunctionalVerificationError(
            "mutation proposals 'proposals' must be an array"
        )

    proposals: list[_ProposalRecord] = []
    for position, entry in enumerate(proposals_raw):
        if not isinstance(entry, dict):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}] must be a JSON object"
            )
        for field in _MUTATION_PROPOSAL_REQUIRED_FIELDS:
            if field not in entry:
                raise _fv.FunctionalVerificationError(
                    f"mutation proposals.proposals[{position}] is missing "
                    f"required field: {field}"
                )

        index = entry["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].index must be an integer"
            )
        category = entry["category"]
        if not isinstance(category, str) or not category:
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].category must be "
                "a non-empty string"
            )
        file_field = entry["file"]
        if not isinstance(file_field, str) or not file_field:
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].file must be a "
                "non-empty string"
            )
        line = entry["line"]
        if not isinstance(line, int) or isinstance(line, bool):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].line must be an integer"
            )
        original_code = entry["original_code"]
        if not isinstance(original_code, str):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].original_code "
                "must be a string"
            )
        mutated_code = entry["mutated_code"]
        if not isinstance(mutated_code, str):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].mutated_code "
                "must be a string"
            )
        detectability_argument = entry.get("detectability_argument")
        if detectability_argument is not None and not isinstance(
            detectability_argument, str
        ):
            raise _fv.FunctionalVerificationError(
                f"mutation proposals.proposals[{position}].detectability_argument "
                "must be a string when given"
            )

        proposals.append(
            _ProposalRecord(
                index=index,
                category=category,
                file=file_field,
                line=line,
                original_code=original_code,
                mutated_code=mutated_code,
                detectability_argument=detectability_argument,
            )
        )

    return scope, proposals


def _validate_proposal_indices(proposals: list[_ProposalRecord]) -> None:
    """Reject a duplicate or non-positive ``index`` before any build runs
    (spike §3b: "the same 'fail the whole request, don't run half a plan'
    posture ``options.sdf`` validation already takes"). Whole-document exit
    1, unlike a bad ``file``/anchor mismatch on one proposal, which is a
    per-proposal ``"rejected"`` result instead (see
    :func:`_resolve_mutation_proposals`)."""
    seen: set[int] = set()
    for proposal in proposals:
        if proposal.index <= 0 or proposal.index in seen:
            raise _fv.FunctionalVerificationError(
                "mutation proposals[].index must be unique and positive "
                f"(got {proposal.index!r})"
            )
        seen.add(proposal.index)


def _validate_mutation_scope(scope: list[str], request_sources: list[str]) -> None:
    """``<proposals>.scope`` must be a subset of `<request>.sources` (spike
    §3b) -- a proposal targeting a file the baseline build never compiles
    cannot be meaningfully tested. Whole-document exit 1, checked before the
    (expensive) baseline ever runs.
    """
    missing = [entry for entry in scope if entry not in request_sources]
    if missing:
        raise _fv.FunctionalVerificationError(
            "mutation proposals 'scope' must be a subset of request.sources "
            f"-- not present in request.sources: {', '.join(missing)}"
        )


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
