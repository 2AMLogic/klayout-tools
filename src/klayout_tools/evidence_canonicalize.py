"""The per-kind ``--check … --rerun`` comparison view, as a document a
consumer can commit and diff whole (``klt evidence canonicalize``, issue
#5541).

Seven verbs (``drc``, ``lvs``, ``erc``, ``extract``, ``synthesize``,
``place-and-route``, ``equiv``) can verify a previously committed
``--format json`` report by re-running the analysis and diffing
*verdict-bearing* fields (:mod:`klayout_tools._report_verify`). The view
that diff sees is, per verb, **two** pieces composed:

1. a by-path exclusion set (``build_rerun_result``'s ``exclude=``) --
   run-scoped and machine-local paths a drift report must not fire on, and
2. for some verbs, a *canonicalizer* (``committed_for_diff``/
   ``fresh_for_diff``) that drops run-scoped bookkeeping keys by name at
   every depth, or normalizes extractor-internal spellings.

Until this module, that knowledge was reachable only by importing the
package: a repo that commits a report as regenerable evidence and guards it
with its own "re-run and diff against the committed file" check had to
hand-maintain a copy of the exclusion list, and fell behind it on every
release -- quietly and totally, because one un-stripped per-run field makes
*every* comparison differ, including two back-to-back runs of a
byte-identical request (issue #5541). :func:`canonicalize` is that view as a
function, and ``klt evidence canonicalize --kind <verb> REPORT`` is it as a
CLI document.

**One definition, two readers.** :data:`_KIND_SOURCES` is the only place the
pairing (kind -> exclusion set, canonicalizer) is written down, and it is
read by both consumers of that pairing: :func:`canonicalize` (the committable
view) and :func:`rerun_result_for`, which every verb's own ``rerun_*_report``
now calls instead of reaching for ``build_rerun_result`` with its own
arguments. A key added to a verb's rerun exclusion set therefore reaches the
canonical view with no second edit, and the two cannot drift apart: there is
exactly one code path that assembles either.

The exclusion sets and canonicalizers themselves stay where the knowledge
belongs -- the shared ones in :mod:`klayout_tools._report_verify`, the
verb-local ones in the verb module whose report shape they describe (and
``extract``'s canonicalizer is built from extract-internal helpers, so it
could not move). :data:`_KIND_SOURCES` names them by module attribute and
:func:`_resolve` imports the owning module **lazily**, at call time: a verb
module imports this one at module scope, so a module-scope import back the
other way would be a cycle.

``lvs`` caveat: ``klt lvs --check … --rerun`` additionally tolerates fields
the *committed* report never carried (``lvs.py``'s pair-dependent
exclusions, issues #1952/#2692). That tolerance is a property of a *pair* of
reports and cannot be expressed on one, so the canonical ``lvs`` view
applies only the static set -- see ``docs/cli/evidence.md``.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ._report_verify import (
    build_rerun_result,
    load_committed_report,
    strip_path_patterns,
)


class EvidenceCanonicalizeError(Exception):
    """A report that cannot be canonicalized at all -- missing, unreadable,
    not JSON, or a JSON value that is not an object. Mirrors every other
    verb's single-exception contract so the CLI adapter has one ``except``
    clause and the shared error envelope (``docs/json-contract.md``)."""


@dataclass(frozen=True)
class _KindSource:
    """Where one kind's two rerun-view pieces are *defined*, as ``(module,
    attribute)`` references resolved lazily by :func:`_resolve`.

    ``module`` is relative to this package. ``canonicalizer`` is ``None``
    for a kind whose ``--rerun`` passes no ``committed_for_diff``.
    """

    exclude: tuple[str, str]
    canonicalizer: tuple[str, str] | None


@dataclass(frozen=True)
class CanonicalKind:
    """One kind's resolved rerun view: the by-path ``exclude`` set and the
    optional canonicalizer, both as the live objects their owning modules
    define."""

    kind: str
    exclude: frozenset[tuple[str, ...]]
    canonicalize: Callable[[dict[str, Any]], dict[str, Any]] | None


#: kind -> where its ``--rerun`` view is defined. **The single source of
#: truth for the pairing** (see this module's docstring): both
#: :func:`canonicalize` and :func:`rerun_result_for` read it, so a verb's
#: exclusion set and its canonical view cannot diverge.
#:
#: The seven keys are exactly the verbs with a ``--rerun`` mode (issue
#: #5541 scoped six; ``klt erc --check ... --rerun``, issue #3060, is the
#: seventh and has the same shape). ``klt signoff --check`` is deliberately
#: absent -- it has no cheap/full split and applies its own two-stage strip
#: (``signoff.py``) -- as are ``klt sim``/``klt pex``, which have no
#: ``--rerun`` view to mirror.
_KIND_SOURCES: dict[str, _KindSource] = {
    "drc": _KindSource(("_report_verify", "VOLATILE_PROVENANCE_PATHS"), None),
    "lvs": _KindSource(("lvs", "_LVS_RERUN_EXCLUDE_PATHS"), None),
    "erc": _KindSource(("_report_verify", "VOLATILE_PROVENANCE_PATHS"), None),
    "extract": _KindSource(
        ("_report_verify", "VOLATILE_PROVENANCE_PATHS"),
        ("extract", "_canonicalize_extract_report_for_rerun_diff"),
    ),
    "synthesize": _KindSource(
        ("_report_verify", "VOLATILE_FLOW_PATHS"),
        ("synthesize", "_canonicalize_synthesize_report_for_rerun_diff"),
    ),
    "place-and-route": _KindSource(
        ("_report_verify", "VOLATILE_FLOW_PATHS"),
        ("place_and_route", "_canonicalize_place_and_route_report_for_rerun_diff"),
    ),
    "equiv": _KindSource(
        ("equiv", "_EQUIV_RERUN_EXCLUDE_PATHS"),
        ("equiv", "_canonicalize_equiv_report_for_rerun_diff"),
    ),
}

#: The ``--kind`` choices, in documentation order.
CANONICAL_KINDS: tuple[str, ...] = tuple(_KIND_SOURCES)


def _resolve(reference: tuple[str, str]) -> Any:
    """The object a :class:`_KindSource` reference names, importing its
    owning module on first use.

    Lazy on purpose: the verb modules import *this* module at module scope
    (their ``rerun_*_report`` calls :func:`rerun_result_for`), so importing
    them back at module scope here would be an import cycle. The
    ``tests/test_evidence_canonicalize.py`` resolution test is what keeps
    a renamed attribute from becoming a runtime ``AttributeError``.
    """
    module_name, attribute = reference
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, attribute)


def kind_spec(kind: str) -> CanonicalKind:
    """The resolved :class:`CanonicalKind` for ``kind``.

    Raises :class:`ValueError` for a kind not in :data:`CANONICAL_KINDS`.
    The CLI never reaches that -- ``--kind`` is an argparse ``choices=``
    option, so an unknown kind is a usage error (exit 2) -- but a
    programmatic caller gets a named failure rather than a ``KeyError``.
    """
    try:
        source = _KIND_SOURCES[kind]
    except KeyError:
        raise ValueError(
            f"unknown report kind {kind!r}; expected one of "
            f"{', '.join(CANONICAL_KINDS)}"
        ) from None
    canonicalizer = (
        None if source.canonicalizer is None else _resolve(source.canonicalizer)
    )
    return CanonicalKind(
        kind=kind,
        exclude=_resolve(source.exclude),
        canonicalize=canonicalizer,
    )


def rerun_result_for(
    kind: str,
    report_path: str,
    *,
    committed: dict[str, Any],
    fresh: dict[str, Any],
    extra_exclude: Iterable[tuple[str, ...]] = (),
) -> dict[str, Any]:
    """``kind``'s ``--rerun`` payload: :func:`~klayout_tools._report_verify
    .build_rerun_result` with the exclusion set and canonicalizer this
    module pairs with ``kind``.

    The single call every verb's ``rerun_*_report`` makes, so ``--rerun``
    and :func:`canonicalize` provably see the same view. ``extra_exclude``
    carries the one *pair-dependent* addition any verb makes (``klt lvs``
    excludes fields the committed report never carried, issues #1952/#2692)
    -- deliberately a caller-supplied argument rather than a registry
    entry, because it is a function of the pair being diffed and so has no
    single-report counterpart.
    """
    spec = kind_spec(kind)
    canonicalizer = spec.canonicalize
    return build_rerun_result(
        report_path=report_path,
        committed=committed,
        fresh=fresh,
        exclude=spec.exclude | frozenset(extra_exclude),
        committed_for_diff=(
            None if canonicalizer is None else canonicalizer(committed)
        ),
        fresh_for_diff=None if canonicalizer is None else canonicalizer(fresh),
    )


def _with_sorted_keys(value: Any) -> Any:
    """``value`` with every mapping rebuilt in sorted-key order, recursively.

    Makes the emitted document's bytes a function of its *content* only, so
    two reports whose keys were serialized in a different order canonicalize
    to byte-identical files and a committed copy can be byte-compared. List
    order is never touched: it is content (and where a verb's own list order
    is an artifact, that verb's canonicalizer already re-sorts it).
    """
    if isinstance(value, Mapping):
        return {key: _with_sorted_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_with_sorted_keys(item) for item in value]
    return value


def canonicalize(report: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """``report`` reduced to exactly the view ``klt <kind> --check …
    --rerun`` diffs: ``kind``'s canonicalizer applied, then its by-path
    exclusion set removed (subtree included), then every mapping rebuilt in
    sorted-key order.

    Two canonical views of the same kind compare equal exactly when that
    kind's ``--rerun`` reports no drift between the two reports (the static
    part of its exclusion set -- see this module's ``lvs`` caveat), so a
    consumer can commit this document and diff it whole instead of
    re-implementing the exclusion list. Verdict-bearing fields are untouched:
    nothing outside the kind's exclusion set / canonicalizer is removed or
    rewritten.

    Idempotent -- ``canonicalize(canonicalize(r, k), k) == canonicalize(r,
    k)``: both steps only remove and reorder.
    """
    spec = kind_spec(kind)
    view: Any = report
    if spec.canonicalize is not None:
        view = spec.canonicalize(dict(report))
    stripped = strip_path_patterns(view, spec.exclude)
    canonical: dict[str, Any] = _with_sorted_keys(stripped)
    return canonical


def load_report(path: str) -> dict[str, Any]:
    """The committed report at ``path``, or from stdin when ``path`` is
    ``"-"``.

    Raises :class:`EvidenceCanonicalizeError` with the same three messages
    every ``--check`` verb produces -- missing file, unparseable JSON, a
    JSON value that is not an object -- never a traceback.
    """
    if path != "-":
        return load_committed_report(path, EvidenceCanonicalizeError)
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise EvidenceCanonicalizeError(
            f"committed report is not valid JSON: <stdin> ({exc})"
        ) from exc
    if not isinstance(data, dict):
        raise EvidenceCanonicalizeError(
            "committed report must be a JSON object: <stdin>"
        )
    return data


def canonicalize_file(path: str, kind: str) -> dict[str, Any]:
    """:func:`canonicalize` of :func:`load_report` -- what ``klt evidence
    canonicalize --kind <kind> <path>`` emits."""
    return canonicalize(load_report(path), kind)
