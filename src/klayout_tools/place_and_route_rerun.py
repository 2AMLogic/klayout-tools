"""``klt place-and-route --check --rerun``'s diff canonicalization.

Extracted from ``place_and_route.py`` and extended by issue #5863: a
committed report is lint-clean, so a consumer has scrubbed its output paths
repo-relative or to `~/`/`$PDK_ROOT/...`-form (``env-provenance
lint-envelope`` requires exactly that), while the fresh side of a ``--rerun``
diff echoes absolute host paths -- every path field would drift
unconditionally, on the producing host included, and the mode could never
exit ``0`` against committed evidence. The fix compares path fields by *file
name* on both sides; ``provenance.pdk.source`` is excluded separately, as a
``_report_verify.VOLATILE_PROVENANCE_PATHS`` entry (shared with the
drc/lvs/extract/equiv/erc verbs that echo the same ``_pdk_block``).
"""

from __future__ import annotations

from typing import Any

from ._report_verify import strip_keys

#: Run-scoped bookkeeping keys dropped from *both* sides of a ``--rerun``
#: diff (issue #2224), via :func:`klayout_tools._report_verify.strip_keys`.
#:
#: ``engine_logs`` is the whole reason this exists: every entry is keyed by a
#: fresh ``uuid4`` ``invocation_id`` (``_openroad_engine._run_openroad``) and
#: its retained log ``directory``/``stdout_path``/``stderr_path``/
#: ``metadata_path`` are named after that id, so two identical runs never
#: agree on the block. The stage scripts' own ``script_sha256`` goes with it
#: -- the script embeds those same per-invocation paths. Unlike ``klt
#: synthesize``, the output artifacts (``def_path``/``gds_path``/
#: ``verilog_path``) are *not* dropped: this verb writes them to a fixed
#: ``.klt/place-and-route/`` directory, so they are stable across re-runs
#: from the same request and a change in one is real drift.
RERUN_BOOKKEEPING_KEYS: frozenset[str] = frozenset({"engine_logs"})

#: Path-bearing keys of this verb's response -- reduced to their *file
#: names* on both sides of a ``--rerun`` diff (issue #5863). The directory
#: is host- and scrub-shape-specific; the file name is the identity (each
#: artifact lands in this request's fixed ``.klt/place-and-route/`` under a
#: name derived from the top module), so a renamed artifact -- or one going
#: ``null`` <-> path -- still drifts. Every name here is a filesystem path
#: *everywhere* in this verb's response, the precondition
#: :func:`normalize_path_fields`' recursion-by-key-name shares with
#: :func:`strip_keys`: the ``{path, scope}`` envelopes (``netlist``,
#: ``provenance.deck.path``, ``layer_map.path``), the artifacts
#: (``def_path``/``unrouted_def_path``/``gds_path``/``verilog_path``, at the
#: top level, under ``power.placed``, and inside every ``corner_runs[]``
#: entry), ``corner_runs[].output_dir``, ``macros[].lef``/``.lib``, and
#: ``spef_sta.spef_path``/``.sdf_path``. Message prose that embeds a path
#: (``warnings[]``, ``power.placed.unavailable_reason``) is deliberately not
#: reduced: it is prose, not a path field, and re-scraping embedded paths
#: would re-implement the consumer's scrub -- the approach issue #5863
#: rejected.
RERUN_PATH_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "netlist",
        "path",
        "def_path",
        "unrouted_def_path",
        "gds_path",
        "verilog_path",
        "output_dir",
        "lef",
        "lib",
        "spef_path",
        "sdf_path",
    }
)


def _basename_of_path(path: Any) -> Any:
    """One path string's final segment -- ``/`` and ``\\`` both split, so a
    committed value carrying Windows separators still compares."""
    if isinstance(path, str):
        return path.replace("\\", "/").rsplit("/", 1)[-1]
    return path


def _normalize_one_path_value(value: Any) -> Any:
    """The file-name view of one matched path-bearing value. ``None`` stays
    ``None`` (an artifact going missing, or appearing, stays a diff); a
    string reduces to its final segment; a ``{"path": ..., "scope": ...}``
    envelope keeps its other members verbatim and reduces its ``path``
    member the same way. Anything else passes through untouched, so an
    unanticipated shape fails loudly as drift rather than silently comparing
    reduced forms."""
    if isinstance(value, str):
        return _basename_of_path(value)
    if isinstance(value, dict):
        return {
            key: _normalize_one_path_value(item) if key == "path" else item
            for key, item in value.items()
        }
    return value


def normalize_path_fields(value: Any, keys: frozenset[str]) -> Any:
    """``value`` with every mapping entry whose key is in ``keys`` replaced
    by its *file name* view (:func:`_normalize_one_path_value`), recursively
    (issue #5863) -- the basename canonicalization a flow verb feeds
    ``_report_verify.build_rerun_result``'s ``committed_for_diff``/
    ``fresh_for_diff`` so a committed, path-scrubbed report still diffs
    against the raw fresh one. Returns a new structure; the input is not
    mutated.

    Recursion is by key *name*, like :func:`strip_keys`, deliberately: the
    same path-bearing key recurs at several nesting depths (``klt
    place-and-route`` echoes ``def_path`` at the top level, under
    ``power.placed``, and inside every ``corner_runs[]`` entry), so a
    path-keyed list would need re-registering every time one of those blocks
    gains a sibling. ``keys`` must therefore name fields that are filesystem
    paths *everywhere* in the verb's response -- a name that is
    verdict-bearing in some other position belongs on a
    ``strip_path_patterns`` list instead (see ``strip_keys``'s own docstring
    for the full trade).
    """
    if isinstance(value, dict):
        return {
            key: (
                _normalize_one_path_value(item)
                if key in keys
                else normalize_path_fields(item, keys)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_path_fields(item, keys) for item in value]
    return value


def canonicalize_place_and_route_report_for_rerun_diff(
    report: dict[str, Any],
) -> dict[str, Any]:
    """``report`` with :data:`RERUN_BOOKKEEPING_KEYS` removed at every depth
    and every :data:`RERUN_PATH_FIELD_KEYS` value reduced to its file name
    (issue #5863) -- the comparison view ``rerun_place_and_route_report``
    diffs, never what it embeds as ``fresh``."""
    return normalize_path_fields(
        strip_keys(report, RERUN_BOOKKEEPING_KEYS), RERUN_PATH_FIELD_KEYS
    )
