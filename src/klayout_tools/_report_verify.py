"""Shared "does a previously committed report still hold" verification --
``klt drc --check``/``--rerun`` and ``klt lvs --check``/``--rerun`` (issue
#1106).

A ``klt drc``/``klt lvs`` JSON report is often committed as evidence
alongside a design (see ``klt signoff``'s manifest-evidence grading), but
nothing previously let a consumer *verify* a committed report still
reproduces against the current inputs/tool version without hand-rolling a
normalize-and-diff -- this module is the reusable core both verbs' ``--check``
(cheap: re-hash and compare, no engine re-run) and ``--rerun`` (full: re-run
the analysis and diff verdict-bearing fields) modes are built from, mirroring
the comparison idiom ``klt signoff``'s ``_grade_evidence()``
(``signoff.py:1092``) and ``_provenance_consistency()`` (``signoff.py:615``)
already established: a mismatch always renders as a **failure to verify**,
never a false pass.

Both modes report the same two-value ``status``:

- ``"match"`` -- every check passed (cheap mode: every hash matched; full
  mode: no verdict-bearing field drifted).
- ``"drifted"`` -- at least one check failed (cheap mode: names which hash
  moved; full mode: names which field(s) changed).

``drc.py``/``lvs.py`` each supply the verb-specific pieces (which hashes to
re-derive, how to re-run the analysis) via the small building blocks below;
this module owns only the shape-agnostic mechanics (loading a committed
report, comparing one hash, diffing two report dicts).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

#: The `provenance` fields every verb's own docstring documents as
#: legitimately varying between two runs of the identical inputs -- the tool
#: build and the PDK release string, never the deck/input content hashes or
#: any verdict-bearing field. Curator's own analysis (issue #1106): these are
#: the fields inferred from the schema, not a pre-existing coded precedent
#: (`klt signoff`'s consistency check goes the other way -- it *requires*
#: these to agree *across* checks in one signoff bundle; here we deliberately
#: allow them to disagree between one committed report and a fresh rerun of
#: the same report, which is the whole reason `--rerun` needs an exclusion
#: list at all).
VOLATILE_PROVENANCE_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("provenance", "klt_version"),
        ("provenance", "klayout_version"),
        ("provenance", "pdk", "version"),
    }
)

#: Sentinel distinguishing "key absent" from a legitimate JSON `null` when
#: diffing two report dicts (see :func:`_diff_json`) -- `None != None` would
#: otherwise be indistinguishable from `None != <missing>`.
_MISSING = object()


def get_path(data: Any, path: tuple[str, ...]) -> Any:
    """Walk ``data`` (a JSON-decoded value) through nested dict keys named
    by ``path``, returning ``None`` the moment a step isn't a dict or the key
    is absent -- never raises. E.g. ``get_path(report, ("provenance",
    "deck", "content_hash"))``."""
    node = data
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def load_committed_report(path: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Read and minimally validate a previously committed ``klt drc``/``klt
    lvs`` ``--format json`` report from ``path``.

    Raises ``error_cls`` (the caller's own ``DrcError``/``LvsError``, so the
    CLI layer's existing single ``except`` clause keeps working unchanged)
    with a clean, specific message -- missing file, unparseable JSON, or a
    JSON value that isn't an object -- never a traceback. This is the
    ``--check``/``--rerun`` counterpart of ``fail informatively, not crash``
    for "an old report predating #331" / a malformed path (issue #1106's
    acceptance criteria).
    """
    if not os.path.isfile(path):
        raise error_cls(f"committed report not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise error_cls(f"committed report is not valid JSON: {path} ({exc})") from exc
    except OSError as exc:
        raise error_cls(f"could not read committed report '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise error_cls(f"committed report must be a JSON object: {path}")
    return data


def hash_check(field: str, expected: str | None, actual: str | None) -> dict[str, Any]:
    """One cheap-mode comparison entry: does the freshly re-derived hash
    (``actual``) match the one recorded in the committed report
    (``expected``)?

    ``match`` is ``False`` whenever ``expected`` is ``None`` -- a report with
    no recorded hash to compare against (predates the field, or the original
    run couldn't hash its input either) can never render a ``"met"``-style
    false pass, mirroring ``klt signoff``'s ``_grade_evidence()`` discipline
    (see this module's docstring): "nothing recorded" is treated as "cannot
    confirm this reproduces", not "assume it does".
    """
    match = expected is not None and expected == actual
    return {"field": field, "expected": expected, "actual": actual, "match": match}


def build_check_result(
    *, report_path: str, checks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assemble the ``--check`` (cheap mode) JSON payload from a list of
    :func:`hash_check` entries -- shared shape for ``klt drc``/``klt lvs``."""
    status = "match" if all(check["match"] for check in checks) else "drifted"
    return {
        "schema_version": 1,
        "mode": "check",
        "report": report_path,
        "status": status,
        "checks": checks,
    }


def build_rerun_result(
    *,
    report_path: str,
    committed: dict[str, Any],
    fresh: dict[str, Any],
    exclude: frozenset[tuple[str, ...]] = VOLATILE_PROVENANCE_PATHS,
) -> dict[str, Any]:
    """Assemble the ``--rerun`` (full mode) JSON payload: diff ``committed``
    against a freshly produced ``fresh`` report (from actually re-running the
    analysis), excluding ``exclude`` paths from causing ``status:
    "drifted"``. ``fresh`` is embedded verbatim so a consumer can inspect the
    full up-to-date report without a second invocation."""
    drift = diff_verdict_fields(committed, fresh, exclude=exclude)
    return {
        "schema_version": 1,
        "mode": "rerun",
        "report": report_path,
        "status": "drifted" if drift else "match",
        "drift": drift,
        "fresh": fresh,
    }


def diff_verdict_fields(
    committed: Any,
    fresh: Any,
    *,
    exclude: frozenset[tuple[str, ...]] = frozenset(),
) -> list[dict[str, Any]]:
    """Recursively diff two JSON-decoded report values, returning one entry
    per leaf field whose value differs -- ``{"field": "a.b.c", "committed":
    ..., "fresh": ...}``, dotted-path field names, sorted-key traversal for
    determinism. A path exactly matching an entry in ``exclude`` (see
    :data:`VOLATILE_PROVENANCE_PATHS`) is skipped entirely, subtree included.

    Dicts are compared key-by-key (the union of both sides' keys, so a field
    added/removed between the committed and fresh schema surfaces as a
    diff -- see this module's docstring caveat on schema evolution). Lists of
    equal length are compared index-by-index (both ``violations``/
    ``mismatches`` are already documented as sorted for determinism, so two
    runs against unchanged inputs produce identically-ordered lists); a
    length mismatch is reported as one whole-list diff entry rather than a
    misleading index-by-index one. Any other type mismatch (or genuinely
    unequal leaf values) is one diff entry at that path.
    """
    diffs: list[dict[str, Any]] = []
    _diff_json(committed, fresh, (), exclude, diffs)
    return diffs


def _diff_json(
    committed: Any,
    fresh: Any,
    path: tuple[str, ...],
    exclude: frozenset[tuple[str, ...]],
    out: list[dict[str, Any]],
) -> None:
    if path in exclude:
        return
    if isinstance(committed, dict) and isinstance(fresh, dict):
        for key in sorted(set(committed) | set(fresh)):
            _diff_json(
                committed.get(key, _MISSING),
                fresh.get(key, _MISSING),
                path + (key,),
                exclude,
                out,
            )
        return
    if (
        isinstance(committed, list)
        and isinstance(fresh, list)
        and len(committed) == len(fresh)
    ):
        for index, (committed_item, fresh_item) in enumerate(
            zip(committed, fresh, strict=True)
        ):
            _diff_json(committed_item, fresh_item, path + (str(index),), exclude, out)
        return
    if committed != fresh:
        out.append(
            {
                "field": ".".join(path),
                "committed": None if committed is _MISSING else committed,
                "fresh": None if fresh is _MISSING else fresh,
            }
        )
