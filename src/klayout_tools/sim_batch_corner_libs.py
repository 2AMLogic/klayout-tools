"""Runner-side per-section corner-library identity and comparison (#5457).

Split out of :mod:`klayout_tools.sim_batch_features` (size ratchet, #2766):
the merge of main's osdi-preload work (#5562) into this branch pushed the
combined module past the 500-line hard limit, and a PR cannot mint a
baseline ceiling for itself. Everything here is the ``models.lib``-absent
half of model attestation: a request that selects its corners with
``corners.process[].sections[].lib`` (issue #2522) carries its model
identity in per-section libraries, not in ``models.lib``.
"""

from __future__ import annotations

import re
from typing import Any


def runner_corner_section_libs(
    report: dict[str, Any] | None,
) -> list[dict[str, str]] | None:
    """The per-section corner libraries the runner's own ``klt sim`` reported
    (issue #5457): ``[{name, sha256}]`` lifted from the collected inner
    report's ``environment.corner_section_libs``, i.e. the libraries the
    worker actually resolved.

    This is the ``models.lib``-absent half of
    :func:`klayout_tools.sim_batch_features.runner_models_lib_sha256`. A
    request that selects its corners with ``corners.process[].sections[].lib``
    (issue #2522) needs no ``models.lib`` at all, so ``models_lib_sha256`` is
    ``null`` on *both* sides by construction and these entries are the only
    runner-side model identity there is -- the shape the docs recommend for
    an off-host run was the one shape whose executed models could not be
    attested.

    ``None`` when the key is absent, is not a list, or carries no usable
    entry (a runner predating #2522, an error envelope, a malformed report),
    so "the runner reported nothing" stays distinguishable from a value.
    ``path``/``scope`` are dropped on the way through: the path is the
    *worker's* and carries no comparable identity, whereas the hash does.
    """
    env = report.get("environment") if isinstance(report, dict) else None
    entries = env.get("corner_section_libs") if isinstance(env, dict) else None
    if not isinstance(entries, list):
        return None
    lifted = [
        {"name": entry["name"], "sha256": entry["sha256"].lower()}
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
        and isinstance(entry.get("sha256"), str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", entry["sha256"])
    ]
    return lifted or None


def _lib_pairs(entries: Any) -> set[tuple[str, str]] | None:
    """``{(name, sha256)}`` of a ``[{name, sha256}]`` list, or ``None`` when
    there is no usable entry. Both sides of
    :func:`_compare_corner_section_libs` reduce to this: the client's entries
    additionally carry ``path``/``scope``, which play no part in identity."""
    if not isinstance(entries, list):
        return None
    pairs = {
        (entry["name"], entry["sha256"].lower())
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
        and isinstance(entry.get("sha256"), str)
        and entry["sha256"]
    }
    return pairs or None


def _compare_corner_section_libs(
    runner_libs: Any, client_libs: Any, *, staged: bool = False
) -> dict[str, Any]:
    """``corner_section_libs_comparison`` (issue #5457): the per-section
    corner libraries the runner loaded against the ones the submitter
    resolved. The ``models.lib``-absent counterpart of
    :func:`klayout_tools.sim_batch_features._compare_models_lib`, and the
    only comparison that can answer "which models ran off-host" for a
    request built out of per-section libraries.

    Entries compare as ``(name, sha256)`` **pair membership** -- not per name
    and not positionally. ``name`` is a basename (``sim.py``'s
    ``_model_file_entries``), so two libraries in different directories can
    share one; and a ``--batch-set`` shard need only be handed the libraries
    its own corners name, a subset of the client's. A runner entry is
    therefore accounted for when the client resolved a library of the same
    basename *and* the same bytes, so neither a shared basename nor a shard
    subset reads as a mismatch.

    Missing data is ``not_comparable``, never a proven mismatch, and klt
    never fails a run on the verdict -- the caller decides.
    """
    runner_pairs = _lib_pairs(runner_libs)
    if runner_pairs is None:
        return {"status": "not_comparable", "reason": "runner_libs_unavailable"}
    client_pairs = _lib_pairs(client_libs)
    if client_pairs is None:
        return {"status": "not_comparable", "reason": "client_libs_unavailable"}
    if staged:
        return {"status": "not_comparable", "reason": "models_staged"}
    unmatched = sorted({name for name, _ in runner_pairs - client_pairs})
    if not unmatched:
        return {"status": "match"}
    return {
        "status": "mismatch",
        "warning": (
            "the per-section corner librar(ies) "
            + ", ".join(unmatched)
            + " the runner loaded are not among the ones the submitter "
            "resolved locally; results may differ from a local run"
        ),
        "names": unmatched,
    }
